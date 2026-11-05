"""چرخه حیات مشترک روتین‌ها/مأموریت‌ها — مورد استفاده زمان‌بند، API و ابزار مدل.

یک قرارداد واحد برای ادعای نوبت (اجرای فوری و زمان‌بندی‌شده)، بودجه، پایان و گزارش.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus, inbox
from xagent_core.models import Conversation, Mission, Routine, Run

from .schedule import TERMINAL_STATUSES

ACTIVE = ("queued", "running", "waiting_approval")


async def routine_conversation(session: AsyncSession, r: Routine) -> Conversation:
    conv = (await session.execute(select(Conversation).where(
        Conversation.channel == "routine", Conversation.external_ref == str(r.id)))).scalar_one_or_none()
    if conv is None:
        conv = Conversation(title=f"روتین: {r.name}", channel="routine", external_ref=str(r.id))
        session.add(conv)
        await session.flush()
    return conv


async def active_run(session: AsyncSession, routine_id: uuid.UUID) -> Run | None:
    return (await session.execute(select(Run).where(
        Run.routine_id == routine_id, Run.status.in_(ACTIVE)).order_by(Run.created_at.desc()).limit(1))
    ).scalars().first()


async def mission_block(session: AsyncSession, r: Routine, now) -> tuple[str, str] | None:  # noqa: ANN001
    """(وضعیت نهایی، دلیل) اگر مجوز مأموریت دیگر معتبر نیست."""
    if r.mission_id is None:
        return None
    m = await session.get(Mission, r.mission_id)
    if m is None or m.status == "revoked":
        return "cancelled", "grant_revoked"
    if m.status == "expired" or (m.expires_at is not None and now >= m.expires_at):
        if m.status == "active":
            m.status = "expired"
        return "expired", "grant_expired"
    if m.status == "pending":
        return None  # تا تأیید کارفرما اجرایی ساخته نمی‌شود (در claim بررسی می‌شود)
    return None


def remaining_budget(r: Routine, m: Mission | None) -> Decimal | None:
    caps: list[Decimal] = []
    if r.budget_usd is not None:
        caps.append(Decimal(r.budget_usd) - Decimal(r.spent_usd or 0))
    if m is not None and m.max_cost_usd is not None:
        caps.append(Decimal(m.max_cost_usd) - Decimal(r.spent_usd or 0))
    return min(caps) if caps else None


async def committed_cost(session: AsyncSession, routine_id: uuid.UUID) -> Decimal:
    """سقف هزینه رزروشده اجراهای فعال همین روتین (هنوز در spent_usd نیامده) — برای پذیرش هم‌زمان رویدادها."""
    rows = (await session.execute(select(Run.state).where(Run.routine_id == routine_id,
                                                          Run.status.in_(ACTIVE)))).scalars().all()
    total = Decimal(0)
    for st in rows:
        mc = (st or {}).get("max_cost")
        if mc is not None:
            total += Decimal(str(mc))
    return total


async def admission_block(session: AsyncSession, r: Routine, now) -> tuple[str, str | None] | None:  # noqa: ANN001
    """قرارداد واحد پذیرش نوبت برای همه انواع روتین (زمان‌بندی‌شده، رویدادی، یک‌باره): (دلیل، وضعیت پایانی|None).

    وضعیت پایانی غیر None یعنی روتین باید بسته شود (وقتی اجرای فعالی ندارد). None یعنی فقط این نوبت پذیرفته نمی‌شود.
    STOP_ALL جداگانه در فراخواننده بررسی می‌شود (بدون تغییر وضعیت).
    """
    from .schedule import exhausted_reason

    if r.status != "active" or not r.enabled or r.paused:
        return "inactive", None
    block = await mission_block(session, r, now)
    if block is not None:
        return block[1], block[0]
    reason = exhausted_reason(r, now)
    if reason is not None:
        return reason, "completed"
    if r.starts_at is not None and now < r.starts_at:
        return "not_started", None
    mission = await session.get(Mission, r.mission_id) if r.mission_id else None
    if mission is not None and mission.status != "active":
        return "grant_pending", None
    remaining = remaining_budget(r, mission)
    if remaining is not None and remaining - await committed_cost(session, r.id) <= 0:
        return "budget", None
    if r.circuit_state == "open":
        if r.circuit_open_until is not None and r.circuit_open_until > now:
            return "circuit_open", None
        r.circuit_state = "half_open"  # پایان دوره خنک‌شدن: یک نوبت آزمایشی
    return None


async def claim_occurrence(session: AsyncSession, r: Routine, slot, trigger: str = "routine",  # noqa: ANN001
                           extra_state: dict[str, Any] | None = None, key: str | None = None) -> uuid.UUID | None:
    """ساخت Run برای یک نوبت مشخص — یکتا با dedupe_key؛ نوبت تکراری None برمی‌گرداند.

    زمان‌بند، «اجرای فوری» ابزار/API و رویدادها همه از همین تابع استفاده می‌کنند تا قرارداد شمارش یکی باشد.
    key: کلید پایدار نوبت (رویداد: routine:<id>:event:<event_id>)؛ پیش‌فرض بر اساس زمان نوبت.
    """
    key = key or f"routine:{r.id}:{slot.isoformat()}"
    if (await session.execute(select(Run.id).where(Run.dedupe_key == key))).first():
        return None
    mission = await session.get(Mission, r.mission_id) if r.mission_id else None
    if mission is not None and mission.status != "active":
        return None
    remaining = remaining_budget(r, mission)
    if remaining is not None:
        remaining -= await committed_cost(session, r.id)
    if remaining is not None and remaining <= 0:
        return None
    per_run = [Decimal(str(r.max_cost_usd))] if r.max_cost_usd else []
    if remaining is not None:
        per_run.append(remaining)
    conv = await routine_conversation(session, r)
    state: dict[str, Any] = {
        "trusted": True,
        # روتین دارای مجوز مأموریت با هویت «mission» اجرا می‌شود: فقط ابزارهای محدوده مجوز، بدون ابزار مدیریتی
        "principal": "mission" if mission is not None else "owner",
        "requested_by": f"routine:{r.id}",
        "routine_slot": slot.isoformat(),
        "max_cost": float(min(per_run)) if per_run else None,
        "max_steps": r.max_steps,
    }
    if mission is not None:
        state["mission_id"] = str(mission.id)
    if extra_state:
        state.update(extra_state)
    run = Run(conversation_id=conv.id, routine_id=r.id, trigger=trigger, input=r.instruction, state=state,
              dedupe_key=key)
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError:
        return None  # Scheduler دیگری همین نوبت را هم‌زمان ادعا کرد
    r.run_count = (r.run_count or 0) + 1
    r.last_run_at = slot
    return run.id


async def pause_for_budget(session: AsyncSession, r: Routine, telegram: Any = None) -> None:
    r.status, r.paused = "paused", True
    r.last_error = f"سقف بودجه روتین ({float(r.budget_usd or 0):.2f}$) مصرف شد؛ برای ادامه بودجه را افزایش دهید."
    await inbox.post(session, kind="error", severity="critical", title=f"روتین «{r.name}» به سقف بودجه رسید",
                     body=r.last_error, routine_id=r.id, mission_id=r.mission_id, link="/routines",
                     dedupe_key=f"routine:{r.id}:budget:{int(Decimal(r.spent_usd or 0) * 10000)}", telegram=telegram)


async def finalize_routine(session: AsyncSession, r: Routine, now, reason: str, status: str | None = None,  # noqa: ANN001
                           telegram: Any = None) -> None:
    """پایان نهایی مأموریت: خروج از زمان‌بندی، ثبت دلیل و گزارش خلاصه (یک‌بار)."""
    if r.status in TERMINAL_STATUSES:
        return
    if status is None:
        status = "completed"
        if (r.success_count or 0) + (r.no_change_count or 0) == 0 and (r.failure_count or 0) > 0:
            status = "failed"
    r.status = status
    r.enabled = False
    r.paused = False
    r.next_run_at = None
    r.completed_at = now
    r.completion_reason = reason
    if r.mission_id:
        m = await session.get(Mission, r.mission_id)
        if m is not None and m.status == "active" and reason in ("end_at", "max_runs", "once"):
            others = (await session.execute(select(Routine.id).where(
                Routine.mission_id == m.id, Routine.id != r.id,
                Routine.status.notin_(list(TERMINAL_STATUSES))))).first()
            if others is None:
                m.status = "completed"
    labels = {"end_at": "پایان مهلت", "max_runs": "رسیدن به سقف دفعات", "once": "اجرای یک‌باره انجام شد",
              "grant_expired": "انقضای مجوز مأموریت", "grant_revoked": "لغو مجوز مأموریت",
              "owner_cancelled": "لغو توسط کارفرما"}
    body = (f"دلیل: {labels.get(reason, reason)}\n"
            f"اجرا: {r.run_count} نوبت — موفق {r.success_count}، بدون تغییر {r.no_change_count}، "
            f"ناموفق {r.failure_count}، ردشده {r.skipped_count}\n"
            f"هزینه کل: {float(r.spent_usd or 0):.4f}$")
    sev = "important" if status == "completed" else "critical"
    await inbox.post(session, kind="mission", severity=sev,
                     title=f"مأموریت «{r.name}» {'به پایان رسید' if status == 'completed' else 'بسته شد'}",
                     body=body, routine_id=r.id, mission_id=r.mission_id, link=f"/routines?id={r.id}",
                     dedupe_key=f"routine:{r.id}:end", data={"status": status, "reason": reason},
                     telegram=telegram)
    await bus.publish("routine.changed", {"id": str(r.id), "action": "finished", "status": status})
