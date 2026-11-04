"""یادگیری پس از هر اجرا (با قابلیت Undo/Rollback).

- ثبت حافظه رویدادی (همیشه، بدون LLM)
- ثبت تجربه تاییدشده یا تلاش ناموفق در حافظه تجربی (برداری)
- ارزیابی دوره آزمایشی یادگیری‌هایی که در این اجرا استفاده شدند → Auto-Rollback در صورت پسرفت
- انتقال نکات مهم کارفرما از حافظه چت به حافظه پایدار (قطعی)
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.models import Employee, MemoryRevision, Message, Run, Skill
from xagent_core.settings_store import DreamSettings, get_setting
from xagent_core.text import truncate
from xagent_memory.revisions import observe_probation
from xagent_memory.store import record_episode, record_experience
from xagent_memory.working import process_owner_message

EPISODE_STATUS = {"succeeded": "success", "failed": "failed", "stopped": "stopped", "paused": "stopped"}


def memory_scope_for(run: Run, employee: Any) -> str:
    """فضای نام حافظه اجرا: کارمند خصوصی ← employee:<id>؛ مدیر و کارمند مشترک ← global."""
    if run.employee_id is not None and employee is not None and getattr(employee, "memory_scope", "shared") == "private":
        return f"employee:{employee.id}"
    return "global"


async def learn_from_run(session: AsyncSession, run: Run, state: dict[str, Any], status: str, trusted: bool) -> None:
    route = state.get("route") or {}
    tools_used: list[str] = state.get("tools_used", [])
    duration = int(((run.finished_at or run.started_at) - run.started_at).total_seconds() * 1000) if run.started_at else 0
    kind = "routine" if run.routine_id else ("task" if run.task_id else "chat")
    if route.get("path") == "command":
        kind = "system"
    employee = await session.get(Employee, run.employee_id) if run.employee_id else None
    scope = memory_scope_for(run, employee)
    await record_episode(
        session,
        scope=scope,
        kind=kind,
        title=truncate(run.input.replace("\n", " "), 250) or "اجرای ایجنت",
        summary=truncate(run.result or run.error or "", 1500),
        run_id=run.id,
        task_id=run.task_id,
        routine_id=run.routine_id,
        tools=tools_used,
        tokens_in=run.tokens_in,
        tokens_out=run.tokens_out,
        cost_usd=run.cost_usd,
        duration_ms=duration,
        status=EPISODE_STATUS.get(status, "failed"),
        meta={"trigger": run.trigger, "route": route.get("path"), "complexity": route.get("complexity"),
              "steps": run.steps_count, "llm_calls": run.llm_calls},
    )
    if route.get("path") == "command":
        return

    dream = await get_setting(session, "dream", DreamSettings)
    tokens = run.tokens_in + run.tokens_out
    validation = state.get("validation") or {}
    success = status == "succeeded" and validation.get("passed", True)

    # یادگیری‌های آزمایشی دخیل در این اجرا (تجربه‌های بازیابی‌شده)
    exp_ids = [uuid.UUID(x) for x in state.get("experience_ids", [])]
    skill_ids = [uuid.UUID(x) for x in state.get("skill_ids", [])]
    for sid in skill_ids:
        sk = await session.get(Skill, sid)
        if sk is not None:
            n = sk.success_count + sk.failure_count
            sk.avg_tokens = (sk.avg_tokens * n + tokens) / (n + 1)
            if success:
                sk.success_count += 1
            else:
                sk.failure_count += 1
    exp_ids = exp_ids + skill_ids
    if exp_ids:
        rev_ids = (
            await session.execute(
                select(MemoryRevision.id).where(
                    MemoryRevision.entity_id.in_(exp_ids), MemoryRevision.status == "probationary"
                )
            )
        ).scalars().all()
        if rev_ids:
            purged = await observe_probation(
                session, list(rev_ids), success, tokens, dream.probation_runs, dream.regression_token_ratio
            )
            state["purged_revisions"] = purged

    # حافظه تجربی: فقط برای کارهایی که ابزار استفاده کردند (کار واقعی) یا روتین‌ها
    if tools_used and (route.get("complexity") == "complex" or run.routine_id):
        evidence = state.get("evidence", [])
        approach = " → ".join(
            f"{e['tool']}({'✓' if e['ok'] else '✗'})" for e in evidence[:20]
        )
        plan_steps = " | ".join(s.get("title", "") for s in (state.get("plan") or {}).get("steps", [])[:8])
        _, rev = await record_experience(
            session,
            signature=truncate(run.input, 1500),
            title=truncate((state.get("goal") or {}).get("goal") or run.input, 250),
            approach=f"برنامه: {plan_steps}\nمسیر ابزار: {approach}",
            tool_sequence=tools_used[:30],
            outcome="verified_success" if success else "failed_attempt",
            tokens_used=tokens,
            duration_ms=duration,
            steps=run.steps_count,
            routine_id=run.routine_id,
            run_id=run.id,
            origin="routine" if run.routine_id else "chat",
            scope=scope,
        )
        if rev:
            state.setdefault("new_revisions", []).append(str(rev))

    # حافظه چت → حافظه پایدار (فقط پیام مستقیم کارفرما)
    if trusted and run.trigger in ("chat", "voice", "telegram") and run.conversation_id:
        last_user = (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == run.conversation_id, Message.role == "user")
                .order_by(Message.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_user is not None and not (last_user.meta or {}).get("memory_processed"):
            cands = await process_owner_message(
                session, last_user.content, run.conversation_id, last_user.id, trusted=True, run_id=run.id,
                origin=run.trigger,
            )
            last_user.meta = {**(last_user.meta or {}), "memory_processed": True,
                              "memory_candidates": [{"layer": c.target_layer, "status": c.status} for c in cands]}
    await session.flush()
