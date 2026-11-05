"""سیستم کنترل نسخه و Undo آنی یادگیری‌ها.

هر تغییر در حافظه رفتاری، معنایی، تجربی یا مهارت‌ها قبل از اعمال یک Snapshot می‌گیرد
(`MemoryRevision` با Revision_ID، نسخه قبلی، نسخه جدید و دلیل تغییر).

- `record_change`: ثبت Snapshot
- `undo_revision`: بازگردانی فوری (با بازگردانی آبشاری تغییرات جدیدتر روی همان موجودیت)
- `evaluate_probation`: پاکسازی خودکار یادگیری مضر در صورت پسرفت (Auto-Rollback on Regression)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.db import utcnow
from xagent_core.models import BehavioralRule, Experience, InstalledTool, MemoryRevision, SemanticFact, Skill
from xagent_core.redact import open_secrets, seal_secrets

ENTITY_MODELS: dict[str, type] = {
    "behavioral_rule": BehavioralRule,
    "semantic_fact": SemanticFact,
    "experience": Experience,
    "skill": Skill,
    "tool": InstalledTool,
}

# فیلدهایی که در Snapshot نگهداری و هنگام Undo بازگردانده می‌شوند
SNAPSHOT_FIELDS: dict[str, list[str]] = {
    "behavioral_rule": ["title", "content", "priority", "scope", "version", "status", "source", "source_ref"],
    "semantic_fact": ["section", "key", "content", "source", "status", "version"],
    "experience": [
        "signature",
        "title",
        "approach",
        "tool_sequence",
        "outcome",
        "status",
        "routine_id",
        "tokens_used",
        "duration_ms",
        "steps",
    ],
    "skill": ["name", "description", "trigger_hint", "procedure", "tool_sequence", "status", "version", "source"],
    "tool": ["enabled", "status", "active_version_id", "config"],
}

PURGED_STATUS = {
    "behavioral_rule": "purged",
    "semantic_fact": "purged",
    "experience": "purged",
    "skill": "purged",
    "tool": "disabled",
}


def _jsonable(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def snapshot(entity_type: str, obj: Any) -> dict[str, Any]:
    data = {f: _jsonable(getattr(obj, f, None)) for f in SNAPSHOT_FIELDS[entity_type]}
    if entity_type == "tool" and isinstance(data.get("config"), dict):
        # اسرار پیکربندی ابزار (env/headers/کلیدها) رمزشده ذخیره می‌شوند؛ Undo آن‌ها را باز می‌کند
        data["config"] = seal_secrets(data["config"])
    return data


def _restore_value(entity_type: str, field: str, value: Any) -> Any:
    if field in ("routine_id", "active_version_id") and isinstance(value, str):
        return uuid.UUID(value)
    if entity_type == "tool" and field == "config":
        return open_secrets(value)  # Snapshotهای قدیمی متن‌ساده هم بدون تغییر بازگردانده می‌شوند
    return value


async def record_change(
    session: AsyncSession,
    entity_type: str,
    obj: Any,
    action: str,
    before: dict[str, Any] | None,
    reason: str,
    actor: str = "agent",
    origin: str = "chat",
    run_id: uuid.UUID | None = None,
    probationary: bool = False,
    baseline: dict[str, Any] | None = None,
) -> MemoryRevision:
    await session.flush()
    rev = MemoryRevision(
        entity_type=entity_type,
        entity_id=obj.id,
        action=action,
        before=before,
        after=snapshot(entity_type, obj) if action != "delete" else None,
        reason=reason,
        actor=actor,
        origin=origin,
        run_id=run_id,
        status="probationary" if probationary else "applied",
        baseline=baseline or {},
    )
    session.add(rev)
    await session.flush()
    await bus.publish(
        "memory.revision",
        {"id": str(rev.id), "entity_type": entity_type, "action": action, "status": rev.status, "reason": reason},
    )
    return rev


@dataclass
class UndoResult:
    reverted: list[str]
    message: str


async def _revert_one(session: AsyncSession, rev: MemoryRevision, actor: str, reason: str) -> None:
    model = ENTITY_MODELS[rev.entity_type]
    obj = await session.get(model, rev.entity_id)
    if rev.action == "create":
        if obj is not None:
            obj.status = PURGED_STATUS[rev.entity_type]
            if rev.entity_type == "tool":
                obj.enabled = False
    elif rev.before is not None:
        if obj is None:
            obj = model(id=rev.entity_id)
            session.add(obj)
        for field, value in rev.before.items():
            setattr(obj, field, _restore_value(rev.entity_type, field, value))
    rev.status = "reverted"
    rev.reverted_at = utcnow()
    rev.reverted_by = actor
    rev.revert_reason = reason


async def undo_revision(
    session: AsyncSession, revision_id: uuid.UUID, actor: str = "owner", reason: str = "بازگردانی توسط کارفرما"
) -> UndoResult:
    rev = await session.get(MemoryRevision, revision_id)
    if rev is None:
        return UndoResult([], "نسخه موردنظر یافت نشد.")
    if rev.status == "reverted":
        return UndoResult([], "این تغییر قبلاً بازگردانده شده است.")
    # ابتدا تغییرات جدیدتر روی همان موجودیت (به ترتیب معکوس) بازگردانده می‌شوند
    newer = (
        (
            await session.execute(
                select(MemoryRevision)
                .where(
                    MemoryRevision.entity_id == rev.entity_id,
                    MemoryRevision.entity_type == rev.entity_type,
                    MemoryRevision.created_at > rev.created_at,
                    MemoryRevision.status != "reverted",
                )
                .order_by(MemoryRevision.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    reverted: list[str] = []
    for n in newer:
        await _revert_one(session, n, actor, f"بازگردانی آبشاری به دلیل Undo نسخه {rev.id}")
        reverted.append(str(n.id))
    await _revert_one(session, rev, actor, reason)
    reverted.append(str(rev.id))
    await session.flush()
    await bus.publish("memory.undo", {"revision_id": str(rev.id), "reverted": reverted, "actor": actor})
    return UndoResult(reverted, f"{len(reverted)} تغییر بازگردانده شد.")


async def undo_last(session: AsyncSession, actor: str = "owner", count: int = 1) -> UndoResult:
    rows = (
        (
            await session.execute(
                select(MemoryRevision)
                .where(MemoryRevision.status.in_(["applied", "probationary", "confirmed"]))
                .order_by(MemoryRevision.created_at.desc())
                .limit(count)
            )
        )
        .scalars()
        .all()
    )
    all_reverted: list[str] = []
    for r in rows:
        res = await undo_revision(session, r.id, actor=actor)
        all_reverted.extend(res.reverted)
    if not all_reverted:
        return UndoResult([], "یادگیری فعالی برای بازگردانی وجود ندارد.")
    return UndoResult(all_reverted, f"{len(all_reverted)} یادگیری بازگردانده شد.")


def is_regression(baseline: dict[str, Any], observed: dict[str, Any], token_ratio: float = 1.3) -> str | None:
    """مقایسه قطعی معیارها؛ در صورت پسرفت دلیل را برمی‌گرداند."""
    runs = observed.get("runs", 0)
    if runs <= 0:
        return None
    fail_rate = observed.get("failures", 0) / runs
    base_fail = baseline.get("fail_rate")
    if base_fail is not None and fail_rate > base_fail + 0.34:
        return f"افزایش نرخ خطا از {base_fail:.0%} به {fail_rate:.0%}"
    if base_fail is None and observed.get("failures", 0) >= 2 and fail_rate >= 0.5:
        return f"خطای پیاپی پس از یادگیری ({observed.get('failures')} از {runs})"
    base_tokens = baseline.get("avg_tokens")
    avg_tokens = observed.get("tokens", 0) / runs
    # Token regressions are meaningful only against comparable task/routine baselines.
    comparable = baseline.get("comparison_scope") in ("routine", "signature")
    if comparable and runs >= 2 and base_tokens and avg_tokens > base_tokens * token_ratio:
        return f"افزایش مصرف توکن از {base_tokens:.0f} به {avg_tokens:.0f}"
    return None


async def observe_probation(
    session: AsyncSession,
    revision_ids: list[uuid.UUID],
    success: bool,
    tokens: int,
    probation_runs: int = 3,
    token_ratio: float = 1.3,
) -> list[str]:
    """ثبت نتیجه یک اجرا برای یادگیری‌های آزمایشی دخیل؛ پسرفت → Undo خودکار، موفقیت کافی → تایید."""
    purged: list[str] = []
    for rid in revision_ids:
        rev = await session.get(MemoryRevision, rid)
        if rev is None or rev.status != "probationary":
            continue
        obs = dict(rev.observed or {})
        obs["runs"] = obs.get("runs", 0) + 1
        obs["failures"] = obs.get("failures", 0) + (0 if success else 1)
        obs["tokens"] = obs.get("tokens", 0) + tokens
        rev.observed = obs
        rev.probation_runs = obs["runs"]
        reason = is_regression(rev.baseline or {}, obs, token_ratio)
        if reason:
            await undo_revision(session, rev.id, actor="evaluator", reason=f"پاکسازی خودکار: {reason}")
            purged.append(str(rev.id))
            continue
        if obs["runs"] >= probation_runs and obs["failures"] == 0:
            rev.status = "confirmed"
            model = ENTITY_MODELS[rev.entity_type]
            obj = await session.get(model, rev.entity_id)
            if obj is not None and getattr(obj, "status", None) == "probationary":
                obj.status = "active"
    await session.flush()
    return purged
