"""ساخت کانتکست انتخابی با بودجه‌بندی دقیق توکن.

هرگز کل حافظه یکجا به مدل ارسال نمی‌شود. برنامه‌ریز مشخص می‌کند کدام لایه‌ها لازم‌اند و
هر لایه سقف توکن مستقل دارد. حافظه رویدادی فقط با کوئری مستقیم (ابزار) خوانده می‌شود.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.text import estimate_tokens, truncate

from sqlalchemy import select

from xagent_core.models import Skill

from .embeddings import cosine, hash_embed
from .store import active_rules, best_experiences, facts_for_context
from .working import load_window


@dataclass
class MemoryPlan:
    behavioral: bool = True
    semantic: bool = True
    experiential: bool = False
    working: bool = True
    scopes: list[str] = field(default_factory=lambda: ["global"])


@dataclass
class ContextBudget:
    total: int = 6000
    behavioral: int = 900
    semantic: int = 900
    experiential: int = 700
    working: int = 3000

    @classmethod
    def for_window(cls, context_window: int, complex_task: bool) -> ContextBudget:
        # کانتکست کوچک‌تر برای کارهای ساده = هزینه کمتر
        total = min(context_window // 4, 24000 if complex_task else 8000)
        total = max(total, 2500)
        return cls(
            total=total,
            behavioral=int(total * 0.15),
            semantic=int(total * 0.15),
            experiential=int(total * 0.12),
            working=int(total * 0.5),
        )


@dataclass
class BuiltContext:
    system_sections: list[str]
    history: list[dict[str, str]]
    used_tokens: int
    experience_ids: list[uuid.UUID]
    rule_ids: list[uuid.UUID]
    stats: dict[str, int]
    skill_ids: list[uuid.UUID] = field(default_factory=list)


def _fit(lines: list[str], budget: int) -> tuple[list[str], int]:
    out, used = [], 0
    for line in lines:
        c = estimate_tokens(line)
        if used + c > budget:
            break
        out.append(line)
        used += c
    return out, used


async def build_context(
    session: AsyncSession,
    query: str,
    plan: MemoryPlan,
    budget: ContextBudget,
    conversation_id: uuid.UUID | None = None,
    routine_id: uuid.UUID | None = None,
) -> BuiltContext:
    sections: list[str] = []
    stats: dict[str, int] = {}
    used_total = 0
    rule_ids: list[uuid.UUID] = []
    exp_ids: list[uuid.UUID] = []
    skill_ids: list[uuid.UUID] = []

    if plan.behavioral:
        rules = await active_rules(session, plan.scopes)
        lines = [f"- [{r.priority}] {truncate(r.content, 400)}" for r in rules]
        fitted, used = _fit(lines, budget.behavioral)
        rule_ids = [r.id for r in rules[: len(fitted)]]
        if fitted:
            sections.append("## قوانین رفتاری کارفرما (الزامی؛ به ترتیب اولویت)\n" + "\n".join(fitted))
        stats["behavioral"] = used
        used_total += used

    if plan.semantic:
        agent_facts, owner_facts = await facts_for_context(session, query)
        lines = [f"- {f.key}: {truncate(f.content, 300)}" for f in agent_facts]
        lines2 = [f"- {truncate(f.content, 300)}" for f in owner_facts]
        fitted1, u1 = _fit(lines, budget.semantic // 2)
        fitted2, u2 = _fit(lines2, budget.semantic - u1)
        if fitted1:
            sections.append("## X Agent (هویت و وظایف)\n" + "\n".join(fitted1))
        if fitted2:
            sections.append("## دانسته‌های مرتبط درباره کارفرما و کسب‌وکار\n" + "\n".join(fitted2))
        stats["semantic"] = u1 + u2
        used_total += u1 + u2

    if plan.experiential and query:
        good, bad = await best_experiences(session, query, routine_id=routine_id)
        lines = []
        for e in good:
            lines.append(
                f"- روش تاییدشده «{truncate(e.title, 80)}» (موفق {e.success_count}×، ~{e.tokens_used} توکن): "
                f"{truncate(e.approach, 500)}"
            )
            exp_ids.append(e.id)
        for e in bad:
            lines.append(f"- ⚠️ روش ناموفق قبلی (تکرار نکن): {truncate(e.approach, 250)}")
        fitted, used = _fit(lines, budget.experiential)
        if fitted:
            sections.append("## تجربه‌های قبلی مشابه\n" + "\n".join(fitted))
        stats["experiential"] = used
        used_total += used
        # مهارت‌های تقطیرشده (Skills) مرتبط
        skills = (
            await session.execute(select(Skill).where(Skill.status.in_(["active", "probationary"])).limit(200))
        ).scalars().all()
        if skills:
            qv = hash_embed(query)
            scored = sorted(
                ((cosine(qv, hash_embed(f"{sk.description} {sk.trigger_hint}")), sk) for sk in skills),
                key=lambda x: x[0],
                reverse=True,
            )
            sk_lines = []
            for sim, sk in scored[:2]:
                if sim < 0.2:
                    continue
                sk_lines.append(f"- مهارت «{sk.name}»: {truncate(sk.procedure, 600)}")
                skill_ids.append(sk.id)
            fitted, used = _fit(sk_lines, budget.experiential // 2)
            if fitted:
                sections.append("## مهارت‌های آموخته‌شده مرتبط\n" + "\n".join(fitted))
                used_total += used

    history: list[dict[str, str]] = []
    if plan.working and conversation_id:
        summary, msgs = await load_window(session, conversation_id, budget.working)
        if summary:
            sections.append("## خلاصه گفتگوی قبلی\n" + truncate(summary, 2000))
            used_total += estimate_tokens(summary)
        for m in msgs:
            history.append({"role": m.role, "content": m.content})
            used_total += estimate_tokens(m.content)
        stats["working"] = sum(estimate_tokens(h["content"]) for h in history)

    return BuiltContext(sections, history, used_total, exp_ids, rule_ids, stats, skill_ids)
