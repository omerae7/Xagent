"""مأموریت جامع — مرحله ۶: مالکیت واحد تلاش مجدد (A6) و حسابداری هر تلاش فیزیکی.

Transport جعلی HTTP زیر SDK رسمی Anthropic؛ هیچ درخواست واقعی به ارائه‌دهنده نمی‌رود.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx2 as httpx
import pytest
from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import session_scope
from xagent_core.models import AIModel, AIProvider, UsageEvent
from xagent_core.security import encrypt_secret

pytestmark = pytest.mark.asyncio


def _transport(status: int, counter: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if status == 200:
            return httpx.Response(200, json={
                "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 3}})
        return httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "boom"}},
                              headers={"retry-after": "0"})
    return httpx.MockTransport(handler)


async def test_A6_single_adapter_call_is_single_wire_attempt():
    from xagent_agent.providers.anthropic_provider import AnthropicProvider
    from xagent_agent.providers.base import ChatRequest, LLMMessage, ProviderError

    counter = {"n": 0}
    p = AnthropicProvider("sk-ant-test", http_client=httpx.AsyncClient(transport=_transport(500, counter)))
    with pytest.raises(ProviderError) as e:
        await p.chat(ChatRequest("claude-haiku-5-5", "", "", [LLMMessage("user", "hi")], max_tokens=16))
    assert e.value.retryable
    assert counter["n"] == 1, f"یک فراخوانی آداپتور {counter['n']} درخواست HTTP ساخت (تلاش مجدد پنهان SDK)"


async def test_A6_three_attempt_policy_is_three_wire_requests_and_three_ledger_rows(app_client, monkeypatch):
    from xagent_agent import llm as llm_mod
    from xagent_agent.llm import call_llm
    from xagent_agent.providers.anthropic_provider import AnthropicProvider
    from xagent_agent.providers.base import LLMMessage, ProviderError
    from xagent_agent.providers.registry import load_model

    await bus.set_stop_all(False)

    async def fast_sleep(_s):
        return None
    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast_sleep)
    marker = f"a6-{uuid.uuid4().hex[:8]}"
    async with session_scope() as s:
        prov = AIProvider(name=f"anth-{marker}", kind="anthropic", api_key_enc=encrypt_secret("sk-ant-test"), enabled=True)
        s.add(prov)
        await s.flush()
        m = AIModel(provider_id=prov.id, model_name="claude-haiku-5-5", enabled=True, capabilities={"tools": True},
                    price_in=Decimal("1"), price_out=Decimal("5"), pricing_source="manual")
        s.add(m)
        await s.flush()
        bound = await load_model(s, m.id)
        counter = {"n": 0}
        bound.provider = AnthropicProvider("sk-ant-test", http_client=httpx.AsyncClient(transport=_transport(500, counter)))
        with pytest.raises(ProviderError):
            await call_llm(s, [bound], purpose=marker, system_stable="", system_dynamic="",
                           messages=[LLMMessage("user", "سلام")], max_tokens=16)
    async with session_scope() as s:
        rows = (await s.execute(select(UsageEvent).where(UsageEvent.purpose == marker))).scalars().all()
    assert counter["n"] == 3, f"سیاست سه‌تلاشی {counter['n']} درخواست فیزیکی ساخت"
    assert len(rows) == 3, f"{len(rows)} رکورد دفتر مصرف برای ۳ تلاش"
    # ۵۰۰ ممکن است هزینه داشته باشد: نامعلوم، نه «صفر تأییدشده»
    assert all(r.attempt_status != "confirmed" for r in rows)


async def test_A6_success_is_one_request_one_confirmed_row(app_client):
    from xagent_agent.llm import call_llm
    from xagent_agent.providers.anthropic_provider import AnthropicProvider
    from xagent_agent.providers.base import LLMMessage
    from xagent_agent.providers.registry import load_model

    await bus.set_stop_all(False)
    marker = f"a6ok-{uuid.uuid4().hex[:8]}"
    async with session_scope() as s:
        prov = AIProvider(name=f"anth-{marker}", kind="anthropic", api_key_enc=encrypt_secret("sk-ant-test"), enabled=True)
        s.add(prov)
        await s.flush()
        m = AIModel(provider_id=prov.id, model_name="claude-haiku-5-5", enabled=True, capabilities={"tools": True},
                    price_in=Decimal("1"), price_out=Decimal("5"), pricing_source="manual")
        s.add(m)
        await s.flush()
        bound = await load_model(s, m.id)
        counter = {"n": 0}
        bound.provider = AnthropicProvider("sk-ant-test", http_client=httpx.AsyncClient(transport=_transport(200, counter)))
        res = await call_llm(s, [bound], purpose=marker, system_stable="", system_dynamic="",
                             messages=[LLMMessage("user", "سلام")], max_tokens=16)
    async with session_scope() as s:
        rows = (await s.execute(select(UsageEvent).where(UsageEvent.purpose == marker))).scalars().all()
    assert res.response.text == "OK" and counter["n"] == 1
    assert len(rows) == 1 and rows[0].attempt_status == "confirmed" and rows[0].tokens_in == 12
