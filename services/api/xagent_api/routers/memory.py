"""حافظه ۵ لایه و مرکز بازگشت (Undo)، رویا و مهارت‌ها."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.models import (
    BehavioralRule,
    Conversation,
    DreamReport,
    Experience,
    MemoryCandidate,
    MemoryRevision,
    Message,
    SemanticFact,
    Skill,
    User,
)
from xagent_memory.revisions import record_change, snapshot, undo_revision
from xagent_memory.store import (
    EpisodeQuery,
    add_rule,
    hybrid_search_facts,
    query_episodes,
    update_rule,
    upsert_fact,
)

from ..deps import current_user, get_session, owner_user, parse_uuid, ser

router = APIRouter(prefix="/api", tags=["memory"])


class RuleIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    content: str | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    scope: str | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class FactIn(BaseModel):
    section: str = Field(pattern="^(agent|owner)$")
    key: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1)


class UndoIn(BaseModel):
    reason: str | None = None


@router.get("/memory/overview")
async def overview(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    async def count(model, *conds):
        return (await session.execute(select(func.count()).select_from(model).where(*conds))).scalar_one()

    from xagent_core.models import Episode

    return {
        "working": {"conversations": await count(Conversation), "messages": await count(Message)},
        "behavioral": await count(BehavioralRule, BehavioralRule.status.in_(["active", "probationary"])),
        "semantic": await count(SemanticFact, SemanticFact.status.in_(["active", "probationary"])),
        "episodic": await count(Episode),
        "experiential": await count(Experience, Experience.status != "purged"),
        "skills": await count(Skill, Skill.status.in_(["active", "probationary"])),
        "revisions": await count(MemoryRevision),
        "probationary": await count(MemoryRevision, MemoryRevision.status == "probationary"),
        "candidates_conflict": await count(MemoryCandidate, MemoryCandidate.status == "conflict"),
    }


# ── رفتاری ──
@router.get("/memory/rules")
async def rules(include_inactive: bool = False, _: User = Depends(current_user),
                session: AsyncSession = Depends(get_session)):
    stmt = select(BehavioralRule).order_by(BehavioralRule.priority.desc(), BehavioralRule.updated_at.desc())
    if not include_inactive:
        stmt = stmt.where(BehavioralRule.status.in_(["active", "probationary"]))
    return {"items": [ser(r) for r in (await session.execute(stmt)).scalars()]}


@router.post("/memory/rules")
async def create_rule(body: RuleIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    if not body.content:
        raise HTTPException(400, "متن قانون لازم است")
    r = await add_rule(session, body.title or body.content[:80], body.content, source="owner",
                       priority=body.priority or 50, scope=body.scope or "global", reason="ثبت از پنل", origin="panel")
    await audit(session, user.username, "memory.rule_create", str(r.id))
    return ser(r)


@router.patch("/memory/rules/{rid}")
async def edit_rule(rid: str, body: RuleIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(BehavioralRule, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "قانون یافت نشد")
    await update_rule(session, r, body.model_dump(exclude_none=True), actor="owner", reason="ویرایش از پنل")
    await audit(session, user.username, "memory.rule_update", str(r.id))
    return ser(r)


@router.delete("/memory/rules/{rid}")
async def delete_rule(rid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    r = await session.get(BehavioralRule, parse_uuid(rid))
    if r is None:
        raise HTTPException(404, "قانون یافت نشد")
    await update_rule(session, r, {"status": "purged"}, actor="owner", reason="حذف از پنل")
    return {"ok": True}


# ── معنایی ──
@router.get("/memory/facts")
async def facts(section: str | None = None, q: str | None = None, _: User = Depends(current_user),
                session: AsyncSession = Depends(get_session)):
    if q:
        rows = await hybrid_search_facts(session, q, section=section, limit=30)
    else:
        stmt = select(SemanticFact).where(SemanticFact.status.in_(["active", "probationary"]))
        if section:
            stmt = stmt.where(SemanticFact.section == section)
        rows = (await session.execute(stmt.order_by(SemanticFact.updated_at.desc()).limit(300))).scalars().all()
    return {"items": [ser(f) for f in rows]}


@router.post("/memory/facts")
async def save_fact(body: FactIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    f = await upsert_fact(session, body.section, body.key, body.content, source="owner", reason="ثبت از پنل",
                          actor="owner", origin="panel")
    return ser(f)


@router.delete("/memory/facts/{fid}")
async def delete_fact(fid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    f = await session.get(SemanticFact, parse_uuid(fid))
    if f is None:
        raise HTTPException(404, "یافت نشد")
    before = snapshot("semantic_fact", f)
    f.status = "purged"
    await record_change(session, "semantic_fact", f, "update", before, "حذف از پنل", actor="owner", origin="panel")
    return {"ok": True}


# ── رویدادی ──
@router.get("/memory/episodes")
async def episodes(kind: str | None = None, status: str | None = None, q: str | None = None,
                   date_from: datetime | None = None, date_to: datetime | None = None,
                   limit: int = Query(50, le=200), offset: int = 0, _: User = Depends(current_user),
                   session: AsyncSession = Depends(get_session)):
    rows = await query_episodes(session, EpisodeQuery(date_from=date_from, date_to=date_to, kind=kind, status=status,
                                                      text=q, limit=limit, offset=offset))
    return {"items": [ser(e) for e in rows]}


# ── تجربی ──
@router.get("/memory/experiences")
async def experiences(outcome: str | None = None, _: User = Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    stmt = select(Experience).where(Experience.status != "purged")
    if outcome:
        stmt = stmt.where(Experience.outcome == outcome)
    rows = (await session.execute(stmt.order_by(Experience.updated_at.desc()).limit(200))).scalars().all()
    return {"items": [ser(e) for e in rows]}


@router.post("/memory/experiences/{eid}/obsolete")
async def obsolete(eid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    e = await session.get(Experience, parse_uuid(eid))
    if e is None:
        raise HTTPException(404, "یافت نشد")
    before = snapshot("experience", e)
    e.outcome = "obsolete"
    await record_change(session, "experience", e, "update", before, "علامت منسوخ از پنل", actor="owner", origin="panel")
    return ser(e)


# ── تعارض‌ها ──
@router.get("/memory/candidates")
async def candidates(status: str = "conflict", _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(MemoryCandidate).where(MemoryCandidate.status == status)
                                  .order_by(MemoryCandidate.created_at.desc()).limit(100))).scalars().all()
    return {"items": [ser(c) for c in rows]}


@router.post("/memory/candidates/{cid}/{action}")
async def resolve_candidate(cid: str, action: str, user: User = Depends(owner_user),
                            session: AsyncSession = Depends(get_session)):
    c = await session.get(MemoryCandidate, parse_uuid(cid))
    if c is None:
        raise HTTPException(404, "یافت نشد")
    if action == "accept":
        if c.target_layer == "behavioral":
            if c.conflict_with:
                old = await session.get(BehavioralRule, c.conflict_with)
                if old:
                    await update_rule(session, old, {"status": "disabled"}, actor="owner",
                                      reason="جایگزین با قانون جدید (حل تعارض)")
            await add_rule(session, c.content[:80], c.content, source="owner", priority=60,
                           reason="تایید کارفرما پس از تعارض", origin="panel")
        else:
            await upsert_fact(session, "owner", f"note-{str(c.id)[:8]}", c.content, reason="تایید کارفرما")
        c.status = "promoted"
    elif action == "reject":
        c.status = "rejected"
    else:
        raise HTTPException(400, "اقدام نامعتبر")
    return ser(c)


# ── مرکز بازگشت ──
@router.get("/memory/revisions")
async def revisions(status: str | None = None, entity_type: str | None = None, limit: int = Query(100, le=500),
                    offset: int = 0, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    stmt = select(MemoryRevision)
    if status:
        stmt = stmt.where(MemoryRevision.status.in_(status.split(",")))
    if entity_type:
        stmt = stmt.where(MemoryRevision.entity_type == entity_type)
    rows = (await session.execute(stmt.order_by(MemoryRevision.created_at.desc()).limit(limit).offset(offset))).scalars()
    return {"items": [ser(r) for r in rows]}


@router.post("/memory/revisions/{rid}/undo")
async def undo(rid: str, body: UndoIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    res = await undo_revision(session, parse_uuid(rid), actor="owner", reason=body.reason or "Undo توسط کارفرما")
    await audit(session, user.username, "memory.undo", rid, {"reverted": res.reverted})
    if not res.reverted:
        raise HTTPException(400, res.message)
    return {"ok": True, "message": res.message, "reverted": res.reverted}


# ── رویا و مهارت‌ها ──
@router.get("/dream/reports")
async def dream_reports(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(DreamReport).order_by(DreamReport.started_at.desc()).limit(60))).scalars()
    return {"items": [ser(r) for r in rows]}


@router.post("/dream/run")
async def dream_now(user: User = Depends(owner_user)):
    await bus.get_redis().xadd("xagent:control", {"cmd": "dream", "force": "1", "by": user.username})
    return {"ok": True, "message": "رویا در صف اجرا قرار گرفت"}


@router.get("/skills")
async def skills(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Skill).where(Skill.status != "purged").order_by(Skill.updated_at.desc()))).scalars()
    return {"items": [ser(s) for s in rows]}


@router.post("/skills/{sid}/{action}")
async def skill_action(sid: str, action: str, user: User = Depends(owner_user),
                       session: AsyncSession = Depends(get_session)):
    s = await session.get(Skill, parse_uuid(sid))
    if s is None:
        raise HTTPException(404, "مهارت یافت نشد")
    before = snapshot("skill", s)
    if action == "activate":
        s.status = "active"
    elif action == "disable":
        s.status = "disabled"
    else:
        raise HTTPException(400, "اقدام نامعتبر")
    await record_change(session, "skill", s, "status", before, f"{action} توسط کارفرما", actor="owner", origin="panel")
    return ser(s)
