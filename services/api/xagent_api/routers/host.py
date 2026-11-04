"""ترمینال مدیریتی VPS (کارگزار فرمان میزبان) — فقط کارفرما؛ Worker فقط از مسیر داخلی احرازشده."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_agent.control import changes
from xagent_core.models import Approval, HostOp, Run, User
from xagent_core.settings_store import HostSettings, get_setting

from .. import hostops
from ..deps import get_session, owner_user, parse_uuid

router = APIRouter(tags=["host"])


class RunIn(BaseModel):
    op: str = Field(min_length=3, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=1000)
    confirm: bool = False  # سطح ۳ از پنل: تأیید صریح دوم کارفرما


class InternalRunIn(RunIn):
    run_id: str | None = None
    tool_call_id: str | None = None
    approved: bool = False
    actor: str = "manager"


def _http(e: hostops.HostDenied) -> HTTPException:
    status = {"not_allowed": 400, "rejected": 400, "rate_limited": 429}.get(e.code, 403)
    return HTTPException(status, str(e))


async def _run(session: AsyncSession, body: RunIn, *, actor_kind: str, requested_by: str, approval: Approval | None = None,
               approved_by: str | None = None, level: int, wait: float) -> dict:
    hs = await get_setting(session, "host", HostSettings)
    row = await hostops.submit(session, op=body.op, args=body.args, level=level, actor_kind=actor_kind,
                               requested_by=requested_by, reason=body.reason,
                               run_id=getattr(body, "run_id", None), approval=approval, approved_by=approved_by,
                               grant_minutes=hs.exec_session_minutes if body.op == "exec.approved" else 15)
    await session.commit()
    if row.status == "running" and wait:
        await hostops.wait_done(session, row, wait)
        await session.commit()
    return hostops.ser_op(row)


# ───────────── کارفرما (پنل) ─────────────
@router.get("/api/host/catalog")
async def host_catalog(_: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    hs = await get_setting(session, "host", HostSettings)
    try:
        cat = await hostops.catalog()
        available, error = True, None
    except hostops.HostUnavailable as e:
        cat, available, error = {}, False, str(e)
    return {"available": available, "error": error, "policy": hs.model_dump(),
            "ops": sorted(cat.values(), key=lambda o: (o["level"], o["op"]))}


@router.get("/api/host/ops")
async def host_ops(limit: int = Query(50, le=200), _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(HostOp).order_by(HostOp.created_at.desc()).limit(limit))).scalars().all()
    for r in rows:
        if r.status == "running":
            await hostops.sync(session, r)
    pending = (await session.execute(select(Approval).where(Approval.category == "host_admin", Approval.status == "pending")
                                     .order_by(Approval.created_at.desc()).limit(20))).scalars().all()
    return {"items": [hostops.ser_op(r) for r in rows],
            "pending_approvals": [{"id": str(a.id), "run_id": str(a.run_id), "op": (a.args or {}).get("op"),
                                   "args": (a.args or {}).get("args"), "reason": a.reason,
                                   "created_at": a.created_at.isoformat() if a.created_at else None} for a in pending]}


@router.get("/api/host/ops/{oid}")
async def host_op(oid: str, offset: int = 0, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(HostOp, parse_uuid(oid))
    if r is None:
        raise HTTPException(404, "عملیات یافت نشد")
    await hostops.sync(session, r)
    data, nxt = hostops.read_output(r.host_job or "", offset) if r.host_job else ("", offset)
    return {**hostops.ser_op(r), "chunk": data, "next_offset": nxt}


@router.post("/api/host/ops")
async def host_run(body: RunIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    try:
        _decision, level, _why = await hostops.decide(session, body.op, body.args, "owner")
    except hostops.HostUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except hostops.HostDenied as e:
        raise _http(e) from e
    if (level >= 3 or body.op in hostops.HARD_APPROVAL) and not body.confirm:
        raise HTTPException(428, "این عملیات سطح ۳ است؛ برای اجرا تأیید صریح (confirm) لازم است.")
    return await _run(session, body, actor_kind="owner", requested_by=user.username,
                      approved_by=user.username if level >= 3 else None, level=level, wait=8 if level == 1 else 2)


@router.post("/api/host/ops/{oid}/cancel")
async def host_cancel(oid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(HostOp, parse_uuid(oid))
    if r is None or not r.host_job:
        raise HTTPException(404, "عملیات در حال اجرا یافت نشد")
    try:
        res = await hostops.hostd_call("job.cancel", {"job": r.host_job}, actor=f"owner:{user.username}")
    except hostops.HostUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {"ok": bool(res.get("ok")), "cancelled": res.get("cancelled", 0)}


@router.post("/api/host/stop")
async def host_emergency_stop(user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    """توقف اضطراری: لغو همه عملیات در حال اجرا و غیرفعال‌سازی کارگزار تا فعال‌سازی دوباره توسط کارفرما."""
    cancelled = 0
    try:
        cancelled = int((await hostops.hostd_call("jobs.cancel_all", {}, actor=f"owner:{user.username}")).get("cancelled", 0))
    except hostops.HostUnavailable:
        pass
    try:
        await changes.change_setting(session, "host", {"enabled": False}, actor="owner", reason="توقف اضطراری ترمینال میزبان")
    except changes.ChangeError as e:
        if e.decision != "noop":
            raise
    await session.commit()
    return {"ok": True, "cancelled": cancelled, "enabled": False}


# ───────────── مسیر داخلی Worker (مدیر) ─────────────
def internal_caller(x_xagent_internal: str = Header(default="")) -> None:
    if not x_xagent_internal or not hmac.compare_digest(x_xagent_internal, hostops.internal_token()):
        raise HTTPException(401, "توکن داخلی نامعتبر است")


@router.get("/api/internal/host/catalog", dependencies=[Depends(internal_caller)])
async def internal_catalog(session: AsyncSession = Depends(get_session)):
    try:
        cat = await hostops.catalog()
    except hostops.HostUnavailable as e:
        raise HTTPException(503, str(e)) from e
    hs = await get_setting(session, "host", HostSettings)
    return {"enabled": hs.enabled, "update_policy": hs.update_policy,
            "ops": [{k: o[k] for k in ("op", "level", "description", "args")} | {"mode": hs.op_modes.get(o["op"], "default")}
                    for o in sorted(cat.values(), key=lambda o: (o["level"], o["op"]))]}


@router.post("/api/internal/host/run", dependencies=[Depends(internal_caller)])
async def internal_run(body: InternalRunIn, session: AsyncSession = Depends(get_session)):
    if body.actor != "manager":
        raise HTTPException(403, "عملیات میزبان فقط از اجرای معتبر مالک قابل درخواست است.")
    # The shared internal service token is *not* evidence that a specific LLM
    # run belongs to the owner. Never trust actor/principal supplied by a caller.
    try:
        rid = parse_uuid(body.run_id or "")
    except (ValueError, HTTPException) as e:
        raise HTTPException(403, "شناسه اجرای مالک الزامی است.") from e
    run = await session.get(Run, rid)
    if (run is None or run.status not in ("running", "waiting_approval")
            or run.employee_id is not None or not (run.state or {}).get("trusted")
            or (run.state or {}).get("principal") != "owner"):
        raise HTTPException(403, "اجرای مالک فعال و معتبر نیست.")
    try:
        decision, level, why = await hostops.decide(session, body.op, body.args, "manager")
    except hostops.HostUnavailable as e:
        raise HTTPException(503, str(e)) from e
    except hostops.HostDenied as e:
        raise _http(e) from e
    approval = None
    approved_by = None
    if decision == "approval":
        if not body.approved:
            return {"status": "approval_required", "level": level, "reason": why}
        try:
            approval = await hostops.verify_approval(session, body.run_id, body.tool_call_id, body.op, body.args)
        except hostops.HostDenied as e:
            raise _http(e) from e
        approved_by = approval.decided_by
    elif decision == "auto_grant":
        approved_by = "policy:update_policy=auto"
    return await _run(session, body, actor_kind="manager", requested_by=body.actor, approval=approval,
                      approved_by=approved_by, level=level, wait=25 if level <= 2 else 3)


@router.get("/api/internal/host/ops/{oid}", dependencies=[Depends(internal_caller)])
async def internal_op(oid: str, session: AsyncSession = Depends(get_session)):
    r = await session.get(HostOp, parse_uuid(oid))
    if r is None:
        raise HTTPException(404, "عملیات یافت نشد")
    await hostops.sync(session, r)
    await session.commit()
    return hostops.ser_op(r)
