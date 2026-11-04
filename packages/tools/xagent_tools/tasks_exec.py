"""اجرای قطعی تسک‌های ایجنت و هماهنگی Task↔Run.

سه نوع تسک:
- human: فقط پیگیری و یادآوری؛ هرگز بدون تغییر صریح نوع به اجرای خودکار تبدیل نمی‌شود.
- agent: در start_at (یا due_at) به‌صورت یک Run مستقل در صف قرار می‌گیرد (کلید یکتای نوبت، بدون اجرای تکراری).
- dependent: پس از done شدن تسک depends_on_id اجرا می‌شود.

وضعیت تسک بر اساس نتیجه واقعی Run (نه ابزار مدل) نهایی می‌شود و در میز کار گزارش می‌شود.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus, inbox
from xagent_core.db import utcnow
from xagent_core.models import Conversation, Run, Task
from xagent_core.text import truncate

MISSED_GRACE = timedelta(hours=1)


async def start_task_run(session: AsyncSession, t: Task, slot: datetime | None = None,
                         requested_by: str = "task") -> Run | None:
    """ساخت Run برای تسک (یکتا برای هر موعد). None یعنی این موعد قبلاً اجرا شده است."""
    key = f"task:{t.id}:{(slot or utcnow()).isoformat()}" if slot else None
    if key and (await session.execute(select(Run.id).where(Run.dedupe_key == key))).first():
        return None
    conv = Conversation(title=f"تسک: {t.title[:50]}", channel="web", task_id=t.id)
    session.add(conv)
    await session.flush()
    run = Run(conversation_id=conv.id, task_id=t.id, trigger="api", dedupe_key=key,
              input=f"تسک «{t.title}» را انجام بده.\n{t.description or ''}".strip(),
              state={"trusted": True, "principal": "owner", "requested_by": requested_by, "task_id": str(t.id)})
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
    except IntegrityError:
        return None
    t.status = "in_progress"
    t.last_run_id = run.id
    return run


async def due_tasks(session: AsyncSession, now: datetime) -> list[tuple[Task, datetime]]:
    """تسک‌های ایجنت که موعدشان رسیده + تسک‌های وابسته‌ای که پیش‌نیازشان انجام شده است."""
    from sqlalchemy import func

    rows = (await session.execute(select(Task).where(
        Task.kind == "agent", Task.status == "todo",
        func.coalesce(Task.start_at, Task.due_at) <= now,
    ).with_for_update(skip_locked=True).limit(50))).scalars().all()
    out = [(t, t.start_at or t.due_at) for t in rows]
    deps = (await session.execute(select(Task).where(
        Task.kind == "dependent", Task.status == "todo", Task.depends_on_id.is_not(None),
    ).with_for_update(skip_locked=True).limit(50))).scalars().all()
    for t in deps:
        parent = await session.get(Task, t.depends_on_id)
        if parent is not None and parent.status == "done" and (t.start_at is None or t.start_at <= now):
            out.append((t, parent.completed_at or now))
    return out


async def settle_task(session: AsyncSession, run: Run, telegram: Any = None) -> bool:
    """نهایی‌سازی قطعی تسک از روی نتیجه Run (یک‌بار برای هر وضعیت)."""
    if run.task_id is None or run.parent_run_id is not None or run.status in ("queued", "running", "waiting_approval"):
        return False
    st = dict(run.state or {})
    if st.get("task_settled") == run.status:
        return False
    t = await session.get(Task, run.task_id)
    if t is None:
        return False
    t.last_run_id = run.id
    link = f"/chat/{run.conversation_id}" if run.conversation_id else "/tasks"
    if run.status == "succeeded":
        t.status, t.completed_at = "done", utcnow()
        t.result_summary = truncate(run.result or "", 1500)
        await inbox.post(session, kind="report", severity="info", title=f"تسک «{t.title}» انجام شد",
                         body=t.result_summary or "", run_id=run.id, conversation_id=run.conversation_id, link=link,
                         dedupe_key=f"task:{t.id}:{run.id}:done", data={"task_id": str(t.id)}, telegram=telegram)
    elif run.status == "paused":
        t.status = "waiting"
        t.result_summary = truncate(f"نیمه‌کاره متوقف شد: {run.stop_reason or ''}", 500)
        await inbox.post(session, kind="long_task", severity="important", title=f"تسک «{t.title}» نیمه‌کاره ماند",
                         body=t.result_summary, run_id=run.id, conversation_id=run.conversation_id, link=link,
                         dedupe_key=f"task:{t.id}:{run.id}:paused", telegram=telegram)
    else:
        t.status = "failed"
        t.result_summary = truncate(run.error or run.result or run.status, 1500)
        await inbox.post(session, kind="error", severity="important", title=f"تسک «{t.title}» انجام نشد",
                         body=t.result_summary, run_id=run.id, conversation_id=run.conversation_id, link=link,
                         dedupe_key=f"task:{t.id}:{run.id}:failed", telegram=telegram)
    st["task_settled"] = run.status
    run.state = st
    await bus.publish("task.changed", {"id": str(t.id), "action": "settled", "status": t.status})
    return True


def _task_uuid(v: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(v)) if v else None
    except ValueError:
        return None
