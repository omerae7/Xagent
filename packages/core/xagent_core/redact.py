"""پاک‌سازی اسرار از متن خطا، لاگ و پاسخ API.

دو لایه:
۱. الگوهای شناخته‌شده کلید ارائه‌دهندگان (OpenAI/Anthropic/Google/xAI/Groq/OpenRouter/تلگرام، Bearer، key= در URL، رمز در URL)
۲. مقدار دقیق اسرار شناخته‌شده سیستم (کلیدهای رمزگشایی‌شده ارائه‌دهندگان، اسرار env) که در حافظه ثبت می‌شوند؛
   حتی اگر یک ارائه‌دهنده سفارشی کلید را با قالب ناشناخته در پیام خطا تکرار کند، پنهان می‌شود.
"""

from __future__ import annotations

import logging
import re
import threading

MASK = "••••"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(sk-(?:ant-|proj-|or-v1-|svcacct-)?[A-Za-z0-9_\-]{8,})"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), MASK),
    (re.compile(r"\b(?:xai|gsk|pplx|nvapi|hf)[-_][A-Za-z0-9_\-]{12,}"), MASK),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"), MASK),  # توکن ربات تلگرام
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + MASK),
    (re.compile(r"(?i)([?&](?:key|api_key|apikey|access_token|token|secret)=)[^&\s\"'#]+"), r"\1" + MASK),
    (re.compile(r"(?i)((?:api[_-]?key|x-api-key|x-goog-api-key|authorization|password|secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{6,}"), r"\1" + MASK),
    (re.compile(r"([a-z][a-z0-9+.\-]*://[^:/\s@]+:)[^@\s/]+@"), r"\1" + MASK + "@"),  # رمز داخل URL
]

_known: set[str] = set()
_lock = threading.Lock()


def register_secret(value: str | None) -> None:
    """ثبت یک مقدار سری تا هر جا در متن ظاهر شد پنهان شود (فقط مقادیر به‌اندازه کافی بلند)."""
    if value and len(value) >= 8:
        with _lock:
            _known.add(value)


def scrub(text: object) -> str:
    s = text if isinstance(text, str) else str(text)
    if not s:
        return s
    if _known:
        with _lock:
            known = sorted(_known, key=len, reverse=True)
        for v in known:
            if v in s:
                s = s.replace(v, MASK)
    for rx, rep in _PATTERNS:
        s = rx.sub(rep, s)
    return s


class SecretScrubFilter(logging.Filter):
    """فیلتر لاگ: پیام نهایی هر رکورد پیش از نوشتن پاک‌سازی می‌شود."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = scrub(msg)
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True


def install_log_scrubber() -> None:
    """افزودن فیلتر به همه Handlerهای ریشه (فیلتر Logger روی رکوردهای فرزندان اعمال نمی‌شود)."""
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, SecretScrubFilter) for f in h.filters):
            h.addFilter(SecretScrubFilter())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            if not any(isinstance(f, SecretScrubFilter) for f in h.filters):
                h.addFilter(SecretScrubFilter())
