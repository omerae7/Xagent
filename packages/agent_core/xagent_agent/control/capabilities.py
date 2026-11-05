"""کشف مدل‌ها از API رسمی هر ارائه‌دهنده و آزمون عملی قابلیت‌ها.

- هیچ شناسه یا قیمتی ساخته نمی‌شود: فقط آنچه API ارائه‌دهنده برمی‌گرداند ثبت می‌شود.
- قابلیت‌ها با آزمون واقعی (فراخوانی ابزار، تصویر) تأیید می‌شوند؛ حدس از روی نام فقط «heuristic» است.
- منبع و تاریخ هر قابلیت و قیمت ذخیره می‌شود.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import AIModel, AIProvider
from xagent_core.redact import scrub
from xagent_core.security import decrypt_secret

from ..providers.base import ImagePart, LLMMessage, ProviderError, ToolSpec
from ..providers.openai_compat import DEFAULT_BASES
from ..providers.registry import CATALOG, load_model


class DiscoveryError(Exception):
    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(scrub(message))
        self.status = status


@dataclass
class DiscoveredModel:
    id: str
    label: str
    caps: dict[str, bool] = field(default_factory=dict)  # فقط قابلیت‌هایی که API اعلام کرده
    context_window: int | None = None
    price: tuple[Decimal, Decimal] | None = None  # (ورودی، خروجی) دلار به ازای هر میلیون توکن — فقط از API
    cache_price: Decimal | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # جزئیات قیمت/استدلال اعلام‌شده توسط API


def _http_error(kind: str, status: int) -> DiscoveryError:
    if status in (401, 403):
        hint = (" (کلید نامعتبر است یا اشتراک Kimi Code این مدل/سرویس را شامل نمی‌شود)" if kind == "kimi_code"
                else " (کلید نامعتبر یا بدون دسترسی)")
        return DiscoveryError(f"ارائه‌دهنده درخواست را رد کرد: HTTP {status}{hint}", status)
    if status == 404:
        return DiscoveryError("این سرویس نقطه فهرست مدل ندارد (HTTP 404)؛ مدل را دستی یا از کاتالوگ مستند اضافه کنید.",
                              status)
    if status == 429:
        return DiscoveryError("محدودیت نرخ ارائه‌دهنده (HTTP 429)؛ بعداً دوباره تلاش کنید.", status)
    return DiscoveryError(f"ارائه‌دهنده فهرست مدل‌ها را برنگرداند (HTTP {status}).", status)


def _xai_merge(lm: dict[str, Any], md: dict[str, Any]) -> list[DiscoveredModel]:
    """ترکیب ‎/v1/language-models (قیمت و Modality) و ‎/v1/models (پنجره متن، reasoning_effort، قیمت بافت بلند)."""
    from ..providers.params import XAI_PRICE_UNIT, xai_price_per_m

    now = utcnow().isoformat()
    out: list[DiscoveredModel] = []
    for mid in [*lm.keys(), *(k for k in md if k not in lm)]:
        L, M = lm.get(mid) or {}, md.get(mid) or {}

        def field_(name: str, L: dict[str, Any] = L, M: dict[str, Any] = M) -> Any:
            return L.get(name) if L.get(name) is not None else M.get(name)

        pin = xai_price_per_m(field_("prompt_text_token_price"))
        pout = xai_price_per_m(field_("completion_text_token_price"))
        pcache = xai_price_per_m(field_("cached_prompt_text_token_price"))
        caps: dict[str, bool] = {}
        mods_in = L.get("input_modalities") or M.get("input_modalities")
        if isinstance(mods_in, list) and mods_in:
            caps["vision"] = "image" in mods_in
        mods_out = L.get("output_modalities") or M.get("output_modalities")
        if isinstance(mods_out, list) and mods_out and "text" not in mods_out:
            caps["tools"] = False
        meta: dict[str, Any] = {}
        mc = M.get("capabilities") if isinstance(M.get("capabilities"), dict) else {}
        efforts = mc.get("reasoning_effort")
        if isinstance(efforts, list) and efforts:
            meta["reasoning"] = {"efforts": [e for e in efforts if isinstance(e, str)],
                                 "default": mc.get("default_reasoning_effort"), "source": "provider", "at": now}
        raw_prices = {k: field_(k) for k in ("prompt_text_token_price", "cached_prompt_text_token_price",
                                             "completion_text_token_price", "prompt_image_token_price")}
        detail: dict[str, Any] = {"unit": XAI_PRICE_UNIT, "raw": raw_prices, "source": "xai_api", "at": now,
                                  "valid": bool(pin and pout)}
        threshold = M.get("long_context_threshold")
        lin = xai_price_per_m(M.get("prompt_text_token_price_long_context"))
        lout = xai_price_per_m(M.get("completion_text_token_price_long_context"))
        if isinstance(threshold, int) and threshold > 0 and lin and lout:
            # مقدار ۰ یعنی قیمت بافت بلند جداگانه ندارد (همان قیمت عادی)؛ فقط مقدار مثبت ثبت می‌شود
            lcache = xai_price_per_m(M.get("cached_prompt_text_token_price_long_context"))
            detail["long_context"] = {"threshold": threshold, "in": str(lin), "out": str(lout),
                                      "cache": str(lcache) if lcache else None}
        meta["pricing_detail"] = detail
        aliases = L.get("aliases") or M.get("aliases")
        if isinstance(aliases, list) and aliases:
            meta["aliases"] = [a for a in aliases if isinstance(a, str)][:10]
        ctx = M.get("context_length")
        out.append(DiscoveredModel(mid, mid, caps, ctx if isinstance(ctx, int) and ctx > 0 else None,
                                   (pin, pout) if pin and pout else None, pcache if pin and pout else None, meta))
    return out


async def _xai_models(base: str, key: str) -> list[DiscoveredModel]:
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as c:
            r1 = await c.get(base.rstrip("/") + "/language-models", headers=headers)
            r2 = await c.get(base.rstrip("/") + "/models", headers=headers)
    except httpx.HTTPError as e:
        raise DiscoveryError(f"ارتباط با xAI ناموفق بود: {type(e).__name__}") from e
    for r in (r1, r2):
        if r.status_code in (401, 403, 429):
            raise _http_error("xai", r.status_code)
    lm: dict[str, Any] = {}
    md: dict[str, Any] = {}
    try:
        if r1.status_code < 400:
            lm = {m["id"]: m for m in (r1.json().get("models") or []) if isinstance(m, dict) and isinstance(m.get("id"), str)}
        if r2.status_code < 400:
            md = {m["id"]: m for m in (r2.json().get("data") or []) if isinstance(m, dict) and isinstance(m.get("id"), str)}
    except (ValueError, AttributeError) as e:
        raise DiscoveryError("پاسخ فهرست مدل‌های xAI ساختار شناخته‌شده ندارد.") from e
    if not lm and not md:
        raise _http_error("xai", r1.status_code if r1.status_code >= 400 else r2.status_code)
    return _xai_merge(lm, md)


def _heuristic_caps(model_id: str) -> dict[str, bool]:
    n = model_id.lower()
    if "embed" in n:
        return {"embedding": True, "tools": False}
    if "whisper" in n or "transcribe" in n:
        return {"stt": True, "tools": False}
    if re.search(r"(^|[-_])tts|speech", n):
        return {"tts": True, "tools": False}
    if re.search(r"dall-e|imagen|image-gen|gpt-image|moderation", n):
        return {"tools": False}
    return {}


async def list_provider_models(provider: AIProvider, cursor: str | None = None) -> tuple[list[DiscoveredModel], str | None]:
    key = decrypt_secret(provider.api_key_enc)
    if not key and provider.kind != "ollama":
        raise DiscoveryError("کلید API برای این ارائه‌دهنده ثبت نشده است.")
    kind = provider.kind
    params: dict[str, Any] = {}
    if kind == "anthropic":
        url = (provider.base_url or "https://api.anthropic.com/v1").rstrip("/") + "/models"
        headers = {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
        params = {"limit": 100, **({"after_id": cursor} if cursor else {})}
    elif kind == "google":
        url = (provider.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/") + "/models"
        headers = {"x-goog-api-key": key or ""}
        params = {"pageSize": 200, **({"pageToken": cursor} if cursor else {})}
    else:
        base_kind = "openai" if kind in ("openai", "openai_chat") else kind
        base = provider.base_url or DEFAULT_BASES.get(base_kind) or CATALOG.get(kind, {}).get("base_url")
        if not base:
            raise DiscoveryError("برای این ارائه‌دهنده Base URL مشخص نیست.")
        if kind == "xai":
            return await _xai_models(base, key or ""), None
        url = base.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if cursor:
            params["after"] = cursor
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=False) as c:
            r = await c.get(url, headers=headers, params=params)
    except httpx.HTTPError as e:
        raise DiscoveryError(f"ارتباط با ارائه‌دهنده ناموفق بود: {type(e).__name__}") from e
    if r.status_code == 404 and kind == "kimi_code":
        # Kimi Code فهرست مدل عمومی ندارد: شناسه‌های مستند (بدون قابلیت/قیمت ادعایی) تا آزمون عملی
        docs = [DiscoveredModel(x["name"], x["label"], {}, None, None, None,
                                {"listing": {"source": "docs_catalog", "note": x.get("note", "")}})
                for x in CATALOG["kimi_code"]["models"]]
        return docs, None
    if r.status_code >= 400:
        raise _http_error(kind, r.status_code)
    try:
        data = r.json()
    except ValueError as e:
        raise DiscoveryError("پاسخ فهرست مدل‌ها JSON نیست.") from e
    items = data.get("models") if kind == "google" else data.get("data")
    if not isinstance(items, list):
        raise DiscoveryError("ساختار پاسخ فهرست مدل‌ها ناشناخته است.")
    out: list[DiscoveredModel] = []
    for it in items[:500]:
        if not isinstance(it, dict):
            continue
        mid = it.get("id") or it.get("name")
        if not isinstance(mid, str) or not mid:
            continue
        caps: dict[str, bool] = {}
        ctx = None
        price = None
        if kind == "google":
            mid = mid.removeprefix("models/")
            methods = it.get("supportedGenerationMethods") or []
            if "embedContent" in methods and "generateContent" not in methods:
                caps = {"embedding": True, "tools": False}
            elif "generateContent" not in methods:
                continue
            ctx = it.get("inputTokenLimit")
        elif kind == "openrouter":
            ctx = it.get("context_length")
            params_supported = it.get("supported_parameters") or []
            if params_supported:
                caps["tools"] = "tools" in params_supported
            mods = (it.get("architecture") or {}).get("input_modalities") or []
            if mods:
                caps["vision"] = "image" in mods
            pr = it.get("pricing") or {}
            try:
                if pr.get("prompt") is not None and pr.get("completion") is not None:
                    price = (Decimal(str(pr["prompt"])) * 1_000_000, Decimal(str(pr["completion"])) * 1_000_000)
            except (ArithmeticError, ValueError):
                price = None
        elif kind in ("moonshot", "kimi_code"):
            # فقط فیلدهایی که API واقعاً برگرداند؛ قیمت در این API اعلام نمی‌شود (نامعلوم، نه رایگان)
            c = it.get("context_length")
            ctx = c if isinstance(c, int) and c > 0 else None
            if isinstance(it.get("supports_image_in"), bool):
                caps["vision"] = it["supports_image_in"]
            if isinstance(it.get("supports_reasoning"), bool) and it["supports_reasoning"]:
                meta_th = {"value": True, "control": "always" if "thinking" in mid.lower() else "unknown",
                           "source": "provider", "at": utcnow().isoformat()}
                out.append(DiscoveredModel(mid, it.get("display_name") or mid, caps, ctx, None, None,
                                           {"thinking": meta_th}))
                continue
        out.append(DiscoveredModel(mid, it.get("displayName") or it.get("display_name") or it.get("name") or mid,
                                   caps, int(ctx) if isinstance(ctx, int) else None, price))
    nxt = None
    if kind == "google":
        nxt = data.get("nextPageToken")
    elif kind == "anthropic" and data.get("has_more"):
        nxt = data.get("last_id")
    elif data.get("has_more") and out:
        nxt = out[-1].id
    return out, nxt


async def sync_provider(session: AsyncSession, provider: AIProvider, max_pages: int = 5) -> dict[str, Any]:
    """کشف مدل‌های قابل دسترس با این کلید و ثبت مدل‌های تازه (غیرفعال، تا پس از آزمون فعال شوند)."""
    found: list[DiscoveredModel] = []
    cursor = None
    for _ in range(max_pages):
        page, cursor = await list_provider_models(provider, cursor)
        found.extend(page)
        if not cursor:
            break
    existing = {m.model_name: m for m in (await session.execute(
        select(AIModel).where(AIModel.provider_id == provider.id))).scalars()}
    catalog = {x["name"]: x for x in CATALOG.get(provider.kind, {}).get("models", [])}
    now = utcnow()
    created, updated = [], []
    for d in found:
        caps = {**_heuristic_caps(d.id), **d.caps}
        meta = {k: {"value": v, "source": "provider" if k in d.caps else "heuristic", "at": now.isoformat()}
                for k, v in caps.items()}
        m = existing.get(d.id)
        if m is None:
            m = AIModel(provider_id=provider.id, model_name=d.id, display_name=d.label, enabled=False,
                        capabilities=caps, capability_meta=meta, source="discovered",
                        context_window=d.context_window or 128000)
            m.capability_meta = {**meta, **d.meta}
            preset = catalog.get(d.id)
            if d.price:
                m.price_in, m.price_out = d.price
                if d.cache_price:
                    m.price_cache_read = d.cache_price
                m.pricing_source, m.pricing_checked_at = "provider_api", now
            elif preset:
                m.price_in, m.price_out, m.price_cache_read, m.price_cache_write = (
                    Decimal(str(x)) for x in preset["price"])
                m.pricing_source, m.pricing_checked_at = "catalog_unverified", now
            session.add(m)
            created.append(d.id)
        else:
            # فقط داده‌های قطعی API به‌روز می‌شوند؛ نتایج آزمون عملی بازنویسی نمی‌شوند
            meta_cur = dict(m.capability_meta or {})
            caps_cur = dict(m.capabilities or {})
            for k, v in d.caps.items():
                if (meta_cur.get(k) or {}).get("source") not in ("probe", "manual"):
                    caps_cur[k] = v
                    meta_cur[k] = {"value": v, "source": "provider", "at": now.isoformat()}
            for k, v in d.meta.items():
                if (meta_cur.get(k) or {}).get("source") != "manual":
                    meta_cur[k] = v
            m.capabilities, m.capability_meta = caps_cur, meta_cur
            if d.price and m.pricing_source in ("unknown", "provider_api", "catalog_unverified"):
                m.price_in, m.price_out = d.price
                if d.cache_price:
                    m.price_cache_read = d.cache_price
                m.pricing_source, m.pricing_checked_at = "provider_api", now
            if d.context_window:
                m.context_window = d.context_window
            updated.append(d.id)
    await session.flush()
    visible = {d.id for d in found}
    missing = [n for n in existing if n not in visible]
    return {"provider": provider.name, "found": len(found), "created": created, "updated": len(updated),
            "not_visible_with_key": missing}


# ───────────── آزمون عملی قابلیت‌ها ─────────────
def _png_red(size: int = 16) -> str:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


RED_PNG = _png_red()


async def probe_model(session: AsyncSession, model_id: str, checks: tuple[str, ...] = ("chat", "tools", "vision"),
                      run_id: Any = None) -> dict[str, Any]:
    """آزمون واقعی مدل؛ نتیجه با منبع «probe» و زمان در دیتابیس ذخیره می‌شود."""
    from ..llm import call_llm

    bound = await load_model(session, model_id, include_disabled=True)
    m = await session.get(AIModel, bound.id) if bound else None
    if bound is None or m is None:
        return {"ok": False, "error": "مدل یا ارائه‌دهنده یافت نشد."}
    results: dict[str, Any] = {}
    now = utcnow().isoformat()
    caps = dict(m.capabilities or {})
    meta = dict(m.capability_meta or {})

    def setcap(cap: str, val: bool, note: str = "") -> None:
        caps[cap] = val
        meta[cap] = {"value": val, "source": "probe", "at": now, **({"note": note[:200]} if note else {})}

    if "chat" in checks and not caps.get("embedding") and not caps.get("tts") and not caps.get("stt"):
        try:
            r = await call_llm(session, [bound], purpose="probe", system_stable="", system_dynamic="",
                               messages=[LLMMessage("user", "Reply with exactly: OK")], max_tokens=64, run_id=run_id)
            results["chat"] = {"ok": True, "reply": r.response.text[:40]}
        except ProviderError as e:
            results["chat"] = {"ok": False, "error": str(e)[:300]}
    chat_ok = results.get("chat", {}).get("ok")
    if "tools" in checks and chat_ok:
        tool = ToolSpec("report_status", "Report the status string.",
                        {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]})
        try:
            r = await call_llm(session, [bound], purpose="probe", system_stable="", system_dynamic="",
                               messages=[LLMMessage("user", "Call the report_status tool with status 'ok'. Do not answer in text.")],
                               tools=[tool], max_tokens=256, run_id=run_id)
            ok = any(c.name == "report_status" for c in r.response.tool_calls)
            setcap("tools", ok, "" if ok else "مدل به‌جای فراخوانی ابزار متن برگرداند")
            results["tools"] = {"ok": ok}
        except ProviderError as e:
            setcap("tools", False, str(e))
            results["tools"] = {"ok": False, "error": str(e)[:300]}
    if "vision" in checks and chat_ok:
        try:
            r = await call_llm(session, [bound], purpose="probe", system_stable="", system_dynamic="",
                               messages=[LLMMessage("user", "What is the dominant color of this image? Answer with one English word.",
                                                    images=[ImagePart("image/png", RED_PNG)])], max_tokens=64, run_id=run_id)
            ok = "red" in r.response.text.lower() or "قرمز" in r.response.text
            setcap("vision", ok, "" if ok else f"پاسخ نادرست: {r.response.text[:60]}")
            results["vision"] = {"ok": ok, "reply": r.response.text[:40]}
        except ProviderError as e:
            setcap("vision", False, str(e))
            results["vision"] = {"ok": False, "error": str(e)[:300]}
    m.capabilities, m.capability_meta = caps, meta
    m.updated_at = utcnow()
    await session.flush()
    return {"ok": bool(chat_ok) or bool(caps.get("embedding") or caps.get("tts") or caps.get("stt")),
            "model": m.display_name or m.model_name, "results": results, "capabilities": caps}
