"""شناسنامه پویای قابلیت‌ها (Dynamic Capability Manifest).

از رجیستری ابزارها و وضعیت واقعی سرویس‌ها ساخته می‌شود، نه متن ثابت. هر قابلیت در یکی از سه دسته:
- ready: نصب‌شده، فعال و اتصالش برقرار است (قابل اجرا همین حالا)
- needs_setup: پشتیبانی می‌شود ولی اتصال/تنظیم/مدل لازم را ندارد (با «چگونه» برای رفع)
- unsupported: در این نصب یا برای این نقش پشتیبانی نمی‌شود
نصب‌بودن یک ابزار هرگز به معنای آماده‌بودن اتصال آن نیست.
"""

from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.models import AIModel, AIProvider, Employee, InstalledTool, Routine
from xagent_core.settings_store import StopLossSettings, TelegramSettings, get_setting

# ── کاوش واقعی آمادگی (با سقف زمان و کش کوتاه؛ نه در هر نوبت مدل) ──
PROBE_TTL = 60.0
PROBE_TIMEOUT = 2.0
_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _cached(name: str, fn) -> dict[str, Any]:  # noqa: ANN001
    import asyncio
    import time

    now = time.monotonic()
    hit = _probe_cache.get(name)
    if hit and now - hit[0] < PROBE_TTL:
        return hit[1]
    try:
        res = await asyncio.wait_for(fn(), timeout=PROBE_TIMEOUT)
    except Exception as e:  # noqa: BLE001 — نتیجه نامعلوم «آماده» نیست
        res = {"ok": None, "error": type(e).__name__}
    res["checked_at"] = time.time()
    _probe_cache[name] = (now, res)
    return res


def invalidate_probes() -> None:
    _probe_cache.clear()


async def probe_browser() -> dict[str, Any]:
    """مرورگر فقط وقتی آماده است که نقطه CDP واقعاً پاسخ دهد (نه به‌خاطر نام نوع Sandbox)."""
    import httpx

    from xagent_tools.builtin.computer import _cdp_url

    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
        r = await c.get(_cdp_url().rstrip("/") + "/json/version")
    return {"ok": r.status_code == 200, "status": r.status_code}


async def probe_desktop() -> dict[str, Any]:
    from xagent_tools.sandbox import get_sandbox

    h = await get_sandbox().health()
    return {"ok": bool(h.get("ok")), "kind": h.get("kind")}


async def _redis_get(key: str) -> str | None:
    try:
        v = await bus.get_redis().get(key)
        return v.decode() if isinstance(v, bytes) else v
    except Exception:  # noqa: BLE001
        return None


