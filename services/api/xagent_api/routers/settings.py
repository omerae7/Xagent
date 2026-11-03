"""تنظیمات: پایه/زمان، هوش مصنوعی (ارائه‌دهندگان و مدل‌ها، اسمارت/حرفه‌ای)، Harness، Stop-Loss، رویا،
تلگرام، توقف اضطراری و به‌روزرسانی هسته از GitHub."""

from __future__ import annotations

import json
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
from xagent_agent.providers.registry import CATALOG, test_model
from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.config import get_settings
from xagent_core.db import utcnow
from xagent_core.models import AIModel, AIProvider, HarnessRule, User
from xagent_core.security import decrypt_secret, encrypt_secret, mask_secret
from xagent_core.settings_store import DEFAULTS, AISettings, all_settings, set_setting

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
    await set_setting(session, key, value)
    await audit(session, user.username, f"settings.{key}", None, value.model_dump(mode="json"))
    await bus.publish("settings.changed", {"key": key})
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


def _ser_provider(p: AIProvider) -> dict[str, Any]:
    return ser(p, extra={"api_key_masked": mask_secret(decrypt_secret(p.api_key_enc)), "has_key": bool(p.api_key_enc)})


@router.get("/ai/catalog")
async def catalog(_: User = Depends(current_user)):
    return {"providers": CATALOG, "roles": ROLE_LABELS, "role_caps": ROLE_CAPS}


@router.get("/ai/providers")
async def providers(_: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    ps = (await session.execute(select(AIProvider).order_by(AIProvider.created_at))).scalars().all()
    ms = (await session.execute(select(AIModel).order_by(AIModel.created_at))).scalars().all()
    return {"providers": [_ser_provider(p) for p in ps], "models": [ser(m) for m in ms]}


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


def _apply_model(m: AIModel, body: ModelIn) -> None:
    for f in ("model_name", "display_name", "enabled", "capabilities", "context_window"):
        v = getattr(body, f)
        if v is not None:
            setattr(m, f, v)
    for f in ("price_in", "price_out", "price_cache_read", "price_cache_write"):
        v = getattr(body, f)
        if v is not None:
            setattr(m, f, Decimal(str(v)))


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
        m.capabilities = {"tools": True}
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
        log = (s.data_dir / "control" / "update.log").read_text()[-6000:]
    except OSError:
        log = ""
    return {"status": status, "log": log, "version": _version()}
