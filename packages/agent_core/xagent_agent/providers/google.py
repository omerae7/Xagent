"""ارائه‌دهنده Google Gemini (REST مستقیم)."""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any

import httpx

from .base import ChatRequest, LLMMessage, LLMResponse, ProviderError, ToolCall, Usage

BASE = "https://generativelanguage.googleapis.com/v1beta"
_ALLOWED_SCHEMA_KEYS = {"type", "description", "properties", "required", "items", "enum", "format", "nullable"}


def _sanitize_schema(schema: Any) -> Any:
    """Gemini فقط زیرمجموعه‌ای از OpenAPI را می‌پذیرد."""
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k not in _ALLOWED_SCHEMA_KEYS:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()}
            elif k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                out[k] = (non_null[0] if non_null else "string").upper()
                if "null" in v:
                    out["nullable"] = True
            elif k == "type" and isinstance(v, str):
                out[k] = v.upper()
            else:
                out[k] = _sanitize_schema(v)
        return out
    if isinstance(schema, list):
        return [_sanitize_schema(x) for x in schema]
    return schema


class GoogleProvider:
    kind = "google"

    def __init__(self, api_key: str | None, base_url: str | None = None, timeout: float = 120):
        self.base_url = (base_url or BASE).rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"x-goog-api-key": api_key or "", "Content-Type": "application/json"}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _convert(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "assistant":
                if m.raw and m.raw.get("kind") == "google" and m.raw.get("parts"):
                    out.append({"role": "model", "parts": m.raw["parts"]})
                    continue
                parts: list[dict[str, Any]] = []
                if m.content:
                    parts.append({"text": m.content})
                for t in m.tool_calls:
                    parts.append({"functionCall": {"name": t.name, "args": t.arguments}})
                out.append({"role": "model", "parts": parts or [{"text": "…"}]})
            elif m.role == "tool":
                part = {"functionResponse": {"name": m.tool_name or "tool", "response": {"result": m.content}}}
                if out and out[-1]["role"] == "user" and all("functionResponse" in p for p in out[-1]["parts"]):
                    out[-1]["parts"].append(part)
                else:
                    out.append({"role": "user", "parts": [part]})
            else:
                parts = [{"inline_data": {"mime_type": i.mime, "data": i.data_b64}} for i in m.images]
                parts.append({"text": m.content or "…"})
                out.append({"role": "user", "parts": parts})
        return out

    async def chat(self, req: ChatRequest) -> LLMResponse:
        body: dict[str, Any] = {
            "contents": self._convert(req.messages),
            "generationConfig": {"maxOutputTokens": req.max_tokens},
        }
        if req.temperature is not None:
            body["generationConfig"]["temperature"] = req.temperature
        system = req.system_stable + ("\n\n" + req.system_dynamic if req.system_dynamic else "")
        if system.strip():
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if req.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": t.name, "description": t.description, "parameters": _sanitize_schema(t.parameters)}
                        for t in req.tools
                    ]
                }
            ]
        elif req.json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        t0 = time.monotonic()
        try:
            r = await self._client.post(f"{self.base_url}/models/{req.model}:generateContent", json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"خطای ارتباط با Gemini: {e}", retryable=True) from e
        if r.status_code >= 400:
            raise ProviderError(
                f"Gemini HTTP {r.status_code}: {r.text[:500]}",
                retryable=r.status_code == 429 or r.status_code >= 500,
                status=r.status_code,
            )
        data = r.json()
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []
        texts, calls = [], []
        for p in parts:
            if "text" in p and not p.get("thought"):
                texts.append(p["text"])
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append(ToolCall(id=f"g_{uuid.uuid4().hex[:10]}", name=fc.get("name", ""), arguments=fc.get("args") or {}))
        um = data.get("usageMetadata") or {}
        prompt = int(um.get("promptTokenCount") or 0)
        cached = int(um.get("cachedContentTokenCount") or 0)
        usage = Usage(
            input_uncached=max(0, prompt - cached),
            output=int(um.get("candidatesTokenCount") or 0) + int(um.get("thoughtsTokenCount") or 0),
            cache_read=cached,
        )
        return LLMResponse(
            text="".join(texts),
            tool_calls=calls,
            usage=usage,
            stop_reason=cand.get("finishReason", "STOP"),
            model=req.model,
            latency_ms=int((time.monotonic() - t0) * 1000),
            raw={"kind": "google", "parts": parts},
        )

    async def embed(self, model: str, texts: list[str], dim: int) -> list[list[float]]:
        out = []
        for t in texts:
            r = await self._client.post(
                f"{self.base_url}/models/{model}:embedContent",
                json={"content": {"parts": [{"text": t}]}, "outputDimensionality": dim},
            )
            if r.status_code >= 400:
                raise ProviderError(f"Gemini embedding HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
            out.append(r.json()["embedding"]["values"])
        return out

    async def transcribe(self, model: str, audio: bytes, mime: str, language: str = "fa") -> str:
        req = ChatRequest(
            model=model,
            system_stable="",
            system_dynamic="",
            messages=[LLMMessage(role="user", content="این صدا را دقیقاً به متن فارسی تبدیل کن. فقط متن را برگردان.")],
            max_tokens=2048,
        )
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": mime.split(";")[0], "data": base64.b64encode(audio).decode()}},
                        {"text": req.messages[0].content},
                    ],
                }
            ]
        }
        r = await self._client.post(f"{self.base_url}/models/{model}:generateContent", json=body)
        if r.status_code >= 400:
            raise ProviderError(f"Gemini STT HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
        parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip()

    async def speak(self, model: str, text: str, voice: str) -> tuple[bytes, str]:
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or "Kore"}}},
            },
        }
        r = await self._client.post(f"{self.base_url}/models/{model}:generateContent", json=body)
        if r.status_code >= 400:
            raise ProviderError(f"Gemini TTS HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
        parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        for p in parts:
            inline = p.get("inlineData") or p.get("inline_data")
            if inline:
                pcm = base64.b64decode(inline["data"])
                return _pcm_to_wav(pcm), "audio/wav"
        raise ProviderError("Gemini صدایی برنگرداند.")


def _pcm_to_wav(pcm: bytes, rate: int = 24000) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()