async def build_manifest(session: AsyncSession, principal: str = "owner", employee: Any = None,
                         telegram: Any = None, sandbox_kind: str | None = None) -> dict[str, Any]:
    out: dict[str, list[dict[str, Any]]] = {"ready": [], "needs_setup": [], "unsupported": [], "unverified": []}

    def add(key: str, label: str, state: str, detail: str = "", how: str = "", tools: str = "") -> None:
        out[state].append({"key": key, "label": label, "detail": detail, "how": how, "load": tools})

    is_owner = principal == "owner" and employee is None
    # ── پایه (همیشه آماده) ──
    add("web", "جست‌وجو و خواندن وب (HTTP، محافظت SSRF)", "ready", tools="web")
    add("memory", "حافظه ۵ لایه و جست‌وجوی سابقه کارها", "ready", tools="memory, episodes")
    add("tasks", "تسک‌ها و پیگیری‌ها", "ready", tools="tasks")
    add("crm", "مخاطبان و CRM", "ready", tools="crm")
    add("files", "فایل‌های فضای کاری ایجنت", "ready", tools="files")
    # ── زمان‌بند و روتین‌ها: وابسته به Worker زنده ──
    hb = await _redis_get("xagent:worker:heartbeat")
    active = (await session.execute(select(func.count()).select_from(Routine).where(
        Routine.status == "active"))).scalar_one()
    if hb:
        add("routines", "روتین/مأموریت زمان‌دار (اجرای مستقل، پایان خودکار، گزارش به میز کار)", "ready",
            f"{active} روتین فعال؛ حداقل فاصله ۶۰ ثانیه", tools="routines")
    else:
        add("routines", "روتین/مأموریت زمان‌دار", "needs_setup", "Worker پس‌زمینه Heartbeat ندارد",
            "سرویس worker را بررسی کنید (xagent status)", tools="routines")
    # ── تلگرام ──
    tg_cfg = await get_setting(session, "telegram", TelegramSettings)
    connected = bool(getattr(telegram, "connected", False)) if telegram is not None else False
    if telegram is None:
        raw = await _redis_get("xagent:telegram:status")
        try:
            connected = bool(json.loads(raw).get("connected")) if raw else False
        except ValueError:
            connected = False
    if connected:
        add("telegram", "تلگرام شخصی (MTProto): خواندن رویدادمحور، ارسال در محدوده Allowlist/مجوز مأموریت",
            "ready", f"Allowlist: {len(tg_cfg.allowlist)} گفتگو", tools="telegram")
    else:
        add("telegram", "تلگرام شخصی", "needs_setup",
            "فعال نیست یا نشست متصل نیست" if tg_cfg.enabled else "هنوز متصل نشده است",
            "تنظیمات ← تلگرام: ورود با شماره و کد (فقط کارفرما)", tools="telegram")
    # ── مرورگر و دسکتاپ: آمادگی از کاوش واقعی، نه از نام نوع Sandbox ──
    kind = sandbox_kind or os.environ.get("XAGENT_SANDBOX", "desktop")
    allowed = _employee_groups(employee)
    if kind == "desktop":
        b = await _cached("browser", probe_browser)
        d = await _cached("desktop", probe_desktop)
        for key, label, res, tools in (
                ("browser", "مرورگر Chrome ایجنت (تب‌ها، باز کردن، خواندن، کلیک، تایپ؛ Takeover کارفرما)", b, "browser"),
                ("desktop", "کنترل تصویری دسکتاپ (ماوس/صفحه‌کلید، با قفل یک‌اجرایی)", d, "desktop")):
            if allowed is not None and key not in allowed:
                add(key, label, "unsupported", "خارج از گروه‌های مجاز این کارمند")
            elif res.get("ok") is True:
                add(key, label, "ready", "اتصال آزموده شد", tools=tools)
            elif res.get("ok") is None:
                add(key, label, "unverified", f"آمادگی تأیید نشد ({res.get('error') or 'نامعلوم'})",
                    "پیش از ادعای انجام، با ابزار امتحان کن و نتیجه واقعی را گزارش بده", tools=tools)
            else:
                add(key, label, "needs_setup", "سرویس مرورگر/دسکتاپ پاسخ نمی‌دهد",
                    "xagent status / ری‌استارت سرویس desktop (فقط کارفرما)", tools=tools)
    elif kind == "runner":
        add("browser", "مرورگر تعاملی و دسکتاپ", "unsupported", "این نصب Runner بدون Chromium/XFCE است",
            "web_fetch/web_search برای خواندن وب؛ برای مرورگر تعاملی نصب با پروفایل دسکتاپ لازم است")
    else:
        add("browser", "مرورگر تعاملی و دسکتاپ", "unsupported", "این نصب در حالت سبک (بدون دسکتاپ) است",
            "نصب با پروفایل دارای دسکتاپ؛ تا آن زمان web_fetch/web_search برای خواندن وب آماده است")
    # ── مدل‌ها ──
    # فقط مدل فعال از ارائه‌دهنده فعال و بدون وضعیت «از کار افتاده» قابل اتکاست
    models = (await session.execute(select(AIModel).join(AIProvider, AIProvider.id == AIModel.provider_id).where(
        AIModel.enabled.is_(True), AIProvider.enabled.is_(True), AIModel.health != "failing"))).scalars().all()
    tool_models = [m for m in models if (m.capabilities or {}).get("tools")]
    if tool_models:
        add("models", "مدل‌های اجرایی با فراخوانی ابزار", "ready", f"{len(tool_models)} مدل فعال")
    else:
        add("models", "مدل اجرایی با ابزار", "needs_setup", "هیچ مدل فعالی با قابلیت ابزار نیست",
            "تنظیمات ← هوش مصنوعی: افزودن ارائه‌دهنده و کلید API (فقط کارفرما)")
    for cap, label in (("vision", "بینایی (تحلیل تصویر)"), ("stt", "گفتار به متن"), ("tts", "متن به گفتار")):
        if any((m.capabilities or {}).get(cap) for m in models):
            add(cap, label, "ready")
        else:
            add(cap, label, "needs_setup", "مدل دارای این قابلیت فعال نیست", "افزودن/کشف مدل مناسب در تنظیمات")
    # ── ابزارهای نصب‌شده ──
    inst = (await session.execute(select(InstalledTool.status, func.count()).where(InstalledTool.enabled.is_(True))
                                  .group_by(InstalledTool.status))).all()
    counts = dict(inst)
    if counts.get("ready") and (allowed is None or "installed" in allowed):
        add("installed", "ابزارهای نصب‌شده (MCP/GitHub)", "ready", f"{counts['ready']} ابزار فعال و آماده", tools="installed")
    if any(k for k in counts if k not in ("ready",)):
        add("installed_broken", "ابزارهای نصب‌شده معیوب/در حال نصب", "needs_setup",
            "، ".join(f"{k}: {v}" for k, v in counts.items() if k != "ready"), "صفحه ابزارها: آزمون یا Rollback")
    # ── تفویض به کارمندان ──
    if employee is None:
        emp = (await session.execute(select(func.count()).select_from(Employee).where(Employee.enabled.is_(True)))).scalar_one()
        if emp:
            add("delegation", "تفویض کار به کارمندان تخصصی", "ready", f"{emp} کارمند فعال", tools="admin")
        else:
            add("delegation", "تفویض کار به کارمندان", "needs_setup", "کارمندی تعریف نشده",
                "مدیر می‌تواند با employee_upsert کارمند بسازد", tools="admin")
    # ── اداره سیستم و میزبان (فقط کارفرما) ──
    if is_owner:
        add("admin", "اداره سیستم: مدل‌ها، کارمندان، تنظیمات غیرامنیتی، عیب‌یابی، Rollback", "ready", tools="admin")
        sock = get_settings().hostd_socket
        if os.path.exists(sock):
            add("host", "ترمینال مدیریتی VPS (عملیات ساختاریافته، سطح ۳ با کارت اجازه)", "ready", tools="host")
        else:
            add("host", "ترمینال مدیریتی VPS", "needs_setup", "کارگزار میزبان در دسترس نیست",
                "xagent doctor / نصب xagent-hostd", tools="host")
    else:
        add("admin", "اداره سیستم و ترمینال VPS", "unsupported", "فقط در اجرای مستقیم کارفرما")
    # ── اجراکننده ایجنت بیرونی (نه LLM): Cursor Cloud Agents ──
    from xagent_tools.cursor_agents import config as cursor_config
    from xagent_tools.cursor_agents import readiness

    # تا کارفرما Cursor را فعال نکرده، هیچ سطری به پرامپت اضافه نمی‌شود (بدون هزینه توکن برای همه درخواست‌ها)
    cursor_on = (await cursor_config(session))[0].enabled
    cst, cwhy = await readiness(session) if cursor_on else ("off", "")
    if not cursor_on:
        pass
    elif is_owner and cst in ("ready", "untested"):
        add("cursor", "اجراکننده بیرونی Cursor Cloud Agents (کار کدنویسی روی مخزن مجاز، شاخه تازه، بدون Merge خودکار؛ "
            "هزینه نامعلوم)", "ready", cwhy, tools="external")
    elif is_owner:
        add("cursor", "اجراکننده بیرونی Cursor Cloud Agents", "needs_setup", cwhy,
            "تنظیمات ← Cursor Cloud Agents: کلید، مخازن مجاز و فعال‌سازی (فقط کارفرما)", tools="external")
    add("payment", "پرداخت و امور مالی", "unsupported", "طبق Harness ممنوع است")
    add("calls", "تماس تلفنی و پیامک", "unsupported", "ابزاری برای آن نصب نشده است")
    sl = await get_setting(session, "stoploss", StopLossSettings)
    return {
        "role": "employee" if employee is not None else ("owner" if is_owner else principal),
        **out,
        "limits": {"max_cost_per_task_usd": sl.max_cost_per_task_usd, "daily_cost_usd": sl.daily_cost_usd,
                   "max_steps_simple": sl.max_steps_simple, "max_steps_complex": sl.max_steps_complex,
                   "max_steps_interactive": sl.max_steps_interactive, "max_steps_hard": sl.max_steps_hard},
    }


