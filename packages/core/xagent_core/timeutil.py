"""زمان و تقویم: منطقه زمانی قابل تنظیم و تبدیل شمسی/میلادی."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime

from .text import fa_digits

FA_WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def get_zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "Asia/Tehran")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Asia/Tehran")


# ساعت مجازی فقط برای تست‌های قطعی زمان‌بند (Virtual Clock)؛ در اجرای عادی همیشه None است.
_FAKE_NOW: datetime | None = None


def now_utc() -> datetime:
    if _FAKE_NOW is not None:
        return _FAKE_NOW
    return datetime.now(UTC)


def set_fake_now(value: datetime | None) -> None:
    """تنظیم ساعت مجازی (فقط تست). مقدار None به ساعت واقعی برمی‌گردد."""
    global _FAKE_NOW
    if value is not None and value.tzinfo is None:
        raise ValueError("ساعت مجازی باید آگاه از منطقه زمانی باشد")
    _FAKE_NOW = value


def to_local(dt: datetime, tz: str | None) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(get_zone(tz))


def jalali_str(dt: datetime, tz: str | None = None, with_time: bool = True, persian_digits: bool = True) -> str:
    local = to_local(dt, tz)
    j = jdatetime.datetime.fromgregorian(datetime=local.replace(tzinfo=None))
    s = f"{j.year:04d}/{j.month:02d}/{j.day:02d}"
    if with_time:
        s += f" {local.hour:02d}:{local.minute:02d}"
    return fa_digits(s) if persian_digits else s


def describe_now(tz: str | None) -> dict[str, str]:
    """توصیف کامل زمان فعلی برای قرار دادن در کانتکست مدل (بدون مصرف ابزار)."""
    now = to_local(now_utc(), tz)
    j = jdatetime.datetime.fromgregorian(datetime=now.replace(tzinfo=None))
    return {
        "timezone": str(now.tzinfo),
        "gregorian": now.strftime("%Y-%m-%d %H:%M"),
        "jalali": f"{j.year:04d}/{j.month:02d}/{j.day:02d} {now.hour:02d}:{now.minute:02d}",
        "weekday_fa": FA_WEEKDAYS[now.weekday()],
        "iso": now.isoformat(),
    }


def parse_jalali_date(value: str, tz: str | None = None) -> datetime:
    """'1404/08/12' یا '1404/08/12 14:30' → datetime آگاه از منطقه زمانی (UTC)."""
    value = value.strip().replace("-", "/")
    date_part, _, time_part = value.partition(" ")
    y, m, d = (int(x) for x in date_part.split("/"))
    hh, mm = 0, 0
    if time_part:
        hh, mm = (int(x) for x in time_part.split(":")[:2])
    g = jdatetime.datetime(y, m, d, hh, mm).togregorian()
    return g.replace(tzinfo=get_zone(tz)).astimezone(UTC)
