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
OWNER_ONLY_SETTINGS = {"telegram", "telegram_secret", "harness", "auth", "security", "host"}
# فیلدهای حساس ارائه‌دهنده (اسرار/مقصد ترافیک)
OWNER_ONLY_PROVIDER_FIELDS = {"api_key", "api_key_enc", "base_url", "kind"}
# فیلدهای حساس ابزار
OWNER_ONLY_TOOL_FIELDS = {"harness_category", "config", "source_url"}
# قیمت مدل مبنای Stop-Loss است؛ مدیر نمی‌تواند آن را تغییر دهد (فقط کارفرما یا API رسمی ارائه‌دهنده)
OWNER_ONLY_MODEL_FIELDS = {"price_in", "price_out", "price_cache_read", "price_cache_write", "pricing_source"}
# گروه‌های ابزاری که هرگز به کارمند داده نمی‌شوند
EMPLOYEE_FORBIDDEN_GROUPS = {"admin", "host"}

# در این بخش‌ها «بزرگ‌تر شدن» یعنی شل‌شدن محدودیت
LOOSER_IF_GREATER = {
    "stoploss": {"max_cost_per_task_usd", "max_tokens_per_task", "daily_cost_usd", "monthly_cost_usd",
                 "max_steps_simple", "max_steps_complex", "max_runtime_seconds", "max_repeat_same_action",
                 "max_consecutive_errors", "employee_max_cost_usd", "employee_max_steps",
                 "max_steps_interactive", "max_steps_hard", "step_extensions", "no_progress_limit"},
    "routine_safety": {"circuit_failure_threshold", "lease_ttl_seconds"},
    "dream": {"max_cost_usd", "max_tokens", "max_calls", "max_runtime_seconds"},
}
LOOSER_IF_SMALLER = {"routine_safety": {"circuit_cooldown_minutes"},
                     "stoploss": {"unpriced_input_per_m", "unpriced_output_per_m"}}


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
    if key == "dream" and not before.get("enabled", False) and after.get("enabled", False):
        loosened.append("enabled")  # paid background activity requires explicit owner approval
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
    if target_type == "model" and fields & OWNER_ONLY_MODEL_FIELDS:
        return PolicyResult("forbidden", "قیمت مدل مبنای کنترل هزینه است و فقط کارفرما (یا API رسمی ارائه‌دهنده) تعیینش می‌کند.",
                            [])
    if target_type == "tool" and fields & OWNER_ONLY_TOOL_FIELDS:
        return PolicyResult("forbidden", "تغییر دسته مجوز یا پیکربندی اجرایی ابزار فقط با کارفرماست.", [])
    return PolicyResult("allow", "در محدوده اختیارات مدیر", [])


def check_employee(data: dict[str, Any], caps: Any, actor: str) -> PolicyResult:
    """سقف هزینه/گام کارمند نباید از سقف کارفرما بیشتر باشد و گروه‌های مدیریتی هرگز به کارمند داده نمی‌شوند."""
    groups = set(data.get("tool_groups") or [])
    if groups & EMPLOYEE_FORBIDDEN_GROUPS:
        return PolicyResult("forbidden", "ابزارهای اداره سیستم و میزبان هرگز به کارمند داده نمی‌شوند.", [])
    over = []
    cost = data.get("max_cost_usd")
    if cost is not None and float(cost) > float(caps.employee_max_cost_usd):
        over.append(f"سقف هزینه {float(cost)} > {caps.employee_max_cost_usd}")
    steps = data.get("max_steps")
    if steps is not None and int(steps) > int(caps.employee_max_steps):
        over.append(f"سقف گام {int(steps)} > {caps.employee_max_steps}")
    if over:
        who = "ابتدا سقف کارمند را در Stop-Loss بالا ببرید" if actor == "owner" else "فقط کارفرما می‌تواند سقف را بالا ببرد"
        return PolicyResult("forbidden", "بیش از سقف غیرقابل‌تجاوز کارفرما: " + "، ".join(over) + f" ({who}).", over)
    return PolicyResult("allow", "در محدوده سقف کارفرما", [])
