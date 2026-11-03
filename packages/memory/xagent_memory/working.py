"""حافظه چت (Working RAM) و انتقال نکات مهم کارفرما به حافظه پایدار.

تشخیص نکات مهم ابتدا کاملاً قطعی (الگوهای زبانی) است؛ فقط وقتی سیگنال وجود دارد ممکن است
مدل حافظه برای ساختاردهی دقیق‌تر فراخوانی شود. پیام‌های اشخاص ثالث هرگز منبع یادگیری قانون نیستند.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.models import BehavioralRule, Conversation, MemoryCandidate, Message, SemanticFact
from xagent_core.text import estimate_tokens, normalize_fa

from .embeddings import cosine, hash_embed
from .store import add_rule, upsert_fact

RULE_PATTERNS = [
    r"از (این|الان|حالا) به بعد",
    r"\bهمیشه\b",
    r"\bهرگز\b",
    r"هیچ ?وقت",
    r"\bقانون\b",
    r"یادت (باشه|بمونه|نره)",
    r"دیگه .{0,40}(نکن|نفرست|ننویس)",
    r"\bاولویت\b.{0,30}(بده|باشه)",
    r"\bfrom now on\b",
    r"\balways\b",
    r"\bnever\b",
    r"^rule\s*:",
]
FACT_PATTERNS = [
    r"\bاسم من\b",
    r"\bمن .{1,40} هستم\b",
    r"(شرکت|کسب ?و ?کار|فروشگاه|برند) (من|ما)",
    r"\bهمکار(م|مون|ان)\b",
    r"به خاطر بسپار",
    r"یادداشت کن",
    r"\bبدان که\b",
    r"\bremember that\b",
    r"\bmy (name|company|business)\b",
]
NEGATIONS = re.compile(r"(نکن|نباید|هرگز|هیچ ?وقت|ممنوع|\bnot\b|\bnever\b|don't|ن[ـ]?فرست)")

_rule_re = [re.compile(p, re.IGNORECASE) for p in RULE_PATTERNS]
_fact_re = [re.compile(p, re.IGNORECASE) for p in FACT_PATTERNS]


@dataclass
class Detected:
    layer: str  # behavioral | semantic
    content: str


def detect_candidates(text: str) -> list[Detected]:
    if not text or len(text) < 6:
        return []
    out: list[Detected] = []
    norm = normalize_fa(text)
    sentences = [s.strip() for s in re.split(r"[\.!\?؟\n]+", text) if s.strip()]
    for s in sentences:
        ns = normalize_fa(s)
        if any(r.search(ns) for r in _rule_re):
            out.append(Detected("behavioral", s))
        elif any(r.search(ns) for r in _fact_re):
            out.append(Detected("semantic", s))
    if not out and any(r.search(norm) for r in _rule_re):
        out.append(Detected("behavioral", text.strip()))
    return out[:5]


async def load_window(
    session: AsyncSession, conversation_id: uuid.UUID, budget_tokens: int = 3000, max_messages: int = 30
) -> tuple[str | None, list[Message]]:
    """خلاصه فشرده + آخرین پیام‌ها تا سقف بودجه توکن (جدیدترین‌ها اولویت دارند)."""
    conv = await session.get(Conversation, conversation_id)
    rows = list(
        (
            await session.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id, Message.role.in_(["user", "assistant"]))
                .order_by(Message.created_at.desc())
                .limit(max_messages)
            )
        )
        .scalars()
        .all()
    )
    picked: list[Message] = []
    used = estimate_tokens(conv.summary or "") if conv else 0
    for m in rows:
        if conv and conv.summary_upto and m.created_at <= conv.summary_upto:
            break
        cost = estimate_tokens(m.content)
        if picked and used + cost > budget_tokens:
            break
        picked.append(m)
        used += cost
    picked.reverse()
    return (conv.summary if conv else None), picked


def _negated(s: str) -> bool:
    return bool(NEGATIONS.search(normalize_fa(s)))


async def process_owner_message(
    session: AsyncSession,
    text: str,
    conversation_id: uuid.UUID | None,
    message_id: uuid.UUID | None,
    trusted: bool,
    run_id: uuid.UUID | None = None,
    origin: str = "chat",
) -> list[MemoryCandidate]:
    """شناسایی، بررسی تعارض و انتقال نکات مهم کارفرما به حافظه پایدار."""
    if not trusted:
        return []
    detected = detect_candidates(text)
    if not detected:
        return []
    # بررسی تکرار/تعارض با جاسازی محلی (بدون هزینه API)
    results: list[MemoryCandidate] = []
    for d in detected:
        cand = MemoryCandidate(
            conversation_id=conversation_id,
            message_id=message_id,
            target_layer=d.layer,
            section="owner" if d.layer == "semantic" else None,
            content=d.content,
            trusted=trusted,
        )
        session.add(cand)
        vec = hash_embed(d.content)
        if d.layer == "behavioral":
            existing = (
                await session.execute(select(BehavioralRule).where(BehavioralRule.status.in_(["active", "probationary"])))
            ).scalars().all()
            best, best_sim = None, 0.0
            for r in existing:
                sim = cosine(vec, hash_embed(r.content))
                if sim > best_sim:
                    best, best_sim = r, sim
            if best is not None and best_sim > 0.86:
                cand.status, cand.conflict_with, cand.note = "rejected", best.id, "تکراری — قبلاً ثبت شده است"
            elif best is not None and best_sim > 0.55 and _negated(best.content) != _negated(d.content):
                cand.status, cand.conflict_with = "conflict", best.id
                cand.note = f"در تعارض با قانون «{best.title}» — نیازمند تایید کارفرما"
            else:
                title = d.content.strip()[:80]
                await add_rule(
                    session, title=title, content=d.content.strip(), source="owner", priority=60,
                    reason="دستور مستقیم کارفرما در گفتگو", origin=origin, run_id=run_id,
                )
                cand.status = "promoted"
        else:
            existing_facts = (
                await session.execute(
                    select(SemanticFact).where(SemanticFact.section == "owner", SemanticFact.status == "active")
                )
            ).scalars().all()
            dup = None
            for f in existing_facts:
                if cosine(vec, hash_embed(f.content)) > 0.9:
                    dup = f
                    break
            if dup is not None:
                cand.status, cand.conflict_with, cand.note = "rejected", dup.id, "تکراری"
            else:
                key = "note-" + hashlib.sha1(normalize_fa(d.content).encode()).hexdigest()[:10]
                await upsert_fact(
                    session, "owner", key, d.content.strip(), source="owner",
                    reason="نکته مهم کارفرما در گفتگو", actor="owner", origin=origin, run_id=run_id,
                )
                cand.status = "promoted"
        results.append(cand)
    await session.flush()
    return results
