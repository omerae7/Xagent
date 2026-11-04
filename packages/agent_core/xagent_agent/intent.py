"""تشخیص قطعی مأموریت زمان‌دار از زبان طبیعی فارسی/انگلیسی (بدون LLM).

نمونه‌ها:
- «تا ۱۰ دقیقه آینده، هر یک دقیقه یک بار تلگرام را بررسی کن» → interval=60s، duration=10m، immediate
- «دقیقه‌ای یک بار پیام‌ها را چک کن»، «ساعتی یک‌بار»، «هر نیم ساعت»، «دو بار در ساعت»
- «هر روز ساعت ۹ صبح گزارش بده» → cron «0 9 * * *»
- «for the next 30 minutes check my telegram every 2 minutes»

خروجی برای انتخاب ابزار (گروه routines + سرویس‌ها) و پیش‌فرض‌های قطعی routine_create استفاده می‌شود؛
مدل می‌تواند صریحاً مقادیر دیگری بدهد ولی اگر فراموش کند، قرارداد کارفرما از دست نمی‌رود.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from xagent_core.text import normalize_fa

_NUM_WORDS = {
    "یک": 1, "یه": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "شیش": 6, "هفت": 7, "هشت": 8, "نه": 9,
    "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15, "پونزده": 15, "شانزده": 16,
    "هفده": 17, "هجده": 18, "هیجده": 18, "نوزده": 19, "بیست": 20, "بیست و پنج": 25, "سی": 30, "چهل": 40,
    "چهل و پنج": 45, "پنجاه": 50, "شصت": 60, "نود": 90, "صد": 100, "نیم": 0.5, "ربع": 0.25,
    "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "half an": 0.5, "half a": 0.5,
}
_UNITS = {
    "ثانیه": 1, "دقیقه": 60, "ساعت": 3600, "روز": 86400, "هفته": 604800,
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "day": 86400, "days": 86400, "week": 604800,
    "weeks": 604800,
}
_ADJ_UNITS = {"دقیقه ای": 60, "ساعتی": 3600, "روزی": 86400, "هفته ای": 604800, "ثانیه ای": 1}
_WORDS_RE = "|".join(sorted((re.escape(w) for w in _NUM_WORDS), key=len, reverse=True))
NUM = rf"(\d+(?:\.\d+)?|{_WORDS_RE})"
UNIT = r"(ثانیه|دقیقه|ساعت|روز|هفته|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"

_INTERVAL_PATTERNS = [
    # «هر ۵ دقیقه»، «هر یک دقیقه»، «هر دقیقه»، «هر نیم ساعت»
    re.compile(rf"(?:^|\s)هر\s+(?:{NUM}\s+)?{UNIT}(?![a-z])"),
    # every 2 minutes / every minute
    re.compile(rf"\bevery\s+(?:{NUM}\s+)?{UNIT}\b"),
]
_ADJ_PATTERN = re.compile(r"(دقیقه ای|ساعتی|روزی|هفته ای|ثانیه ای)\s+(?:یک|یه|1)\s*(?:بار|دفعه)")
_TIMES_PER = re.compile(rf"{NUM}\s*(?:بار|دفعه)\s+در\s+{UNIT}")
_TIMES_PER_EN = re.compile(rf"\b{NUM}\s+times?\s+(?:a|an|per)\s+{UNIT}\b")
_DURATION_PATTERNS = [
    re.compile(rf"(?:تا|برای|به مدت|طی|در)\s+{NUM}\s+{UNIT}\s*(?:آینده|بعد|دیگه|دیگر|آتی)"),
    re.compile(rf"(?:برای|به مدت|طی)\s+{NUM}\s+{UNIT}"),
    re.compile(rf"\b(?:for|during|over)\s+(?:the\s+next\s+)?{NUM}\s+{UNIT}\b"),
]
_COUNT_PATTERN = re.compile(rf"(?:^|\s){NUM}\s*(?:بار|دفعه)(?!\s+در)")
_DAILY_AT = re.compile(r"هر\s+روز\s+(?:صبح\s+|عصر\s+|شب\s+)?ساعت\s+(\d{1,2})(?::(\d{2}))?\s*(صبح|بعد از ظهر|عصر|شب)?")
_DAILY_AT_EN = re.compile(r"\bevery\s+day\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b")
_IMMEDIATE = re.compile(r"(از\s+(?:همین\s+)?(?:الان|حالا)|همین\s+(?:الان|حالا)|\bstarting now\b|\bfrom now\b|\bright now\b)")
_CONDITIONAL = re.compile(r"(اگر|اگه|در صورتی که|در صورت|\bif\b|\bwhenever\b)")
_REPORT = re.compile(r"(گزارش|خبرم کن|خبر بده|اطلاع بده|بهم بگو|به من بگو|\breport\b|\bnotify\b|let me know|tell me)")
_REPLY = re.compile(r"(پاسخ بده|جواب بده|جواب بدی|پاسخ بدی|جوابش|پاسخش|\breply\b|\brespond\b|\banswer\b)")
_SERVICES = {
    "telegram": re.compile(r"(تلگرام|telegram|پیام ?ها|پیامها|پیام جدید|\bmessages?\b|\bdms?\b)"),
    "web": re.compile(r"(سایت|صفحه|وب|قیمت|نرخ|https?://|\bprice\b|\bwebsite\b|\bpage\b)"),
    "tasks": re.compile(r"(تسک|وظیفه|\btasks?\b)"),
}


@dataclass
class MissionIntent:
    recurring: bool = False
    interval_seconds: int | None = None
    cron: str | None = None
    duration_minutes: float | None = None
    max_runs: int | None = None
    immediate: bool = False
    conditional: bool = False
    wants_report: bool = False
    wants_reply: bool = False
    services: list[str] = field(default_factory=list)
    watch_source: str | None = None
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        parts = []
        if self.interval_seconds:
            parts.append(f"هر {_human(self.interval_seconds)}")
        if self.cron:
            parts.append(f"زمان‌بندی cron «{self.cron}»")
        if self.duration_minutes:
            parts.append(f"به مدت {_human(int(self.duration_minutes * 60))}")
        if self.max_runs:
            parts.append(f"حداکثر {self.max_runs} نوبت (شامل بررسی فوری)")
        if self.immediate:
            parts.append("شروع فوری")
        if self.conditional:
            parts.append("اقدام مشروط به رویداد")
        if self.wants_reply:
            parts.append("پاسخ‌گویی در محدوده مجوز")
        if self.wants_report:
            parts.append("گزارش موارد مهم")
        return "، ".join(parts)


def _human(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400} روز"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} ساعت"
    if seconds % 60 == 0:
        return f"{seconds // 60} دقیقه"
    return f"{seconds} ثانیه"


def _num(v: str | None) -> float:
    if not v:
        return 1.0
    v = v.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", v):
        return float(v)
    return float(_NUM_WORDS.get(v, 1))


def _prepare(text: str) -> str:
    t = normalize_fa(text)
    t = re.sub(r"(\d)\s*(?=[^\d\s.:])", r"\1 ", t)  # «۵دقیقه» → «5 دقیقه»
    t = re.sub(r"[،,؛;]", " ", t)
    t = re.sub(r"\bnext\s+(\d)", r"next \1", t)
    return re.sub(r"\s+", " ", t)


def parse_intent(text: str) -> MissionIntent:
    t = _prepare(text)
    it = MissionIntent()
    consumed: list[tuple[int, int]] = []

    def take(m: re.Match) -> None:
        consumed.append(m.span())
        it.matched.append(m.group(0).strip())

    m = _DAILY_AT.search(t) or _DAILY_AT_EN.search(t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        suffix = (m.group(3) or "").strip()
        if suffix in ("بعد از ظهر", "عصر", "شب", "pm") and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            it.cron = f"{minute} {hour} * * *"
            it.recurring = True
            take(m)
    if not it.cron:
        for pat in _INTERVAL_PATTERNS:
            m = pat.search(t)
            if m:
                it.interval_seconds = int(_num(m.group(1)) * _UNITS[m.group(2)])
                take(m)
                break
        if it.interval_seconds is None:
            m = _ADJ_PATTERN.search(t)
            if m:
                it.interval_seconds = _ADJ_UNITS[m.group(1)]
                take(m)
        if it.interval_seconds is None:
            m = _TIMES_PER.search(t) or _TIMES_PER_EN.search(t)
            if m:
                n = max(1.0, _num(m.group(1)))
                it.interval_seconds = int(_UNITS[m.group(2)] / n)
                take(m)
        if it.interval_seconds:
            it.recurring = True
    for pat in _DURATION_PATTERNS:
        m = pat.search(t)
        if m and not any(a <= m.start() < b for a, b in consumed):
            secs = _num(m.group(1)) * _UNITS[m.group(2)]
            if secs >= 60:
                it.duration_minutes = round(secs / 60, 2)
                take(m)
                break
    for m in _COUNT_PATTERN.finditer(t):
        if any(a <= m.start() < b for a, b in consumed):
            continue
        n = int(_num(m.group(1)))
        if it.recurring and 1 < n <= 10_000:
            it.max_runs = n
            take(m)
            break
    if it.recurring and it.interval_seconds:
        it.interval_seconds = max(60, it.interval_seconds)  # کف زمان‌بند: ۶۰ ثانیه
        if it.duration_minutes and not it.max_runs:
            import math

            it.max_runs = max(1, math.ceil(it.duration_minutes * 60 / it.interval_seconds))
    it.immediate = bool(_IMMEDIATE.search(t)) or (it.recurring and it.interval_seconds is not None)
    it.conditional = bool(_CONDITIONAL.search(t))
    it.wants_report = bool(_REPORT.search(t))
    it.wants_reply = bool(_REPLY.search(t))
    it.services = [name for name, pat in _SERVICES.items() if pat.search(t)]
    if it.recurring and "telegram" in it.services and re.search(r"(تلگرام|telegram|پیام|message)", t):
        it.watch_source = "telegram"
    return it
