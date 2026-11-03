"""ارائه‌دهنده سازگار با OpenAI: OpenAI، OpenRouter، DeepSeek، Groq، xAI، Mistral، Ollama و هر API سازگار."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .base import ChatRequest, LLMMessage, LLMResponse, ProviderError, ToolCall, Usage

DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "xai": "https://api.x.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "ollama": "http://host.docker.internal:11434/v1",
}

# مدل‌های استدلالی OpenAI که temperature نمی‌پذیرند و max_completion_tokens لازم دارند
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def _is_reasoning(model: str) -> bool:
    m = model.split("/")[-1].lower()
    return m.startswith(_REASONING_PREFIXES)


class OpenAICompatProvider:
    def __init__(self, kind: str, api_key: str | None, base_url: str | None = None, timeout: float = 120):
        self.kind = kind
        self.base_url = (base_url or DEFAULT_BASES.get(kind) or DEFAULT_BASES["openai"]).rstrip("/")
        # httpx sets Content-Type per request: JSON for json= and multipart for files=.
        # A client-wide JSON header breaks Whisper/STT file uploads (HTTP 400).
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if kind == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/omerae7/Xagent"
            headers["X-Title"] = "X Agent"
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _convert(self, req: ChatRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        system = req.system_stable + ("\n\n" + req.system_dynamic if req.system_dynamic else "")
        if system.strip():
            out.append({"role": "system", "content": system})
        for m in req.messages:
            out.extend(self._convert_msg(m))
        return out

    @staticmethod
    def _convert_msg(m: LLMMessage) -> list[dict[str, Any]]:
        if m.role == "tool":
            return [{"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content or ""}]
        if m.role == "assistant":
            d: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": t.id,
                        "type": "function",
                        "function": {"name": t.name, "arguments": json.dumps(t.arguments, ensure_ascii=False)},
                    }
                    for t in m.tool_calls
                ]
            return [d]
        if m.images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": m.content or ""}]
            for img in m.images:
                parts.append({"type": "image_url", "image_url": {"url": f"data:{img.mime};base64,{img.data_b64}"}})
            return [{"role": m.role, "content": parts}]
        return [{"role": m.role, "content": m.content}]

    async def chat(self, req: ChatRequest) -> LLMResponse:
        body: dict[str, Any] = {"model": req.model, "messages": self._convert(req)}
        if _is_reasoning(req.model) and self.kind == "openai":
            body["max_completion_tokens"] = req.max_tokens
            if req.effort:
                body["reasoning_effort"] = req.effort
        else:
            body["max_tokens"] = req.max_tokens
            if req.temperature is not None:
                body["temperature"] = req.temperature
        if req.tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in req.tools
            ]
            body["tool_choice"] = "auto"
        if req.json_mode and not req.tools:
            body["response_format"] = {"type": "json_object"}
        if self.kind == "openrouter":
            body["usage"] = {"include": True}
        t0 = time.monotonic()
        try:
            r = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"خطای ارتباط با {self.kind}: {e}", retryable=True) from e
        if r.status_code >= 400:
            raise ProviderError(
                f"{self.kind} HTTP {r.status_code}: {r.text[:500]}",
                retryable=r.status_code in (408, 409, 429) or r.status_code >= 500,
                status=r.status_code,
            )
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls)}", name=fn.get("name", ""), arguments=args))
        u = data.get("usage") or {}
        prompt = int(u.get("prompt_tokens") or 0)
        cached = int(
            (u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0
        )
        usage = Usage(
            input_uncached=max(0, prompt - cached),
            output=int(u.get("completion_tokens") or 0),
            cache_read=cached,
        )
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            usage=usage,
            stop_reason=choice.get("finish_reason") or "stop",
            model=data.get("model") or req.model,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    async def embed(self, model: str, texts: list[str], dim: int) -> list[list[float]]:
        body: dict[str, Any] = {"model": model, "input": texts}
        if self.kind == "openai" and "text-embedding-3" in model:
            body["dimensions"] = dim
        r = await self._client.post("/embeddings", json=body)
        if r.status_code >= 400:
            raise ProviderError(f"embedding HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d.get("index", 0))]

    async def transcribe(self, model: str, audio: bytes, mime: str, language: str = "fa") -> str:
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/wav": "wav", "audio/mp4": "m4a"}
        files = {"file": (f"audio.{ext.get(mime.split(';')[0], 'webm')}", audio, mime.split(";")[0])}
        r = await self._client.post(
            "/audio/transcriptions", data={"model": model, "language": language}, files=files
        )
        if r.status_code >= 400:
            raise ProviderError(f"STT HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
        return r.json().get("text", "")

    async def speak(self, model: str, text: str, voice: str) -> tuple[bytes, str]:
        r = await self._client.post(
            "/audio/speech", json={"model": model, "input": text, "voice": voice, "response_format": "mp3"}
        )
        if r.status_code >= 400:
            raise ProviderError(f"TTS HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
        return r.content, "audio/mpeg"
