"""سیاست مجوز تغییرات مدیر.

اصل: مدیر صفر تا صد تنظیمات عملیاتی را خودش تغییر می‌دهد؛ فقط «امنیت» با کارفرماست.
- مجاز خودکار: مدل‌ها، مسیریابی، کارمندان، ابزارهای نصب‌شده (روشن/خاموش)، تنظیمات پایه، رویا، سخت‌تر کردن محدودیت‌ها
- نیازمند اجازه کارفرما (کارت اجازه): شل‌کردن سقف هزینه/توکن/گام/زمان و مدارقطع‌کن
- ممنوع برای مدیر: قوانین Harness، احراز هویت، کلیدها و اسرار، تلگرام، به‌روزرسانی هسته، پرداخت، افزایش مجوز خود
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Decision = Literal["allow", "approval", "forbidden"]

# بخش‌هایی از تنظیمات که فقط کارفرما تغییر می‌دهد
OWNER_ONLY_SETTINGS = {"telegram", "telegram_secret", "harness", "auth", "security"}
# فیلدهای حساس ارائه‌دهنده (اسرار/مقصد ترافیک)
OWNER_ONLY_PROVIDER_FIELDS = {"api_key", "api_key_enc", "base_url", "kind"}
# فیلدهای حساس ابزار
OWNER_ONLY_TOOL_FIELDS = {"harness_category", "config", "source_url"}

# در این بخش‌ها «بزرگ‌تر شدن» یعنی شل‌شدن محدودیت
LOOSER_IF_GREATER = {
    "stoploss": {"max_cost_per_task_usd", "max_tokens_per_task", "daily_cost_usd", "monthly_cost_usd",
                 "max_steps_simple", "max_steps_complex", "max_runtime_seconds", "max_repeat_same_action",
                 "max_consecutive_errors"},
    "routine_safety": {"circuit_failure_threshold", "lease_ttl_seconds"},
    "dream": {"max_cost_usd"},
}
LOOSER_IF_SMALLER = {"routine_safety": {"circuit_cooldown_minutes"}}


@dataclass
class PolicyResult:
    decision: Decision
    reason: str
    loosened: list[str]


def check_setting(key: str, before: dict[str, Any], after: dict[str, Any], actor: str) -> PolicyResult:
    if actor == "owner":
        return PolicyResult("allow", "تغییر توسط کارفرما", [])
    if key in OWNER_ONLY_SETTINGS:
        return PolicyResult("forbidden", f"بخش «{key}» تنظیم امنیتی است و فقط کارفرما می‌تواند آن را تغییر دهد.", [])
    loosened: list[str] = []
    for f in LOOSER_IF_GREATER.get(key, set()):
        b, a = before.get(f), after.get(f)
        if isinstance(a, int | float) and isinstance(b, int | float) and a > b:
            loosened.append(f)
    for f in LOOSER_IF_SMALLER.get(key, set()):
        b, a = before.get(f), after.get(f)
        if isinstance(a, int | float) and isinstance(b, int | float) and a < b:
            loosened.append(f)
    if key == "dream" and before.get("enabled") is True and after.get("enabled") is False:
        pass  # خاموش‌کردن رویا امنیتی نیست
    if loosened:
        return PolicyResult("approval", "شل‌شدن محدودیت‌های هزینه/ایمنی نیازمند اجازه کارفرماست: " + "، ".join(loosened),
                            loosened)
    return PolicyResult("allow", "در محدوده اختیارات مدیر", [])


def check_fields(target_type: str, fields: set[str], actor: str) -> PolicyResult:
    if actor == "owner":
        return PolicyResult("allow", "تغییر توسط کارفرما", [])
    if target_type == "harness":
        return PolicyResult("forbidden", "قوانین Harness (مجوزها) فقط توسط کارفرما تغییر می‌کنند.", [])
    if target_type == "provider" and fields & OWNER_ONLY_PROVIDER_FIELDS:
        return PolicyResult("forbidden", "کلید API، نشانی و نوع ارائه‌دهنده اطلاعات امنیتی‌اند و فقط کارفرما تغییرشان می‌دهد.",
                            [])
    if target_type == "tool" and fields & OWNER_ONLY_TOOL_FIELDS:
        return PolicyResult("forbidden", "تغییر دسته مجوز یا پیکربندی اجرایی ابزار فقط با کارفرماست.", [])
    return PolicyResult("allow", "در محدوده اختیارات مدیر", [])