def _employee_groups(employee: Any) -> set[str] | None:
    groups = set(getattr(employee, "tool_groups", None) or []) if employee is not None else set()
    return groups | {"core", "memory"} if groups else None


def render_manifest(m: dict[str, Any]) -> str:
    """متن فشرده برای کانتکست مدل (بدون جزئیات حساس)."""
    def line(c: dict[str, Any]) -> str:
        extra = f" — {c['detail']}" if c.get("detail") else ""
        load = f" [tools_load: {c['load']}]" if c.get("load") else ""
        return f"- {c['label']}{extra}{load}"

    parts = ["## شناسنامه واقعی قابلیت‌ها (از وضعیت زنده سیستم)",
             "### آماده و قابل اجرا", *[line(c) for c in m["ready"]]]
    if m["needs_setup"]:
        parts += ["### پشتیبانی می‌شود ولی نیازمند تنظیم/اتصال (ادعای آماده‌بودن نکن؛ راه رفع را بگو)",
                  *[line(c) + (f" → {c['how']}" if c.get("how") else "") for c in m["needs_setup"]]]
    if m.get("unverified"):
        parts += ["### آمادگی تأییدنشده (کاوش نتیجه نداد؛ «آماده» فرض نکن)", *[line(c) + (f" → {c['how']}" if c.get("how") else "")
                                                                   for c in m["unverified"]]]
    if m["unsupported"]:
        parts += ["### پشتیبانی‌نشده در این نصب/نقش", *[line(c) for c in m["unsupported"]]]
    parts.append("قاعده: پیش از گفتن «نمی‌توانم»، ابزار مرتبط را بارگذاری و امتحان کن؛ اگر واقعاً ممکن نبود، "
                 "علت دقیق، مرحله ناموفق و اقدام لازم را گزارش کن. هیچ نتیجه‌ای را بدون شواهد اجرای ابزار موفق اعلام نکن.")
    return "\n".join(parts)
