"""OpenAI Responses API adapter (also supports endpoints implementing this protocol).

The agent core keeps one provider-neutral transcript. This adapter replays reasoning and
function-call response items so multi-step tool runs work without an OpenAI-specific state store.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .base import ChatRequest, LLMMessage, LLMResponse, ProviderError, ToolCall, Usage
from .openai_compat import OpenAICompatProvider


class ResponsesProvider(OpenAICompatProvider):
    """Responses API for OpenAI and user-defined compatible providers."""

    def __init__(self, kind: str, api_key: str | None, base_url: str | None = None, timeout: float = 180):
        # Preserve embedding, transcription and speech support from the compatible provider.
        super().__init__(kind, api_key, base_url or "https://api.openai.com/v1", timeout)

    @staticmethod
    def _items(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "tool":
                if m.tool_call_id:
                    items.append({
                        "type": "function_call_output",
                        "call_id": m.tool_call_id,
                        "output": m.content or "(empty)",
                    })
                continue
            if m.role == "assistant":
                if m.raw and m.raw.get("kind") == "openai_responses":
                    raw_items = m.raw.get("output")
                    if isinstance(raw_items, list):
                        for item in raw_items:
                            if not isinstance(item, dict):
                                continue
                            if item.get("type") in ("message", "function_call", "reasoning"):
                                items.append(item)
                        continue
                if m.content:
                    items.append({"role": "assistant", "content": m.content})
                for call in m.tool_calls:
                    items.append({
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    })
                continue
            if m.role == "system":
                items.append({"role": "developer", "content": m.content})
            elif m.images:
                content: list[dict[str, Any]] = [{"type": "input_text", "text": m.content or "Describe this image."}]
                for img in m.images:
                    content.append({
                        "type": "input_image",
                        "image_url": f"data:{img.mime};base64,{img.data_b64}",
                    })
                items.append({"role": "user", "content": content})
            else:
                items.append({"role": "user", "content": m.content})
        return items

    async def chat(self, req: ChatRequest) -> LLMResponse:
        system = req.system_stable + ("\n\n" + req.system_dynamic if req.system_dynamic else "")
        body: dict[str, Any] = {
            "model": req.model,
            "input": self._items(req.messages),
            "max_output_tokens": max(1, req.max_tokens),
        }
        if system.strip():
            body["instructions"] = system
        if req.effort and req.model.lower().startswith(("o1", "o3", "o4", "gpt-5", "gpt-6")):
            body["reasoning"] = {"effort": req.effort}
        if req.tools:
            body["tools"] = [{
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "strict": False,
            } for t in req.tools]
            body["tool_choice"] = "auto"
        elif req.json_mode:
            body["text"] = {"format": {"type": "json_object"}}

        t0 = time.monotonic()
        try:
            response = await self._client.post("/responses", json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"Responses API connection failed: {e}", retryable=True) from e
        if response.status_code >= 400:
            raise ProviderError(
                f"Responses API HTTP {response.status_code}: {response.text[:400]}",
                status=response.status_code,
                retryable=response.status_code in (408, 409, 429) or response.status_code >= 500,
            )
        data = response.json()
        if data.get("status") == "failed":
            raise ProviderError(f"Responses API failed: {str(data.get('error'))[:400]}")
        output = data.get("output") or []
        texts: list[str] = []
        calls: list[ToolCall] = []
        for item in output:
            if item.get("type") == "function_call":
                args = item.get("arguments") or "{}"
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"_raw": args}
                calls.append(ToolCall(
                    id=item.get("call_id") or item.get("id", ""),
                    name=item.get("name", ""),
                    arguments=args if isinstance(args, dict) else {"_value": args},
                ))
            elif item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        texts.append(part.get("text") or "")
                    elif part.get("type") == "refusal":
                        texts.append(part.get("refusal") or "The model refused the request.")
        usage_data = data.get("usage") or {}
        input_details = usage_data.get("input_tokens_details") or {}
        output_details = usage_data.get("output_tokens_details") or {}
        prompt = int(usage_data.get("input_tokens") or 0)
        cached = int(input_details.get("cached_tokens") or 0)
        return LLMResponse(
            text="".join(texts) or (data.get("output_text") or ""),
            tool_calls=calls,
            usage=Usage(
                input_uncached=max(0, prompt - cached),
                cache_read=cached,
                output=int(usage_data.get("output_tokens") or 0),
                reasoning_tokens=int(output_details.get("reasoning_tokens") or 0),
                audio_input_tokens=int(input_details.get("audio_tokens") or 0),
                audio_output_tokens=int(output_details.get("audio_tokens") or 0),
                details={"input_tokens_details": input_details, "output_tokens_details": output_details},
                reported=data.get("usage") is not None,
            ),
            provider_request_id=str(response.headers.get("x-request-id") or data.get("id") or "") or None,
            stop_reason=data.get("status") or "completed",
            model=data.get("model") or req.model,
            latency_ms=int((time.monotonic() - t0) * 1000),
            raw={"kind": "openai_responses", "output": output},
        )
