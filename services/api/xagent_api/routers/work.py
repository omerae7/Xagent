"""وظایف (لیست/کانبان/تقویم)، روتین‌ها (ایجاد/ویرایش/Pause/Resume/Test Run) و CRM."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.db import utcnow
from xagent_core.models import (
    Contact,
    Conversation,
    CRMInteraction,
    Mission,
    Project,
    Routine,
    Run,
    Task,
    User,
)
from xagent_core.text import normalize_fa
from xagent_crm import contact_overview, log_interaction, upsert_contact
from xagent_tools.lifecycle import active_run, claim_occurrence, finalize_routine, routine_conversation
from xagent_tools.schedule import (
    TERMINAL_STATUSES,
    apply_window,
    compute_next_run,
    display_state,
    validate_routine,
)
from xagent_tools.watch import normalize_watch

from ..deps import current_user, get_session, owner_user, parse_uuid, ser

router = APIRouter(prefix="/api", tags=["work"])


# ───────────── وظایف ─────────────
class TaskIn(BaseModel):
    title: str | None = Field(default=None, max_length=256)
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(todo|in_progress|waiting|done|cancelled)$")
    priority: str | None = Field(default=None, pattern="^(low|normal|high|urgent)$")
    assignee_type: str | None = Field(default=None, pattern="^(agent|contact|owner)$")
    assignee_contact_id: str | None = None
    project_id: str | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    tags: list[str] | None = None
    sort_order: float | None = None
    run_now: bool = False


def _apply_task(t: Task, body: TaskIn) -> None:
    for f in ("title", "description", "status", "priority", "assignee_type", "start_at", "due_at", "tags", "sort_order"):
        v = getattr(body, f)
        if v is not None:
            setattr(t, f, v)
    if body.assignee_contact_id is not None:
        t.assignee_contact_id = parse_uuid(body.assignee_contact_id) if body.assignee_contact_id else None
    if body.project_id is not None:
        t.project_id = parse_uuid(body.project_id) if body.project_id else None
    if t.status == "done" and t.completed_at is None:
        t.completed_at = utcnow()
    if t.status != "done":
        t.completed_at = None


async def _run_task(session: AsyncSession, t: Task) -> Run:
    conv = Conversation(title=f"تسک: {t.title[:50]}", channel="web", task_id=t.id)
    session.add(conv)
    await session.flush()
    run = Run(conversation_id=conv.id, task_id=t.id, trigger="api",
              input=f"تسک «{t.title}» را انجام بده.\n{t.description or ''}\n(شناسه تسک: {t.id} — در پایان وضعیت تسک را به‌روزرسانی کن)",
              state={"trusted": True, "principal": "owner", "requested_by": "task"})
    session.add(run)
    t.status = "in_progress"
    await session.flush()
    return run


@router.get("/tasks")
async def list_tasks(status: str | None = None, q: str | None = None, project_id: str | None = None,
                     date_from: datetime | None = None, date_to: datetime | None = None,
                     limit: int = Query(300, le=1000), _: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    stmt = select(Task)
    if status:
        stmt = stmt.where(Task.status.in_(status.split(",")))
    if q:
        stmt = stmt.where(func.xa_normalize(Task.title).like(f"%{normalize_fa(q)}%"))
    if project_id:
        stmt = stmt.where(Task.project_id == parse_uuid(project_id))
    if date_from:
        stmt = stmt.where(or_(Task.due_at >= date_from, Task.start_at >= date_from))
    if date_to:
        stmt = stmt.where(or_(Task.due_at <= date_to, Task.start_at <= date_to))
    rows = (await session.execute(stmt.order_by(Task.sort_order, Task.due_at.asc().nulls_last(),
                                                Task.created_at.desc()).limit(limit))).scalars().all()
    contacts = {c.id: c.name for c in (await session.execute(
        select(Contact).where(Contact.id.in_({t.assignee_contact_id for t in rows if t.assignee_contact_id})))).scalars()}
    return {"items": [ser(t, extra={"assignee_name": contacts.get(t.assignee_contact_id)}) for t in rows]}


@router.post("/tasks")
async def create_task(body: TaskIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    if not body.title:
        raise HTTPException(400, "عنوان لازم است")
    t = Task(title=body.title, created_by="owner")
    _apply_task(t, body)
    session.add(t)
    await session.flush()
    run = await _run_task(session, t) if body.run_now else None
    await session.commit()
    if run:
        await bus.enqueue_run(run.id)
    await bus.publish("task.changed", {"id": str(t.id), "action": "create"})
    return ser(t, extra={"run_id": run.id if run else None})


@router.patch("/tasks/{tid}")
async def update_task(tid: str, body: TaskIn, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, parse_uuid(tid))
    if t is None:
        raise HTTPException(404, "تسک یافت نشد")
    _apply_task(t, body)
    run = await _run_task(session, t) if body.run_now else None
    await session.commit()
    if run:
        await bus.enqueue_run(run.id)
    await bus.publish("task.changed", {"id": str(t.id), "action": "update"})
    return ser(t, extra={"run_id": run.id if run else None})


@router.delete("/tasks/{tid}")
async def delete_task(tid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    t = await session.get(Task, parse_uuid(tid))
    if t is None:
        raise HTTPException(404, "تسک یافت نشد")
    await audit(session, user.username, "task.delete", str(t.id), {"title": t.title})
    await session.delete(t)
    await bus.publish("task.changed", {"id": tid, "action": "delete"})
    return {"ok": True}


# ───────────── روتین‌ها ─────────────
class RoutineIn(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = None
    instruction: str | None = None
    trigger_type: str | None = Field(default=None, pattern="^(cron|interval|event|once)$")
    cron_expr: str | None = None
    interval_seconds: int | None = Field(default=None, ge=60)
    run_at: datetime | None = None
    event_filter: dict[str, Any] | None = None
    timezone: str | None = None
    enabled: bool | None = None
    time_sensitive: bool | None = None
    max_steps: int | None = Field(default=None, ge=1, le=200)
    max_cost_usd: float | None = Field(default=None, ge=0, le=50)
    # چرخه حیات مأموریت
    starts_at: datetime | None = None
    end_at: datetime | None = None
    duration_minutes: float | None = Field(default=None, gt=0, le=60 * 24 * 90)
    max_runs: int | None = Field(default=None, ge=1, le=100_000)
    budget_usd: float | None = Field(default=None, ge=0, le=500)
    watch: dict[str, Any] | None = None
    report_policy: str | None = Field(default=None, pattern="^(all|important|errors|off)$")
    run_now: bool = False


def _apply_routine(r: Routine, body: RoutineIn) -> None:
    for f in ("name", "description", "instruction", "trigger_type", "cron_expr", "interval_seconds", "run_at",
              "event_filter", "timezone", "time_sensitive", "max_steps", "max_cost_usd", "budget_usd",
              "report_policy"):
        v = getattr(body, f)
        if v is not None:
            setattr(r, f, v)
    if body.watch is not None:
        try:
            r.watch = normalize_watch(body.watch)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
    if body.enabled is not None and r.status in ("active", "disabled"):
        r.enabled = body.enabled
        r.status = "active" if body.enabled else "disabled"


async def ser_routine(session: AsyncSession, r: Routine) -> dict[str, Any]:
    active = await active_run(session, r.id)
    extra: dict[str, Any] = {
        "display_state": display_state(r, active.status if active else None),
        "active_run_id": active.id if active else None,
        "remaining_runs": max(0, r.max_runs - (r.run_count or 0)) if r.max_runs else None,
        "mission": None,
    }
    if r.mission_id:
        m = await session.get(Mission, r.mission_id)
        if m is not None:
            extra["mission"] = {"id": str(m.id), "status": m.status, "scope": m.scope, "title": m.title,
                                "expires_at": m.expires_at, "actions_count": m.actions_count,
                                "max_actions": m.max_actions}
    return ser(r, extra=extra)


async def _run_now(session: AsyncSession, r: Routine) -> tuple[Any, str | None]:
    """اجرای فوری اولیه به‌عنوان نوبت اول همان قرارداد شمارش (زمان‌بند آن را تکرار نمی‌کند)."""
    if r.trigger_type not in ("interval", "cron"):
        return None, "trigger"
    if await bus.is_stop_all():
        return None, "stop_all"
    now = utcnow()
    slot = r.starts_at if r.trigger_type == "interval" and r.starts_at and r.starts_at <= now else now
    run_id = await claim_occurrence(session, r, slot)
    r.next_run_at = compute_next_run(r, now)
    return run_id, None if run_id else "duplicate"


@router.get("/routines")
async def list_routines(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Routine).order_by(Routine.created_at.desc()))).scalars().all()
    return {"items": [await ser_routine(session, r) for r in rows]}


@router.get("/routines/{rid}")
async def get_routine(rid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(Routine, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "روتین یافت نشد")
    return await ser_routine(session, r)


@router.post("/routines")
async def create_routine(body: RoutineIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    if not body.name or not body.instruction:
        raise HTTPException(400, "نام و دستور روتین لازم است")
    r = Routine(name=body.name, instruction=body.instruction, created_by="owner", status="active", run_count=0,
                watch={}, watch_state={})
    _apply_routine(r, body)
    apply_window(r, start=body.starts_at, duration_minutes=body.duration_minutes, end_at=body.end_at,
                 max_runs=body.max_runs)
    if err := validate_routine(r):
        raise HTTPException(400, err)
    session.add(r)
    await session.flush()
    r.next_run_at = compute_next_run(r, first=True)
    run_id, blocked = (await _run_now(session, r)) if body.run_now else (None, None)
    await audit(session, user.username, "routine.create", str(r.id), {"name": r.name, "run_now": body.run_now})
    await session.commit()
    if run_id:
        await bus.enqueue_run(run_id, "routine")
    await bus.publish("routine.changed", {"id": str(r.id), "action": "create"})
    await session.refresh(r)
    out = await ser_routine(session, r)
    out["run_id"] = str(run_id) if run_id else None
    out["run_now_blocked"] = blocked
    return out


@router.patch("/routines/{rid}")
async def update_routine(rid: str, body: RoutineIn, user: User = Depends(owner_user),
                         session: AsyncSession = Depends(get_session)):
    r = await session.get(Routine, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "روتین یافت نشد")
    _apply_routine(r, body)
    new_window = body.duration_minutes or body.end_at or body.max_runs or body.starts_at
    if new_window:
        if body.duration_minutes and not body.starts_at:
            r.starts_at = utcnow()
        apply_window(r, start=body.starts_at, duration_minutes=body.duration_minutes, end_at=body.end_at,
                     max_runs=body.max_runs)
        if r.status in TERMINAL_STATUSES:  # تمدید مأموریت پایان‌یافته با پنجره جدید
            r.status, r.enabled, r.completed_at, r.completion_reason = "active", True, None, None
    if err := validate_routine(r):
        raise HTTPException(400, err)
    r.next_run_at = compute_next_run(r, first=bool(new_window))
    await audit(session, user.username, "routine.update", str(r.id))
    await bus.publish("routine.changed", {"id": str(r.id), "action": "update"})
    return await ser_routine(session, r)


@router.post("/routines/{rid}/{action}")
async def routine_action(rid: str, action: str, user: User = Depends(owner_user),
                         session: AsyncSession = Depends(get_session)):
    r = await session.get(Routine, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "روتین یافت نشد")
    run = None
    if action in ("pause", "resume", "test") and r.status in TERMINAL_STATUSES:
        raise HTTPException(409, "این مأموریت پایان یافته است؛ برای اجرای دوباره مهلت یا سقف جدید تعیین کنید.")
    if action == "pause":
        r.paused, r.status = True, "paused"
    elif action == "resume":
        r.paused, r.status, r.enabled = False, "active", True
        r.circuit_state = "closed"
        r.consecutive_failures = 0
        r.circuit_open_until = None
        r.last_error = None
        r.next_run_at = compute_next_run(r)
    elif action == "cancel":
        await finalize_routine(session, r, utcnow(), "owner_cancelled", status="cancelled")
    elif action == "test":
        conv = await routine_conversation(session, r)
        run = Run(conversation_id=conv.id, routine_id=r.id, trigger="routine", input=r.instruction,
                  state={"trusted": True, "principal": "mission" if r.mission_id else "owner",
                         "mission_id": str(r.mission_id) if r.mission_id else None,
                         "requested_by": "routine_test", "test_run": True,
                         "max_cost": float(r.max_cost_usd) if r.max_cost_usd else None,
                         "max_steps": r.max_steps})
        session.add(run)
    else:
        raise HTTPException(400, "اقدام نامعتبر")
    await audit(session, user.username, f"routine.{action}", str(r.id))
    await session.commit()
    if run:
        await bus.enqueue_run(run.id, "test")
    await bus.publish("routine.changed", {"id": str(r.id), "action": action})
    out = await ser_routine(session, r)
    out["run_id"] = str(run.id) if run else None
    return out


@router.delete("/routines/{rid}")
async def delete_routine(rid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(Routine, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "روتین یافت نشد")
    await audit(session, user.username, "routine.delete", str(r.id), {"name": r.name})
    await session.delete(r)
    await bus.publish("routine.changed", {"id": rid, "action": "delete"})
    return {"ok": True}


@router.get("/routines/{rid}/runs")
async def routine_runs(rid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Run).where(Run.routine_id == parse_uuid(rid))
                                  .order_by(Run.created_at.desc()).limit(50))).scalars().all()
    return {"items": [ser(r, exclude={"plan", "goal", "state"}) for r in rows]}


# ───────────── CRM ─────────────
class ContactIn(BaseModel):
    kind: str | None = Field(default=None, pattern="^(colleague|contact|client|vendor)$")
    name: str | None = None
    role_title: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    telegram_username: str | None = None
    notes: str | None = None
    tags: list[str] | None = None


class ProjectIn(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(active|on_hold|done|archived)$")
    color: str | None = None
    due_at: datetime | None = None
    member_ids: list[str] | None = None


class InteractionIn(BaseModel):
    contact_id: str | None = None
    project_id: str | None = None
    channel: str = "note"
    direction: str = "out"
    summary: str
    content: str = ""
    follow_up_at: datetime | None = None


@router.get("/crm/contacts")
async def contacts(q: str | None = None, kind: str | None = None, _: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)):
    stmt = select(Contact)
    if kind:
        stmt = stmt.where(Contact.kind == kind)
    if q:
        nq = f"%{normalize_fa(q)}%"
        stmt = stmt.where(or_(func.xa_normalize(Contact.name).like(nq), func.xa_normalize(Contact.company).like(nq),
                              Contact.telegram_username.ilike(nq), Contact.phone.like(nq)))
    rows = (await session.execute(stmt.order_by(Contact.last_contact_at.desc().nulls_last(), Contact.name))).scalars()
    return {"items": [ser(c) for c in rows]}


@router.post("/crm/contacts")
async def create_contact(body: ContactIn, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    if not body.name:
        raise HTTPException(400, "نام لازم است")
    c = Contact(name=body.name)
    session.add(c)
    for f, v in body.model_dump(exclude_none=True).items():
        setattr(c, f, v.lstrip("@") if f == "telegram_username" else v)
    await session.flush()
    await bus.publish("crm.changed", {"id": str(c.id)})
    return ser(c)


@router.patch("/crm/contacts/{cid}")
async def update_contact(cid: str, body: ContactIn, _: User = Depends(owner_user),
                         session: AsyncSession = Depends(get_session)):
    c = await upsert_contact(session, body.model_dump(exclude_none=True), parse_uuid(cid))
    await bus.publish("crm.changed", {"id": str(c.id)})
    return ser(c)


@router.get("/crm/contacts/{cid}")
async def get_contact(cid: str, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    ov = await contact_overview(session, parse_uuid(cid))
    if not ov:
        raise HTTPException(404, "مخاطب یافت نشد")
    return {"contact": ser(ov["contact"]), "interactions": [ser(i) for i in ov["interactions"]],
            "tasks": [ser(t) for t in ov["tasks"]], "projects": [ser(p) for p in ov["projects"]]}


@router.delete("/crm/contacts/{cid}")
async def delete_contact(cid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    c = await session.get(Contact, parse_uuid(cid))
    if c is None:
        raise HTTPException(404, "مخاطب یافت نشد")
    await audit(session, user.username, "crm.contact_delete", str(c.id), {"name": c.name})
    await session.delete(c)
    return {"ok": True}


@router.post("/crm/interactions")
async def add_interaction(body: InteractionIn, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    it = await log_interaction(session, parse_uuid(body.contact_id) if body.contact_id else None, body.summary,
                               body.channel, body.direction, body.content,
                               parse_uuid(body.project_id) if body.project_id else None, body.follow_up_at)
    return ser(it)


@router.get("/crm/followups")
async def followups(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(CRMInteraction).where(
        CRMInteraction.follow_up_at.is_not(None), CRMInteraction.follow_up_done.is_(False))
        .order_by(CRMInteraction.follow_up_at).limit(50))).scalars().all()
    return {"items": [ser(i) for i in rows]}


@router.get("/crm/projects")
async def projects(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Project).order_by(Project.created_at.desc()))).scalars().all()
    counts = dict((await session.execute(select(Task.project_id, func.count()).where(Task.project_id.is_not(None))
                                          .group_by(Task.project_id))).all())
    return {"items": [ser(p, extra={"task_count": counts.get(p.id, 0)}) for p in rows]}


@router.post("/crm/projects")
async def create_project(body: ProjectIn, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    if not body.name:
        raise HTTPException(400, "نام لازم است")
    p = Project(name=body.name)
    for f, v in body.model_dump(exclude_none=True).items():
        setattr(p, f, v)
    session.add(p)
    await session.flush()
    return ser(p)


@router.patch("/crm/projects/{pid}")
async def update_project(pid: str, body: ProjectIn, _: User = Depends(owner_user),
                         session: AsyncSession = Depends(get_session)):
    p = await session.get(Project, parse_uuid(pid))
    if p is None:
        raise HTTPException(404, "پروژه یافت نشد")
    for f, v in body.model_dump(exclude_none=True).items():
        setattr(p, f, v)
    return ser(p)


@router.delete("/crm/projects/{pid}")
async def delete_project(pid: str, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    p = await session.get(Project, parse_uuid(pid))
    if p is None:
        raise HTTPException(404, "پروژه یافت نشد")
    await session.delete(p)
    return {"ok": True}
