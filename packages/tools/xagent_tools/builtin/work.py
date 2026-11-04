"""ابزارهای کاری: وظایف، روتین‌ها و CRM."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import utcnow
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
def _routine_category(a: dict[str, Any]) -> str:
    """مأموریتی که پاسخ‌گویی بیرونی دارد یک‌بار با کارت «ارسال پیام بیرونی» تأیید می‌شود (نه در هر نوبت)."""
    return "external_message" if (a.get("authorization") or {}).get("reply") else "routine_change"


async def _approver(ctx: ToolContext) -> str:
    from xagent_core.models import Approval

    if ctx.run_id and ctx.extra.get("tool_call_id"):
        ap = (await ctx.session.execute(select(Approval).where(
            Approval.run_id == ctx.run_id, Approval.tool_call_id == ctx.extra["tool_call_id"],
            Approval.category == "external_message", Approval.status == "approved"))).scalars().first()
        if ap is not None:
            return f"approval:{ap.decided_by or 'owner'}"
    return "harness:auto"


async def routine_create(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from xagent_core.models import Mission
    from xagent_core.settings_store import TelegramSettings, get_setting

    from .. import missions
    from ..lifecycle import claim_occurrence
    from ..schedule import apply_window
    from ..watch import normalize_watch

    intent = ctx.extra.get("mission_intent") or {}
    if ctx.extra.get("principal") not in ("owner", None) or not ctx.trusted_owner:
        return ToolResult(False, "ساخت روتین فقط در اجرای مستقیم کارفرما مجاز است.")
    interval = a.get("interval_seconds") or (int(float(a["interval_minutes"]) * 60) if a.get("interval_minutes") else None)
    interval = interval or intent.get("interval_seconds")
    trigger = a.get("trigger_type") or ("interval" if interval else ("cron" if (a.get("cron") or intent.get("cron")) else "once"))
    try:
        watch = normalize_watch(a.get("watch")) if a.get("watch") else (
            {"source": "telegram", "chats": [], "private_only": True, "include_bots": False}
            if intent.get("watch_source") == "telegram" else {})
    except ValueError as e:
        return ToolResult(False, f"برنامه پایش نامعتبر: {e}")
    r = Routine(
        name=a["name"][:160],
        description=a.get("description", ""),
        instruction=a["instruction"],
        trigger_type=trigger,
        cron_expr=a.get("cron") or intent.get("cron"),
        interval_seconds=interval,
        run_at=_date(a.get("run_at"), ctx.timezone),
        event_filter=a.get("event_filter") or {},
        timezone=ctx.timezone,
        time_sensitive=bool(a.get("time_sensitive", False)),
        created_by="agent",
        status="active",
        run_count=0,
        watch=watch,
        watch_state={},
        max_cost_usd=a.get("max_cost_usd"),
        budget_usd=a.get("budget_usd"),
        report_policy=a.get("report_policy") or "important",
    )
    duration = a.get("duration_minutes") or intent.get("duration_minutes")
    end_at = _date(a.get("end_at"), ctx.timezone) if a.get("end_at") else None
    apply_window(r, duration_minutes=duration, end_at=end_at, max_runs=a.get("max_runs") or intent.get("max_runs"))
    err = validate_routine(r)
    if err:
        return ToolResult(False, f"روتین نامعتبر: {err}")
    auth = a.get("authorization") or {}
    if auth.get("reply") and watch.get("source") != "telegram":
        return ToolResult(False, "پاسخ‌گویی خودکار فقط برای پایش تلگرام پشتیبانی می‌شود.")
    mission = None
    if watch:
        tg = await get_setting(ctx.session, "telegram", TelegramSettings)
        scope = missions.build_scope(watch, auth, tg.allowlist)
        if scope["telegram"].get("reply") and not scope["telegram"].get("reply_to"):
            return ToolResult(False, "برای پاسخ‌گویی خودکار باید گفتگوهای مجاز (reply_to) مشخص باشند یا Allowlist تلگرام "
                                     "تنظیم شده باشد. از کارفرما بپرس به چه کسانی پاسخ داده شود.")
        if scope["telegram"].get("reply") and "external_message" not in (ctx.extra.get("approved") or set()):
            return ToolResult(False, "مجوز پاسخ‌گویی تأیید نشده است.")
        mission = Mission(
            title=r.name, owner=str(ctx.extra.get("requested_by") or "owner")[:64], created_by_run=ctx.run_id,
            status="active", scope=scope, max_cost_usd=a.get("budget_usd"), max_runs=r.max_runs,
            max_actions=int(auth.get("max_replies") or 50) if scope["telegram"].get("reply") else None,
            expires_at=r.end_at or (utcnow() + timedelta(days=7)), approved_by=await _approver(ctx),
            approved_at=utcnow(), actions_count=0,
        )
        ctx.session.add(mission)
        await ctx.session.flush()
        r.mission_id = mission.id
    ctx.session.add(r)
    await ctx.session.flush()
    r.next_run_at = compute_next_run(r, first=True)
    run_now = a.get("run_now")
    if run_now is None:
        run_now = bool(intent.get("immediate")) and trigger == "interval"
    first_run = None
    blocked = None
    if run_now and trigger in ("interval", "cron"):
        if await bus.is_stop_all():
            blocked = "توقف اضطراری فعال است؛ اجرای فوری انجام نشد"
        else:
            now = utcnow()
            slot = r.starts_at if trigger == "interval" and r.starts_at and r.starts_at <= now else now
            first_run = await claim_occurrence(ctx.session, r, slot)
            r.next_run_at = compute_next_run(r, now)
            if first_run is not None:
                ctx.extra.setdefault("enqueue_runs", []).append(first_run)
    await ctx.session.flush()
    await bus.publish("routine.changed", {"id": str(r.id), "action": "create"})
    # بازخوانی از دیتابیس برای اعتبارسنجی ثبت (نه اعتماد به مقادیر حافظه)
    saved = (await ctx.session.execute(select(Routine).where(Routine.id == r.id))).scalar_one()
    await ctx.session.refresh(saved)
    tz = ctx.timezone
    lines = [
        f"✅ روتین «{saved.name}» ثبت و از دیتابیس بازخوانی شد (شناسه {saved.id}).",
        f"وضعیت: {saved.status} | نوع: {saved.trigger_type}"
        + (f" هر {saved.interval_seconds // 60} دقیقه" if saved.interval_seconds else "")
        + (f" | cron {saved.cron_expr}" if saved.cron_expr else ""),
        f"شروع: {jalali_str(saved.starts_at, tz) if saved.starts_at else '-'} | پایان: "
        f"{jalali_str(saved.end_at, tz) if saved.end_at else 'بدون مهلت'} | سقف نوبت: {saved.max_runs or 'نامحدود'}"
        f" (قرارداد: بازه نیمه‌باز، شامل اجرای فوری)",
        f"اجرای فوری: {'ثبت شد (نوبت ۱)' if first_run else (blocked or 'خیر')} | موعد بعدی: "
        f"{jalali_str(saved.next_run_at, tz) if saved.next_run_at else '-'}",
    ]
    if watch:
        lines.append(f"پیش‌بررسی قطعی: {watch['source']} (نوبت بدون رویداد جدید = بدون فراخوانی مدل)")
    if mission is not None:
        lines.append(f"مجوز مأموریت: {missions.describe_scope(mission.scope)} | انقضا: "
                     f"{jalali_str(mission.expires_at, tz) if mission.expires_at else '-'} | تأیید: {mission.approved_by}")
    data = {"id": str(saved.id), "status": saved.status, "next_run_at": saved.next_run_at.isoformat() if saved.next_run_at else None,
            "end_at": saved.end_at.isoformat() if saved.end_at else None, "max_runs": saved.max_runs,
            "run_count": saved.run_count, "first_run_id": str(first_run) if first_run else None,
            "mission_id": str(mission.id) if mission else None}
    ok = saved.status == "active" and (saved.next_run_at is not None or first_run is not None)
    return ToolResult(ok, "\n".join(lines), data, verified=ok)


async def routine_list(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = (await ctx.session.execute(select(Routine).order_by(Routine.created_at.desc()).limit(30))).scalars().all()
    if not rows:
        return ToolResult(True, "روتینی تعریف نشده است.")
    labels = {"active": "فعال", "paused": "متوقف موقت", "disabled": "غیرفعال", "completed": "تکمیل‌شده",
              "failed": "ناموفق", "cancelled": "لغوشده", "expired": "منقضی"}
    lines = []
    for r in rows:
        nxt = jalali_str(r.next_run_at, ctx.timezone) if r.next_run_at else "-"
        end = jalali_str(r.end_at, ctx.timezone) if r.end_at else "-"
        lines.append(f"- {r.name} [{labels.get(r.status, r.status)}|{r.trigger_type}] بعدی: {nxt} | پایان: {end} | "
                     f"نوبت {r.run_count}/{r.max_runs or '∞'} | موفق {r.success_count} بی‌تغییر {r.no_change_count} "
                     f"ناموفق {r.failure_count} | آخرین: {r.last_status or '-'} (id={r.id})")
    return ToolResult(True, "\n".join(lines))


async def routine_set_state(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from ..lifecycle import finalize_routine
    from ..schedule import TERMINAL_STATUSES

    r = await ctx.session.get(Routine, _uuid(a.get("id")))
    if r is None:
        return ToolResult(False, "روتین یافت نشد.")
    action = a["action"]
    if r.status in TERMINAL_STATUSES and action != "cancel":
        return ToolResult(False, f"روتین «{r.name}» پایان یافته ({r.status})؛ برای اجرای دوباره روتین جدید بساز.")
    if action == "pause":
        r.paused, r.status = True, "paused"
    elif action in ("resume", "enable"):
        r.paused, r.enabled, r.status = False, True, "active"
        r.circuit_state = "closed"
        r.consecutive_failures = 0
        r.next_run_at = compute_next_run(r)
    elif action == "disable":
        r.enabled, r.status = False, "disabled"
    elif action == "cancel":
        await finalize_routine(ctx.session, r, utcnow(), "owner_cancelled", status="cancelled")
    else:
        return ToolResult(False, "اقدام نامعتبر")
    await ctx.session.flush()
    await bus.publish("routine.changed", {"id": str(r.id), "action": action})
    return ToolResult(True, f"روتین «{r.name}»: {action} (وضعیت: {r.status})", verified=True)


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
         "ساخت روتین/مأموریت مستقل: زمان‌بندی‌شده (interval/cron/once) یا رویدادمحور. برای «هر N دقیقه … تا M دقیقه» "
         "از interval_seconds + duration_minutes + run_now=true استفاده کن؛ بررسی اول فوراً انجام می‌شود و در پایان مهلت "
         "خودکار تمام و گزارش می‌شود. برای پایش تلگرام watch={source:'telegram'} (بدون هزینه مدل وقتی خبری نیست). "
         "پاسخ‌گویی خودکار: authorization={reply:true, reply_to:[...]} (یک کارت اجازه برای کل مأموریت).",
         obj({"name": s("نام"), "instruction": s("دستور کامل هر نوبت (برای پایش: با پیام جدید چه کنم)"),
              "description": s("شرح"), "trigger_type": s("interval|cron|once|event"), "cron": s("عبارت cron به وقت محلی"),
              "interval_seconds": i("فاصله نوبت‌ها به ثانیه (حداقل ۶۰)"), "interval_minutes": {"type": "number"},
              "duration_minutes": {"type": "number", "description": "مدت مأموریت از الان (دقیقه)"},
              "end_at": s("زمان پایان شمسی (اختیاری)"), "max_runs": i("سقف نوبت‌ها شامل اجرای فوری"),
              "run_now": {"type": "boolean", "description": "اجرای فوری نوبت اول"},
              "run_at": s("زمان شمسی برای once"),
              "watch": {"type": "object", "description": "پیش‌بررسی قطعی: {source:'telegram', chats:[...], private_only} یا {source:'url', url}"},
              "authorization": {"type": "object", "description": "{reply:true, reply_to:[@user|chat_id], reply_instructions, max_replies, out_of_scope:'report'|'ask'}"},
              "budget_usd": {"type": "number", "description": "سقف هزینه کل مأموریت"},
              "max_cost_usd": {"type": "number", "description": "سقف هزینه هر نوبت"},
              "report_policy": s("all|important|errors|off"),
              "event_filter": {"type": "object", "description": "فیلتر رویداد: source, chat_ids, keywords"},
              "time_sensitive": {"type": "boolean", "description": "زمان‌حساس"}}, ["name", "instruction"]),
         routine_create, "routines", category="routine_change", action="create", label="ساخت روتین/مأموریت",
         category_fn=_routine_category),
    Tool("routine_list", "فهرست روتین‌ها با وضعیت چرخه حیات، نوبت‌ها و نتیجه.", obj({}), routine_list, "routines",
         label="فهرست روتین‌ها"),
    Tool("routine_set_state", "توقف موقت/ادامه/غیرفعال/فعال/لغو روتین.",
         obj({"id": s("شناسه"), "action": s("pause|resume|disable|enable|cancel")}, ["id", "action"]),
         routine_set_state, "routines", category="routine_change", action="update", label="تغییر وضعیت روتین"),
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
