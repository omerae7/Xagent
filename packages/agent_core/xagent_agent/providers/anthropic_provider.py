"""ارائه‌دهنده Anthropic (Claude) با SDK رسمی.

- کش پرامپت: بخش ثابت System با cache_control و بخش متغیر (زمان/کانتکست) بعد از نقطه کش
- بلاک‌های پاسخ (از جمله thinking) بدون تغییر در گام‌های بعدی بازپخش می‌شوند
- temperature ارسال نمی‌شود (در مدل‌های جدید پذیرفته نیست)؛ عمق استدلال با effort کنترل می‌شود
- Fallback سمت سرور برای ردهای ایمنی مدل‌های نسل ۵ (fallbacks="default")
"""

from __future__ import annotations

import re
import time
from typing import Any

import anthropic

from .base import ChatRequest, LLMMessage, LLMResponse, ProviderError, ToolCall, Usage

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_MODEL_RE = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?")


def _family(model: str) -> tuple[str, float] | None:
    m = _MODEL_RE.search(model)
    if not m:
        return None
    major = int(m.group(2))
    minor = int(m.group(3)) if m.group(3) and len(m.group(3)) <= 2 else 0
    return m.group(1), major + minor / 10


def supports_effort(model: str) -> bool:
    f = _family(model)
    if not f:
        return False
    fam, ver = f
    if ver >= 5:
        return True
    return (fam == "opus" and ver >= 4.5) or (fam == "sonnet" and ver >= 4.6)


def supports_fallbacks(model: str) -> bool:
    f = _family(model)
    if not f:
        return False
    fam, ver = f
    return (fam in ("opus", "fable") and ver >= 5) or (fam == "sonnet" and ver >= 5.5)


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self, api_key: str | None, base_url: str | None = None, timeout: float = 300,
                 http_client: Any = None):
        # تلاش مجدد فقط در llm.call_llm (یک لایه، هر تلاش فیزیکی = یک رزرو در دفتر مصرف). تلاش مجدد پنهان SDK
        # (پیش‌فرض ۲) درخواست‌های واقعی را بدون ثبت در دفتر چند برابر می‌کرد (یافته A6 ممیزی).
        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = anthropic.AsyncAnthropic(**kwargs)
        self._no_fallback: set[str] = set()

    async def aclose(self) -> None:
        await self._client.close()

    @staticmethod
    def _convert(messages: list[LLMMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                # دستور میانی اپراتور به‌صورت متن کاربر (سازگار با همه مدل‌ها)
                out.append({"role": "user", "content": [{"type": "text", "text": m.content}]})
            elif m.role == "assistant":
                if m.raw and m.raw.get("kind") == "anthropic" and m.raw.get("content"):
                    out.append({"role": "assistant", "content": m.raw["content"]})
                    continue
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for t in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": t.id, "name": t.name, "input": t.arguments})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
            elif m.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content or "(بدون خروجی)",
                    "is_error": m.is_error,
                }
                # همه نتایج ابزار یک گام در یک پیام کاربر قرار می‌گیرند
                if out and out[-1]["role"] == "user" and out[-1].get("_tool_results"):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block], "_tool_results": True})
            else:
                parts: list[dict[str, Any]] = []
                for img in m.images:
                    parts.append(
                        {"type": "image", "source": {"type": "base64", "media_type": img.mime, "data": img.data_b64}}
                    )
                parts.append({"type": "text", "text": m.content or "…"})
                out.append({"role": "user", "content": parts})
        for o in out:
            o.pop("_tool_results", None)
        # پیام‌های متوالی هم‌نقش ادغام می‌شوند
        merged: list[dict[str, Any]] = []
        for o in out:
            if merged and merged[-1]["role"] == o["role"] == "user":
                merged[-1]["content"].extend(o["content"])
            else:
                merged.append(o)
        return merged

    async def chat(self, req: ChatRequest) -> LLMResponse:
        system: list[dict[str, Any]] = []
        if req.system_stable:
            system.append({"type": "text", "text": req.system_stable, "cache_control": {"type": "ephemeral"}})
        if req.system_dynamic:
            system.append({"type": "text", "text": req.system_dynamic})
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": max(1, req.max_tokens),
            "messages": self._convert(req.messages),
        }
        if system:
            params["system"] = system
        if req.tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in req.tools
            ]
        if req.effort and supports_effort(req.model):
            params["output_config"] = {"effort": req.effort}
        use_fallback = supports_fallbacks(req.model) and req.model not in self._no_fallback
        t0 = time.monotonic()
        try:
            if use_fallback:
                try:
                    resp = await self._client.beta.messages.create(
                        **params, betas=[_FALLBACK_BETA], fallbacks="default"
                    )
                except anthropic.BadRequestError as e:
                    if "fallback" not in str(e).lower():
                        raise
                    self._no_fallback.add(req.model)
                    resp = await self._client.messages.create(**params)
            else:
                resp = await self._client.messages.create(**params)
        except anthropic.RateLimitError as e:
            raise ProviderError(f"محدودیت نرخ Anthropic: {e}", retryable=True, status=429) from e
        except anthropic.APIStatusError as e:
            raise ProviderError(
                f"Anthropic HTTP {e.status_code}: {str(e)[:400]}",
                retryable=e.status_code >= 500 or e.status_code in (408, 409),
                status=e.status_code,
            ) from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"خطای ارتباط با Anthropic: {e}", retryable=True) from e

        texts: list[str] = []
        calls: list[ToolCall] = []
        raw_blocks: list[dict[str, Any]] = []
        for block in resp.content:
            raw_blocks.append(block.model_dump(mode="json", exclude_none=True))
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        u = resp.usage
        usage = Usage(
            input_uncached=int(getattr(u, "input_tokens", 0) or 0),
            output=int(getattr(u, "output_tokens", 0) or 0),
            cache_read=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            details={"cache_creation": u.cache_creation.model_dump(mode="json")
                     if getattr(u, "cache_creation", None) is not None else {}},
        )
        stop = resp.stop_reason or "end_turn"
        if stop == "refusal":
            texts = ["درخواست توسط سامانه ایمنی مدل رد شد. لطفاً درخواست را بازنویسی کنید یا مدل دیگری انتخاب کنید."]
            calls = []
        return LLMResponse(
            text="".join(texts),
            tool_calls=calls,
            usage=usage,
            stop_reason=stop,
            model=resp.model,
            latency_ms=int((time.monotonic() - t0) * 1000),
            provider_request_id=str(getattr(resp, "id", "") or "") or None,
            raw={"kind": "anthropic", "content": raw_blocks},
        )

    async def embed(self, model: str, texts: list[str], dim: int) -> list[list[float]]:
        raise ProviderError("Anthropic سرویس Embedding ندارد؛ از جاسازی محلی یا ارائه‌دهنده دیگر استفاده کنید.")

    async def transcribe(self, model: str, audio: bytes, mime: str, language: str = "fa") -> str:
        raise ProviderError("Anthropic تبدیل گفتار به متن ارائه نمی‌کند.")

    async def speak(self, model: str, text: str, voice: str) -> tuple[bytes, str]:
        raise ProviderError("Anthropic تولید صدا ارائه نمی‌کند.")
