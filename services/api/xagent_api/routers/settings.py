"""تنظیمات: پایه/زمان، هوش مصنوعی (ارائه‌دهندگان و مدل‌ها، اسمارت/حرفه‌ای)، Harness، Stop-Loss، رویا،
تلگرام، توقف اضطراری و به‌روزرسانی هسته از GitHub."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_agent import harness
from xagent_agent.commands import run_command
from xagent_agent.model_select import PRO_ROLE_CAPS_DOC, ROLE_CAPS, ROLE_LABELS
from xagent_agent.providers.registry import CATALOG, PROVIDER_STATUS_FA, provider_status, test_model
from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.config import get_settings
from xagent_core.db import utcnow
from xagent_core.models import AIModel, AIProvider, HarnessRule, User
from xagent_core.security import decrypt_secret, encrypt_secret, mask_secret
from xagent_core.settings_store import DEFAULTS, AISettings, all_settings

from ..deps import current_user, get_session, owner_user, parse_uuid, ser

router = APIRouter(prefix="/api/settings", tags=["settings"])
TIMEZONES = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))


@router.get("")
async def get_all(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    data = await all_settings(session)
    return {"settings": data, "timezones": TIMEZONES, "version": _version()}


@router.put("/{key}")
async def put(key: str, body: dict[str, Any], user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    model = DEFAULTS.get(key)
    if model is None:
        raise HTTPException(404, "بخش تنظیمات ناشناخته است")
    try:
        value = model.model_validate(body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"مقادیر نامعتبر: {e}") from e
    if key == "general" and value.timezone not in TIMEZONES and value.timezone != "UTC":
        raise HTTPException(422, "منطقه زمانی نامعتبر است")
    if key == "ai":
        await _validate_ai(session, value)
    if key == "cursor":
        from .external_agents import validate_cursor

        value = validate_cursor(value)
    from xagent_agent.control import changes as _changes

    try:
        await _changes.change_setting(session, key, value.model_dump(mode="json"), actor="owner", reason="ویرایش از پنل")
    except _changes.ChangeError as e:
        if e.decision != "noop":
            raise HTTPException(422, str(e)) from e
    return {"ok": True, "value": value.model_dump(mode="json")}


async def _validate_ai(session: AsyncSession, ai: AISettings) -> None:
    for ids in (ai.smart.manager_model_ids, ai.smart.worker_model_ids):
        if len(ids) > 3:
            raise HTTPException(422, "حداکثر ۳ مدل برای هر سطح مجاز است")
    for role, mid in ai.pro.items():
        if not mid:
            continue
        m = await session.get(AIModel, parse_uuid(mid))
        if m is None:
            raise HTTPException(422, f"مدل انتخاب‌شده برای «{ROLE_LABELS.get(role, role)}» وجود ندارد")
        cap = ROLE_CAPS.get(role)
        if cap and cap != "tools" and not (m.capabilities or {}).get(cap):
            raise HTTPException(422, f"مدل «{m.display_name or m.model_name}» قابلیت لازم برای «{ROLE_LABELS[role]}» "
                                     f"({PRO_ROLE_CAPS_DOC.get(cap, cap)}) را ندارد")
        if cap == "tools" and not (m.capabilities or {}).get("tools"):
            raise HTTPException(422, f"مدل «{m.display_name or m.model_name}» از فراخوانی ابزار پشتیبانی نمی‌کند")


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _version() -> dict[str, Any]:
    s = get_settings()
    vf = s.data_dir / "control" / "version.json"
    try:
        return json.loads(vf.read_text())
    except (OSError, ValueError):
        return {"version": "0.1.0", "commit": None}


# ───────────── ارائه‌دهندگان و مدل‌ها ─────────────
class ProviderIn(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    kind: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    enabled: bool | None = None


class ModelIn(BaseModel):
    provider_id: str | None = None
    model_name: str | None = None
    display_name: str | None = None
    enabled: bool | None = None
    capabilities: dict[str, bool] | None = None
    context_window: int | None = None
    price_in: float | None = None
    price_out: float | None = None
    price_cache_read: float | None = None
    price_cache_write: float | None = None
    # کنترل استدلال مدل‌های Kimi (فقط با تأیید مستندات/آزمون توسط کارفرما): none | toggle | always
    thinking_control: str | None = Field(None, pattern="^(none|toggle|always)$")


def _ser_provider(p: AIProvider, models: list[AIModel] | None = None) -> dict[str, Any]:
    status, reason = provider_status(p, models or [])
    return ser(p, extra={"api_key_masked": mask_secret(decrypt_secret(p.api_key_enc)), "has_key": bool(p.api_key_enc),
                         "status": status, "status_label": PROVIDER_STATUS_FA[status], "status_reason": reason,
                         "type": CATALOG.get(p.kind, {}).get("type", "llm")})


@router.get("/ai/catalog")
async def catalog(_: User = Depends(current_user)):
    return {"providers": CATALOG, "roles": ROLE_LABELS, "role_caps": ROLE_CAPS}


@router.get("/ai/providers")
async def providers(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    ps = (await session.execute(select(AIProvider).order_by(AIProvider.created_at))).scalars().all()
    ms = (await session.execute(select(AIModel).order_by(AIModel.created_at))).scalars().all()
    return {"providers": [_ser_provider(p, list(ms)) for p in ps], "models": [ser(m) for m in ms]}


@router.post("/ai/providers")
async def create_provider(body: ProviderIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    if not body.kind or body.kind not in CATALOG:
        raise HTTPException(400, "نوع ارائه‌دهنده نامعتبر است")
    p = AIProvider(name=body.name or CATALOG[body.kind]["label"], kind=body.kind,
                   base_url=body.base_url or CATALOG[body.kind].get("base_url"),
                   api_key_enc=encrypt_secret(body.api_key), enabled=True if body.enabled is None else body.enabled)
    session.add(p)
    await session.flush()
    await audit(session, user.username, "ai.provider_create", p.name, {"kind": p.kind})
    return _ser_provider(p)


@router.patch("/ai/providers/{pid}")
async def update_provider(pid: str, body: ProviderIn, user: User = Depends(owner_user),
                          session: AsyncSession = Depends(get_session)):
    p = await session.get(AIProvider, parse_uuid(pid))
    if p is None:
        raise HTTPException(404, "یافت نشد")
    if body.name is not None:
        p.name = body.name
    if body.base_url is not None:
        p.base_url = body.base_url or None
    if body.api_key:
        p.api_key_enc = encrypt_secret(body.api_key)
    if body.enabled is not None:
        p.enabled = body.enabled
    p.updated_at = utcnow()
    await audit(session, user.username, "ai.provider_update", p.name)
    return _ser_provider(p)


@router.delete("/ai/providers/{pid}")
async def delete_provider(pid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    p = await session.get(AIProvider, parse_uuid(pid))
    if p is None:
        raise HTTPException(404, "یافت نشد")
    await audit(session, user.username, "ai.provider_delete", p.name)
    await session.delete(p)
    return {"ok": True}



@router.get("/ai/providers/{pid}/discover")
async def discover_models(
    pid: str, cursor: str | None = None,
    _: User = Depends(owner_user), session: AsyncSession = Depends(get_session),
):
    """فهرست مدل‌های قابل دسترس با همین کلید (فهرست‌شدن به معنی تأیید ابزار/بینایی/صوت نیست).

    قیمت فقط وقتی برگردانده می‌شود که API ارائه‌دهنده آن را اعلام کرده باشد (مثلاً xAI: سنت به ازای ۱۰۰M توکن
    → دلار به ازای ۱M). نتیجه کشف وضعیت اتصال را هم ثبت می‌کند (آماده/نیازمند تنظیم/موقتاً در دسترس نیست).
    """
    from xagent_agent.control.capabilities import DiscoveryError, list_provider_models

    p = await session.get(AIProvider, parse_uuid(pid))
    if p is None:
        raise HTTPException(404, "ارائه‌دهنده یافت نشد")
    if not decrypt_secret(p.api_key_enc) and p.kind != "ollama":
        raise HTTPException(400, "کلید API ثبت نشده است")
    if cursor and len(cursor) > 256:
        raise HTTPException(400, "شناسه صفحه نامعتبر است")
    try:
        found, next_cursor = await list_provider_models(p, cursor)
    except DiscoveryError as e:
        p.last_test_at, p.last_test_ok, p.last_test_error = utcnow(), False, str(e)[:1000]
        await session.commit()
        raise HTTPException(502, f"دریافت مدل‌ها از شرکت ناموفق بود: {e}") from e
    p.last_test_at, p.last_test_ok, p.last_test_error = utcnow(), True, None
    items = []
    for d in found[:200]:
        if p.kind == "google" and d.caps.get("embedding"):
            continue
        detail = d.meta.get("pricing_detail") or {}
        items.append({"id": d.id, "label": d.label, "caps": d.caps, "context_window": d.context_window,
                      "price_in": float(d.price[0]) if d.price else None,
                      "price_out": float(d.price[1]) if d.price else None,
                      "price_cache_read": float(d.cache_price) if d.cache_price else None,
                      "price_status": "provider_api" if d.price else "unknown",
                      "long_context": detail.get("long_context"),
                      "reasoning": d.meta.get("reasoning"), "thinking": d.meta.get("thinking")})
    return {"items": items, "next_cursor": next_cursor}


def _apply_model(m: AIModel, body: ModelIn) -> None:
    for f in ("model_name", "display_name", "enabled", "capabilities", "context_window"):
        v = getattr(body, f)
        if v is not None:
            setattr(m, f, v)
    for f in ("price_in", "price_out", "price_cache_read", "price_cache_write"):
        v = getattr(body, f)
        if v is not None:
            setattr(m, f, Decimal(str(v)))
    if body.thinking_control is not None:
        meta = dict(m.capability_meta or {})
        meta["thinking"] = {"value": body.thinking_control != "none", "control": body.thinking_control,
                            "source": "manual", "at": utcnow().isoformat()}
        m.capability_meta = meta


@router.post("/ai/models")
async def create_model(body: ModelIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    if not body.provider_id or not body.model_name:
        raise HTTPException(400, "ارائه‌دهنده و نام مدل لازم است")
    p = await session.get(AIProvider, parse_uuid(body.provider_id))
    if p is None:
        raise HTTPException(404, "ارائه‌دهنده یافت نشد")
    preset = next((m for m in CATALOG.get(p.kind, {}).get("models", []) if m["name"] == body.model_name), None)
    m = AIModel(provider_id=p.id, model_name=body.model_name)
    if preset:
        m.display_name = preset["label"]
        m.capabilities = {c: True for c in preset["caps"]}
        m.context_window = preset["ctx"] or 128000
        m.price_in, m.price_out, m.price_cache_read, m.price_cache_write = (Decimal(str(x)) for x in preset["price"])
    else:
        # Unknown providers/models must not inherit unverified capabilities.
        m.capabilities = {}
    _apply_model(m, body)
    session.add(m)
    await session.flush()
    await audit(session, user.username, "ai.model_create", m.model_name)
    return ser(m)


@router.patch("/ai/models/{mid}")
async def update_model(mid: str, body: ModelIn, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    m = await session.get(AIModel, parse_uuid(mid))
    if m is None:
        raise HTTPException(404, "یافت نشد")
    _apply_model(m, body)
    m.updated_at = utcnow()
    return ser(m)


@router.delete("/ai/models/{mid}")
async def delete_model(mid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    m = await session.get(AIModel, parse_uuid(mid))
    if m is None:
        raise HTTPException(404, "یافت نشد")
    await audit(session, user.username, "ai.model_delete", m.model_name)
    await session.delete(m)
    return {"ok": True}


@router.post("/ai/models/{mid}/test")
async def test(mid: str, _: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    m = await session.get(AIModel, parse_uuid(mid))
    if m is None:
        raise HTTPException(404, "یافت نشد")
    ok, msg, latency = await test_model(session, m)
    return {"ok": ok, "message": msg, "latency_ms": latency}


@router.get("/ai/models/available")
async def available(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(AIModel, AIProvider).join(AIProvider, AIProvider.id == AIModel.provider_id)
        .where(AIModel.enabled.is_(True), AIProvider.enabled.is_(True)))).all()
    return {"items": [{"id": str(m.id), "label": m.display_name or m.model_name, "provider": p.name,
                       "kind": p.kind, "capabilities": m.capabilities} for m, p in rows]}


# ───────────── Harness ─────────────
class HarnessIn(BaseModel):
    mode: str = Field(pattern="^(auto|ask|conditional|forbidden)$")
    rule_text: str = ""
    conditions: dict[str, Any] = Field(default_factory=dict)


@router.get("/harness")
async def get_harness(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    await harness.ensure_defaults(session)
    rules = await harness.load_rules(session)
    return {"items": [
        {**ser(rules[c]), "label": meta["label"], "icon": meta.get("icon"), "locked": meta.get("locked", False),
         "never_auto": c in harness.NEVER_AUTO}
        for c, meta in harness.CATEGORIES.items() if c in rules
    ]}


@router.get("/permissions")
async def list_permissions(user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    from xagent_core.models import PermissionGrant
    rows = (await session.execute(select(PermissionGrant).where(PermissionGrant.owner == user.username)
                                  .order_by(PermissionGrant.created_at.desc()).limit(200))).scalars()
    return {"items": [ser(g, exclude={"scope_hash", "policy_revision"}) for g in rows]}


@router.post("/permissions/{gid}/revoke")
async def revoke_permission(gid: str, user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    from xagent_core.models import PermissionGrant
    g = await session.get(PermissionGrant, parse_uuid(gid), with_for_update=True)
    if g is None or g.owner != user.username:
        raise HTTPException(404, "مجوز پیدا نشد")
    if g.revoked_at is None:
        g.revoked_at = utcnow()
        await audit(session, user.username, "permission.revoked", str(g.id), {})
    return {"ok": True}


@router.put("/harness/{category}")
async def put_harness(category: str, body: HarnessIn, user: User = Depends(owner_user),
                      session: AsyncSession = Depends(get_session)):
    if category not in harness.CATEGORIES:
        raise HTTPException(404, "دسته ناشناخته")
    if category in harness.NEVER_AUTO and body.mode == "auto":
        raise HTTPException(422, "این دسته هرگز نمی‌تواند خودکار باشد (نیازمند تایید صریح کارفرما)")
    r = (await session.execute(select(HarnessRule).where(HarnessRule.category == category))).scalar_one_or_none()
    if r is None:
        r = HarnessRule(category=category)
        session.add(r)
    before = {"mode": r.mode, "rule_text": r.rule_text, "conditions": r.conditions}
    r.mode, r.rule_text, r.conditions = body.mode, body.rule_text, body.conditions
    await audit(session, user.username, "harness.update", category, {"before": before, "after": body.model_dump()})
    await bus.publish("settings.changed", {"key": "harness"})
    return ser(r)


# ───────────── سیستم ─────────────
@router.post("/system/stop-all")
async def stop_all(user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    return {"message": await run_command(session, "stop_all", "", actor=user.username)}


@router.post("/system/resume-all")
async def resume_all(user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    return {"message": await run_command(session, "resume_all", "", actor=user.username)}


@router.post("/system/update")
async def update_core(user: User = Depends(owner_user), session: AsyncSession = Depends(get_session)):
    """درخواست به‌روزرسانی هسته: فایل درخواست برای سرویس systemd میزبان نوشته می‌شود (بدون سوکت داکر در کانتینر)."""
    s = get_settings()
    ctl = s.data_dir / "control"
    ctl.mkdir(parents=True, exist_ok=True)
    try:
        cur = json.loads((ctl / "update.status").read_text())
    except (OSError, ValueError):
        cur = {}
    if cur.get("state") in ("running", "rolling_back") or (ctl / "update.request").exists():
        raise HTTPException(409, "یک به‌روزرسانی در حال اجرا یا در صف است؛ تا پایان آن صبر کنید.")
    req = {"requested_by": user.username, "at": utcnow().isoformat(), "branch": s.repo_branch}
    (ctl / "update.request").write_text(json.dumps(req), encoding="utf-8")
    (ctl / "update.status").write_text(json.dumps({"state": "requested", "at": req["at"]}), encoding="utf-8")
    await audit(session, user.username, "system.update_requested", None, req)
    return {"ok": True, "message": "درخواست به‌روزرسانی ثبت شد؛ سرویس به‌روزرسان سرور آن را اجرا می‌کند."}


@router.get("/system/update")
async def update_status(_: User = Depends(current_user)):
    s = get_settings()
    try:
        status = json.loads((s.data_dir / "control" / "update.status").read_text())
    except (OSError, ValueError):
        status = {"state": "idle"}
    try:
        log = _ANSI.sub("", (s.data_dir / "control" / "update.log").read_text(errors="replace"))[-8000:]
    except OSError:
        log = ""
    return {"status": status, "log": log, "version": _version()}
