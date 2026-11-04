"""زمان‌بند روتین‌ها: چرخه حیات کامل مأموریت، مدارقطع‌کن، بازیابی اجراهای یتیم و رویای شبانه.

مالکیت و هم‌زمانی (بازطراحی پس از ریشه‌یابی نشت Lease):
- ادعای نوبت با قفل سطری PostgreSQL (SELECT … FOR UPDATE SKIP LOCKED) انجام می‌شود؛ قفل در پایان
  تراکنش در همه مسیرها (commit/rollback/خطا) خودکار آزاد می‌شود — دیگر Lease بی‌صاحبی باقی نمی‌ماند.
- هر نوبت کلید یکتای runs.dedupe_key دارد؛ حتی دو Scheduler هم‌زمان یا ری‌استارت هم یک نوبت را دوبار
  اجرا نمی‌کنند.
- مالکیت اجرای در حال انجام همان Lease اجرای Run است که با Heartbeat تمدید می‌شود؛ اگر Worker از کار
  بیفتد، reconcile_runs پس از انقضای آن اجرا را دوباره در صف می‌گذارد (ادامه از Checkpoint).
- در فاصله نوبت‌ها هیچ فراخوانی مدلی انجام نمی‌شود؛ تیک زمان‌بند فقط کوئری دیتابیس است.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus, inbox
from xagent_core.audit import notify
from xagent_core.db import session_scope, utcnow
from xagent_core.models import DreamReport, Mission, Routine, Run, Task
from xagent_core.settings_store import (
    DreamSettings,
    GeneralSettings,
    ReportSettings,
    RoutineSafetySettings,
    get_setting,
)
from xagent_core.text import truncate
from xagent_core.timeutil import get_zone, jalali_str, now_utc
from xagent_tools.lifecycle import (
    active_run,
    claim_occurrence,
    finalize_routine,
    mission_block,
    pause_for_budget,
    remaining_budget,
)
from xagent_tools.schedule import compute_next_run, exhausted_reason, slot_at_or_before

log = logging.getLogger("xagent.scheduler")
ACTIVE = ("queued", "running", "waiting_approval")


# ───────────────────────── ادعای نوبت و ساخت اجرا ─────────────────────────


async def _dispatch_due(session: AsyncSession, r: Routine, now) -> uuid.UUID | None:  # noqa: ANN001
    block = await mission_block(session, r, now)
    if block is not None:
        if await active_run(session, r.id) is None:
            await finalize_routine(session, r, now, block[1], status=block[0])
        return None
    slot = slot_at_or_before(r, now) or r.next_run_at
    if r.trigger_type == "interval" and r.next_run_at and slot and slot > r.next_run_at and r.interval_seconds:
        missed = int((slot - r.next_run_at).total_seconds() // r.interval_seconds)
        r.skipped_count = (r.skipped_count or 0) + missed  # نوبت‌های خاموشی ادغام می‌شوند، نه جبران
    if r.end_at is not None and slot >= r.end_at:
        r.next_run_at = None
        if await active_run(session, r.id) is None:
            await finalize_routine(session, r, now, "end_at")
        return None
    active = await active_run(session, r.id)
    if active is not None:
        r.skipped_count = (r.skipped_count or 0) + 1
        r.next_run_at = compute_next_run(r, now)
        log.info("روتین %s هنوز اجرای فعال دارد؛ نوبت %s رد شد", r.name, slot)
        return None
    mission = await session.get(Mission, r.mission_id) if r.mission_id else None
    remaining = remaining_budget(r, mission)
    if remaining is not None and remaining <= 0:
        await pause_for_budget(session, r)
        return None
    run_id = await claim_occurrence(session, r, slot)
    r.next_run_at = compute_next_run(r, now)
    if run_id is not None and r.circuit_state == "open":
        r.circuit_state = "half_open"
    return run_id


async def tick_routines(telegram: Any = None) -> int:
    """یک تیک زمان‌بند. در STOP_ALL هیچ اجرای جدیدی ساخته نمی‌شود و وضعیتی جابه‌جا نمی‌شود."""
    if await bus.is_stop_all():
        return 0
    created: list[uuid.UUID] = []
    async with session_scope() as session:
        now = utcnow()
        due = (await session.execute(select(Routine).where(
            Routine.status == "active", Routine.trigger_type != "event",
            Routine.next_run_at.is_not(None), Routine.next_run_at <= now,
            or_(Routine.circuit_state != "open", Routine.circuit_open_until <= now),
        ).order_by(Routine.next_run_at).limit(50).with_for_update(skip_locked=True))).scalars().all()
        for r in due:
            try:
                async with session.begin_nested():
                    rid = await _dispatch_due(session, r, now)
                if rid is not None:
                    created.append(rid)
            except Exception as e:  # noqa: BLE001 — خطای یک روتین مانع بقیه نشود؛ نوبت حفظ می‌شود
                log.exception("خطای زمان‌بندی روتین %s", r.id)
                await session.refresh(r)
                r.last_error = truncate(f"خطای زمان‌بند: {type(e).__name__}: {e}", 500)
        await sweep_lifecycle(session, now, telegram)
    for rid in created:
        await bus.enqueue_run(rid, "routine")
    if created:
        await bus.publish("routine.changed", {"action": "dispatched", "count": len(created)})
    return len(created)


# ───────────────────────── پایان، توقف و گزارش ─────────────────────────


async def sweep_lifecycle(session: AsyncSession, now, telegram: Any = None) -> int:  # noqa: ANN001
    """بستن مأموریت‌هایی که نوبت دیگری ندارند و اجرای فعالی هم ندارند (پوشش Crash و تأخیر)."""
    # روتین‌های رویدادی هم شامل‌اند: روتین منقضی/تمام‌شده حتی اگر رویداد تازه‌ای نرسد بسته می‌شود
    rows = (await session.execute(select(Routine).where(
        Routine.status == "active",
        or_(Routine.next_run_at.is_(None), Routine.end_at <= now, Routine.mission_id.is_not(None),
            Routine.trigger_type == "event"),
    ).limit(200).with_for_update(skip_locked=True))).scalars().all()
    n = 0
    for r in rows:
        block = await mission_block(session, r, now)
        reason = block[1] if block else exhausted_reason(r, now)
        if reason is None or await active_run(session, r.id) is not None:
            continue
        await finalize_routine(session, r, now, reason, status=block[0] if block else None, telegram=telegram)
        n += 1
    return n


def _outcome(run: Run) -> str:
    if run.status == "succeeded":
        return "no_change" if run.outcome == "no_change" else "success"
    if run.status == "paused":
        return "interrupted" if run.stop_reason in ("stop_all", "cancelled") else "partial"
    if run.status in ("stopped", "cancelled"):
        return "cancelled"
    return "failure"


async def after_routine_run(run_id, telegram: Any = None) -> None:  # noqa: ANN001
    """ثبت نتیجه نوبت، شمارنده‌ها، هزینه، مدارقطع‌کن، گزارش و پایان مأموریت — یک‌بار به ازای هر وضعیت."""
    async with session_scope() as session:
        run = await session.get(Run, run_id, with_for_update=True)
        if run is None or run.routine_id is None or run.status in ACTIVE:
            return
        st = dict(run.state or {})
        settled = st.get("routine_settled")
        if settled == run.status:
            return
        r = await session.get(Routine, run.routine_id, with_for_update=True)
        # سازگاری با اجراهای زمان‌بند قبلی که Lease را در state نگه می‌داشتند
        lease = st.pop("lease", None)
        if lease:
            await bus.release_lease(f"routine:{run.routine_id}", lease)
        if r is None:
            st["routine_settled"] = run.status
            run.state = st
            return
        cost = Decimal(run.cost_usd or 0)
        accounted = Decimal(str(st.get("routine_cost_accounted", 0)))
        r.spent_usd = Decimal(r.spent_usd or 0) + (cost - accounted)
        st["routine_cost_accounted"] = float(cost)
        outcome = _outcome(run)
        first_settle = settled is None
        if settled == "paused" and st.get("routine_outcome") == "partial" and outcome in ("success", "no_change"):
            r.failure_count = max(0, (r.failure_count or 0) - 1)  # اجرای نیمه‌کاره بعداً کامل شد
        if first_settle or settled == "paused":
            if outcome == "success":
                r.success_count = (r.success_count or 0) + 1
            elif outcome == "no_change":
                r.no_change_count = (r.no_change_count or 0) + 1
            elif outcome in ("failure", "partial") and first_settle:
                r.failure_count = (r.failure_count or 0) + 1
        st["routine_settled"] = run.status
        st["routine_outcome"] = outcome
        run.state = st
        r.last_status = outcome
        if outcome in ("success", "no_change"):
            r.last_error = None
            r.consecutive_failures = 0
            r.circuit_state = "closed"
            r.circuit_open_until = None
        elif outcome in ("failure", "partial"):
            r.last_error = truncate(run.error or run.stop_reason or run.result or outcome, 500)
            await _circuit(session, r)
        await _report_run(session, r, run, outcome, telegram)
        now = utcnow()
        mission = await session.get(Mission, r.mission_id) if r.mission_id else None
        remaining = remaining_budget(r, mission)
        if r.status == "active" and remaining is not None and remaining <= 0:
            await pause_for_budget(session, r, telegram)
        elif r.status == "active":
            reason = exhausted_reason(r, now)
            if reason is not None and (r.trigger_type == "once" or r.next_run_at is None or reason == "max_runs"):
                await finalize_routine(session, r, now, reason, telegram=telegram)
    await bus.publish("routine.changed", {"id": str(run.routine_id), "action": "run_finished"})


async def _circuit(session: AsyncSession, r: Routine) -> None:
    safety = await get_setting(session, "routine_safety", RoutineSafetySettings)
    r.consecutive_failures = (r.consecutive_failures or 0) + 1
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


async def _report_run(session: AsyncSession, r: Routine, run: Run, outcome: str, telegram: Any) -> None:
    """گزارش پیش‌دستانه: بدون تغییر → هیچ؛ نتیجه مهم/خطا → میز کار (با پیوند به Run)."""
    if outcome in ("no_change", "interrupted", "cancelled") or r.report_policy == "off":
        return
    cfg = await get_setting(session, "reports", ReportSettings)
    general = await get_setting(session, "general", GeneralSettings)
    when = jalali_str(run.finished_at or utcnow(), general.timezone)
    link = f"/chat/{run.conversation_id}" if run.conversation_id else "/routines"
    common = {"run_id": run.id, "routine_id": r.id, "mission_id": r.mission_id,
              "conversation_id": run.conversation_id, "link": link, "telegram": telegram,
              "dedupe_key": f"run:{run.id}:{run.status}"}
    if outcome == "success":
        watched = bool(r.watch)
        if r.report_policy == "errors" or (not watched and not cfg.routine_success):
            return
        events = int((run.state or {}).get("watch_events", 0))
        title = (f"«{r.name}»: {events} مورد جدید بررسی شد" if watched else f"گزارش روتین «{r.name}»")
        await inbox.post(session, kind="report", severity="important" if watched else "info", title=title,
                         body=f"{when}\n{run.result or ''}", data={"outcome": outcome, "events": events}, **common)
        return
    sev = "critical" if r.time_sensitive or r.circuit_state == "open" else "important"
    title = (f"اجرای روتین «{r.name}» نیمه‌کاره ماند" if outcome == "partial"
             else f"اجرای روتین «{r.name}» ناموفق بود")
    body = f"{when}\n{r.last_error or ''}\nوضعیت فعلی در صفحه روتین‌ها قابل پیگیری است."
    await inbox.post(session, kind="error", severity=sev, title=title, body=body,
                     data={"outcome": outcome, "stop_reason": run.stop_reason}, **common)


# ───────────────────────── تسک‌های موعددار ─────────────────────────


async def tick_tasks(telegram: Any = None) -> int:
    """تسک‌های ایجنت/وابسته که موعدشان رسیده در صف می‌روند؛ تسک انسانی فقط یادآوری می‌شود. بدون LLM."""
    from xagent_tools.tasks_exec import MISSED_GRACE, due_tasks, start_task_run

    if await bus.is_stop_all():
        return 0
    created: list[uuid.UUID] = []
    async with session_scope() as session:
        now = utcnow()
        for t, slot in await due_tasks(session, now):
            if t.missed_policy == "skip" and slot is not None and now - slot > MISSED_GRACE:
                t.status, t.result_summary = "cancelled", "موعد اجرا از دست رفت (سیاست: رد نوبت ازدست‌رفته)"
                await inbox.post(session, kind="error", severity="important", title=f"موعد تسک «{t.title}» از دست رفت",
                                 body=t.result_summary, link="/tasks", dedupe_key=f"task:{t.id}:missed",
                                 telegram=telegram)
                continue
            run = await start_task_run(session, t, slot=slot)
            if run is not None:
                created.append(run.id)
        # تسک انسانی: یادآوری یک‌باره در موعد (بدون اجرای خودکار)
        reminders = (await session.execute(select(Task).where(
            Task.kind == "human", Task.status.notin_(["done", "cancelled", "failed"]), Task.due_at.is_not(None),
            Task.due_at <= now, Task.reminded_at.is_(None)).limit(50).with_for_update(skip_locked=True))).scalars().all()
        for t in reminders:
            t.reminded_at = now
            await inbox.post(session, kind="report", severity="important", title=f"یادآوری: موعد «{t.title}» رسید",
                             body=t.description or "", link="/tasks", dedupe_key=f"task:{t.id}:reminder",
                             data={"task_id": str(t.id)}, telegram=telegram)
    for rid in created:
        await bus.enqueue_run(rid, "task")
    if created:
        await bus.publish("task.changed", {"action": "started", "count": len(created)})
    return len(created)


async def after_task_run(run_id, telegram: Any = None) -> None:  # noqa: ANN001
    from xagent_tools.tasks_exec import settle_task

    async with session_scope() as session:
        run = await session.get(Run, run_id, with_for_update=True)
        if run is not None:
            await settle_task(session, run, telegram)


# ───────────────────────── بازیابی اجراهای یتیم ─────────────────────────


async def reconcile_runs(stale_after_seconds: int = 180) -> int:
    """اجرای running/queued بدون Lease زنده (Worker مرده یا پیام صف گم‌شده) دوباره در صف قرار می‌گیرد.

    ادامه از آخرین Checkpoint انجام می‌شود و گام‌های انجام‌شده تکرار نمی‌شوند. اگر Redis در دسترس
    نباشد کاری انجام نمی‌شود (حالت امن: هیچ اجرایی بی‌دلیل دوباره شروع نمی‌شود).
    """
    redis = bus.get_redis()
    ids: list[uuid.UUID] = []
    async with session_scope() as session:
        now = utcnow()
        rows = (await session.execute(select(Run).where(Run.status.in_(["running", "queued"]))
                                      .with_for_update(skip_locked=True).limit(200))).scalars().all()
        for run in rows:
            try:
                if await redis.exists(f"xagent:lease:run:{run.id}"):
                    continue
            except Exception:  # noqa: BLE001
                log.warning("Redis در دسترس نیست؛ بازیابی اجراها به بعد موکول شد")
                return 0
            # «running» بدون Lease زنده یتیم است (Lease پیش از running گرفته و فقط پس از وضعیت نهایی آزاد می‌شود)؛
            # «queued» ممکن است مشروعاً در صف منتظر ظرفیت Worker باشد → فقط پس از stale_after دوباره در صف
            if run.status == "queued" and run.created_at is not None and \
                    (now - run.created_at).total_seconds() < stale_after_seconds:
                continue
            run.status = "queued"
            ids.append(run.id)
    for rid in ids:
        await bus.enqueue_run(rid, "reconcile")
    if ids:
        log.info("%d اجرای یتیم دوباره در صف قرار گرفت", len(ids))
    return len(ids)


TERMINAL_RUN = ("succeeded", "failed", "stopped", "cancelled", "paused")


async def settle_terminal_runs(min_age_seconds: int = 30, lookback_days: int = 14, telegram: Any = None) -> int:
    """تسویه دوباره Runهای پایانی که تسویه روتین/تسک آن‌ها انجام نشده است (Crash پس از نهایی‌سازی).

    فقط از توابع تسویه موجود و نشانگرهای routine_settled / task_settled استفاده می‌شود؛ هر دو idempotent‌اند،
    پس شمارنده، هزینه و گزارش یک‌بار اعمال می‌شود. هیچ اقدام بیرونی Run تکرار نمی‌شود — فقط حسابداری و وضعیت.
    """
    from sqlalchemy import String, cast

    now = utcnow()
    async with session_scope() as session:
        status_text = cast(Run.status, String)
        rows = (await session.execute(select(Run.id).where(
            Run.status.in_(TERMINAL_RUN), Run.parent_run_id.is_(None),
            Run.created_at >= now - timedelta(days=lookback_days),
            or_(Run.finished_at.is_(None), Run.finished_at <= now - timedelta(seconds=min_age_seconds)),
            or_(
                (Run.routine_id.is_not(None)) & (Run.state["routine_settled"].astext.is_distinct_from(status_text)),
                (Run.task_id.is_not(None)) & (Run.state["task_settled"].astext.is_distinct_from(status_text)),
            ),
        ).order_by(Run.created_at).limit(100))).scalars().all()
    n = 0
    for rid in rows:
        try:
            await after_routine_run(rid, telegram)
            await after_task_run(rid, telegram)
            n += 1
        except Exception:  # noqa: BLE001 — یک Run خراب مانع تسویه بقیه نشود؛ دور بعد دوباره تلاش می‌شود
            log.exception("تسویه دوباره Run %s ناموفق بود", rid)
    if n:
        log.info("%d اجرای پایانی تسویه‌نشده تسویه شد", n)
    return n


# ───────────────────────── رویای شبانه ─────────────────────────


async def dream_due() -> bool:
    """Read-only schedule check; any started/failed/finished slot blocks duplicates."""
    if await bus.is_stop_all():
        return False
    async with session_scope() as session:
        cfg = await get_setting(session, "dream", DreamSettings)
        general = await get_setting(session, "general", GeneralSettings)
        if not cfg.enabled:
            return False
        tz = get_zone(general.timezone)
        local = now_utc().astimezone(tz)
        if (local.hour, local.minute) < (cfg.hour, cfg.minute):
            return False
        slot_key = f"scheduled:{local.date().isoformat()}"
        existing = (await session.execute(select(DreamReport.id).where(
            DreamReport.slot_key == slot_key))).first()
        return existing is None


async def claim_scheduled_dream():
    """Commit an atomic one-per-night slot BEFORE any paid API call.

    Unique index protects against concurrent workers and lease TTL expiry.
    A crash leaves the slot as 'running' for diagnostics, NOT eligible for
    unbounded retries on the same night.
    """
    if not await dream_due():
        return None
    async with session_scope() as session:
        cfg = await get_setting(session, "dream", DreamSettings)
        general = await get_setting(session, "general", GeneralSettings)
        if not cfg.enabled or await bus.is_stop_all():
            return None
        local = now_utc().astimezone(get_zone(general.timezone))
        slot_key = f"scheduled:{local.date().isoformat()}"
        result = await session.execute(
            insert(DreamReport).values(
                id=uuid.uuid4(),
                started_at=utcnow(), slot_key=slot_key, trigger="scheduled",
                status="running", events_reviewed=0, tokens=0, cost_usd=0,
                summary="", learnings=[], skills_proposed=[], cleanup={},
            ).on_conflict_do_nothing(index_elements=["slot_key"]).returning(DreamReport.id)
        )
        return result.scalar_one_or_none()
