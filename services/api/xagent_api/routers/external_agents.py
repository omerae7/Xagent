"""اجراکننده‌های ایجنت بیرونی (Cursor Cloud Agents): تنظیم امن (فقط کارفرما)، آزمون اتصال، فهرست و لغو اجراها."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.audit import audit
from xagent_core.models import ExternalAgentRun, User
from xagent_core.settings_store import CursorSettings, get_setting, set_setting
from xagent_tools import cursor_agents as ca

from ..deps import current_user, get_session, owner_user, parse_uuid

router = APIRouter(prefix="/api/external-agents", tags=["external-agents"])
_REPO = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+$")
_REF = re.compile(r"^[\w./-]{1,120}$")


class CursorConfigIn(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    allowed_repos: list[str] | None = None
    allowed_refs: list[str] | None = None
    auto_create_pr: bool | None = None
    max_concurrent: int | None = Field(None, ge=1, le=10)
    max_launches_per_day: int | None = Field(None, ge=0, le=100)
    model: str | None = Field(None, max_length=80)
    poll_seconds: int | None = Field(None, ge=15, le=3600)
    max_runtime_hours: int | None = Field(None, ge=1, le=72)
    api_key: str | None = Field(None, min_length=8, max_length=400)
    clear_key: bool = False


def validate_cursor(cfg: CursorSettings) -> CursorSettings:
    try:
        cfg.base_url = ca.validate_base_url(cfg.base_url)
    except ca.CursorError as e:
        raise HTTPException(422, str(e)) from e
    repos = []
    for r in cfg.allowed_repos:
        n = ca.norm_repo(r)
        if not _REPO.match(n):
            raise HTTPException(422, f"نشانی مخزن نامعتبر است: {r} (فقط https://github.com/owner/repo)")
        repos.append(n)
    cfg.allowed_repos = sorted(set(repos))
    for ref in cfg.allowed_refs:
        if not _REF.match(ref) or ".." in ref:
            raise HTTPException(422, f"نام شاخه نامعتبر است: {ref}")
    if cfg.model and not re.fullmatch(r"[\w.:-]{1,80}", cfg.model):
        raise HTTPException(422, "شناسه مدل نامعتبر است")
    return cfg


async def _view(session: AsyncSession) -> dict[str, Any]:
    cfg, key = await ca.config(session)
    status, reason = await ca.readiness(session)
    from xagent_core.models import SettingKV

    st = await session.get(SettingKV, "cursor_status")
    return {"config": cfg.model_dump(mode="json"), "has_key": bool(key), "api_key_masked": ca.masked(key),
            "status": status, "status_reason": reason, "last_check": (st.value if st else {}) or {},
            "type": "external_agent_executor",
            "notes": ["این یک اجراکننده ایجنت کدنویسی است، نه مدل زبانی؛ در زنجیره مدل‌ها قرار نمی‌گیرد.",
                      "Merge خودکار وجود ندارد؛ کار روی شاخه تازه انجام می‌شود.",
                      "هزینه هر اجرا از API مستند Cursor قابل دریافت نیست و «نامعلوم» نمایش داده می‌شود."]}


@router.get("/cursor/config")
async def get_cursor(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await _view(session)


@router.put("/cursor/config")
async def put_cursor(body: CursorConfigIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    cfg = await get_setting(session, "cursor", CursorSettings)
    data = cfg.model_dump()
    for k, v in body.model_dump(exclude={"api_key", "clear_key"}).items():
        if v is not None:
            data[k] = v
    cfg = validate_cursor(CursorSettings.model_validate(data))
    await set_setting(session, "cursor", cfg)
    changed_key = False
    if body.clear_key:
        await ca.set_key(session, None)
        changed_key = True
    elif body.api_key:
        await ca.set_key(session, body.api_key.strip())
        changed_key = True
    if changed_key:
        from xagent_core.models import SettingKV

        st = await session.get(SettingKV, "cursor_status")
        if st is not None:
            await session.delete(st)
            await session.flush()
    await audit(session, user.username, "cursor.config", "cursor",
                {"enabled": cfg.enabled, "repos": len(cfg.allowed_repos), "key_changed": changed_key})
    return await _view(session)


@router.post("/cursor/test")
async def test_cursor(user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    res = await ca.test_connection(session)
    await audit(session, user.username, "cursor.test", "cursor", {"status": res.get("status")})
    return {**res, **{k: v for k, v in (await _view(session)).items() if k in ("status", "status_reason")}}


@router.get("")
async def list_runs(status: str | None = Query(None, max_length=16), limit: int = Query(50, ge=1, le=200),
                    _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    q = select(ExternalAgentRun).order_by(ExternalAgentRun.created_at.desc()).limit(limit)
    if status == "active":
        q = q.where(ExternalAgentRun.status.in_(ca.ACTIVE))
    elif status:
        q = q.where(ExternalAgentRun.status == status)
    rows = (await session.execute(q)).scalars().all()
    return {"items": [ca.serialize(r) for r in rows]}


@router.get("/{rid}")
async def get_run(rid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    row = await session.get(ExternalAgentRun, parse_uuid(rid))
    if row is None:
        raise HTTPException(404, "یافت نشد")
    return {**ca.serialize(row), "prompt": row.prompt}


@router.post("/{rid}/cancel")
async def cancel_run(rid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    row = await session.get(ExternalAgentRun, parse_uuid(rid))
    if row is None:
        raise HTTPException(404, "یافت نشد")
    msg = await ca.request_cancel(session, row, user.username)
    await audit(session, user.username, "cursor.cancel", str(row.id))
    return {"message": msg, "item": ca.serialize(row)}
