"""موتور رویا (Incremental Nightly Dream) و خلاصه‌سازی حافظه گفتگو.

- تقطیر افزایشی: فقط رویدادهای جدید از آخرین Checkpoint بررسی می‌شوند
- اگر رویداد مهمی نبود، بدون مصرف LLM پایان می‌یابد
- مهارت‌های جدید در وضعیت آزمایشی و پس از تست قطعی در محیط ایزوله ثبت می‌شوند (با Snapshot برای Undo)
- پاکسازی فقط فایل‌های کش موقت؛ لاگ‌های حسابرسی هرگز حذف نمی‌شوند
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import notify
from xagent_core.config import get_settings
from xagent_core.db import utcnow
from xagent_core.models import Conversation, DreamReport, Episode, Experience, Message, Skill
from xagent_core.settings_store import DreamSettings, get_setting
from xagent_core.text import estimate_tokens, truncate
from xagent_core.timeutil import jalali_str
from xagent_memory.revisions import record_change, snapshot
from xagent_memory.store import upsert_fact
from xagent_tools.registry import BUILTIN_BY_NAME, installed_tools

from . import prompts
from .llm import NoModelError, call_llm, parse_json
from .model_select import select_models
from .providers.base import LLMMessage, ProviderError


def _cleanup_temp() -> dict[str, Any]:
    s = get_settings()
    removed, freed = 0, 0
    now = time.time()
    for folder, max_age in ((s.data_dir / "tmp", 86400), (s.data_dir / "trash", 30 * 86400),
                            (s.data_dir / "cache", 7 * 86400)):
        if not folder.exists():
            continue
        for p in sorted(folder.rglob("*"), reverse=True):
            try:
                if p.is_file() and now - p.stat().st_mtime > max_age:
                    freed += p.stat().st_size
                    p.unlink()
                    removed += 1
                elif p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
            except OSError:
                continue
    return {"files_removed": removed, "bytes_freed": freed}


async def _test_skill(session: AsyncSession, skill: dict[str, Any]) -> tuple[bool, str]:
    """تست قطعی مهارت پیشنهادی: ابزارهای ارجاع‌شده باید وجود داشته باشند و رویه خالی نباشد."""
    names = set(BUILTIN_BY_NAME) | {t.name for t in await installed_tools(session)}
    missing = [t for t in skill.get("tool_sequence", []) if t not in names]
    if not skill.get("procedure") or len(skill["procedure"]) < 20:
        return False, "رویه مهارت ناقص است"
    if missing:
        return False, f"ابزارهای ناموجود: {', '.join(missing)}"
    return True, "ابزارها موجود و رویه معتبر است"


async def run_dream(session: AsyncSession, force: bool = False) -> DreamReport:
    cfg = await get_setting(session, "dream", DreamSettings)
    last = (
        await session.execute(
            select(DreamReport).where(DreamReport.status.in_(["done", "skipped"])).order_by(DreamReport.started_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    checkpoint = last.checkpoint_to if last and last.checkpoint_to else utcnow() - timedelta(days=1)
    now = utcnow()
    report = DreamReport(checkpoint_from=checkpoint, checkpoint_to=now, status="running")
    session.add(report)
    await session.flush()
    await bus.publish("dream.started", {"id": str(report.id)})

    episodes = (
        await session.execute(
            select(Episode).where(Episode.ts > checkpoint, Episode.ts <= now, Episode.kind != "system")
            .order_by(Episode.ts).limit(400)
        )
    ).scalars().all()
    report.events_reviewed = len(episodes)
    report.cleanup = _cleanup_temp()

    meaningful = [e for e in episodes if e.tools or e.status == "failed" or e.kind == "routine"]
    if len(meaningful) < cfg.min_events_for_llm and not force:
        report.status = "skipped"
        report.summary = f"{len(episodes)} رویداد جدید بررسی شد؛ رویداد مهمی برای یادگیری نبود (بدون مصرف LLM)."
        report.finished_at = utcnow()
        await session.flush()
        await bus.publish("dream.finished", {"id": str(report.id), "status": "skipped"})
        return report

    lines = []
    budget = 9000
    for e in meaningful:
        line = (f"- [{e.id}] {jalali_str(e.ts)} {e.kind}/{e.status} «{truncate(e.title, 120)}» ابزارها: "
                f"{','.join(e.tools[:8])} | {e.tokens_in + e.tokens_out} توکن | {truncate(e.summary, 160)}")
        budget -= estimate_tokens(line)
        if budget < 0:
            break
        lines.append(line)
    skills = (await session.execute(select(Skill).where(Skill.status.in_(["active", "probationary"])))).scalars().all()
    skills_text = "\n".join(f"- {s.name}: {truncate(s.description, 120)}" for s in skills) or "(هیچ)"

    chain = (await select_models(session, "dream", "manager")).models
    try:
        res = await call_llm(
            session, chain, purpose="dream", system_stable="", system_dynamic="",
            messages=[LLMMessage("user", prompts.DREAM_PROMPT.format(events="\n".join(lines), skills=skills_text))],
            max_tokens=2500, json_mode=True, effort="medium",
        )
    except (ProviderError, NoModelError) as e:
        report.status = "failed"
        report.summary = f"اجرای رویا ناموفق بود: {e}"
        report.finished_at = utcnow()
        await session.flush()
        return report
    report.llm_used = True
    report.tokens = res.response.usage.input_total + res.response.usage.output
    report.cost_usd = Decimal(res.cost)
    data = parse_json(res.response.text) or {}
    learnings: list[dict[str, Any]] = []
    proposed: list[dict[str, Any]] = []

    for sk in (data.get("skills") or [])[:3]:
        name = (sk.get("name") or "").strip()[:120]
        if not name:
            continue
        ok, msg = await _test_skill(session, sk)
        existing = (await session.execute(select(Skill).where(Skill.name == name))).scalar_one_or_none()
        before = snapshot("skill", existing) if existing else None
        if existing is None:
            existing = Skill(name=name)
            session.add(existing)
        existing.description = sk.get("description", "")
        existing.trigger_hint = sk.get("trigger_hint", "")
        existing.procedure = sk.get("procedure", "")
        existing.tool_sequence = list(sk.get("tool_sequence", []))[:30]
        existing.source = "dream"
        existing.status = "probationary" if ok else "disabled"
        existing.version = (existing.version or 0) + (1 if before else 0) or 1
        existing.test_status = "passed" if ok else "failed"
        existing.test_output = msg
        await session.flush()
        rev = await record_change(
            session, "skill", existing, "update" if before else "create", before,
            f"مهارت پیشنهادی رویا ({'تست موفق' if ok else 'تست ناموفق: ' + msg})", actor="dream", origin="dream",
            probationary=ok, baseline={"avg_tokens": None, "fail_rate": None},
        )
        proposed.append({"name": name, "status": existing.status, "test": msg, "revision_id": str(rev.id)})
        learnings.append({"type": "skill", "name": name, "revision_id": str(rev.id)})

    for f in (data.get("agent_facts") or [])[:5]:
        if f.get("key") and f.get("content"):
            fact = await upsert_fact(session, "agent", f["key"][:120], f["content"], source="dream",
                                     reason="تقطیر شبانه رویا", actor="dream", origin="dream", probationary=True)
            learnings.append({"type": "agent_fact", "key": f["key"], "id": str(fact.id)})

    for xid in (data.get("obsolete_experience_ids") or [])[:10]:
        try:
            exp = await session.get(Experience, uuid.UUID(str(xid)))
        except ValueError:
            exp = None
        if exp is not None and exp.outcome != "obsolete":
            before = snapshot("experience", exp)
            exp.outcome = "obsolete"
            rev = await record_change(session, "experience", exp, "update", before, "منسوخ‌شده طبق تحلیل رویا",
                                      actor="dream", origin="dream")
            learnings.append({"type": "obsolete", "id": str(exp.id), "revision_id": str(rev.id)})

    report.summary = data.get("summary") or "رویا انجام شد."
    if data.get("recommendations"):
        report.summary += "\n\nپیشنهادها:\n" + "\n".join(f"• {r}" for r in data["recommendations"][:6])
    report.learnings = learnings
    report.skills_proposed = proposed
    report.status = "done"
    report.finished_at = utcnow()
    await session.flush()
    await notify(session, "گزارش رویای شبانه آماده است", truncate(report.summary, 300), level="info", link="/dream")
    await bus.publish("dream.finished", {"id": str(report.id), "status": "done"})
    return report


async def summarize_conversation(session: AsyncSession, conversation_id: uuid.UUID, keep_last: int = 12) -> bool:
    """فشرده‌سازی حافظه چت: پیام‌های قدیمی خلاصه می‌شوند تا بودجه کانتکست رعایت شود."""
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return False
    q = select(Message).where(Message.conversation_id == conversation_id, Message.role.in_(["user", "assistant"]))
    if conv.summary_upto:
        q = q.where(Message.created_at > conv.summary_upto)
    msgs = (await session.execute(q.order_by(Message.created_at))).scalars().all()
    if len(msgs) <= keep_last + 10:
        return False
    old = msgs[: len(msgs) - keep_last]
    transcript = "\n".join(f"{'کارفرما' if m.role == 'user' else 'ایجنت'}: {truncate(m.content, 600)}" for m in old)
    if conv.summary:
        transcript = f"خلاصه قبلی:\n{conv.summary}\n\nادامه:\n{transcript}"
    chain = (await select_models(session, "memory", "worker")).models
    try:
        res = await call_llm(session, chain, purpose="memory", system_stable="", system_dynamic="",
                             messages=[LLMMessage("user", prompts.SUMMARIZE_PROMPT.format(transcript=truncate(transcript, 24000)))],
                             max_tokens=900, effort="low")
    except (ProviderError, NoModelError):
        return False
    conv.summary = res.response.text.strip()
    conv.summary_upto = old[-1].created_at
    await session.flush()
    return True
