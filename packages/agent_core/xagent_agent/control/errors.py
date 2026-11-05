"""طبقه‌بندی قطعی خطاها (API، مدل، ابزار، اتصال، دسترسی، تنظیمات) برای تصمیم‌گیری و خوددرمانی."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorClass:
    code: str
    label: str
    retryable: bool
    # اقدام پیشنهادی: none | retry_later | fallback_model | disable_model | reprobe | fix_config | owner_action
    action: str
    cooldown_minutes: int = 0


CLASSES: dict[str, ErrorClass] = {
    "auth": ErrorClass("auth", "کلید API نامعتبر یا دسترسی رد شد", False, "owner_action", 360),
    "quota": ErrorClass("quota", "اعتبار یا سهمیه حساب تمام شده است", False, "fallback_model", 180),
    "rate_limit": ErrorClass("rate_limit", "محدودیت نرخ درخواست", True, "retry_later", 2),
    "model_not_found": ErrorClass("model_not_found", "شناسه مدل نزد ارائه‌دهنده وجود ندارد یا در دسترس این کلید نیست", False,
                                  "disable_model", 1440),
    "unsupported_tools": ErrorClass("unsupported_tools", "مدل از فراخوانی ابزار پشتیبانی نمی‌کند", False, "reprobe", 0),
    "unsupported_input": ErrorClass("unsupported_input", "مدل این نوع ورودی (تصویر/صوت/پارامتر) را نمی‌پذیرد", False,
                                    "reprobe", 0),
    "context_length": ErrorClass("context_length", "طول کانتکست از ظرفیت مدل بیشتر است", False, "fallback_model", 0),
    "bad_request": ErrorClass("bad_request", "درخواست نامعتبر برای این مدل/پروتکل", False, "fallback_model", 30),
    "server": ErrorClass("server", "خطای سمت سرور ارائه‌دهنده", True, "retry_later", 5),
    "timeout": ErrorClass("timeout", "پاسخ در زمان مقرر نرسید", True, "retry_later", 3),
    "network": ErrorClass("network", "خطای شبکه یا DNS", True, "retry_later", 5),
    "safety_refusal": ErrorClass("safety_refusal", "رد توسط سامانه ایمنی مدل", False, "fallback_model", 0),
    "tool_disabled": ErrorClass("tool_disabled", "ابزار غیرفعال است", False, "fix_config", 0),
    "tool_missing": ErrorClass("tool_missing", "ابزار نصب یا انتخاب نشده است", False, "fix_config", 0),
    "permission": ErrorClass("permission", "مجوز Harness یا دسترسی کافی نیست", False, "owner_action", 0),
    "dependency": ErrorClass("dependency", "سرویس وابسته (دسکتاپ/Runner/تلگرام) در دسترس نیست", True, "retry_later", 5),
    "config": ErrorClass("config", "تنظیمات ناقص یا نامعتبر", False, "fix_config", 0),
    "unknown": ErrorClass("unknown", "خطای ناشناخته", True, "retry_later", 1),
}

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("auth", re.compile(r"\b(401|403)\b|invalid[_ ]api[_ ]key|incorrect api key|unauthori[sz]ed|permission[_ ]denied|"
                        r"authentication|forbidden|api key not valid", re.I)),
    ("quota", re.compile(r"insufficient[_ ]quota|quota|billing|credit balance|exceeded your current|payment required|\b402\b",
                         re.I)),
    ("rate_limit", re.compile(r"\b429\b|rate[_ ]limit|too many requests|overloaded|محدودیت نرخ", re.I)),
    ("model_not_found", re.compile(r"model[_ ]not[_ ]found|does not exist|no such model|unknown model|not found.*model|"
                                   r"model.*not found|is not supported for generatecontent|\b404\b", re.I)),
    ("unsupported_tools", re.compile(r"(tool|function)s?.{0,40}(not supported|unsupported|does not support)|"
                                     r"does not support (tools|function)", re.I)),
    ("unsupported_input", re.compile(r"(image|vision|audio|input_image).{0,40}(not supported|unsupported)|"
                                     r"unsupported parameter|unrecognized request argument|not supported with this model",
                                     re.I)),
    ("context_length", re.compile(r"context[_ ]length|maximum context|too many tokens|prompt is too long|"
                                  r"context window", re.I)),
    ("safety_refusal", re.compile(r"refusal|سامانه ایمنی|content[_ ]policy|safety", re.I)),
    ("timeout", re.compile(r"timeout|timed out|پاسخ نداد", re.I)),
    ("network", re.compile(r"name or service not known|connection (refused|reset|error)|network|dns|ssl|"
                           r"خطای ارتباط", re.I)),
    ("server", re.compile(r"\b5\d\d\b|internal server error|bad gateway|service unavailable", re.I)),
    ("bad_request", re.compile(r"\b400\b|\b422\b|invalid_request|bad request", re.I)),
]


def classify(message: str | None, status: int | None = None) -> ErrorClass:
    m = message or ""
    if status in (401, 403):
        return CLASSES["auth"]
    if status == 402:
        return CLASSES["quota"]
    if status == 429 and not re.search(r"quota|billing|credit", m, re.I):
        return CLASSES["rate_limit"]
    for code, pat in _RULES:
        if pat.search(m):
            return CLASSES[code]
    if status == 404:
        return CLASSES["model_not_found"]
    if status and status >= 500:
        return CLASSES["server"]
    return CLASSES["unknown"]
