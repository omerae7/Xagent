"""زمان‌بند روتین‌ها با قفل اتمیک (Redis Lease + TTL)، مدارقطع‌کن و رویای شبانه."""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import or_, select

from xagent_core import bus
from xagent_core.audit import notify
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Conversation, DreamReport, Routine, Run
from xagent_core.settings_store import DreamSettings, GeneralSettings, RoutineSafetySettings, get_setting
from xagent_core.timeutil import get_zone, now_utc
from xagent_tools.schedule import compute_next_run

log = logging.getLogger("xagent.scheduler")
ACTIVE = ("queued", "running", "waiting_approval")


async def tick_routines() -> int:
    if await bus.is_stop_all():
        return 0
    created: list = []
    async with session_scope() as session:
        safety = await get_setting(session, "routine_safety", RoutineSafetySettings)
        now = utcnow()
        due = (await session.execute(select(Routine).where(
            Routine.enabled.is_(True), Routine.paused.is_(False), Routine.trigger_type != "event",
            Routine.next_run_at.is_not(None), Routine.next_run_at <= now,
            or_(Routine.circuit_state != "open", Routine.circuit_open_until <= now),
        ).limit(50))).scalars().all()
        for r in due:
            lease = await bus.acquire_lease(f"routine:{r.id}", safety.lease_ttl_seconds * 1000, "scheduler")
            if lease is None:
                continue  # روتین مشابه در حال زمان‌بندی است
            active = (await session.execute(select(Run.id).where(
                Run.routine_id == r.id, Run.status.in_(ACTIVE)).limit(1))).scalar_one_or_none()
            r.last_run_at = now
            r.next_run_at = compute_next_run(r, now)
            if active is not None:
                log.info("روتین %s هنوز در حال اجراست؛ این نوبت رد شد", r.name)
                continue
            if r.circuit_state == "open":
                r.circuit_state = "half_open"
            conv = (await session.execute(select(Conversation).where(
                Conversation.channel == "routine", Conversation.external_ref == str(r.id)))).scalar_one_or_none()
            if conv is None:
                conv = Conversation(title=f"روتین: {r.name}", channel="routine", external_ref=str(r.id))
                session.add(conv)
                await session.flush()
            run = Run(conversation_id=conv.id, routine_id=r.id, trigger="routine", input=r.instruction,
                      state={"trusted": True, "max_cost": float(r.max_cost_usd) if r.max_cost_usd else None,
                             "max_steps": r.max_steps, "lease": lease})
            session.add(run)
            await session.flush()
            r.run_count += 1
            created.append(run.id)
    for rid in created:
        await bus.enqueue_run(rid, "routine")
    return len(created)


async def after_routine_run(run_id) -> None:  # noqa: ANN001
    """به‌روزرسانی وضعیت روتین و مدارقطع‌کن پس از پایان اجرا."""
    async with session_scope() as session:
        run = await session.get(Run, run_id)
        if run is None or run.routine_id is None or run.status in ACTIVE or run.status == "paused":
            return
        r = await session.get(Routine, run.routine_id)
        if r is None:
            return
        safety = await get_setting(session, "routine_safety", RoutineSafetySettings)
        lease = (run.state or {}).get("lease")
        if lease:
            await bus.release_lease(f"routine:{r.id}", lease)
        ok = run.status == "succeeded"
        r.last_status = "success" if ok else run.status
        if ok:
            r.consecutive_failures = 0
            r.circuit_state = "closed"
            r.circuit_open_until = None
        else:
            r.consecutive_failures += 1
            if r.consecutive_failures >= safety.circuit_failure_threshold:
                cooldown = safety.circuit_cooldown_minutes // (3 if r.time_sensitive else 1)
                r.circuit_state = "open"
                r.circuit_open_until = utcnow() + timedelta(minutes=max(1, cooldown))
                await notify(
                    session,
                    f"{'🚨 ' if r.time_sensitive else ''}مدارقطع‌کن روتین «{r.name}» فعال شد",
                    f"{r.consecutive_failures} خطای پیاپی. اجرای بعدی پس از {cooldown} دقیقه به‌صورت آزمایشی."
                    + (" (روتین زمان‌حساس — لطفاً بررسی کنید)" if r.time_sensitive else ""),
                    level="error" if r.time_sensitive else "warning",
                    link="/routines",
                )
        if r.trigger_type == "once" and ok:
            r.enabled = False
    await bus.publish("routine.changed", {"id": str(run.routine_id), "action": "run_finished"})


async def dream_due() -> bool:
    async with session_scope() as session:
        cfg = await get_setting(session, "dream", DreamSettings)
        general = await get_setting(session, "general", GeneralSettings)
        if not cfg.enabled:
            return False
        tz = get_zone(general.timezone)
        local = now_utc().astimezone(tz)
        if (local.hour, local.minute) < (cfg.hour, cfg.minute):
            return False
        start_local = local.replace(hour=cfg.hour, minute=cfg.minute, second=0, microsecond=0)
        last = (await session.execute(select(DreamReport).where(
            DreamReport.started_at >= start_local).limit(1))).scalar_one_or_none()
        return last is None
