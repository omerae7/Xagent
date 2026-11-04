"""ابزارهای اجراکننده ایجنت بیرونی (Cursor Cloud Agents) برای مدیر.

این ابزارها کار کدنویسی را به سرویس بیرونی واگذار می‌کنند (نه فراخوانی LLM). ایجاد Agent هزینه‌زا و خارج از سرور
است → دسته Harness «external_agent» (پیش‌فرض: کارت اجازه کارفرما). وضعیت و لغو بی‌خطرند.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from xagent_core.models import ExternalAgentRun, Run
from xagent_core.text import truncate

from .. import cursor_agents as ca
from ..base import Tool, ToolContext, ToolResult, i, obj, s


async def cursor_delegate(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    task = (a.get("task") or "").strip()
    if len(task) < 20:
        return ToolResult(False, "شرح کار برای Cursor باید دقیق و کامل باشد (هدف، فایل‌ها/ماژول‌ها، معیار پذیرش، تست).")
    cid = ctx.extra.get("tool_call_id") or uuid.uuid4().hex
    task_id = None
    if ctx.run_id:
        run = await ctx.session.get(Run, ctx.run_id)
        tid = (run.task_id or (run.state or {}).get("task_id")) if run else None
        try:
            task_id = uuid.UUID(str(tid)) if tid else None
        except ValueError:
            task_id = None
    res = await ca.launch(ctx.session, dedupe_key=f"cursor:{ctx.run_id}:{cid}", repo=a.get("repo", ""), task=task,
                          title=(a.get("title") or task)[:120], starting_ref=a.get("starting_ref"),
                          requested_by="owner" if ctx.trusted_owner else "agent", run_id=ctx.run_id, task_id=task_id)
    data = {"external_agent_run_id": str(res.row_id) if res.row_id else None, "status": res.status,
            "duplicate": res.duplicate}
    note = ("\nنتیجه نهایی را خود سیستم پس از پایان اجرای Cursor در میز کار گزارش می‌کند؛ اکنون منتظر نمان و "
            "ادعای انجام‌شدن نکن.") if res.ok else ""
    return ToolResult(res.ok, res.message + note, data, verified=res.ok and res.status == "running")


async def cursor_status(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    q = select(ExternalAgentRun).order_by(ExternalAgentRun.created_at.desc()).limit(min(int(a.get("limit") or 10), 30))
    rows = (await ctx.session.execute(q)).scalars().all()
    st, why = await ca.readiness(ctx.session)
    lines = [f"اتصال Cursor: {st} ({why})"]
    for r in rows:
        lines.append(f"- {r.id} | {ca.STATUS_FA.get(r.status, r.status)} | {r.repo_url} | {r.title}"
                     + (f" | PR: {r.pr_url}" if r.pr_url else "") + (f" | شاخه: {r.branch}" if r.branch else "")
                     + (f"\n  نتیجه: {truncate(r.result_summary, 400)}" if r.result_summary else "")
                     + " | هزینه: نامعلوم")
    return ToolResult(True, "\n".join(lines) if rows else lines[0] + "\nهیچ کاری به Cursor واگذار نشده است.",
                      untrusted=True)


async def cursor_cancel(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    try:
        rid = uuid.UUID(str(a.get("id")))
    except ValueError:
        return ToolResult(False, "شناسه نامعتبر است")
    row = await ctx.session.get(ExternalAgentRun, rid)
    if row is None:
        return ToolResult(False, "اجرای Cursor یافت نشد")
    return ToolResult(True, await ca.request_cancel(ctx.session, row, "owner" if ctx.trusted_owner else "agent"))


TOOLS = [
    Tool("cursor_delegate",
         "واگذاری یک کار کدنویسی واقعی (رفع باگ، پیاده‌سازی، Refactor، نوشتن تست) روی یک مخزن GitHub مجاز به "
         "Cursor Cloud Agent. فقط وقتی مناسب است که کار به تغییر کد در مخزن نیاز دارد؛ پرسش متنی ساده را خودت پاسخ بده. "
         "Agent روی شاخه تازه کار می‌کند، هرگز Merge خودکار ندارد و هزینه آن نامعلوم است.",
         obj({"repo": s("نشانی مخزن مجاز، مثل https://github.com/owner/repo"),
              "task": s("شرح دقیق کار: هدف، محل تغییر، معیار پذیرش و تست‌ها (بدون کلید/رمز/فایل خصوصی)"),
              "title": s("عنوان کوتاه"), "starting_ref": s("شاخه مبدأ (اختیاری؛ باید مجاز باشد)")},
             ["repo", "task"]),
         cursor_delegate, "external", category="external_agent", label="واگذاری به Cursor"),
    Tool("cursor_status", "وضعیت کارهای واگذارشده به Cursor Cloud Agents (شاخه، PR، نتیجه).",
         obj({"limit": i("حداکثر")}), cursor_status, "external", label="وضعیت Cursor"),
    Tool("cursor_cancel", "لغو یک کار در حال اجرای Cursor (کاهش هزینه).", obj({"id": s("شناسه اجرا")}, ["id"]),
         cursor_cancel, "external", label="لغو Cursor"),
]
