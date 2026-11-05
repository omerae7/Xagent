"""نگاشت پارامترهای خاص هر مدل و تبدیل واحد قیمت ارائه‌دهندگان.

اصل: هیچ پارامتری حدسی ارسال نمی‌شود. فقط وقتی ویژگی مدل از API ارائه‌دهنده، آزمون عملی یا تنظیم صریح
کارفرما ثبت شده باشد (در capability_meta)، پارامتر متناظر به درخواست افزوده می‌شود.

- xAI: ‎/v1/models برای هر مدل capabilities.reasoning_effort (مقادیر مجاز) را برمی‌گرداند؛ فقط همان مقادیر
  به‌صورت reasoning_effort ارسال می‌شوند (مدل‌هایی که آن را نمی‌پذیرند چیزی دریافت نمی‌کنند).
- Kimi (Open Platform / Kimi Code): پارامتر thinking = {"type": "enabled"|"disabled"} فقط برای مدل‌هایی که
  کنترل thinking آن‌ها «toggle» ثبت شده است. مدل‌های همیشه-استدلالی (always) پارامتری دریافت نمی‌کنند.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

# واحد قیمت xAI در ‎/v1/models و ‎/v1/language-models: «سنت دلار به ازای هر ۱۰۰ میلیون توکن».
# دلار به ازای هر ۱ میلیون توکن = مقدار ÷ ۱۰۰ (سنت→دلار) ÷ ۱۰۰ (۱۰۰M→1M) = مقدار ÷ ۱۰٬۰۰۰
XAI_PRICE_UNIT = "usd_cents_per_100m_tokens"
_XAI_DIVISOR = Decimal(10_000)


def xai_price_per_m(value: Any) -> Decimal | None:
    """تبدیل قیمت xAI به دلار/میلیون توکن. مقدار نامعتبر، منفی یا صفر → None (نامعلوم، نه رایگان)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d / _XAI_DIVISOR


def request_params(kind: str, model_name: str, meta: dict[str, Any] | None, effort: str | None) -> dict[str, Any]:
    meta = meta or {}
    out: dict[str, Any] = {}
    if kind == "xai":
        allowed = (meta.get("reasoning") or {}).get("efforts") or []
        if effort and effort in allowed:
            out["reasoning_effort"] = effort
    elif kind in ("moonshot", "kimi_code"):
        th = meta.get("thinking") or {}
        if th.get("value") and th.get("control") == "toggle":
            out["thinking"] = {"type": "enabled" if effort in ("medium", "high") else "disabled"}
    return out
