"""رویدادهای زنده (SSE)، داشبورد، اعلان‌ها، مصرف/ارزیابی و بار سیستم."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from datetime import UTC, datetime, timedelta

import psutil
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import Integer, case, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.models import (
    AIModel,
    Approval,
    BehavioralRule,
    ConfigChange,
    DreamReport,
    Employee,
    Episode,
    Experience,
    Incident,
    InstalledTool,
    Notification,
    Routine,
    Run,
    SemanticFact,
    Skill,
    SystemMetric,
    Task,
    UsageEvent,
    User,
)
from xagent_core.settings_store import GeneralSettings, StopLossSettings, get_setting
from xagent_core.timeutil import get_zone, now_utc

from ..deps import current_user, get_session, owner_user, ser

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

    control = await _control_snapshot(session)
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
        "control": control,
        "timezone": general.timezone,
    }


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one() or 0)


async def _control_snapshot(session: AsyncSession) -> dict:
    """نقشه زنده اجزای ایجنت برای داشبورد: مدیر، کارمندان، مدل‌ها، ابزارها، حافظه، رویا و تغییرات (فقط داده واقعی)."""
    c = func.count()
    day = now_utc() - timedelta(days=1)
    emp_total = await _count(session, select(c).select_from(Employee))
    emp_on = await _count(session, select(c).select_from(Employee).where(Employee.enabled.is_(True)))
    emp_busy = await _count(session, select(func.count(func.distinct(Run.employee_id))).where(
        Run.employee_id.is_not(None), Run.status.in_(["running", "queued", "waiting_approval"])))
    m_on = await _count(session, select(c).select_from(AIModel).where(AIModel.enabled.is_(True)))
    m_fail = await _count(session, select(c).select_from(AIModel).where(AIModel.enabled.is_(True), AIModel.health == "failing"))
    m_deg = await _count(session, select(c).select_from(AIModel).where(AIModel.enabled.is_(True), AIModel.health == "degraded"))
    t_on = await _count(session, select(c).select_from(InstalledTool).where(InstalledTool.enabled.is_(True), InstalledTool.status == "ready"))
    t_err = await _count(session, select(c).select_from(InstalledTool).where(InstalledTool.status == "error"))
    skills = await _count(session, select(c).select_from(Skill).where(Skill.status.in_(["active", "probationary"])))
    mem = {
        "behavioral": await _count(session, select(c).select_from(BehavioralRule)),
        "semantic": await _count(session, select(c).select_from(SemanticFact)),
        "episodic": await _count(session, select(c).select_from(Episode)),
        "experience": await _count(session, select(c).select_from(Experience)),
    }
    inc_open = await _count(session, select(c).select_from(Incident).where(Incident.status.in_(["open", "fixing", "escalated"])))
    ch_day = await _count(session, select(c).select_from(ConfigChange).where(ConfigChange.created_at >= day))
    last = (await session.execute(select(ConfigChange).order_by(ConfigChange.created_at.desc()).limit(1))).scalar_one_or_none()
    dream = (await session.execute(select(DreamReport).order_by(DreamReport.started_at.desc()).limit(1))).scalar_one_or_none()
    return {
        "employees": {"total": emp_total, "enabled": emp_on, "busy": emp_busy},
        "models": {"enabled": m_on, "failing": m_fail, "degraded": m_deg},
        "tools": {"ready": t_on, "error": t_err, "skills": skills},
        "memory": mem,
        "incidents_open": inc_open,
        "changes_24h": ch_day,
        "last_change": ser(last, exclude={"before", "after", "verification"}) if last else None,
        "dream": {"status": dream.status, "started_at": dream.started_at.isoformat()} if dream else None,
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
    # Provider usage is authoritative; unknown/timeout reservations are kept
    # separate from confirmed tokens and may represent additional billing.
    by_status = (await session.execute(
        select(UsageEvent.attempt_status, func.count(), func.sum(UsageEvent.tokens_in),
               func.sum(UsageEvent.tokens_out), func.sum(UsageEvent.cost_usd),
               func.sum(UsageEvent.reserved_cost_usd), func.sum(UsageEvent.reserved_tokens))
        .where(UsageEvent.ts >= since).group_by(UsageEvent.attempt_status))).all()
    ledger_purposes = (await session.execute(
        select(UsageEvent.purpose, func.count(), func.sum(UsageEvent.tokens_in),
               func.sum(UsageEvent.tokens_out), func.sum(UsageEvent.cache_read),
               func.sum(UsageEvent.cache_write), func.sum(UsageEvent.cost_usd),
               func.sum(UsageEvent.reserved_cost_usd))
        .where(UsageEvent.ts >= since).group_by(UsageEvent.purpose))).all()
    subsets = (await session.execute(select(
        func.coalesce(func.sum(cast(UsageEvent.usage_details["reasoning_tokens"].astext, Integer)), 0),
        func.coalesce(func.sum(cast(UsageEvent.usage_details["audio_input_tokens"].astext, Integer)), 0),
        func.coalesce(func.sum(cast(UsageEvent.usage_details["audio_output_tokens"].astext, Integer)), 0),
    ).where(UsageEvent.ts >= since))).one()
    latest_attempts = (await session.execute(
        select(UsageEvent).where(UsageEvent.ts >= since)
        .order_by(UsageEvent.ts.desc()).limit(40))).scalars().all()
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
        "ledger": {
            "statuses": [{"status": st, "calls": int(n), "tokens_in": int(i or 0),
                          "tokens_out": int(o or 0), "cost": float(cost or 0),
                          "reserved_cost": float(res or 0), "reserved_tokens": int(rt or 0)}
                         for st, n, i, o, cost, res, rt in by_status],
            "purposes": [{"purpose": purpose or "-", "calls": int(n), "tokens_in": int(i or 0),
                          "tokens_out": int(o or 0), "cache_read": int(cr or 0), "cache_write": int(cw or 0),
                          "cost": float(cost or 0), "reserved_cost": float(res or 0)}
                         for purpose, n, i, o, cr, cw, cost, res in ledger_purposes],
            "subsets": {"reasoning_tokens": int(subsets[0] or 0),
                        "audio_input_tokens": int(subsets[1] or 0),
                        "audio_output_tokens": int(subsets[2] or 0)},
            "recent": [{"at": row.ts.isoformat(), "request_id": row.request_id,
                        "provider_request_id": row.provider_request_id, "model": row.model_name,
                        "provider": row.provider_kind, "purpose": row.purpose,
                        "status": row.attempt_status, "tokens_in": row.tokens_in,
                        "tokens_out": row.tokens_out, "cache_read": row.cache_read,
                        "cache_write": row.cache_write, "cost": float(row.cost_usd),
                        "reserved_cost": float(row.reserved_cost_usd),
                        "reported": (row.usage_details or {}).get("reported"),
                        "non_token_units": (row.usage_details or {}).get("non_token_units")}
                       for row in latest_attempts],
        },
        "runs": [{"status": s, "count": int(c), "avg_seconds": float(d or 0), "avg_tokens": float(t or 0),
                  "avg_cost": float(co or 0)} for s, c, d, t, co in runs],
        "by_trigger": [{"trigger": t, "count": int(c), "cost": float(co or 0)} for t, c, co in by_trigger],
        "success_rate": (totals.get("succeeded", 0) / finished) if finished else None,
        "top_runs": [ser(r, exclude={"plan", "goal", "result", "route"}) for r in top_runs],
    }


@router.post("/usage/reconcile")
async def reconcile_usage_csv(file: UploadFile = File(...), _: User = Depends(owner_user),
                              session: AsyncSession = Depends(get_session)):
    """Compare official OpenAI one-minute CSV aggregates without making up missing requests.

    The CSV is processed in memory; no provider secrets or raw prompts are stored.
    OpenAI's aggregate key IDs are not assumed to match local provider keys.
    """
    raw = await file.read(8_000_001)
    if len(raw) > 8_000_000:
        raise HTTPException(413, "فایل CSV از حد مجاز بزرگ‌تر است")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        if not {"start_time_iso", "model", "input_tokens", "output_tokens"}.issubset(reader.fieldnames or []):
            raise ValueError("ستون‌های گزارش OpenAI ناقص است")
        sums: dict[str, dict[str, int]] = {}
        hourly: dict[str, int] = {}
        key_ids: set[str] = set()
        min_ts: datetime | None = None
        max_ts: datetime | None = None

        def value(row: dict, key: str) -> int:
            from decimal import Decimal, InvalidOperation
            raw_num = row.get(key) or "0"
            try:
                amount = Decimal(str(raw_num))
            except InvalidOperation as exc:
                raise ValueError(f"عدد نامعتبر در ستون {key}") from exc
            if amount < 0 or amount != amount.to_integral_value():
                raise ValueError(f"عدد غیرمجاز در ستون {key}")
            return int(amount)

        for idx, row in enumerate(reader):
            if idx >= 100_000:
                raise ValueError("تعداد ردیف‌های فایل بیش از حد مجاز است")
            model = str(row.get("model") or "").strip()
            if not model:
                continue
            at = datetime.fromisoformat(str(row["start_time_iso"]).replace("Z", "+00:00")).astimezone(UTC)
            if at > now_utc() + timedelta(days=1):
                raise ValueError("تاریخ گزارش در آینده است")
            min_ts = at if min_ts is None else min(min_ts, at)
            max_ts = at + timedelta(minutes=1) if max_ts is None else max(max_ts, at + timedelta(minutes=1))
            bucket = sums.setdefault(model, {"requests": 0, "input": 0, "output": 0, "cached": 0})
            bucket["requests"] += value(row, "num_model_requests")
            bucket["input"] += value(row, "input_tokens")
            bucket["output"] += value(row, "output_tokens")
            bucket["cached"] += value(row, "input_cached_tokens")
            hour = at.strftime("%Y-%m-%dT%H:00Z")
            hourly[hour] = hourly.get(hour, 0) + value(row, "num_model_requests")
            if row.get("api_key_id"):
                key_ids.add(row["api_key_id"])
    except (UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(422, f"فایل گزارش قابل بررسی نیست: {exc}") from exc
    if not sums or min_ts is None or max_ts is None:
        raise HTTPException(422, "هیچ مصرفی در فایل وجود ندارد")
    internal = (await session.execute(
        select(UsageEvent.model_name, func.count(), func.sum(UsageEvent.tokens_in),
               func.sum(UsageEvent.tokens_out), func.sum(UsageEvent.cache_read))
        .where(UsageEvent.ts >= min_ts, UsageEvent.ts < max_ts,
               UsageEvent.provider_kind.in_(["openai", "openai_chat"]),
               UsageEvent.attempt_status == "confirmed")
        .group_by(UsageEvent.model_name))).all()
    by_model = {model: {"requests": int(n), "input": int(i or 0),
                        "output": int(o or 0), "cached": int(cr or 0)}
                for model, n, i, o, cr in internal}
    models = sorted(set(sums) | set(by_model))
    return {
        "from_utc": min_ts.isoformat(), "to_utc": max_ts.isoformat(),
        "external_key_count": len(key_ids),
        "external": {k: sums[k] for k in sums},
        "internal": by_model,
        "by_model": [
            {"model": name, "official": sums.get(name, {}),
             "local_confirmed": by_model.get(name, {}),
             "delta_input": sums.get(name, {}).get("input", 0) - by_model.get(name, {}).get("input", 0),
             "delta_output": sums.get(name, {}).get("output", 0) - by_model.get(name, {}).get("output", 0)}
            for name in models
        ],
        "external_hourly_requests": [{"hour": h, "requests": hourly[h]} for h in sorted(hourly)],
        "notice": "CSV تجمیعی است و نام فراخواننده را نشان نمی‌دهد. اختلاف می‌تواند ناشی از "
                  "کلید مشترک، بازه زمانی، تأخیر گزارش یا ثبت‌نشدن مصرف داخلی باشد.",
    }
