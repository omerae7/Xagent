"""ابزارهای کاری: وظایف، روتین‌ها و CRM."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from xagent_core import bus
from xagent_core.models import Project, Routine, Task
from xagent_core.text import truncate
from xagent_core.timeutil import jalali_str, parse_jalali_date
from xagent_crm import find_contacts, log_interaction, upsert_contact

from ..base import Tool, ToolContext, ToolResult, i, obj, s
from ..schedule import compute_next_run, validate_routine


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None


def _date(v: str | None, tz: str):
    if not v:
        return None
    return parse_jalali_date(v, tz)


# ── وظایف ──
async def task_create(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    assignee_id = None
    if a.get("assignee"):
        found = await find_contacts(ctx.session, a["assignee"], 1)
        assignee_id = found[0].id if found else None
    t = Task(
        title=a["title"][:256],
        description=a.get("description", ""),
        priority=a.get("priority", "normal"),
        status=a.get("status", "todo"),
        assignee_type="contact" if assignee_id else a.get("assignee_type", "agent"),
        assignee_contact_id=assignee_id,
        due_at=_date(a.get("due"), ctx.timezone),
        created_by="agent",
        source={"run_id": str(ctx.run_id) if ctx.run_id else None},
    )
    ctx.session.add(t)
    await ctx.session.flush()
    await bus.publish("task.changed", {"id": str(t.id), "action": "create"})
    due = f" — مهلت {jalali_str(t.due_at, ctx.timezone)}" if t.due_at else ""
    return ToolResult(True, f"تسک «{t.title}» ساخته شد (شناسه {t.id}){due}", {"id": str(t.id)}, verified=True)


async def task_update(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    t = await ctx.session.get(Task, _uuid(a.get("id")))
    if t is None:
        return ToolResult(False, "تسک یافت نشد.")
    for f in ("title", "description", "status", "priority"):
        if a.get(f):
            setattr(t, f, a[f])
    if a.get("due"):
        t.due_at = _date(a["due"], ctx.timezone)
    if t.status == "done" and t.completed_at is None:
        from xagent_core.db import utcnow

        t.completed_at = utcnow()
    await ctx.session.flush()
    await bus.publish("task.changed", {"id": str(t.id), "action": "update"})
    return ToolResult(True, f"تسک «{t.title}» به‌روزرسانی شد (وضعیت: {t.status})", verified=True)


async def task_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    q = select(Task).order_by(Task.due_at.asc().nulls_last(), Task.created_at.desc()).limit(int(a.get("limit", 20)))
    if a.get("status"):
        q = q.where(Task.status == a["status"])
    else:
        q = q.where(Task.status.notin_(["done", "cancelled"]))
    rows = (await ctx.session.execute(q)).scalars().all()
    if not rows:
        return ToolResult(True, "تسکی وجود ندارد.")
    lines = [
        f"- [{t.status}|{t.priority}] {truncate(t.title, 80)}"
        + (f" — مهلت {jalali_str(t.due_at, ctx.timezone)}" if t.due_at else "")
        + f" (id={t.id})"
        for t in rows
    ]
    return ToolResult(True, "\n".join(lines))


# ── روتین‌ها ──
async def routine_create(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    r = Routine(
        name=a["name"][:160],
        description=a.get("description", ""),
        instruction=a["instruction"],
        trigger_type=a.get("trigger_type", "cron"),
        cron_expr=a.get("cron"),
        interval_seconds=a.get("interval_seconds"),
        run_at=_date(a.get("run_at"), ctx.timezone),
        event_filter=a.get("event_filter") or {},
        timezone=ctx.timezone,
        time_sensitive=bool(a.get("time_sensitive", False)),
        created_by="agent",
    )
    err = validate_routine(r)
    if err:
        return ToolResult(False, f"روتین نامعتبر: {err}")
    r.next_run_at = compute_next_run(r)
    ctx.session.add(r)
    await ctx.session.flush()
    await bus.publish("routine.changed", {"id": str(r.id), "action": "create"})
    nxt = jalali_str(r.next_run_at, ctx.timezone) if r.next_run_at else "رویدادمحور"
    return ToolResult(True, f"روتین «{r.name}» ساخته شد؛ اجرای بعدی: {nxt}", {"id": str(r.id)}, verified=True)


async def routine_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = (await ctx.session.execute(select(Routine).order_by(Routine.created_at.desc()).limit(30))).scalars().all()
    if not rows:
        return ToolResult(True, "روتینی تعریف نشده است.")
    lines = []
    for r in rows:
        state = "متوقف" if r.paused else ("فعال" if r.enabled else "غیرفعال")
        nxt = jalali_str(r.next_run_at, ctx.timezone) if r.next_run_at else "-"
        lines.append(f"- {r.name} [{state}|{r.trigger_type}] بعدی: {nxt} | آخرین: {r.last_status or '-'} (id={r.id})")
    return ToolResult(True, "\n".join(lines))


async def routine_set_state(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    r = await ctx.session.get(Routine, _uuid(a.get("id")))
    if r is None:
        return ToolResult(False, "روتین یافت نشد.")
    action = a["action"]
    if action == "pause":
        r.paused = True
    elif action == "resume":
        r.paused = False
        r.circuit_state = "closed"
        r.consecutive_failures = 0
        r.next_run_at = compute_next_run(r)
    elif action == "disable":
        r.enabled = False
    elif action == "enable":
        r.enabled = True
        r.next_run_at = compute_next_run(r)
    else:
        return ToolResult(False, "اقدام نامعتبر")
    await ctx.session.flush()
    await bus.publish("routine.changed", {"id": str(r.id), "action": action})
    return ToolResult(True, f"روتین «{r.name}»: {action}", verified=True)


# ── CRM ──
async def contact_search(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = await find_contacts(ctx.session, a["query"], int(a.get("limit", 8)))
    if not rows:
        return ToolResult(True, "مخاطبی پیدا نشد.")
    lines = [
        f"- {c.name} ({c.kind}) {c.role_title or ''} {('@' + c.telegram_username) if c.telegram_username else ''}"
        f" {c.phone or ''} (id={c.id})"
        for c in rows
    ]
    return ToolResult(True, "\n".join(lines))


async def contact_upsert(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    c = await upsert_contact(ctx.session, a, _uuid(a.get("id")))
    await bus.publish("crm.changed", {"id": str(c.id)})
    return ToolResult(True, f"مخاطب «{c.name}» ذخیره شد (id={c.id})", {"id": str(c.id)}, verified=True)


async def crm_log_interaction(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    contact_id = _uuid(a.get("contact_id"))
    if contact_id is None and a.get("contact"):
        found = await find_contacts(ctx.session, a["contact"], 1)
        contact_id = found[0].id if found else None
    it = await log_interaction(
        ctx.session, contact_id, a["summary"], channel=a.get("channel", "note"), direction=a.get("direction", "in"),
        follow_up_at=_date(a.get("follow_up"), ctx.timezone),
    )
    return ToolResult(True, "سابقه پیگیری ثبت شد", {"id": str(it.id)}, verified=True)


async def project_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = (await ctx.session.execute(select(Project).where(Project.status != "archived"))).scalars().all()
    if not rows:
        return ToolResult(True, "پروژه‌ای ثبت نشده است.")
    return ToolResult(True, "\n".join(f"- {p.name} [{p.status}] (id={p.id})" for p in rows))


TOOLS = [
    Tool("task_create", "ساخت تسک جدید برای ایجنت، کارفرما یا یک همکار. تاریخ مهلت شمسی مانند 1404/08/20 14:00.",
         obj({"title": s("عنوان"), "description": s("شرح"), "priority": s("low|normal|high|urgent"),
              "assignee": s("نام همکار (اختیاری)"), "assignee_type": s("agent|owner|contact"), "due": s("مهلت شمسی")},
             ["title"]), task_create, "tasks", category="routine_change", action="create", label="ساخت تسک"),
    Tool("task_update", "به‌روزرسانی تسک (وضعیت: todo|in_progress|waiting|done|cancelled).",
         obj({"id": s("شناسه تسک"), "title": s("عنوان"), "description": s("شرح"), "status": s("وضعیت"),
              "priority": s("اولویت"), "due": s("مهلت شمسی")}, ["id"]), task_update, "tasks",
         category="routine_change", action="update", label="ویرایش تسک"),
    Tool("task_list", "فهرست تسک‌های باز (یا با وضعیت مشخص).", obj({"status": s("وضعیت"), "limit": i("حداکثر")}),
         task_list, "tasks", label="فهرست تسک‌ها"),
    Tool("routine_create",
         "ساخت روتین زمان‌بندی‌شده یا رویدادمحور. cron پنج‌بخشی به وقت محلی (مثلاً '0 9 * * 6' = شنبه‌ها ۹ صبح).",
         obj({"name": s("نام"), "instruction": s("دستور کامل برای اجرای هر بار"), "description": s("شرح"),
              "trigger_type": s("cron|interval|once|event"), "cron": s("عبارت cron"),
              "interval_seconds": i("فاصله ثانیه"), "run_at": s("زمان شمسی برای once"),
              "event_filter": {"type": "object", "description": "فیلتر رویداد: source, chat_ids, keywords"},
              "time_sensitive": {"type": "boolean", "description": "زمان‌حساس"}}, ["name", "instruction"]),
         routine_create, "routines", category="routine_change", action="create", label="ساخت روتین"),
    Tool("routine_list", "فهرست روتین‌ها و وضعیت آن‌ها.", obj({}), routine_list, "routines", label="فهرست روتین‌ها"),
    Tool("routine_set_state", "توقف/ادامه/غیرفعال/فعال کردن روتین.",
         obj({"id": s("شناسه"), "action": s("pause|resume|disable|enable")}, ["id", "action"]), routine_set_state,
         "routines", category="routine_change", action="update", label="تغییر وضعیت روتین"),
    Tool("contact_search", "جست‌وجوی همکار/مخاطب با نام، یوزرنیم تلگرام، تلفن یا شرکت.",
         obj({"query": s("عبارت"), "limit": i("حداکثر")}, ["query"]), contact_search, "crm", label="جست‌وجوی مخاطب"),
    Tool("contact_upsert", "ساخت یا به‌روزرسانی پروفایل همکار/مخاطب.",
         obj({"id": s("شناسه برای ویرایش"), "name": s("نام"), "kind": s("colleague|contact|client|vendor"),
              "role_title": s("سمت"), "company": s("شرکت"), "phone": s("تلفن"), "email": s("ایمیل"),
              "telegram_username": s("یوزرنیم تلگرام"), "notes": s("یادداشت")}, ["name"]), contact_upsert, "crm",
         label="ذخیره مخاطب"),
    Tool("crm_log_interaction", "ثبت سابقه تماس/پیگیری با یک مخاطب (با امکان تاریخ پیگیری بعدی).",
         obj({"contact": s("نام مخاطب"), "contact_id": s("شناسه"), "summary": s("خلاصه"),
              "channel": s("telegram|call|meeting|email|note"), "direction": s("in|out"),
              "follow_up": s("تاریخ پیگیری شمسی")}, ["summary"]), crm_log_interaction, "crm", label="ثبت پیگیری"),
    Tool("project_list", "فهرست پروژه‌ها.", obj({}), project_list, "crm", label="فهرست پروژه‌ها"),
]
