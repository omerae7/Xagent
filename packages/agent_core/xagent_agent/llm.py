"""فراخوانی مدل با زنجیره Fallback، تلاش مجدد، ثبت دقیق مصرف توکن و هزینه."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.models import AIModel

from .providers.base import ChatRequest, LLMMessage, LLMResponse, ProviderError, ToolSpec
from .providers.registry import BoundModel
from .usage_ledger import begin_attempt, finish_attempt


@dataclass
class CallResult:
    response: LLMResponse
    model: BoundModel
    cost: Decimal


class NoModelError(RuntimeError):
    pass


async def call_llm(
    session: AsyncSession,
    chain: list[BoundModel],
    *,
    purpose: str,
    system_stable: str,
    system_dynamic: str,
    messages: list[LLMMessage],
    tools: list[ToolSpec] | None = None,
    max_tokens: int = 4096,
    temperature: float | None = None,
    json_mode: bool = False,
    effort: str | None = None,
    run_id: uuid.UUID | None = None,
    origin_id: str | None = None,
) -> CallResult:
    if not chain:
        raise NoModelError(
            "هیچ مدل هوش مصنوعی فعالی تنظیم نشده است. از «تنظیمات ← هوش مصنوعی» یک مدل اضافه و فعال کنید."
        )
    last_err: Exception | None = None
    from .control import health

    # مدل‌های در دوره خنک‌شدن (خطای کلید/سهمیه/نبود مدل) به انتهای زنجیره می‌روند
    healthy, cooling = [], []
    for b in chain:
        row = await session.get(AIModel, b.id)
        (cooling if row is not None and health.in_cooldown(row) else healthy).append(b)
    ordered = healthy + cooling
    for idx, model in enumerate(ordered):
        if idx > 0:
            await bus.publish("model.fallback", {"run_id": str(run_id) if run_id else None, "to": model.label,
                                                 "reason": str(last_err)[:200] if last_err else ""})
        req = ChatRequest(
            model=model.model_name,
            system_stable=system_stable,
            system_dynamic=system_dynamic,
            messages=messages,
            tools=tools or [],
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
            effort=effort,  # type: ignore[arg-type]
        )
        # Each attempt is committed before sending. The independent ledger outlives
        # agent rollback; retries and fallback are separately billed/reserved.
        for attempt in range(1 if purpose == "dream" else 3):
            attempt_id = await begin_attempt(model, req, purpose=purpose, run_id=run_id, origin_id=origin_id)
            try:
                resp = await model.provider.chat(req)
            except ProviderError as e:
                # Some HTTP rejections are guaranteed not to have generated tokens.
                # Ambiguous 5xx / timeout outcomes retain their budget reservation.
                await finish_attempt(attempt_id, error=e,
                                     known_rejected=e.status in (400, 401, 403, 404, 422))
                last_err = e
                ec = await health.record_failure(session, model.id, str(e), e.status)
                if purpose != "dream" and e.retryable and ec.retryable and attempt < 2:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
                break
            except BaseException as e:
                # A connection failure or cancellation may still be billable.
                # Keep the persisted pending attempt if finalization is interrupted.
                await finish_attempt(attempt_id, error=e, known_rejected=False)
                raise
            u = resp.usage
            cost = model.cost(u.input_uncached, u.output, u.cache_read, u.cache_write)
            # If this fails, fail closed; original pending attempt is still durable.
            await finish_attempt(attempt_id, response=resp, cost=cost)
            await bus.publish(
                "usage",
                {"run_id": str(run_id) if run_id else None,
                 "attempt_id": str(attempt_id), "model": model.label,
                 "purpose": purpose, "tokens_in": u.input_total,
                 "tokens_out": u.output, "cache_read": u.cache_read,
                 "cost": float(cost), "unverified": not u.reported},
            )
            await health.record_success(session, model.id)
            return CallResult(resp, model, cost)
    raise ProviderError(f"همه مدل‌های زنجیره ناموفق بودند: {last_err}")


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(t)
        if m:
            try:
                v = json.loads(m.group(0))
                return v if isinstance(v, dict) else None
            except json.JSONDecodeError:
                return None
    return None
