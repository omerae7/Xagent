"""ابزارهای ترمینال مدیریتی VPS برای مدیر (فقط اجرای کارفرما؛ هرگز کارمند، Viewer یا رویداد بیرونی).

Worker هیچ دسترسی مستقیمی به میزبان ندارد: درخواست‌ها از مسیر داخلی احرازشده API می‌روند و API سیاست کارفرما،
سطح عملیات، سقف تعداد و کارت اجازه را اعمال می‌کند. عملیات سطح ۳ کارت اجازه می‌سازد و پس از تأیید کارفرما اجرا می‌شود.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.text import truncate
from xagent_tools.base import Tool, ToolContext, ToolResult, obj, s
from xagent_tools.errors import EscalateCategory


def _token() -> str:
    import hashlib
    import hmac

    return hmac.new(get_settings().secret_key.encode(), b"xagent-internal-v1", hashlib.sha256).digest().hex()


def client_factory() -> httpx.AsyncClient:
    """قابل جایگزینی در آزمون (ASGITransport)."""
    return httpx.AsyncClient(base_url=get_settings().api_internal_url, timeout=httpx.Timeout(60, read=90), trust_env=False)


def _guard(ctx: ToolContext) -> None:
    if not ctx.trusted_owner or ctx.extra.get("principal") != "owner" or ctx.extra.get("employee_name"):
        raise PermissionError("دسترسی به ترمینال میزبان فقط برای مدیر در اجرای کارفرماست (نه کارمند، نه منبع بیرونی).")


async def _call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    async with client_factory() as c:
        r = await c.request(method, path, json=payload, headers={"X-XAgent-Internal": _token()})
    try:
        data = r.json()
    except ValueError:
        data = {"detail": r.text[:500]}
    return r.status_code, data


def _fmt(op: dict) -> str:
    out = op.get("output") or ""
    lines = [f"عملیات: {op['op']} (سطح {op['level']}) — وضعیت: {op['status']}"
             + (f" — کد خروج: {op['exit_code']}" if op.get("exit_code") is not None else ""),
             f"شناسه: {op['id']}"]
    if op.get("approved_by"):
        lines.append(f"مجوز: {op['approved_by']}")
    if op.get("error"):
        lines.append(f"خطا: {op['error']}")
    if out:
        lines.append("خروجی:\n" + truncate(out[-8000:], 8000))
    if op["status"] == "running":
        lines.append("هنوز در حال اجراست؛ با host_op_status پیگیری کن.")
    return "\n".join(lines)


async def host_catalog(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard(ctx)
    code, data = await _call("GET", "/api/internal/host/catalog")
    if code != 200:
        return ToolResult(False, f"ترمینال میزبان در دسترس نیست: {data.get('detail')}")
    lines = [f"کارگزار: {'فعال' if data['enabled'] else 'غیرفعال (توسط کارفرما)'} — سیاست آپدیت: {data['update_policy']}"]
    for o in data["ops"]:
        lvl = {1: "۱ خودکار", 2: "۲ خودکار مشروط", 3: "۳ نیازمند تأیید"}[o["level"]]
        args = f" آرگومان‌ها: {', '.join(o['args'])}" if o["args"] else ""
        mode = f" [{o['mode']}]" if o["mode"] != "default" else ""
        lines.append(f"- {o['op']} (سطح {lvl}){mode}: {o['description']}{args}")
    return ToolResult(True, "\n".join(lines), {"count": len(data["ops"])})


async def host_run(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard(ctx)
    if await bus.is_stop_all():
        return ToolResult(False, "توقف اضطراری سراسری فعال است؛ عملیات میزبان اجرا نمی‌شود.")
    op = str(a.get("op", "")).strip()
    args = a.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return ToolResult(False, "args باید شیء JSON باشد.")
    approved = "host_admin" in ctx.extra.get("approved", set())
    code, data = await _call("POST", "/api/internal/host/run", {
        "op": op, "args": args, "reason": str(a.get("reason", ""))[:1000], "run_id": str(ctx.run_id) if ctx.run_id else None,
        "tool_call_id": ctx.extra.get("tool_call_id"), "approved": approved, "actor": "manager"})
    if code == 200 and data.get("status") == "approval_required":
        raise EscalateCategory(
            "host_admin",
            f"اجرای «{op}» روی سرور (سطح {data.get('level')}) نیازمند تأیید کارفرماست. دلیل مدیر: {a.get('reason', '')}",
            {"op": op, "args": args},
        )
    if code != 200:
        return ToolResult(False, f"⛔ {data.get('detail') or data}")
    return ToolResult(data["status"] == "succeeded", _fmt(data), {"op_id": data["id"], "status": data["status"]},
                      verified=data["status"] == "succeeded")


async def host_op_status(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard(ctx)
    code, data = await _call("GET", f"/api/internal/host/ops/{a['op_id']}")
    if code != 200:
        return ToolResult(False, f"وضعیت دریافت نشد: {data.get('detail')}")
    return ToolResult(data["status"] == "succeeded", _fmt(data), {"status": data["status"]})


HOST_TOOLS = [
    Tool("host_catalog", "فهرست عملیات مجاز ترمینال مدیریتی VPS (سطح ۱ مشاهده/تشخیص، ۲ اصلاح کم‌ریسک، ۳ با تأیید کارفرما) "
         "و سیاست فعلی کارفرما.", obj({}), host_catalog, "host", label="فهرست عملیات میزبان"),
    Tool("host_run", "اجرای واقعی یک عملیات ساختاریافته روی سرور (نه متن آزاد): وضعیت CPU/RAM/دیسک/شبکه و سرویس‌ها، "
         "لاگ Docker/systemd، xagent status/doctor/backup/update/rollback، بررسی نسخه (update.check = Dry Run)، "
         "Restart سرویس با بررسی سلامت، دستورات تشخیصی Linux. سطح ۳ کارت اجازه می‌سازد. "
         "خروجی، کد خروج و وضعیت برمی‌گردد.",
         obj({"op": s("نام عملیات از host_catalog، مثلاً system.status یا service.restart"),
              "args": {"type": "object", "description": "آرگومان‌های عملیات، مثلاً {\"service\": \"worker\"}"},
              "reason": s("دلیل/هدف این عملیات (در حسابرسی و کارت اجازه ثبت می‌شود)")}, ["op", "reason"]),
         host_run, "host", category="host_ops", label="ترمینال مدیریتی VPS"),
    Tool("host_op_status", "پیگیری وضعیت، خروجی و کد خروج یک عملیات میزبان در حال اجرا.",
         obj({"op_id": s("شناسه عملیات")}, ["op_id"]), host_op_status, "host", label="وضعیت عملیات میزبان"),
]
