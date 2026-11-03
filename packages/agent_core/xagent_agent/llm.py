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
from xagent_core.models import UsageEvent

from .providers.base import ChatRequest, LLMMessage, LLMResponse, ProviderError, ToolSpec
from .providers.registry import BoundModel


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
) -> CallResult:
    if not chain:
        raise NoModelError(
            "هیچ مدل هوش مصنوعی فعالی تنظیم نشده است. از «تنظیمات ← هوش مصنوعی» یک مدل اضافه و فعال کنید."
        )
    last_err: Exception | None = None
    for model in chain:
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
        for attempt in range(3):
            try:
                resp = await model.provider.chat(req)
                u = resp.usage
                cost = model.cost(u.input_uncached, u.output, u.cache_read, u.cache_write)
                session.add(
                    UsageEvent(
                        run_id=run_id,
                        model_id=model.id,
                        provider_kind=model.provider_kind,
                        model_name=model.model_name,
                        purpose=purpose,
                        tokens_in=u.input_total,
                        tokens_out=u.output,
                        cache_read=u.cache_read,
                        cache_write=u.cache_write,
                        cost_usd=cost,
                        latency_ms=resp.latency_ms,
                        success=True,
                    )
                )
                await session.flush()
                await bus.publish(
                    "usage",
                    {
                        "run_id": str(run_id) if run_id else None,
                        "model": model.label,
                        "purpose": purpose,
                        "tokens_in": u.input_total,
                        "tokens_out": u.output,
                        "cache_read": u.cache_read,
                        "cost": float(cost),
                    },
                )
                return CallResult(resp, model, cost)
            except ProviderError as e:
                last_err = e
                session.add(
                    UsageEvent(
                        run_id=run_id,
                        model_id=model.id,
                        provider_kind=model.provider_kind,
                        model_name=model.model_name,
                        purpose=purpose,
                        success=False,
                        error=str(e)[:1000],
                    )
                )
                await session.flush()
                if e.retryable and attempt < 2:
                    await asyncio.sleep(1.5 * (2**attempt))
                    continue
                break  # مدل بعدی در زنجیره
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
