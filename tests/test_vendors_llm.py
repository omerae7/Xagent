"""پیوست ۲۷ — xAI Grok، Kimi Open Platform و Kimi Code با API جعلی محلی (بدون کلید واقعی)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fake_vendors import Vendors, start
from sqlalchemy import select

from xagent_agent.control.capabilities import DiscoveryError, list_provider_models, sync_provider
from xagent_agent.llm import call_llm
from xagent_agent.providers.base import ChatRequest, LLMMessage, ProviderError, ToolSpec
from xagent_agent.providers.openai_compat import OpenAICompatProvider
from xagent_agent.providers.params import request_params, xai_price_per_m
from xagent_agent.providers.registry import load_model, provider_status
from xagent_core import bus
from xagent_core.db import session_scope
from xagent_core.models import AIModel, AIProvider, UsageEvent
from xagent_core.security import encrypt_secret

pytestmark = pytest.mark.asyncio
TOOL = ToolSpec("lookup", "جست‌وجو", {"type": "object", "properties": {"q": {"type": "string"}}})


@pytest.fixture(scope="module")
def vendors():
    v = Vendors()
    server = start(v)
    yield v
    server.should_exit = True


async def _provider(kind: str, base: str, key: str = "xk-test-0001") -> uuid.UUID:
    async with session_scope() as s:
        p = AIProvider(name=f"{kind}-{uuid.uuid4().hex[:6]}", kind=kind, base_url=base, api_key_enc=encrypt_secret(key),
                       enabled=True)
        s.add(p)
        await s.flush()
        return p.id


# ───────────── واحد قیمت xAI ─────────────
@pytest.mark.parametrize("raw,expected", [
    (12500, Decimal("1.25")), (20000, Decimal("2")), (100000, Decimal("10")), (5000, Decimal("0.5")),
    (1, Decimal("0.0001")), ("3000", Decimal("0.3")),
    (0, None), (None, None), (-5, None), ("abc", None), (True, None), (float("nan"), None),
])
async def test_xai_price_unit_conversion(raw, expected):
    """سنت دلار به ازای ۱۰۰ میلیون توکن → دلار به ازای ۱ میلیون: مقدار ÷ ۱۰٬۰۰۰؛ نامعتبر/صفر = نامعلوم (نه رایگان)."""
    assert xai_price_per_m(raw) == expected


async def test_xai_discovery_prices_caps_and_long_context(vendors):
    vendors.reset()
    pid = await _provider("xai", vendors.url("xai/v1"))
    async with session_scope() as s:
        p = await s.get(AIProvider, pid)
        found, nxt = await list_provider_models(p)
        assert nxt is None
        by = {d.id: d for d in found}
        assert set(by) == {"grok-test", "grok-mini-test", "grok-noprice"}
        g = by["grok-test"]
        assert g.price == (Decimal("2"), Decimal("10")) and g.cache_price == Decimal("0.5")
        assert g.caps == {"vision": True} and g.context_window == 256000
        assert g.meta["pricing_detail"]["unit"] == "usd_cents_per_100m_tokens"
        assert g.meta["pricing_detail"]["long_context"] == {"threshold": 128000, "in": "4", "out": "20", "cache": "1"}
        assert g.meta["aliases"] == ["grok-test-latest"]
        mini = by["grok-mini-test"]
        assert mini.meta["reasoning"]["efforts"] == ["low", "high"]
        assert "long_context" not in mini.meta["pricing_detail"]  # مقدار ۰ = بدون قیمت جداگانه
        assert by["grok-noprice"].price is None and by["grok-noprice"].meta["pricing_detail"]["valid"] is False
        # کشف به همان کلید وابسته است: هر دو نقطه با همان کلید فراخوانی شدند
        assert vendors.auth_of("/xai/v1/") == {"Bearer xk-test-0001"}
        r = await sync_provider(s, p)
        assert set(r["created"]) == {"grok-test", "grok-mini-test", "grok-noprice"}
        rows = {m.model_name: m for m in (await s.execute(select(AIModel).where(AIModel.provider_id == pid))).scalars()}
        gt = rows["grok-test"]
        assert gt.pricing_source == "provider_api" and gt.price_in == Decimal("2") and gt.price_out == Decimal("10")
        assert gt.price_cache_read == Decimal("0.5") and not gt.enabled
        assert rows["grok-noprice"].pricing_source == "unknown"
        for m in rows.values():
            m.enabled = True
        await s.flush()
        b = await load_model(s, gt.id)
        assert b.long_context and b.long_context[0] == 128000
        # زیر آستانه: قیمت عادی؛ بالای آستانه: قیمت بافت بلند
        assert b.cost(1_000_000, 0, 0, 0) == Decimal("4")  # ۱M > ۱۲۸K → ۴ دلار
        assert b.cost(100_000, 10_000, 0, 0) == Decimal("0.3")  # 0.2 + 0.1
        nop = await load_model(s, rows["grok-noprice"].id)
        assert nop.price_estimated and nop.price_in > 0  # مدل بدون قیمت رایگان فرض نمی‌شود


async def test_xai_discovery_401_is_needs_setup(vendors, app_client):
    vendors.reset()
    vendors.mode["xai-models"] = "401"
    pid = await _provider("xai", vendors.url("xai/v1"))
    async with session_scope() as s:
        with pytest.raises(DiscoveryError) as e:
            await list_provider_models(await s.get(AIProvider, pid))
        assert e.value.status == 401
    r = await app_client.get(f"/api/settings/ai/providers/{pid}/discover")
    assert r.status_code == 502 and "401" in r.json()["detail"]
    r = await app_client.get("/api/settings/ai/providers")
    me = next(x for x in r.json()["providers"] if x["id"] == str(pid))
    assert me["status"] == "needs_setup"
    vendors.mode.clear()
    r = await app_client.get(f"/api/settings/ai/providers/{pid}/discover")
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()["items"]}
    assert items["grok-test"]["price_in"] == 2.0 and items["grok-test"]["price_status"] == "provider_api"
    assert items["grok-noprice"]["price_in"] is None and items["grok-noprice"]["price_status"] == "unknown"
    r = await app_client.get("/api/settings/ai/providers")
    assert next(x for x in r.json()["providers"] if x["id"] == str(pid))["status"] == "available"


async def test_xai_chat_tool_call_result_usage_and_reasoning_param(vendors):
    vendors.reset()
    p = OpenAICompatProvider("xai", "xk-test-0001", vendors.url("xai/v1"))
    try:
        meta = {"reasoning": {"efforts": ["low", "high"]}}
        extra = request_params("xai", "grok-mini-test", meta, "high")
        assert extra == {"reasoning_effort": "high"}
        assert request_params("xai", "grok-test", {}, "high") == {}  # مدل بدون اعلام → پارامتر ارسال نمی‌شود
        assert request_params("xai", "grok-mini-test", meta, "medium") == {}  # مقدار غیرمجاز ارسال نمی‌شود
        msgs = [LLMMessage("user", "بگرد")]
        r1 = await p.chat(ChatRequest("grok-mini-test", "sys", "", msgs, tools=[TOOL], extra=extra))
        assert r1.tool_calls and r1.tool_calls[0].name == "lookup" and r1.tool_calls[0].arguments == {"q": "x"}
        assert r1.usage.cache_read == 20 and r1.usage.input_uncached == 100 and r1.usage.reasoning_tokens == 7
        msgs += [LLMMessage("assistant", "", tool_calls=r1.tool_calls, raw=r1.raw),
                 LLMMessage("tool", "نتیجه", tool_call_id="call_1", tool_name="lookup")]
        r2 = await p.chat(ChatRequest("grok-mini-test", "sys", "", msgs, tools=[TOOL]))
        assert r2.text == "پاسخ نهایی" and not r2.tool_calls
        b1, b2 = vendors.bodies("/xai/v1/chat")
        assert b1["reasoning_effort"] == "high" and "reasoning_effort" not in b2
        assert b2["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "نتیجه"}
    finally:
        await p.aclose()


@pytest.mark.parametrize("mode,status,retryable", [("401", 401, False), ("429", 429, True), ("500", 500, True)])
async def test_xai_http_errors(vendors, mode, status, retryable):
    vendors.reset()
    vendors.mode["xai-chat"] = mode
    p = OpenAICompatProvider("xai", "xk-test-0001", vendors.url("xai/v1"))
    try:
        with pytest.raises(ProviderError) as e:
            await p.chat(ChatRequest("grok-test", "", "", [LLMMessage("user", "hi")]))
        assert e.value.status == status and e.value.retryable is retryable
        assert "xk-test-0001" not in str(e.value)
    finally:
        await p.aclose()


async def test_xai_timeout_is_retryable(vendors):
    vendors.reset()
    vendors.mode["xai-chat"] = "slow"
    p = OpenAICompatProvider("xai", "xk-test-0001", vendors.url("xai/v1"), timeout=0.5)
    try:
        with pytest.raises(ProviderError) as e:
            await p.chat(ChatRequest("grok-test", "", "", [LLMMessage("user", "hi")]))
        assert e.value.retryable
    finally:
        await p.aclose()


async def test_failover_from_rejected_xai_to_kimi(vendors):
    """کلید xAI رد می‌شود (۴۰۱) → زنجیره به Kimi می‌رود؛ هر تلاش جداگانه در دفتر مصرف ثبت می‌شود."""
    await bus.set_stop_all(False)
    vendors.reset()
    vendors.mode["xai-chat"] = "401"
    marker = f"failover-{uuid.uuid4().hex[:8]}"
    xp = await _provider("xai", vendors.url("xai/v1"))
    kp = await _provider("moonshot", vendors.url("kimi/v1"), key="mk-test-0002")
    async with session_scope() as s:
        xm = AIModel(provider_id=xp, model_name="grok-test", enabled=True, capabilities={"tools": True},
                     price_in=Decimal("2"), price_out=Decimal("10"), pricing_source="provider_api")
        km = AIModel(provider_id=kp, model_name="kimi-plain", enabled=True, capabilities={"tools": True},
                     price_in=Decimal("0.6"), price_out=Decimal("2.5"), pricing_source="manual")
        s.add_all([xm, km])
        await s.flush()
        chain = [await load_model(s, xm.id), await load_model(s, km.id)]
        res = await call_llm(s, chain, purpose=marker, system_stable="", system_dynamic="",
                             messages=[LLMMessage("user", "سلام")])
        assert res.model.model_name == "kimi-plain" and res.response.text == "OK"
        assert res.response.usage.cache_read == 40  # usage.cached_tokens در Kimi
    async with session_scope() as s:
        rows = (await s.execute(select(UsageEvent).where(UsageEvent.purpose == marker))).scalars().all()
        st = sorted((r.model_name, r.attempt_status) for r in rows)
        assert ("kimi-plain", "confirmed") in st and any(n == "grok-test" and a != "confirmed" for n, a in st)
    # کلید هر سرویس فقط به همان سرویس رفت
    assert vendors.auth_of("/xai/v1/") == {"Bearer xk-test-0001"}
    assert vendors.auth_of("/kimi/v1/") == {"Bearer mk-test-0002"}


# ───────────── Kimi Open Platform ─────────────
async def test_kimi_discovery_and_unknown_price(vendors):
    vendors.reset()
    pid = await _provider("moonshot", vendors.url("kimi/v1"), key="mk-test-0002")
    async with session_scope() as s:
        p = await s.get(AIProvider, pid)
        found, _ = await list_provider_models(p)
        by = {d.id: d for d in found}
        assert by["kimi-test-vision"].caps == {"vision": True} and by["kimi-test-vision"].context_window == 262144
        th = by["kimi-test-thinking"].meta["thinking"]
        assert th["value"] and th["control"] == "always"  # همیشه-استدلالی → پارامتر thinking ارسال نمی‌شود
        assert by["kimi-plain"].caps == {} and by["kimi-plain"].meta == {}
        assert all(d.price is None for d in found)  # API قیمت اعلام نمی‌کند → نامعلوم
        await sync_provider(s, p)
        m = (await s.execute(select(AIModel).where(AIModel.provider_id == pid,
                                                   AIModel.model_name == "kimi-plain"))).scalar_one()
        m.enabled = True
        await s.flush()
        b = await load_model(s, m.id)
        assert b.price_estimated and b.price_in > 0


async def test_kimi_thinking_toggle_reasoning_isolated_and_replayed(vendors):
    vendors.reset()
    p = OpenAICompatProvider("moonshot", "mk-test-0002", vendors.url("kimi/v1"))
    try:
        meta = {"thinking": {"value": True, "control": "toggle", "source": "manual"}}
        assert request_params("moonshot", "kimi-x", meta, "low") == {"thinking": {"type": "disabled"}}
        assert request_params("moonshot", "kimi-x", {"thinking": {"value": True, "control": "always"}}, "high") == {}
        assert request_params("moonshot", "kimi-x", {}, "high") == {}
        extra = request_params("moonshot", "kimi-x", meta, "medium")
        assert extra == {"thinking": {"type": "enabled"}}
        msgs = [LLMMessage("user", "ابزار را صدا بزن")]
        r1 = await p.chat(ChatRequest("kimi-x", "sys", "", msgs, tools=[TOOL], extra=extra))
        assert r1.tool_calls and r1.text == ""  # استدلال وارد متن پاسخ نمی‌شود
        assert r1.raw == {"kind": "openai_compat", "provider": "moonshot", "reasoning_content": "استدلال داخلی مدل"}
        msgs += [LLMMessage("assistant", "", tool_calls=r1.tool_calls, raw=r1.raw),
                 LLMMessage("tool", "داده", tool_call_id="call_1", tool_name="lookup")]
        r2 = await p.chat(ChatRequest("kimi-x", "sys", "", msgs, tools=[TOOL], extra=extra))
        assert r2.text == "پاسخ نهایی" and "استدلال" not in r2.text
        b1, b2 = vendors.bodies("/kimi/v1/chat")
        assert b1["thinking"] == {"type": "enabled"} and b1["tool_choice"] == "auto"
        assistant = next(m for m in b2["messages"] if m["role"] == "assistant")
        assert assistant["reasoning_content"] == "استدلال داخلی مدل"  # بازپخش فقط در حلقه ابزار همان ارائه‌دهنده
        # استدلال Kimi هرگز به ارائه‌دهنده دیگر (مثلاً xAI) بازپخش نمی‌شود
        other = OpenAICompatProvider._convert_msg(LLMMessage("assistant", "", tool_calls=r1.tool_calls, raw=r1.raw), "xai")
        assert "reasoning_content" not in other[0]
    finally:
        await p.aclose()


async def test_kimi_vision_message_format(vendors):
    vendors.reset()
    from xagent_agent.providers.base import ImagePart

    p = OpenAICompatProvider("moonshot", "mk-test-0002", vendors.url("kimi/v1"))
    try:
        await p.chat(ChatRequest("kimi-test-vision", "", "", [LLMMessage("user", "چه می‌بینی؟",
                                                                       images=[ImagePart("image/png", "AAAA")])]))
        content = vendors.bodies("/kimi/v1/chat")[0]["messages"][-1]["content"]
        assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    finally:
        await p.aclose()


async def test_kimi_rate_limit_retryable(vendors):
    vendors.reset()
    vendors.mode["kimi-chat"] = "429"
    p = OpenAICompatProvider("moonshot", "mk-test-0002", vendors.url("kimi/v1"))
    try:
        with pytest.raises(ProviderError) as e:
            await p.chat(ChatRequest("kimi-plain", "", "", [LLMMessage("user", "hi")]))
        assert e.value.status == 429 and e.value.retryable
    finally:
        await p.aclose()


# ───────────── Kimi Code (مستقل) ─────────────
async def test_kimi_code_separate_key_docs_listing_and_entitlement(vendors):
    vendors.reset()
    pid = await _provider("kimi_code", vendors.url("kimicode/coding/v1"), key="kc-test-0003")
    async with session_scope() as s:
        p = await s.get(AIProvider, pid)
        found, _ = await list_provider_models(p)  # ‎/models وجود ندارد → فهرست مستند بدون ادعای قابلیت/قیمت
        assert {d.id for d in found} == {"kimi-for-coding", "kimi-for-coding-highspeed", "k3", "k3-256k"}
        assert all(d.caps == {} and d.price is None for d in found)
    kc = OpenAICompatProvider("kimi_code", "kc-test-0003", vendors.url("kimicode/coding/v1"))
    try:
        r = await kc.chat(ChatRequest("kimi-for-coding", "", "", [LLMMessage("user", "hi")]))
        assert r.text == "OK"
        assert vendors.auth_of("/kimicode/") == {"Bearer kc-test-0003"}
        assert not vendors.bodies("/kimi/v1/")  # هیچ درخواستی به Open Platform نرفت
        vendors.mode["kimicode-chat"] = "401"
        with pytest.raises(ProviderError) as e:
            await kc.chat(ChatRequest("k3", "", "", [LLMMessage("user", "hi")]))
        assert e.value.status == 401 and not e.value.retryable
    finally:
        await kc.aclose()


async def test_provider_status_mapping():
    def P(**kw):
        base = dict(id=uuid.uuid4(), kind="xai", api_key_enc=b"x", last_test_ok=None, last_test_error=None)
        base.update(kw)
        return AIProvider(**base)

    assert provider_status(P(api_key_enc=None), [])[0] == "needs_setup"
    assert provider_status(P(), [])[0] == "untested"
    assert provider_status(P(last_test_ok=False, last_test_error="HTTP 401 (کلید)"), [])[0] == "needs_setup"
    assert provider_status(P(last_test_ok=False, last_test_error="HTTP 404"), [])[0] == "unsupported"
    assert provider_status(P(last_test_ok=False, last_test_error="HTTP 429"), [])[0] == "temporarily_unavailable"
    p = P(last_test_ok=True)
    assert provider_status(p, [])[0] == "available"
    assert provider_status(p, [AIModel(provider_id=p.id, model_name="m", enabled=True, health="ok")])[0] == "ready"
