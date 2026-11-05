"""Provider usage subsets, CSV reconciliation, and atomic budget tests."""
from __future__ import annotations

import asyncio
import csv
import io
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from xagent_agent.providers.base import ChatRequest, LLMMessage
from xagent_agent.providers.google import GoogleProvider
from xagent_agent.providers.openai_responses import ResponsesProvider
from xagent_agent.providers.registry import load_model
from xagent_agent.usage_ledger import BudgetDenied, begin_aux_attempt
from xagent_core import bus
from xagent_core.db import session_scope
from xagent_core.models import UsageEvent
from xagent_core.settings_store import StopLossSettings, get_setting, set_setting
from xagent_core.timeutil import get_zone, now_utc


@pytest.mark.asyncio
async def test_openai_reasoning_cached_audio_are_subsets_not_extra_tokens():
    def handler(_):
        return httpx.Response(200, headers={"x-request-id": "req_123"}, json={
            "id": "resp_1", "status": "completed", "model": "model-x",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {
                "input_tokens": 990, "output_tokens": 510,
                "input_tokens_details": {"cached_tokens": 300, "audio_tokens": 40},
                "output_tokens_details": {"reasoning_tokens": 200, "audio_tokens": 20},
            },
        })
    p = ResponsesProvider("openai", "fake")
    await p._client.aclose()
    p._client = httpx.AsyncClient(base_url="https://api.openai.com/v1",
                                  transport=httpx.MockTransport(handler))
    try:
        res = await p.chat(ChatRequest(model="model-x", system_stable="",
              system_dynamic="", messages=[LLMMessage("user", "test")], max_tokens=510))
        assert res.usage.input_total == 990
        assert res.usage.output == 510
        assert res.usage.reasoning_tokens == 200
        assert res.usage.audio_input_tokens == 40
        assert res.usage.audio_output_tokens == 20
        assert res.usage.input_total + res.usage.output == 1500
        assert res.provider_request_id == "req_123"
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_google_thoughts_are_reported_separately():
    def handler(_):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 100, "cachedContentTokenCount": 25,
                "candidatesTokenCount": 40, "thoughtsTokenCount": 20,
            },
        })
    p = GoogleProvider("fake")
    await p._client.aclose()
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        res = await p.chat(ChatRequest(model="gemini-test", system_stable="",
              system_dynamic="", messages=[LLMMessage("user", "test")]))
        assert res.usage.input_total == 100
        assert res.usage.output == 60
        assert res.usage.reasoning_tokens == 20
    finally:
        await p.aclose()


@pytest.mark.asyncio
async def test_atomic_preflight_reservations_never_exceed_owner_daily_cap(configured_model):
    await bus.set_stop_all(False)
    async with session_scope() as session:
        old = await get_setting(session, "stoploss", StopLossSettings)

        bound = await load_model(session, configured_model)
        reserve = bound.cost(256, 1, 0, 0)

        local = now_utc().astimezone(get_zone("Asia/Tehran"))
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        used = Decimal((await session.execute(select(func.coalesce(func.sum(
            UsageEvent.cost_usd + UsageEvent.reserved_cost_usd), 0)).where(UsageEvent.ts >= day))).scalar_one())
        limit = used + reserve * 3 + Decimal("0.0000001")
        await set_setting(session, "stoploss", old.model_copy(update={
            "daily_cost_usd": float(limit), "monthly_cost_usd": max(60.0, float(limit) + 1),
        }))
    ids = []
    try:
        async def attempt():
            try:
                return await begin_aux_attempt(bound, purpose="budget_concurrent",
                                               input_hint=256, output_hint=1)
            except BudgetDenied:
                return None
        results = await asyncio.gather(*(attempt() for _ in range(8)))
        ids = [x for x in results if x]
        assert 1 <= len(ids) <= 3, f"Atomic cap should allow <=3 attempts, got {len(ids)}"
    finally:
        async with session_scope() as session:
            for rid in ids:
                row = await session.get(UsageEvent, rid)
                if row:
                    await session.delete(row)
            await set_setting(session, "stoploss", old)


@pytest.mark.asyncio
async def test_csv_reconciliation_does_not_assume_provider_key_identity(app_client):
    start = "2026-11-04T00:01:00+00:00"
    data = io.StringIO()
    writer = csv.DictWriter(data, fieldnames=[
        "start_time_iso", "model", "num_model_requests", "api_key_id", "input_tokens",
        "output_tokens", "input_cached_tokens"])
    writer.writeheader()
    writer.writerow({"start_time_iso": start, "model": "gpt-6.1-sol",
                     "num_model_requests": 2, "api_key_id": "key_external",
                     "input_tokens": 1980, "output_tokens": 700, "input_cached_tokens": 0})
    res = await app_client.post("/api/usage/reconcile",
        files={"file": ("usage.csv", data.getvalue().encode(), "text/csv")})
    assert res.status_code == 200, res.text
    doc = res.json()
    assert doc["external_key_count"] == 1
    row = next(x for x in doc["by_model"] if x["model"] == "gpt-6.1-sol")
    assert row["official"]["requests"] == 2
    assert row["official"]["input"] == 1980
    assert "تجمیعی" in doc["notice"]
