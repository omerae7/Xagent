"""رویدادهای زنده (SSE)، داشبورد، اعلان‌ها، مصرف/ارزیابی و بار سیستم."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta

import psutil
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import Integer, case, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.models import (
    Approval,
    Episode,
    Notification,
    Routine,
    Run,
    SystemMetric,
    Task,
    UsageEvent,
    User,
)
from xagent_core.settings_store import GeneralSettings, StopLossSettings, get_setting
from xagent_core.timeutil import get_zone, now_utc

from ..deps import current_user, get_session, ser

router = APIRouter(prefix="/api", tags=["live"])


@router.get("/events/stream")
async def stream(request: Request, _: User = Depends(current_user)):
    async def gen():
        yield "retry: 3000\n\n"
        sub = bus.subscribe()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        async def pump():
            async for ev in sub:
                try:
                    queue.put_nowait(ev)
                except asyncio.QueueFull:
                    pass

        task = asyncio.create_task(pump())
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: {ev.get('type', 'message')}\ndata: {json.dumps(ev.get('data', {}), ensure_ascii=False, default=str)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def system_snapshot() -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    disk = psutil.disk_usage("/data" if os.path.exists("/data") else "/")
    try:
        load1, load5, load15 = psutil.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count() or 1,
        "mem": vm.percent,
        "mem_used": vm.used,
        "mem_total": vm.total,
        "swap": sw.percent,
        "swap_total": sw.total,
        "disk": disk.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "net_rx": net.bytes_recv,
        "net_tx": net.bytes_sent,
        "uptime": int(now_utc().timestamp() - psutil.boot_time()),
    }


@router.get("/system/metrics")
async def metrics(hours: int = Query(6, le=168), _: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    since = now_utc() - timedelta(hours=hours)
    rows = (await session.execute(select(SystemMetric).where(SystemMetric.ts >= since).order_by(SystemMetric.ts))).scalars().all()
    step = max(1, len(rows) // 240)
    return {"current": system_snapshot(), "series": [ser(r) for r in rows[::step]]}


@router.get("/dashboard")
async def dashboard(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    general = await get_setting(session, "general", GeneralSettings)
    sl = await get_setting(session, "stoploss", StopLossSettings)
    tz = get_zone(general.timezone)
    local_now = now_utc().astimezone(tz)
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)

    run_counts = dict((await session.execute(
        select(Run.status, func.count()).where(Run.status.in_(["queued", "running", "waiting_approval", "paused"]))
        .group_by(Run.status))).all())
    active_runs = (await session.execute(
        select(Run).where(Run.status.in_(["running", "queued", "waiting_approval"])).order_by(Run.created_at.desc()).limit(8)
    )).scalars().all()

    def usage_since(ts):
        return select(
            func.coalesce(func.sum(UsageEvent.tokens_in), 0), func.coalesce(func.sum(UsageEvent.tokens_out), 0),
            func.coalesce(func.sum(UsageEvent.cache_read), 0), func.coalesce(func.sum(UsageEvent.cost_usd), 0),
            func.count(),
        ).where(UsageEvent.ts >= ts)

    d_in, d_out, d_cache, d_cost, d_calls = (await session.execute(usage_since(day_start))).one()
    m_in, m_out, m_cache, m_cost, m_calls = (await session.execute(usage_since(month_start))).one()

    hourly = (await session.execute(
        select(func.date_trunc("hour", UsageEvent.ts).label("h"), func.sum(UsageEvent.tokens_in),
               func.sum(UsageEvent.tokens_out), func.sum(UsageEvent.cost_usd))
        .where(UsageEvent.ts >= now_utc() - timedelta(hours=24)).group_by("h").order_by("h"))).all()

    task_counts = dict((await session.execute(select(Task.status, func.count()).group_by(Task.status))).all())
    due_soon = (await session.execute(
        select(Task).where(Task.status.notin_(["done", "cancelled"]), Task.due_at.is_not(None),
                           Task.due_at <= now_utc() + timedelta(days=2)).order_by(Task.due_at).limit(6))).scalars().all()
    routines = (await session.execute(
        select(Routine).where(Routine.enabled.is_(True), Routine.paused.is_(False), Routine.next_run_at.is_not(None))
        .order_by(Routine.next_run_at).limit(5))).scalars().all()
    routine_total = (await session.execute(select(func.count()).select_from(Routine))).scalar_one()
    approvals = (await session.execute(select(func.count()).select_from(Approval).where(Approval.status == "pending"))).scalar_one()
    notes = (await session.execute(select(Notification).order_by(Notification.ts.desc()).limit(8))).scalars().all()
    unread = (await session.execute(select(func.count()).select_from(Notification).where(Notification.read.is_(False)))).scalar_one()
    week = now_utc() - timedelta(days=7)
    ep_total, ep_ok = (await session.execute(
        select(func.count(), func.coalesce(func.sum(cast(Episode.status == "success", Integer)), 0))
        .where(Episode.ts >= week, Episode.kind != "system"))).one()

    running = run_counts.get("running", 0)
    state = "executing" if running else ("waiting" if run_counts.get("waiting_approval") else
                                         ("thinking" if run_counts.get("queued") else "idle"))
    return {
        "agent": {"name": general.agent_name, "state": state, "stop_all": await bus.is_stop_all(),
                  "takeover": await bus.is_takeover()},
        "runs": {"counts": run_counts, "active": [ser(r, exclude={"plan", "goal", "result"}) for r in active_runs]},
        "usage": {
            "today": {"tokens_in": int(d_in), "tokens_out": int(d_out), "cache_read": int(d_cache),
                      "cost": float(d_cost), "calls": int(d_calls), "limit": sl.daily_cost_usd},
            "month": {"tokens_in": int(m_in), "tokens_out": int(m_out), "cache_read": int(m_cache),
                      "cost": float(m_cost), "calls": int(m_calls), "limit": sl.monthly_cost_usd},
            "hourly": [{"ts": h.isoformat(), "tokens_in": int(a or 0), "tokens_out": int(b or 0), "cost": float(c or 0)}
                       for h, a, b, c in hourly],
        },
        "tasks": {"counts": task_counts, "due_soon": [ser(t) for t in due_soon]},
        "routines": {"total": routine_total, "next": [ser(r) for r in routines]},
        "approvals_pending": approvals,
        "notifications": {"unread": unread, "items": [ser(n) for n in notes]},
        "success_rate_7d": (float(ep_ok) / ep_total) if ep_total else None,
        "episodes_7d": ep_total,
        "system": system_snapshot(),
        "timezone": general.timezone,
    }


@router.get("/notifications")
async def notifications(unread_only: bool = False, limit: int = Query(50, le=200), _: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    stmt = select(Notification).order_by(Notification.ts.desc()).limit(limit)
    if unread_only:
        stmt = stmt.where(Notification.read.is_(False))
    return {"items": [ser(n) for n in (await session.execute(stmt)).scalars()]}


@router.post("/notifications/read-all")
async def read_all(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await session.execute(update(Notification).where(Notification.read.is_(False)).values(read=True))
    return {"ok": True}


@router.get("/usage")
async def usage(days: int = Query(30, le=365), _: User = Depends(current_user),
                session: AsyncSession = Depends(get_session)):
    general = await get_setting(session, "general", GeneralSettings)
    since = now_utc() - timedelta(days=days)
    day = func.date_trunc("day", func.timezone(general.timezone, UsageEvent.ts)).label("d")
    daily = (await session.execute(
        select(day, func.sum(UsageEvent.tokens_in), func.sum(UsageEvent.tokens_out), func.sum(UsageEvent.cache_read),
               func.sum(UsageEvent.cost_usd), func.count())
        .where(UsageEvent.ts >= since).group_by(day).order_by(day))).all()
    by_model = (await session.execute(
        select(UsageEvent.model_name, UsageEvent.provider_kind, func.sum(UsageEvent.tokens_in),
               func.sum(UsageEvent.tokens_out), func.sum(UsageEvent.cache_read), func.sum(UsageEvent.cost_usd),
               func.count(), func.avg(UsageEvent.latency_ms),
               func.sum(case((UsageEvent.success.is_(False), 1), else_=0)))
        .where(UsageEvent.ts >= since).group_by(UsageEvent.model_name, UsageEvent.provider_kind)
        .order_by(func.sum(UsageEvent.cost_usd).desc()))).all()
    by_purpose = (await session.execute(
        select(UsageEvent.purpose, func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), func.sum(UsageEvent.cost_usd),
               func.count())
        .where(UsageEvent.ts >= since).group_by(UsageEvent.purpose))).all()
    runs = (await session.execute(
        select(Run.status, func.count(), func.avg(func.extract("epoch", Run.finished_at - Run.started_at)),
               func.avg(Run.tokens_in + Run.tokens_out), func.avg(Run.cost_usd))
        .where(Run.created_at >= since).group_by(Run.status))).all()
    by_trigger = (await session.execute(
        select(Run.trigger, func.count(), func.sum(Run.cost_usd)).where(Run.created_at >= since).group_by(Run.trigger))).all()
    top_runs = (await session.execute(
        select(Run).where(Run.created_at >= since).order_by(Run.cost_usd.desc()).limit(10))).scalars().all()
    totals = {s: c for s, c, *_ in runs}
    finished = sum(totals.get(k, 0) for k in ("succeeded", "failed", "stopped"))
    return {
        "daily": [{"day": d.date().isoformat(), "tokens_in": int(a or 0), "tokens_out": int(b or 0),
                   "cache_read": int(c or 0), "cost": float(e or 0), "calls": int(n)} for d, a, b, c, e, n in daily],
        "by_model": [{"model": m, "provider": p, "tokens_in": int(a or 0), "tokens_out": int(b or 0),
                      "cache_read": int(c or 0), "cost": float(e or 0), "calls": int(n), "avg_latency_ms": float(lat or 0),
                      "errors": int(err or 0)} for m, p, a, b, c, e, n, lat, err in by_model],
        "by_purpose": [{"purpose": p or "-", "tokens": int(t or 0), "cost": float(c or 0), "calls": int(n)}
                       for p, t, c, n in by_purpose],
        "runs": [{"status": s, "count": int(c), "avg_seconds": float(d or 0), "avg_tokens": float(t or 0),
                  "avg_cost": float(co or 0)} for s, c, d, t, co in runs],
        "by_trigger": [{"trigger": t, "count": int(c), "cost": float(co or 0)} for t, c, co in by_trigger],
        "success_rate": (totals.get("succeeded", 0) / finished) if finished else None,
        "top_runs": [ser(r, exclude={"plan", "goal", "result", "route"}) for r in top_runs],
    }
