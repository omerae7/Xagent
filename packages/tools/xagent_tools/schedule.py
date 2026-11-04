"""زمان‌بندی و چرخه حیات روتین‌ها (cron به وقت محلی، بازه‌ای، یک‌باره، رویدادمحور).

قرارداد شمارش (صریح و آزموده‌شده):
- پنجره اجرا نیمه‌باز است: [starts_at, end_at). نوبتی که دقیقاً روی end_at بیفتد اجرا نمی‌شود.
- نوبت‌های بازه‌ای روی شبکه ثابت starts_at + k×interval هستند؛ تأخیر Worker شبکه را جابه‌جا نمی‌کند.
- max_runs تعداد نوبت‌هایی است که واقعاً Run ساخته‌اند، شامل اجرای فوری اولیه.
  نوبتی که به‌خاطر اجرای فعال قبلی یا خاموشی Worker از دست رفته جزو max_runs نیست
  (در skipped_count ثبت می‌شود) و بعداً جبران هم نمی‌شود (ادغام نوبت‌های عقب‌افتاده).
- مثال «از الان تا ۱۰ دقیقه، هر دقیقه»: نوبت‌های t0 … t0+9 → ۱۰ بررسی (شامل بررسی فوری).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from croniter import croniter

from xagent_core.models import Routine
from xagent_core.timeutil import get_zone, now_utc

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "expired"}
SCHEDULABLE_STATUSES = {"active"}
MIN_INTERVAL_SECONDS = 60


def validate_routine(r: Routine) -> str | None:
    if r.trigger_type == "cron":
        if not r.cron_expr or not croniter.is_valid(r.cron_expr):
            return "عبارت cron معتبر نیست"
    elif r.trigger_type == "interval":
        if not r.interval_seconds or r.interval_seconds < MIN_INTERVAL_SECONDS:
            return "فاصله اجرا باید حداقل ۶۰ ثانیه باشد"
    elif r.trigger_type == "once":
        if not r.run_at:
            return "زمان اجرا مشخص نشده است"
    elif r.trigger_type == "event":
        if not (r.event_filter or {}).get("source"):
            return "منبع رویداد (source) مشخص نشده است"
    else:
        return "نوع راه‌انداز نامعتبر است"
    if r.max_runs is not None and r.max_runs < 1:
        return "سقف دفعات اجرا باید حداقل ۱ باشد"
    if r.end_at is not None and r.starts_at is not None and r.end_at <= r.starts_at:
        return "زمان پایان باید بعد از زمان شروع باشد"
    return None


def apply_window(r: Routine, *, start: datetime | None = None, duration_minutes: float | None = None,
                 end_at: datetime | None = None, max_runs: int | None = None) -> None:
    """ثبت پنجره زمانی مأموریت و قرارداد شمارش (مشترک بین API، ابزار مدل و تست‌ها)."""
    r.starts_at = start or r.starts_at or now_utc()
    if duration_minutes:
        r.end_at = r.starts_at + timedelta(minutes=float(duration_minutes))
    elif end_at is not None:
        r.end_at = end_at
    if max_runs is not None:
        r.max_runs = int(max_runs)
    elif r.trigger_type == "interval" and r.interval_seconds and r.end_at and r.max_runs is None:
        # تعداد نوبت‌های شبکه در بازه نیمه‌باز [start, end)
        span = (r.end_at - r.starts_at).total_seconds()
        r.max_runs = max(1, math.ceil(span / r.interval_seconds))


def _cron_next(r: Routine, base: datetime) -> datetime:
    tz = get_zone(r.timezone)
    nxt = croniter(r.cron_expr, base.astimezone(tz)).get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(UTC)


def _anchor(r: Routine, base: datetime) -> datetime:
    return r.starts_at or r.created_at or base


def slot_at_or_before(r: Routine, now: datetime) -> datetime | None:
    """آخرین نوبت شبکه که موعدش رسیده (برای ادغام نوبت‌های عقب‌افتاده پس از خاموشی)."""
    if r.trigger_type == "interval" and r.interval_seconds:
        anchor = _anchor(r, now)
        if now < anchor:
            return None
        k = math.floor((now - anchor).total_seconds() / r.interval_seconds)
        return anchor + timedelta(seconds=k * r.interval_seconds)
    return r.next_run_at


def _raw_next(r: Routine, base: datetime) -> datetime | None:
    if r.trigger_type == "cron" and r.cron_expr:
        start = r.starts_at
        if start is not None and base < start:
            base = start - timedelta(seconds=1)
        return _cron_next(r, base)
    if r.trigger_type == "interval" and r.interval_seconds:
        anchor = _anchor(r, base)
        if base < anchor:
            return anchor
        k = math.floor((base - anchor).total_seconds() / r.interval_seconds) + 1
        return anchor + timedelta(seconds=k * r.interval_seconds)
    if r.trigger_type == "once":
        if r.run_at and r.run_count == 0:
            return r.run_at
        return None
    return None


def compute_next_run(r: Routine, after: datetime | None = None, *, first: bool = False) -> datetime | None:
    """موعد بعدی با رعایت وضعیت، پنجره زمانی و سقف دفعات. None یعنی نوبت دیگری وجود ندارد.

    first=True برای روتین تازه: اگر شروع همین حالا یا در آینده است، خود starts_at اولین نوبت است.
    """
    if r.status in TERMINAL_STATUSES:
        return None
    if r.max_runs is not None and (r.run_count or 0) >= r.max_runs:
        return None
    base = after or now_utc()
    if first and r.trigger_type == "interval" and r.starts_at is not None and r.starts_at >= base - timedelta(seconds=1):
        nxt: datetime | None = r.starts_at
    else:
        nxt = _raw_next(r, base)
    if nxt is None:
        return None
    if r.end_at is not None and nxt >= r.end_at:
        return None
    return nxt


def exhausted_reason(r: Routine, now: datetime) -> str | None:
    """آیا مأموریت به پایان طبیعی رسیده است؟ (فقط وقتی اجرای فعالی باقی نمانده بررسی شود)"""
    if r.max_runs is not None and (r.run_count or 0) >= r.max_runs:
        return "max_runs"
    if r.end_at is not None and now >= r.end_at:
        return "end_at"
    if r.trigger_type == "once" and (r.run_count or 0) >= 1:
        return "once"
    if r.trigger_type in ("interval", "cron") and compute_next_run(r, now) is None and r.end_at is not None:
        return "end_at"
    return None


def display_state(r: Routine, active_run_status: str | None, now: datetime | None = None) -> str:
    """وضعیت نمایشی یکسان برای API/UI: running | queued | waiting_approval | waiting | circuit_open | <status>."""
    if r.status != "active":
        return r.status
    if active_run_status in ("running", "queued", "waiting_approval"):
        return active_run_status
    now = now or now_utc()
    if r.circuit_state == "open" and r.circuit_open_until and r.circuit_open_until > now:
        return "circuit_open"
    if r.trigger_type == "event":
        return "listening"
    return "waiting"
