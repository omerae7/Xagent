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

# ── پاک‌سازی ساختاری (JSON): کلیدهای سری، نقشه‌های env/headers و مقادیر مهروموم‌شده ──
SECRET_KEY = re.compile(r"pass(word|wd|phrase)?$|^pass|secret|(^|[_-])token$|^token|access[_-]?token|refresh[_-]?token|"
                        r"api[_-]?(key|hash)|apikey|authori[sz]ation|cookie|credential|(private|signing|encryption)[_-]?key|"
                        r"bearer|session[_-]?(string|key|token)|(^|[_-])(otp|pin|cvv)$|^auth$", re.I)
OPAQUE_MAPS = frozenset({"env", "environment", "headers", "secrets"})  # همه مقادیر این نقشه‌ها سری فرض می‌شوند
SEALED = "$enc"  # {"$enc": "<fernet>"}: مقدار سری رمزشده در Snapshot ذخیره‌شده


def _masked(v: object) -> object:
    return MASK if isinstance(v, str) and v else v  # اسرار رشته‌اند؛ اعداد (مثلاً شمار توکن) دست‌نخورده می‌مانند


def redact_obj(value: object, key: str | None = None, opaque: bool = False) -> object:
    """کپی امن یک ساختار JSON برای خروجی API/رویداد: مقدار کلیدهای سری، همه مقادیر env/headers و
    مقادیر مهروموم‌شده پنهان می‌شوند؛ ساختار و نام کلیدها برای نمایش و تصمیم‌گیری می‌ماند."""
    if isinstance(value, dict):
        if set(value) == {SEALED}:
            return MASK
        inner = opaque or (key is not None and key.lower() in OPAQUE_MAPS)
        out = {}
        for k, v in value.items():
            if isinstance(v, dict | list):
                out[k] = redact_obj(v, str(k), opaque=inner)
            elif inner or SECRET_KEY.search(str(k)):
                out[k] = _masked(v)
            else:
                out[k] = redact_obj(v, str(k))
        return out
    if isinstance(value, list):
        return [redact_obj(v, key, opaque) for v in value]
    if isinstance(value, str):
        return _masked(value) if opaque else scrub(value)
    return value


def seal_secrets(value: object, key: str | None = None, opaque: bool = False) -> object:
    """رمزنگاری مقادیر سری داخل یک ساختار پیش از ذخیره در Snapshot (برگشت‌پذیر با open_secrets برای Undo)."""
    from .security import encrypt_secret

    if isinstance(value, dict):
        if set(value) == {SEALED}:
            return value
        inner = opaque or (key is not None and key.lower() in OPAQUE_MAPS)
        out = {}
        for k, v in value.items():
            if isinstance(v, dict | list):
                out[k] = seal_secrets(v, str(k), opaque=inner)
            elif isinstance(v, str) and v and (inner or SECRET_KEY.search(str(k))):
                out[k] = {SEALED: encrypt_secret(v).decode()}
            else:
                out[k] = v
        return out
    if isinstance(value, list):
        return [seal_secrets(v, key, opaque) for v in value]
    return value


def open_secrets(value: object) -> object:
    from .security import decrypt_secret

    if isinstance(value, dict):
        if set(value) == {SEALED}:
            return decrypt_secret(str(value[SEALED]).encode())
        return {k: open_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [open_secrets(v) for v in value]
    return value


def secret_paths(value: object, path: str = "$", key: str | None = None, opaque: bool = False):
    """مسیر مقادیر سری متن‌ساده (نه مقدار) برای فهرست‌برداری فقط‌خواندنی سوابق تاریخی."""
    if isinstance(value, dict):
        if set(value) == {SEALED}:
            return
        inner = opaque or (key is not None and key.lower() in OPAQUE_MAPS)
        for k, v in value.items():
            p = f"{path}.{k}"
            if isinstance(v, dict | list):
                yield from secret_paths(v, p, str(k), inner)
            elif isinstance(v, str) and v:
                if inner:
                    yield p, "opaque_map"
                elif SECRET_KEY.search(str(k)):
                    yield p, "secret_key"
                elif scrub(v) != v:
                    yield p, "pattern"
    elif isinstance(value, list):
        for n, v in enumerate(value):
            yield from secret_paths(v, f"{path}[{n}]", key, opaque)
    elif isinstance(value, str) and value and scrub(value) != value:
        yield path, "pattern"


def register_secret(value: str | None, min_len: int = 8) -> None:
    """ثبت یک مقدار سری تا هر جا در متن ظاهر شد پنهان شود (فقط مقادیر به‌اندازه کافی بلند)."""
    if value and len(value) >= max(min_len, 6):
        with _lock:
            _known.add(value)


def scrub_known(text: str) -> str:
    """فقط مقادیر سری ثبت‌شده (کلیدها، رمزهای تایپ‌شده) — بدون الگوهای عمومی که متن عادی را تغییر دهند."""
    if not text or not _known:
        return text
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for v in known:
        if v in text:
            text = text.replace(v, MASK)
    return text


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
