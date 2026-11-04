"""Durable, provider-neutral request attempt accounting.

A provider response may be billable even when the agent transaction later fails.
For that reason each attempt is committed independently *before* network I/O and
finalized independently after it. Unknown outcomes retain reserved budget rather
than being silently treated as free. Provider reports are the source of truth for
tokens; input/output details are subsets, not additive token counts.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import UsageEvent
from xagent_core.redact import scrub
from xagent_core.settings_store import DreamSettings, GeneralSettings, StopLossSettings, get_setting

from .providers.base import ChatRequest, LLMResponse, ProviderError

_BUDGET_LOCK = 816192473


class BudgetDenied(ProviderError):
    """Preflight rejected; no provider call was sent."""


def estimate_input_tokens(req: ChatRequest) -> int:
    # Deliberately conservative, with a reserve for hidden tool/system framing.
    chars = len(req.system_stable) + len(req.system_dynamic)
    image_count = 0
    for m in req.messages:
        chars += len(m.content) + 64
        image_count += len(m.images)
        for t in m.tool_calls:
            chars += len(t.name) + len(json.dumps(t.arguments, ensure_ascii=False))
    for t in req.tools:
        chars += len(t.description) + len(json.dumps(t.parameters, ensure_ascii=False))
    return max(256, chars // 2 + image_count * 4096)


def _boundary(tz: str, month: bool = False) -> datetime:
    local = utcnow().astimezone(ZoneInfo(tz))
    local = local.replace(day=1 if month else local.day, hour=0, minute=0, second=0, microsecond=0)
    return local.astimezone(UTC)


async def begin_attempt(model: Any, req: ChatRequest, *, purpose: str,
                        run_id: uuid.UUID | None = None) -> uuid.UUID:
    """Atomic budget reservation and independent committed attempt.

    No network operation should follow if this raises.
    """
    if await bus.is_stop_all():
        raise BudgetDenied("STOP_ALL: فراخوانی جدید ممنوع است")
    attempt_id = uuid.uuid4()
    predicted_input = estimate_input_tokens(req)
    max_output = max(1, int(req.max_tokens))
    reserve = model.cost(predicted_input, max_output, 0, 0)
    if reserve < 0:
        raise BudgetDenied("قیمت مدل نامعتبر است")
    async with session_scope() as s:
        await s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BUDGET_LOCK})
        settings = await get_setting(s, "stoploss", StopLossSettings)
        general = await get_setting(s, "general", GeneralSettings)
        start_day = _boundary(general.timezone)
        start_month = _boundary(general.timezone, month=True)

        async def spending(since: datetime, purpose_only: bool = False) -> Decimal:
            q = select(func.coalesce(func.sum(
                UsageEvent.cost_usd + UsageEvent.reserved_cost_usd), 0)).where(UsageEvent.ts >= since)
            if purpose_only:
                q = q.where(UsageEvent.purpose == purpose)
            return Decimal((await s.execute(q)).scalar_one())

        if await spending(start_day) + reserve > Decimal(str(settings.daily_cost_usd)):
            raise BudgetDenied("سقف روزانه هزینه اجازه درخواست جدید نمی‌دهد")
        if await spending(start_month) + reserve > Decimal(str(settings.monthly_cost_usd)):
            raise BudgetDenied("سقف ماهانه هزینه اجازه درخواست جدید نمی‌دهد")
        if run_id is not None:
            used = Decimal((await s.execute(select(func.coalesce(func.sum(
                UsageEvent.cost_usd + UsageEvent.reserved_cost_usd), 0)).where(UsageEvent.run_id == run_id))).scalar_one())
            if used + reserve > Decimal(str(settings.max_cost_per_task_usd)):
                raise BudgetDenied("سقف هزینه هر تسک برای درخواست جدید کافی نیست")
        if purpose == "dream":
            dream = await get_setting(s, "dream", DreamSettings)
            if not dream.enabled:
                raise BudgetDenied("Dream غیرفعال است")
            if await spending(start_day, purpose_only=True) + reserve > Decimal(str(dream.max_cost_usd)):
                raise BudgetDenied("سقف هزینه مستقل Dream پر شده است")
            n = (await s.execute(select(func.count()).select_from(UsageEvent).where(
                UsageEvent.ts >= start_day, UsageEvent.purpose == "dream"))).scalar_one()
            if n >= dream.max_calls:
                raise BudgetDenied("حداکثر تعداد درخواست Dream رسیده است")
            used_tokens = (await s.execute(select(func.coalesce(func.sum(
                UsageEvent.tokens_in + UsageEvent.tokens_out + UsageEvent.reserved_tokens), 0)).where(
                    UsageEvent.ts >= start_day, UsageEvent.purpose == "dream"))).scalar_one()
            if int(used_tokens) + predicted_input + max_output > dream.max_tokens:
                raise BudgetDenied("سقف توکن Dream اجازه این درخواست را نمی‌دهد")
        s.add(UsageEvent(
            id=attempt_id, request_id=str(attempt_id), run_id=run_id, model_id=model.id,
            provider_kind=model.provider_kind, model_name=model.model_name,
            purpose=purpose, attempt_status="pending", success=False,
            cost_estimated=model.price_estimated, reserved_cost_usd=reserve,
            reserved_tokens=predicted_input + max_output,
            usage_details={"preflight_input_estimate": predicted_input, "max_output": max_output},
        ))
    return attempt_id


async def finish_attempt(attempt_id: uuid.UUID, *, response: LLMResponse | None = None,
                         cost: Decimal | None = None, error: BaseException | None = None,
                         known_rejected: bool = False) -> str:
    """Commit outcome independently of the agent. Idempotent for duplicate callbacks."""
    async with session_scope() as s:
        row = await s.get(UsageEvent, attempt_id, with_for_update=True)
        if row is None:
            raise RuntimeError("Ledger attempt missing")
        if row.attempt_status != "pending":
            return row.attempt_status
        row.finished_at = utcnow()
        if response is not None:
            u = response.usage
            row.attempt_status = "confirmed" if u.reported else "unknown"
            row.success = True
            row.tokens_in = u.input_total
            row.tokens_out = u.output
            row.cache_read = u.cache_read
            row.cache_write = u.cache_write
            row.latency_ms = response.latency_ms
            row.model_name = response.model or row.model_name
            row.provider_request_id = response.provider_request_id
            row.usage_details = {**(row.usage_details or {}),
                                 "reasoning_tokens": u.reasoning_tokens,
                                 "audio_input_tokens": u.audio_input_tokens,
                                 "audio_output_tokens": u.audio_output_tokens,
                                 "reported": u.reported, "provider": u.details}
            if u.reported:
                row.cost_usd = cost if cost is not None else Decimal(0)
                row.reserved_cost_usd = 0
                row.reserved_tokens = 0
        else:
            row.attempt_status = "rejected" if known_rejected else "unknown"
            row.success = False
            row.error = scrub(str(error or "unknown provider outcome"))[:1000]
            if known_rejected:
                row.reserved_cost_usd = 0
                row.reserved_tokens = 0
        return row.attempt_status
