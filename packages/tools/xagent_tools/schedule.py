"""محاسبه زمان اجرای بعدی روتین‌ها (cron به وقت محلی، بازه‌ای، یک‌باره، رویدادمحور)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from croniter import croniter

from xagent_core.models import Routine
from xagent_core.timeutil import get_zone, now_utc


def validate_routine(r: Routine) -> str | None:
    if r.trigger_type == "cron":
        if not r.cron_expr or not croniter.is_valid(r.cron_expr):
            return "عبارت cron معتبر نیست"
    elif r.trigger_type == "interval":
        if not r.interval_seconds or r.interval_seconds < 60:
            return "فاصله اجرا باید حداقل ۶۰ ثانیه باشد"
    elif r.trigger_type == "once":
        if not r.run_at:
            return "زمان اجرا مشخص نشده است"
    elif r.trigger_type == "event":
        if not (r.event_filter or {}).get("source"):
            return "منبع رویداد (source) مشخص نشده است"
    else:
        return "نوع راه‌انداز نامعتبر است"
    return None


def compute_next_run(r: Routine, after: datetime | None = None) -> datetime | None:
    base = after or now_utc()
    if r.trigger_type == "cron" and r.cron_expr:
        tz = get_zone(r.timezone)
        local = base.astimezone(tz)
        nxt = croniter(r.cron_expr, local).get_next(datetime)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=tz)
        return nxt.astimezone(UTC)
    if r.trigger_type == "interval" and r.interval_seconds:
        last = r.last_run_at or base
        nxt = last + timedelta(seconds=r.interval_seconds)
        return nxt if nxt > base else base + timedelta(seconds=5)
    if r.trigger_type == "once":
        if r.run_at and (r.last_run_at is None):
            return r.run_at
        return None
    return None
