"""ثبت ارائه‌دهندگان، کاتالوگ پیشنهادی مدل‌ها، محاسبه هزینه و اتصال مدل‌های تعریف‌شده در دیتابیس."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import AIModel, AIProvider
from xagent_core.security import decrypt_secret

from .anthropic_provider import AnthropicProvider
from .base import ChatRequest, LLMMessage, Provider, ProviderError
from .google import GoogleProvider
from .openai_compat import DEFAULT_BASES, OpenAICompatProvider
from .openai_responses import ResponsesProvider

# کاتالوگ برای فرم ساده «افزودن مدل» — کاربر می‌تواند هر نام مدل دیگری را هم وارد کند.
# قیمت‌ها به دلار برای هر یک میلیون توکن و قابل ویرایش در پنل هستند.
CATALOG: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "base_url": None,
        "models": [
            {"name": "claude-opus-5-5", "label": "Claude Opus 5.5 — مدیر", "caps": ["vision", "tools"],
             "ctx": 1_000_000, "price": [4.0, 20.0, 0.20, 5.0], "tier": "manager"},
            {"name": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "caps": ["vision", "tools"],
             "ctx": 1_000_000, "price": [2.0, 10.0, 0.20, 2.5], "tier": "manager"},
            {"name": "claude-haiku-5-5", "label": "Claude Haiku 5.5 — کارمند سریع", "caps": ["vision", "tools"],
             "ctx": 1_000_000, "price": [0.10, 0.50, 0.01, 0.125], "tier": "worker"},
            {"name": "claude-fable-5-1", "label": "Claude Fable 5.1 — قوی‌ترین", "caps": ["vision", "tools"],
             "ctx": 1_000_000, "price": [10.0, 50.0, 0.25, 12.5], "tier": "manager"},
        ],
    },
    "openai": {
        "label": "OpenAI — Responses API (مدل‌های جدید)",
        "base_url": DEFAULT_BASES["openai"],
        "models": [
            {"name": "gpt-4.1-mini", "label": "GPT-4.1 mini", "caps": ["vision", "tools"], "ctx": 1_000_000,
             "price": [0.40, 1.60, 0.10, 0], "tier": "worker"},
            {"name": "gpt-4o-mini", "label": "GPT-4o mini", "caps": ["vision", "tools"], "ctx": 128_000,
             "price": [0.15, 0.60, 0.075, 0], "tier": "worker"},
            {"name": "whisper-1", "label": "Whisper (گفتار به متن)", "caps": ["stt"], "ctx": 0, "price": [0, 0, 0, 0]},
            {"name": "gpt-4o-mini-tts", "label": "TTS (متن به گفتار)", "caps": ["tts"], "ctx": 0, "price": [0.6, 12, 0, 0]},
            {"name": "text-embedding-3-small", "label": "Embedding کوچک", "caps": ["embedding"], "ctx": 8191,
             "price": [0.02, 0, 0, 0]},
        ],
    },
    "openai_chat": {
        "label": "OpenAI — Chat Completions (قدیمی/سازگاری)",
        "base_url": DEFAULT_BASES["openai"],
        "models": [],
    },
    "custom_responses": {
        "label": "Responses API (آدرس سفارشی)",
        "base_url": None,
        "models": [],
    },
    "google": {
        "label": "Google Gemini",
        "base_url": None,
        "models": [
            {"name": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "caps": ["vision", "tools", "stt", "audio_in"],
             "ctx": 1_000_000, "price": [0.30, 2.50, 0.075, 0], "tier": "worker"},
            {"name": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "caps": ["vision", "tools", "stt", "audio_in"],
             "ctx": 1_000_000, "price": [1.25, 10.0, 0.31, 0], "tier": "manager"},
            {"name": "gemini-embedding-001", "label": "Gemini Embedding", "caps": ["embedding"], "ctx": 2048,
             "price": [0.15, 0, 0, 0]},
        ],
    },
    "openrouter": {"label": "OpenRouter", "base_url": DEFAULT_BASES["openrouter"], "models": []},
    "deepseek": {
        "label": "DeepSeek",
        "base_url": DEFAULT_BASES["deepseek"],
        "models": [{"name": "deepseek-chat", "label": "DeepSeek Chat", "caps": ["tools"], "ctx": 64_000,
                    "price": [0.27, 1.10, 0.07, 0], "tier": "worker"}],
    },
    "groq": {"label": "Groq", "base_url": DEFAULT_BASES["groq"], "models": []},
    "xai": {"label": "xAI", "base_url": DEFAULT_BASES["xai"], "models": []},
    "mistral": {"label": "Mistral", "base_url": DEFAULT_BASES["mistral"], "models": []},
    "ollama": {"label": "Ollama (محلی)", "base_url": DEFAULT_BASES["ollama"], "models": []},
    "custom": {"label": "سازگار با OpenAI (سفارشی)", "base_url": None, "models": []},
}


def build_provider(kind: str, api_key: str | None, base_url: str | None) -> Provider:
    if kind == "anthropic":
        return AnthropicProvider(api_key, base_url)
    if kind == "google":
        return GoogleProvider(api_key, base_url)
    if kind in ("openai", "custom_responses"):
        return ResponsesProvider(kind, api_key, base_url)
    compat_kind = "openai" if kind in ("custom", "openai_chat") else kind
    return OpenAICompatProvider(compat_kind, api_key, base_url)


@dataclass
class BoundModel:
    id: uuid.UUID
    provider_id: uuid.UUID
    provider_kind: str
    model_name: str
    label: str
    capabilities: dict[str, Any]
    context_window: int
    price_in: Decimal
    price_out: Decimal
    price_cache_read: Decimal
    price_cache_write: Decimal
    provider: Provider
    capability_meta: dict[str, Any] = field(default_factory=dict)
    # قیمت معتبر ندارد: هزینه با قیمت محافظه‌کارانه Stop-Loss برآورد می‌شود
    price_estimated: bool = False

    def has(self, cap: str) -> bool:
        return bool(self.capabilities.get(cap))

    def cost(self, input_uncached: int, output: int, cache_read: int, cache_write: int) -> Decimal:
        m = Decimal(1_000_000)
        return (
            Decimal(input_uncached) * self.price_in
            + Decimal(output) * self.price_out
            + Decimal(cache_read) * (self.price_cache_read or self.price_in)
            + Decimal(cache_write) * (self.price_cache_write or self.price_in)
        ) / m


# کش اتصال‌ها: شناسه ارائه‌دهنده → (زمان به‌روزرسانی، نمونه)
_provider_cache: dict[uuid.UUID, tuple[float, Provider]] = {}


async def _provider_for(row: AIProvider) -> Provider:
    stamp = row.updated_at.timestamp() if row.updated_at else 0.0
    cached = _provider_cache.get(row.id)
    if cached and cached[0] == stamp:
        return cached[1]
    if cached:
        try:
            await cached[1].aclose()
        except Exception:
            pass
    p = build_provider(row.kind, decrypt_secret(row.api_key_enc), row.base_url)
    _provider_cache[row.id] = (stamp, p)
    return p


async def load_model(session: AsyncSession, model_id: str | uuid.UUID, include_disabled: bool = False) -> BoundModel | None:
    try:
        mid = model_id if isinstance(model_id, uuid.UUID) else uuid.UUID(str(model_id))
    except ValueError:
        return None
    m = await session.get(AIModel, mid)
    if m is None or (not m.enabled and not include_disabled):
        return None
    p = await session.get(AIProvider, m.provider_id)
    if p is None or (not p.enabled and not include_disabled):
        return None
    prices = [Decimal(m.price_in or 0), Decimal(m.price_out or 0), Decimal(m.price_cache_read or 0),
              Decimal(m.price_cache_write or 0)]
    # مدل بدون قیمت معتبر هرگز «رایگان» حساب نمی‌شود (جز Ollama محلی یا رایگان اعلام‌شده توسط کارفرما)
    estimated = not (prices[0] > 0 or prices[1] > 0) and p.kind != "ollama" and (m.pricing_source or "") != "manual"
    if estimated:
        from xagent_core.settings_store import StopLossSettings, get_setting

        sl = await get_setting(session, "stoploss", StopLossSettings)
        prices = [Decimal(str(sl.unpriced_input_per_m)), Decimal(str(sl.unpriced_output_per_m)),
                  Decimal(str(sl.unpriced_input_per_m)), Decimal(str(sl.unpriced_input_per_m))]
    return BoundModel(
        id=m.id,
        provider_id=p.id,
        provider_kind=p.kind,
        model_name=m.model_name,
        label=m.display_name or m.model_name,
        capabilities=m.capabilities or {},
        context_window=m.context_window or 128000,
        price_in=prices[0],
        price_out=prices[1],
        price_cache_read=prices[2],
        price_cache_write=prices[3],
        provider=await _provider_for(p),
        capability_meta=m.capability_meta or {},
        price_estimated=estimated,
    )


class ModelEmbedder:
    def __init__(self, bound: BoundModel):
        self.bound = bound
        self.name = f"{bound.provider_kind}:{bound.model_name}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from xagent_core.models import EMBED_DIM
        from .base import ProviderError
        from ..usage_ledger import begin_aux_attempt, finish_aux_attempt

        input_hint = max(256, sum(len(t) for t in texts) // 2)
        aid = await begin_aux_attempt(self.bound, purpose="embedding",
                                      input_hint=input_hint, output_hint=1)
        try:
            vectors = await self.bound.provider.embed(self.bound.model_name, texts, EMBED_DIM)
        except BaseException as error:
            await finish_aux_attempt(aid, unit_details={"texts": len(texts),
                "input_characters": sum(len(t) for t in texts)}, model=self.bound,
                error=error, known_rejected=isinstance(error, ProviderError) and error.status in (400, 401, 403, 404, 422))
            raise
        await finish_aux_attempt(aid, unit_details={"texts": len(texts),
            "input_characters": sum(len(t) for t in texts)}, model=self.bound,
            reported_usage=getattr(vectors, "usage", None))
        return vectors


async def test_model(session: AsyncSession, model: AIModel) -> tuple[bool, str, int]:
    """تست واقعی اتصال: یک درخواست کوچک به مدل."""
    p = await session.get(AIProvider, model.provider_id)
    if p is None:
        return False, "ارائه‌دهنده یافت نشد", 0
    provider = build_provider(p.kind, decrypt_secret(p.api_key_enc), p.base_url)
    t0 = time.monotonic()
    caps = model.capabilities or {}
    try:
        bound = await load_model(session, model.id, include_disabled=True)
        if bound is None:
            raise ProviderError("مدل قابل اتصال نیست؛ تست بدون درخواست API متوقف شد.")
        if caps.get("embedding") and not caps.get("tools"):
            from ..usage_ledger import begin_aux_attempt, finish_aux_attempt
            aid = await begin_aux_attempt(bound, purpose="probe_embedding", input_hint=256)
            try:
                vec = await provider.embed(model.model_name, ["سلام"], 768)
            except BaseException as e:
                await finish_aux_attempt(aid, model=bound, error=e,
                    known_rejected=isinstance(e, ProviderError) and e.status in (400, 401, 403, 404, 422),
                    unit_details={"texts": 1, "input_characters": 4})
                raise
            await finish_aux_attempt(aid, model=bound, reported_usage=getattr(vec, "usage", None),
                                     unit_details={"texts": 1, "input_characters": 4})
            msg = f"Embedding با {len(vec[0])} بعد دریافت شد"
        elif caps.get("tts") and not caps.get("tools"):
            from ..usage_ledger import begin_aux_attempt, finish_aux_attempt
            aid = await begin_aux_attempt(bound, purpose="probe_tts", input_hint=256, output_hint=1024)
            try:
                audio, _ = await provider.speak(model.model_name, "سلام", "alloy")
            except BaseException as e:
                await finish_aux_attempt(aid, model=bound, error=e,
                    known_rejected=isinstance(e, ProviderError) and e.status in (400, 401, 403, 404, 422),
                    unit_details={"input_text_characters": 4})
                raise
            await finish_aux_attempt(aid, model=bound,
                                     unit_details={"input_text_characters": 4, "audio_output_bytes": len(audio)})
            msg = f"صدا تولید شد ({len(audio)} بایت)"
        elif caps.get("stt") and not caps.get("tools") and p.kind != "google":
            msg = "مدل گفتار به متن ثبت شد (بدون درخواست API)"
        else:
            from ..llm import call_llm
            result = await call_llm(session, [bound], purpose="probe",
                system_stable="", system_dynamic="",
                messages=[LLMMessage(role="user", content="فقط بنویس: OK")], max_tokens=16)
            answer = result.response
            msg = f"پاسخ: {answer.text.strip()[:60] or '(خالی)'} — {answer.usage.input_total}+{answer.usage.output} توکن"
        ok = True
    except ProviderError as e:
        ok, msg = False, str(e)
    except Exception as e:  # noqa: BLE001
        ok, msg = False, f"{type(e).__name__}: {e}"
    finally:
        try:
            await provider.aclose()
        except Exception:
            pass
    latency = int((time.monotonic() - t0) * 1000)
    p.last_test_at = utcnow()
    p.last_test_ok = ok
    p.last_test_error = None if ok else msg[:1000]
    return ok, msg, latency
