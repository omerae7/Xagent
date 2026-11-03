"""Provider protocol tests: mocked HTTP, no paid API requests or account credentials."""
from __future__ import annotations

import json

import httpx
import pytest

from xagent_agent.providers.base import ChatRequest, LLMMessage, ToolCall, ToolSpec
from xagent_agent.providers.openai_responses import ResponsesProvider
from xagent_agent.providers.registry import build_provider


def _request(messages=None):
    return ChatRequest(
        model="gpt-future", system_stable="Follow owner instructions",
        system_dynamic="", messages=messages or [LLMMessage(role="user", content="Add two numbers")],
        tools=[ToolSpec(name="add", description="Add two numbers", parameters={
            "type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        })], max_tokens=512,
    )


@pytest.mark.asyncio
async def test_responses_function_call_roundtrip_and_cost_usage():
    bodies = []
    counter = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal counter
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        bodies.append(body)
        counter += 1
        if counter == 1:
            return httpx.Response(200, json={
                "status": "completed", "model": body["model"],
                "output": [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                            "name": "add", "arguments": "{\"a\":2,\"b\":3}"}],
                "usage": {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 20},
                          "output_tokens": 12},
            })
        return httpx.Response(200, json={
            "status": "completed", "model": body["model"],
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "5"}]}],
            "usage": {"input_tokens": 150, "output_tokens": 8},
        })

    provider = ResponsesProvider("openai", "testing-key")
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1", transport=httpx.MockTransport(handle)
    )
    try:
        first = await provider.chat(_request())
        assert first.tool_calls == [ToolCall(id="call_1", name="add", arguments={"a": 2, "b": 3})]
        assert first.usage.input_uncached == 80 and first.usage.cache_read == 20
        second = await provider.chat(_request([
            LLMMessage(role="user", content="Add two numbers"),
            LLMMessage(role="assistant", content="", tool_calls=first.tool_calls, raw=first.raw),
            LLMMessage(role="tool", content="5", tool_call_id="call_1", tool_name="add"),
        ]))
        assert second.text == "5"
        assert bodies[1]["input"][-1] == {"type": "function_call_output", "call_id": "call_1", "output": "5"}
        assert any(x.get("type") == "function_call" for x in bodies[1]["input"])
    finally:
        await provider.aclose()


def test_provider_routes_are_protocol_specific():
    assert isinstance(build_provider("openai", "key", None), ResponsesProvider)
    assert isinstance(build_provider("custom_responses", "key", "https://api.example/v1"), ResponsesProvider)
    assert type(build_provider("custom", "key", "https://api.example/v1")).__name__ == "OpenAICompatProvider"
    assert type(build_provider("google", "key", None)).__name__ == "GoogleProvider"


@pytest.mark.asyncio
async def test_responses_error_is_reported_without_silent_legacy_fallback():
    provider = ResponsesProvider("openai", "testing-key")
    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url="https://api.openai.com/v1",
        transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"error": "unsupported model"})),
    )
    try:
        from xagent_agent.providers.base import ProviderError
        with pytest.raises(ProviderError) as exc:
            await provider.chat(_request())
        assert exc.value.status == 404
    finally:
        await provider.aclose()
