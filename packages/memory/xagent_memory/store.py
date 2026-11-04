"""لایه‌های پایدار حافظه: رفتاری، معنایی، رویدادی و تجربی (برداری).

قانون امنیتی Anti-Injection: فقط منابع مورد اعتماد (کارفرما از پنل/چت مالک یا رویای تاییدشده)
می‌توانند قانون رفتاری ثبت کنند. محتوای وب، فایل یا پیام اشخاص ثالث هرگز به این لایه راه ندارد.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import BehavioralRule, Episode, Experience, SemanticFact
from xagent_core.text import normalize_fa

from .embeddings import fit_dim, get_embedder
from .revisions import record_change, snapshot

# فضای نام حافظه مشترک؛ حافظه خصوصی کارمند: employee:<id>
GLOBAL = "global"

TRUSTED_RULE_SOURCES = {"owner", "dream"}


class UntrustedSourceError(PermissionError):
    pass


# ───────────────────────── حافظه رفتاری ─────────────────────────


async def active_rules(session: AsyncSession, scopes: list[str] | None = None) -> list[BehavioralRule]:
    scopes = scopes or ["global"]
    q = (
        select(BehavioralRule)
        .where(BehavioralRule.status.in_(["active", "probationary"]), BehavioralRule.scope.in_(scopes))
        .order_by(BehavioralRule.priority.desc(), BehavioralRule.updated_at.desc())
    )
    return list((await session.execute(q)).scalars().all())


async def add_rule(
    session: AsyncSession,
    title: str,
    content: str,
    source: str,
    priority: int = 50,
    scope: str = "global",
    reason: str = "",
    origin: str = "panel",
    run_id: uuid.UUID | None = None,
    probationary: bool = False,
) -> BehavioralRule:
    if source not in TRUSTED_RULE_SOURCES:
        raise UntrustedSourceError("فقط کارفرما مجاز به ثبت قانون رفتاری است.")
    rule = BehavioralRule(
        title=title[:200],
        content=content,
        priority=priority,
        scope=scope,
        source=source,
        status="probationary" if probationary else "active",
    )
    session.add(rule)
    await session.flush()
    await record_change(
        session,
        "behavioral_rule",
        rule,
        "create",
        None,
        reason or "ثبت قانون رفتاری",
        actor="owner" if source == "owner" else source,
        origin=origin,
        run_id=run_id,
        probationary=probationary,
    )
    return rule


async def update_rule(
    session: AsyncSession,
    rule: BehavioralRule,
    changes: dict[str, Any],
    actor: str,
    reason: str,
    origin: str = "panel",
) -> BehavioralRule:
    if actor not in TRUSTED_RULE_SOURCES:
        raise UntrustedSourceError("فقط کارفرما مجاز به تغییر قانون رفتاری است.")
    before = snapshot("behavioral_rule", rule)
    for k, v in changes.items():
        if k in {"title", "content", "priority", "scope", "status"} and v is not None:
            setattr(rule, k, v)
    rule.version += 1
    await record_change(session, "behavioral_rule", rule, "update", before, reason, actor=actor, origin=origin)
    return rule


# ───────────────────────── حافظه معنایی ─────────────────────────


async def upsert_fact(
    session: AsyncSession,
    section: str,
    key: str,
    content: str,
    source: str = "owner",
    reason: str = "",
    actor: str = "owner",
    origin: str = "panel",
    run_id: uuid.UUID | None = None,
    probationary: bool = False,
    scope: str = GLOBAL,
) -> SemanticFact:
    if section not in {"agent", "owner"}:
        raise ValueError("بخش حافظه معنایی باید agent یا owner باشد.")
    embedder = await get_embedder(session)
    vec = fit_dim((await embedder.embed([f"{key}: {content}"]))[0])
    existing = (
        await session.execute(select(SemanticFact).where(SemanticFact.scope == scope, SemanticFact.section == section,
                                                         SemanticFact.key == key))
    ).scalar_one_or_none()
    if existing is None:
        fact = SemanticFact(
            scope=scope,
            section=section,
            key=key,
            content=content,
            source=source,
            embedding=vec,
            embedding_model=embedder.name,
            status="probationary" if probationary else "active",
        )
        session.add(fact)
        await session.flush()
        await record_change(
            session, "semantic_fact", fact, "create", None, reason or "ثبت دانسته جدید", actor, origin, run_id,
            probationary,
        )
        return fact
    before = snapshot("semantic_fact", existing)
    existing.content = content
    existing.source = source
    existing.embedding = vec
    existing.embedding_model = embedder.name
    existing.version += 1
    if existing.status == "purged":
        existing.status = "active"
    await record_change(
        session, "semantic_fact", existing, "update", before, reason or "به‌روزرسانی دانسته", actor, origin, run_id,
        probationary,
    )
    return existing


async def facts_for_context(
    session: AsyncSession, query: str | None, limit_owner: int = 8, scopes: Sequence[str] = (GLOBAL,)
) -> tuple[list[SemanticFact], list[SemanticFact]]:
    """بخش agent همیشه (کوتاه)؛ از بخش owner فقط موارد مرتبط با پرسش."""
    agent = list(
        (
            await session.execute(
                select(SemanticFact)
                .where(SemanticFact.section == "agent", SemanticFact.status.in_(["active", "probationary"]),
                       SemanticFact.scope == GLOBAL)
                .order_by(SemanticFact.updated_at.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    if query:
        owner = await hybrid_search_facts(session, query, section="owner", limit=limit_owner, scopes=scopes)
    else:
        owner = list(
            (
                await session.execute(
                    select(SemanticFact)
                    .where(SemanticFact.section == "owner", SemanticFact.status.in_(["active", "probationary"]),
                           SemanticFact.scope.in_(list(scopes)))
                    .order_by(SemanticFact.updated_at.desc())
                    .limit(limit_owner)
                )
            )
            .scalars()
            .all()
        )
    return agent, owner


# ───────────────────────── جست‌وجوی ترکیبی (SQL + FTS + Vector) ─────────────────────────


def _rrf(rank_lists: list[list[uuid.UUID]], k: int = 60) -> dict[uuid.UUID, float]:
    scores: dict[uuid.UUID, float] = {}
    for ranks in rank_lists:
        for i, _id in enumerate(ranks):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + i + 1)
    return scores


async def hybrid_search_facts(
    session: AsyncSession, query: str, section: str | None = None, limit: int = 8, scopes: Sequence[str] = (GLOBAL,)
) -> list[SemanticFact]:
    embedder = await get_embedder(session)
    qvec = fit_dim((await embedder.embed([query]))[0])
    conds = [SemanticFact.status.in_(["active", "probationary"]), SemanticFact.scope.in_(list(scopes))]
    if section:
        conds.append(SemanticFact.section == section)
    tsq = func.plainto_tsquery("simple", func.xa_normalize(query))
    fts_ids = (
        (
            await session.execute(
                select(SemanticFact.id)
                .where(*conds, SemanticFact.tsv.op("@@")(tsq))
                .order_by(func.ts_rank(SemanticFact.tsv, tsq).desc())
                .limit(limit * 3)
            )
        )
        .scalars()
        .all()
    )
    # برای کوئری‌های چندکلمه‌ای، تطبیق OR برای بازیابی بهتر
    words = [w for w in normalize_fa(query).split(" ") if len(w) > 1][:12]
    or_ids: list[uuid.UUID] = []
    if len(words) > 1:
        orq = func.to_tsquery("simple", literal(" | ".join(w.replace("'", "") for w in words)))
        or_ids = list(
            (
                await session.execute(
                    select(SemanticFact.id)
                    .where(*conds, SemanticFact.tsv.op("@@")(orq))
                    .order_by(func.ts_rank(SemanticFact.tsv, orq).desc())
                    .limit(limit * 3)
                )
            )
            .scalars()
            .all()
        )
    vec_ids = (
        (
            await session.execute(
                select(SemanticFact.id)
                .where(*conds, SemanticFact.embedding.is_not(None), SemanticFact.embedding_model == embedder.name)
                .order_by(SemanticFact.embedding.cosine_distance(qvec))
                .limit(limit * 3)
            )
        )
        .scalars()
        .all()
    )
    scores = _rrf([list(fts_ids), or_ids, list(vec_ids)])
    top = sorted(scores, key=lambda i: scores[i], reverse=True)[:limit]
    if not top:
        return []
    rows = {f.id: f for f in (await session.execute(select(SemanticFact).where(SemanticFact.id.in_(top)))).scalars()}
    return [rows[i] for i in top if i in rows]


# ───────────────────────── حافظه رویدادی ─────────────────────────


async def record_episode(session: AsyncSession, **kw: Any) -> Episode:
    ep = Episode(**kw)
    session.add(ep)
    await session.flush()
    return ep


@dataclass
class EpisodeQuery:
    date_from: datetime | None = None
    date_to: datetime | None = None
    kind: str | None = None
    status: str | None = None
    contact_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    routine_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    text: str | None = None
    limit: int = 20
    offset: int = 0
    scopes: Sequence[str] = (GLOBAL,)


async def query_episodes(session: AsyncSession, q: EpisodeQuery) -> list[Episode]:
    """کوئری مستقیم و فیلترشده — بدون پیمایش کل حافظه و بدون LLM."""
    conds = [Episode.scope.in_(list(q.scopes))]
    if q.date_from:
        conds.append(Episode.ts >= q.date_from)
    if q.date_to:
        conds.append(Episode.ts <= q.date_to)
    for col, val in (
        (Episode.kind, q.kind),
        (Episode.status, q.status),
        (Episode.contact_id, q.contact_id),
        (Episode.task_id, q.task_id),
        (Episode.routine_id, q.routine_id),
        (Episode.run_id, q.run_id),
    ):
        if val is not None:
            conds.append(col == val)
    if q.text:
        pat = f"%{normalize_fa(q.text)}%"
        conds.append(or_(func.xa_normalize(Episode.title).like(pat), func.xa_normalize(Episode.summary).like(pat)))
    stmt = select(Episode).where(and_(*conds)) if conds else select(Episode)
    stmt = stmt.order_by(Episode.ts.desc()).limit(min(q.limit, 200)).offset(q.offset)
    return list((await session.execute(stmt)).scalars().all())


# ───────────────────────── حافظه تجربی (برداری) ─────────────────────────


async def record_experience(
    session: AsyncSession,
    signature: str,
    title: str,
    approach: str,
    tool_sequence: list[str],
    outcome: str,
    tokens_used: int,
    duration_ms: int,
    steps: int,
    routine_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    origin: str = "chat",
    scope: str = GLOBAL,
) -> tuple[Experience, uuid.UUID | None]:
    """ثبت تجربه؛ اگر تجربه بسیار مشابهی وجود دارد، آمار آن به‌روز می‌شود.

    خروجی: (تجربه، شناسه Revision آزمایشی در صورت ایجاد تجربه موفق جدید)
    """
    embedder = await get_embedder(session)
    vec = fit_dim((await embedder.embed([signature]))[0])
    similar = (
        await session.execute(
            select(Experience, Experience.embedding.cosine_distance(vec).label("dist"))
            .where(
                Experience.status != "purged",
                Experience.scope == scope,
                Experience.embedding_model == embedder.name,
                Experience.outcome == outcome,
                (Experience.routine_id == routine_id) if routine_id else text("true"),
            )
            .order_by("dist")
            .limit(1)
        )
    ).first()
    if similar is not None and similar.dist is not None and similar.dist < 0.12:
        exp: Experience = similar[0]
        if outcome == "verified_success":
            exp.success_count += 1
            # اگر روش جدید ارزان‌تر بود، جایگزین می‌شود (با Snapshot)
            if tokens_used and (exp.tokens_used == 0 or tokens_used < exp.tokens_used * 0.85):
                before = snapshot("experience", exp)
                exp.approach = approach
                exp.tool_sequence = tool_sequence
                exp.tokens_used = tokens_used
                exp.duration_ms = duration_ms
                exp.steps = steps
                await record_change(
                    session, "experience", exp, "update", before, "روش کم‌هزینه‌تر جایگزین شد", "agent", origin, run_id
                )
        else:
            exp.failure_count += 1
        exp.last_used_at = utcnow()
        await session.flush()
        return exp, None
    exp = Experience(
        scope=scope,
        signature=signature[:2000],
        title=title[:256],
        approach=approach,
        tool_sequence=tool_sequence,
        outcome=outcome,
        status="probationary" if outcome == "verified_success" else "active",
        routine_id=routine_id,
        tokens_used=tokens_used,
        duration_ms=duration_ms,
        steps=steps,
        success_count=1 if outcome == "verified_success" else 0,
        failure_count=0 if outcome == "verified_success" else 1,
        embedding=vec,
        embedding_model=embedder.name,
    )
    session.add(exp)
    await session.flush()
    rev = await record_change(
        session,
        "experience",
        exp,
        "create",
        None,
        "ثبت تجربه جدید" if outcome == "verified_success" else "ثبت تلاش ناموفق برای پرهیز در آینده",
        "agent",
        origin,
        run_id,
        probationary=outcome == "verified_success",
        baseline={"avg_tokens": tokens_used, "fail_rate": 0.0},
    )
    return exp, (rev.id if outcome == "verified_success" else None)


async def best_experiences(
    session: AsyncSession, signature: str, routine_id: uuid.UUID | None = None, limit: int = 2,
    scopes: Sequence[str] = (GLOBAL,),
) -> tuple[list[Experience], list[Experience]]:
    """بهترین و کم‌مصرف‌ترین روش‌های تاییدشده + تلاش‌های ناموفق مشابه (برای پرهیز)."""
    embedder = await get_embedder(session)
    vec = fit_dim((await embedder.embed([signature]))[0])
    base = [Experience.status.in_(["active", "probationary"]), Experience.embedding_model == embedder.name,
            Experience.scope.in_(list(scopes))]
    if routine_id:
        base.append(or_(Experience.routine_id == routine_id, Experience.routine_id.is_(None)))
    dist = Experience.embedding.cosine_distance(vec)
    good_rows = (
        await session.execute(
            select(Experience, dist.label("d"))
            .where(*base, Experience.outcome == "verified_success")
            .order_by(dist)
            .limit(limit * 3)
        )
    ).all()
    good = [r[0] for r in good_rows if r.d is not None and r.d < 0.45]
    good.sort(key=lambda e: (-(e.success_count - e.failure_count), e.tokens_used or 10**9))
    bad_rows = (
        await session.execute(
            select(Experience, dist.label("d"))
            .where(*base, Experience.outcome == "failed_attempt")
            .order_by(dist)
            .limit(limit)
        )
    ).all()
    bad = [r[0] for r in bad_rows if r.d is not None and r.d < 0.35]
    return good[:limit], bad[:limit]
