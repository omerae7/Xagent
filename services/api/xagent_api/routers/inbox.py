"""میز کار و گزارش‌های ایجنت، مجوزهای مأموریت، شناسنامه قابلیت‌ها و پاکسازی منطقی اجراهای قدیمی."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_agent.manifest import build_manifest
from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.db import utcnow
from xagent_core.inbox import ser_item
from xagent_core.models import InboxItem, Mission, MissionAction, Routine, Run, User
from xagent_tools.lifecycle import finalize_routine
from xagent_tools.missions import describe_scope

from ..deps import current_user, get_session, owner_user, parse_uuid, ser

router = APIRouter(prefix="/api", tags=["inbox"])


@router.get("/inbox")
async def list_inbox(kind: str | None = None, unread: bool = False, archived: bool = False,
                     limit: int = Query(60, le=200), offset: int = 0, _: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    q = select(InboxItem).where(InboxItem.archived.is_(archived))
    if kind:
        q = q.where(InboxItem.kind.in_(kind.split(",")))
    if unread:
        q = q.where(InboxItem.read.is_(False))
    rows = (await session.execute(q.order_by(InboxItem.ts.desc()).limit(limit).offset(offset))).scalars().all()
    counts = dict((await session.execute(select(InboxItem.kind, func.count()).where(
        InboxItem.archived.is_(False), InboxItem.read.is_(False)).group_by(InboxItem.kind))).all())
    return {"items": [ser_item(i) for i in rows], "unread_by_kind": counts, "unread": sum(counts.values())}


@router.post("/inbox/read_all")
async def read_all(_: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    await session.execute(update(InboxItem).where(InboxItem.read.is_(False)).values(read=True))
    await bus.publish("inbox.changed", {"action": "read_all"})
    return {"ok": True}


@router.post("/inbox/{iid}/{action}")
async def inbox_action(iid: str, action: str, _: User = Depends(owner_user),
                       session: AsyncSession = Depends(get_session)):
    it = await session.get(InboxItem, parse_uuid(iid))
    if it is None:
        raise HTTPException(404, "گزارش یافت نشد")
    if action == "read":
        it.read = True
    elif action == "unread":
        it.read = False
    elif action == "archive":
        it.archived, it.read = True, True
    elif action == "restore":
        it.archived = False
    else:
        raise HTTPException(400, "اقدام نامعتبر")
    await bus.publish("inbox.changed", {"id": iid, "action": action})
    return ser_item(it)


# ── مجوزهای مأموریت ──
@router.get("/missions")
async def list_missions(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Mission).order_by(Mission.created_at.desc()).limit(100))).scalars().all()
    out = []
    for m in rows:
        routines = (await session.execute(select(Routine.id, Routine.name, Routine.status).where(
            Routine.mission_id == m.id))).all()
        out.append(ser(m, extra={"scope_text": describe_scope(m.scope or {}),
                                 "routines": [{"id": str(r.id), "name": r.name, "status": r.status} for r in routines]}))
    return {"items": out}


@router.get("/missions/{mid}/actions")
async def mission_actions(mid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(MissionAction).where(MissionAction.mission_id == parse_uuid(mid))
                                  .order_by(MissionAction.ts.desc()).limit(200))).scalars().all()
    return {"items": [ser(a) for a in rows]}


@router.post("/missions/{mid}/revoke")
async def revoke_mission(mid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    m = await session.get(Mission, parse_uuid(mid))
    if m is None:
        raise HTTPException(404, "مجوز یافت نشد")
    if m.status in ("revoked", "expired", "completed"):
        return ser(m)
    m.status, m.revoked_at, m.revoked_by = "revoked", utcnow(), user.username
    routines = (await session.execute(select(Routine).where(Routine.mission_id == m.id))).scalars().all()
    for r in routines:
        await finalize_routine(session, r, utcnow(), "grant_revoked", status="cancelled")
    await audit(session, user.username, "mission.revoke", str(m.id), {"title": m.title})
    await bus.publish("routine.changed", {"action": "mission_revoked", "mission_id": mid})
    return ser(m)


# ── شناسنامه قابلیت‌ها ──
@router.get("/agent/capabilities")
async def capabilities(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    return await build_manifest(session, principal="owner" if user.role == "owner" else "viewer")


# ── پاکسازی منطقی (بدون حذف سابقه) ──
@router.post("/runs/close_stale")
async def close_stale_runs(days: int = Query(7, ge=1, le=365), user: User = Depends(owner_user),
                           session: AsyncSession = Depends(get_session)):
    """اجراهای «متوقف موقت» قدیمی بسته می‌شوند (لغو با دلیل stale_closed)؛ هیچ رکورد یا Audit حذف نمی‌شود."""
    cutoff = utcnow() - timedelta(days=days)
    ids = (await session.execute(update(Run).where(Run.status == "paused", Run.created_at < cutoff)
                                 .values(status="cancelled", stop_reason="stale_closed", finished_at=utcnow())
                                 .returning(Run.id))).scalars().all()
    await audit(session, user.username, "runs.close_stale", None, {"days": days, "count": len(ids)})
    await bus.publish("run.status", {"action": "close_stale", "count": len(ids)})
    return {"closed": len(ids)}
