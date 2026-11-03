"""مرکز کنترل مدیر: کارمندان، تغییرات (Audit/Rollback)، رخدادها، عیب‌یابی، خوددرمانی، کشف و آزمون مدل."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_agent.control import capabilities, changes, selfheal
from xagent_core.models import AIModel, AIProvider, ConfigChange, Employee, Incident, Run, User

from ..deps import current_user, get_session, owner_user, parse_uuid, ser

router = APIRouter(prefix="/api/control", tags=["control"])


class EmployeeIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    title: str | None = None
    description: str | None = None
    instructions: str | None = None
    model_ids: list[str] | None = None
    tool_groups: list[str] | None = None
    memory_scope: str | None = Field(default=None, pattern="^(shared|private)$")
    enabled: bool | None = None
    max_steps: int | None = Field(default=None, ge=1, le=100)
    max_cost_usd: float | None = Field(default=None, ge=0, le=20)


def _err(e: changes.ChangeError) -> HTTPException:
    return HTTPException(403 if e.decision == "forbidden" else 422, str(e))


@router.get("/employees")
async def employees(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Employee).order_by(Employee.created_at))).scalars().all()
    return {"items": [ser(e) for e in rows]}


@router.post("/employees")
async def upsert_employee(body: EmployeeIn, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    try:
        e, ch = await changes.upsert_employee(session, body.model_dump(exclude_none=True), actor="owner",
                                              reason="ویرایش از پنل")
    except changes.ChangeError as ex:
        if ex.decision == "noop":
            return {"ok": True}
        raise _err(ex) from ex
    return ser(e, extra={"change_id": ch.id})


@router.delete("/employees/{name}")
async def delete_employee(name: str, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    try:
        await changes.delete_employee(session, name, actor="owner", reason="حذف از پنل")
    except changes.ChangeError as ex:
        raise _err(ex) from ex
    return {"ok": True}


@router.get("/employees/{eid}/runs")
async def employee_runs(eid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Run).where(Run.employee_id == parse_uuid(eid))
                                  .order_by(Run.created_at.desc()).limit(30))).scalars().all()
    return {"items": [ser(r, exclude={"plan", "goal", "route"}) for r in rows]}


@router.get("/changes")
async def list_changes(target_type: str | None = None, limit: int = Query(100, le=500), _: User = Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    q = select(ConfigChange).order_by(ConfigChange.created_at.desc()).limit(limit)
    if target_type:
        q = q.where(ConfigChange.target_type == target_type)
    return {"items": [ser(c) for c in (await session.execute(q)).scalars()]}


@router.post("/changes/{cid}/rollback")
async def rollback_change(cid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    try:
        res = await changes.rollback(session, cid, actor="owner", reason="بازگردانی توسط کارفرما از پنل")
    except changes.ChangeError as ex:
        raise _err(ex) from ex
    return {"ok": True, "message": res.message, "reverted": res.reverted}


@router.get("/incidents")
async def incidents(status: str | None = None, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    q = select(Incident).order_by(Incident.last_seen_at.desc()).limit(100)
    if status:
        q = q.where(Incident.status.in_(status.split(",")))
    return {"items": [ser(i) for i in (await session.execute(q)).scalars()]}


@router.post("/diagnose")
async def diagnose(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await selfheal.diagnose(session)


@router.post("/heal")
async def heal(dry_run: bool = False, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    return await selfheal.heal(session, actor="self_heal", dry_run=dry_run)


@router.post("/providers/{pid}/sync")
async def sync_provider(pid: str, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    p = await session.get(AIProvider, parse_uuid(pid))
    if p is None:
        raise HTTPException(404, "ارائه‌دهنده یافت نشد")
    try:
        res: dict[str, Any] = await capabilities.sync_provider(session, p)
    except capabilities.DiscoveryError as e:
        raise HTTPException(502, str(e)) from e
    return res


@router.post("/models/{mid}/probe")
async def probe(mid: str, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    m = await session.get(AIModel, parse_uuid(mid))
    if m is None:
        raise HTTPException(404, "مدل یافت نشد")
    return await capabilities.probe_model(session, str(m.id))
