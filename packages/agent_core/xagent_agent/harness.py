"""Harness: ماتریس مجوزهای قطعی سمت سرور (مستقل از تصمیم مدل).

برای هر دسته عملیات یکی از حالت‌ها: auto (خودکار در محدوده مجاز) | ask (اجازه در هر بار) |
conditional (مشروط) | forbidden (ممنوع). شروط ساختاریافته قطعی اعمال می‌شوند و متن قانون
اختصاصی (Custom Rule Box) هم به مدل نشان داده می‌شود و هم در کارت درخواست مجوز نمایش داده می‌شود.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.models import HarnessRule

Mode = Literal["auto", "ask", "conditional", "forbidden"]

CATEGORIES: dict[str, dict[str, Any]] = {
    "login": {"label": "لاگین کردن به سایت‌ها", "default": "ask", "icon": "key"},
    "file_modify": {"label": "حذف و ویرایش فایل", "default": "conditional", "icon": "file",
                    "conditions": {"ask_actions": ["delete"]}},
    "external_message": {"label": "ارسال پیام بیرونی", "default": "ask", "icon": "send"},
    "install_tool": {"label": "نصب ابزار از گیت‌هاب", "default": "ask", "icon": "package"},
    "routine_change": {"label": "تغییر روتین/تسک", "default": "auto", "icon": "repeat"},
    "payment": {"label": "پرداخت و امور مالی", "default": "forbidden", "icon": "wallet"},
    "code_exec": {"label": "اجرای کد و فرمان ترمینال", "default": "ask", "icon": "terminal"},
    "desktop_control": {"label": "کنترل دسکتاپ و مرورگر", "default": "auto", "icon": "monitor"},
    "web_irreversible": {"label": "اقدام برگشت‌ناپذیر در وب (حذف، تأیید نهایی، ثبت فرم، تغییر حساب)", "default": "ask",
                         "icon": "alert"},
    "external_agent": {"label": "واگذاری کار هزینه‌زا به ایجنت بیرونی (Cursor Cloud Agents)", "default": "ask",
                       "icon": "plug"},
    "memory_rule": {"label": "تغییر قوانین رفتاری و حافظه", "default": "ask", "icon": "brain"},
    "external_tool": {"label": "ابزارهای نصب‌شده خارجی (MCP/GitHub)", "default": "ask", "icon": "plug"},
    "system_update": {"label": "به‌روزرسانی هسته و سیاست‌های امنیتی", "default": "ask", "icon": "shield",
                      "locked": True},
    "system_config": {"label": "اداره سیستم توسط مدیر (مدل‌ها، کارمندان، تنظیمات غیرامنیتی)", "default": "auto",
                      "icon": "settings"},
    "security_limits": {"label": "شل‌کردن سقف هزینه و محدودیت‌های ایمنی", "default": "ask", "icon": "shield",
                        "locked": True},
    "host_ops": {"label": "ترمینال مدیریتی VPS: مشاهده، تشخیص و اصلاح کم‌ریسک (سطح ۱ و ۲)", "default": "auto",
                 "icon": "terminal"},
    "host_admin": {"label": "ترمینال مدیریتی VPS: آپدیت، Rollback، بسته‌ها، فایروال و فرمان خاص (سطح ۳)",
                   "default": "ask", "icon": "shield", "locked": True},
}
# دسته‌هایی که هرگز نمی‌توانند خودکار شوند (تغییر هسته و سیاست امنیتی نیازمند تایید صریح است)
NEVER_AUTO = {"system_update", "payment", "security_limits", "host_admin"}


@dataclass
class Decision:
    action: Literal["allow", "ask", "deny"]
    reason: str
    category: str
    rule_text: str = ""


async def load_rules(session: AsyncSession, fresh: bool = False) -> dict[str, HarnessRule]:
    """fresh=True: سیاست فعلی پایگاه داده (نه نسخه کهنه نقشه هویت نشست) — برای تصمیم در لحظه اثر."""
    stmt = select(HarnessRule)
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    rows = (await session.execute(stmt)).scalars().all()
    return {r.category: r for r in rows}


async def ensure_defaults(session: AsyncSession) -> None:
    existing = await load_rules(session)
    for cat, meta in CATEGORIES.items():
        if cat not in existing:
            session.add(
                HarnessRule(category=cat, mode=meta["default"], rule_text="", conditions=meta.get("conditions", {}))
            )
    await session.flush()


def _domain(url: str) -> str:
    try:
        return (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
    except ValueError:
        return ""


def _match_any(value: str, patterns: list[str]) -> bool:
    v = value.lower()
    for p in patterns:
        p = p.strip().lower()
        if not p:
            continue
        if fnmatch.fnmatch(v, p) or v == p or v.endswith("." + p.lstrip("*.")):
            return True
    return False


def _targets(args: dict[str, Any]) -> list[str]:
    """استخراج اهداف قابل مقایسه از آرگومان‌ها (دامنه، گیرنده، مسیر)."""
    out: list[str] = []
    for key in ("url", "site", "domain", "repo_url", "href"):
        if isinstance(args.get(key), str):
            out.append(_domain(args[key]))
            out.append(args[key])
    for key in ("chat", "recipient", "to", "username", "peer", "chat_id", "contact"):
        if args.get(key) is not None:
            out.append(str(args[key]).lstrip("@"))
    for key in ("path", "file", "target"):
        if isinstance(args.get(key), str):
            out.append(args[key])
    return [o for o in out if o]


def evaluate(
    rule: HarnessRule | None, category: str, action_name: str, args: dict[str, Any], amount: float | None = None
) -> Decision:
    meta = CATEGORIES.get(category, {"label": category, "default": "ask"})
    mode: str = rule.mode if rule else meta["default"]
    text = rule.rule_text if rule else ""
    cond: dict[str, Any] = (rule.conditions if rule else meta.get("conditions", {})) or {}
    if category in NEVER_AUTO and mode == "auto":
        mode = "ask"
    if mode == "forbidden":
        return Decision("deny", f"«{meta['label']}» در Harness ممنوع است.", category, text)
    if mode == "auto":
        return Decision("allow", "مجاز در محدوده خودکار", category, text)
    if mode == "ask":
        return Decision("ask", f"«{meta['label']}» نیازمند اجازه کارفرما در هر بار است.", category, text)
    # conditional: شروط قطعی
    targets = _targets(args)
    deny = cond.get("deny", []) or []
    allow = cond.get("allow", []) or []
    ask_actions = cond.get("ask_actions", []) or []
    if any(_match_any(t, deny) for t in targets):
        return Decision("ask" if cond.get("deny_mode") != "forbid" else "deny",
                        "هدف در فهرست موارد حساس/ممنوع قانون اختصاصی است.", category, text)
    if action_name in ask_actions or any(action_name.endswith(a) for a in ask_actions):
        return Decision("ask", f"عملیات «{action_name}» طبق شرط نیازمند اجازه است.", category, text)
    max_amount = cond.get("max_amount")
    if amount is not None and max_amount is not None and amount > float(max_amount):
        return Decision("ask", f"مبلغ {amount} بیش از سقف مجاز {max_amount} است.", category, text)
    if allow:
        if targets and any(_match_any(t, allow) for t in targets):
            return Decision("allow", "هدف در فهرست مجاز قانون اختصاصی است.", category, text)
        return Decision("ask", "هدف خارج از فهرست مجاز است.", category, text)
    return Decision("allow", "شروط قانون رعایت شده است.", category, text)


def summarize_args(args: dict[str, Any], limit: int = 600) -> str:
    s = json.dumps(args, ensure_ascii=False, default=str)
    s = re.sub(r'"(password|api_key|token|secret)"\s*:\s*"[^"]*"', r'"\1": "••••"', s)
    return s if len(s) <= limit else s[: limit - 1] + "…"


SECRET_KEYS = re.compile(r"(pass(word)?|api_?key|token|secret|otp|pin|cvv)", re.IGNORECASE)


def redact(args: dict[str, Any], max_len: int = 1500) -> dict[str, Any]:
    """کپی امن آرگومان‌ها برای نمایش/ذخیره: پنهان‌سازی اسرار و کوتاه‌سازی مقادیر طولانی."""
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if SECRET_KEYS.search(str(k)):
            out[k] = "••••"
        elif isinstance(v, str):
            out[k] = v if len(v) <= max_len else v[: max_len - 1] + "…"
        elif isinstance(v, dict):
            out[k] = redact(v, max_len)
        else:
            try:
                json.dumps(v)
                out[k] = v
            except (TypeError, ValueError):
                out[k] = str(v)[:max_len]
    return out


def rules_for_prompt(rules: dict[str, HarnessRule], skip: set[str] | frozenset[str] = frozenset()) -> str:
    """skip: دسته‌هایی که ابزارشان در این نصب اصلاً فعال نیست (بدون هزینه توکن در هر درخواست)."""
    lines = []
    labels = {"auto": "خودکار", "ask": "با اجازه", "conditional": "مشروط", "forbidden": "ممنوع"}
    for cat, meta in CATEGORIES.items():
        if cat in skip:
            continue
        r = rules.get(cat)
        mode = r.mode if r else meta["default"]
        extra = f" — {r.rule_text.strip()}" if r and r.rule_text.strip() else ""
        lines.append(f"- {meta['label']}: {labels.get(mode, mode)}{extra}")
    return "\n".join(lines)
