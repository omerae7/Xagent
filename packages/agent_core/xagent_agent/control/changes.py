"""سرویس تغییرات پایدار: هر تغییر مدیر/کارفرما/خوددرمانی با قبل و بعد ثبت، ممیزی و قابل Rollback است."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.db import utcnow
from xagent_core.models import AIModel, AIProvider, ConfigChange, Employee, InstalledTool, SettingKV
from xagent_core.settings_store import DEFAULTS

from . import policy

MODEL_FIELDS = ("enabled", "display_name", "capabilities", "capability_meta", "context_window", "price_in", "price_out",
                "price_cache_read", "price_cache_write", "pricing_source", "health", "cooldown_until")
EMPLOYEE_FIELDS = ("name", "title", "description", "instructions", "model_ids", "tool_groups", "memory_scope",
                   "enabled", "max_steps", "max_cost_usd")


class ChangeError(Exception):
    def __init__(self, message: str, decision: str = "forbidden"):
        super().__init__(message)
        self.decision = decision


def _j(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, list):
        return [_j(x) for x in v]
    if isinstance(v, dict):
        return {k: _j(x) for k, x in v.items()}
    return v


def snap(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: _j(getattr(obj, f, None)) for f in fields}


async def _record(session: AsyncSession, *, actor: str, target_type: str, target_id: str, action: str, summary: str,
                  reason: str, before: dict | None, after: dict | None, run_id: uuid.UUID | None = None,
                  risk: str = "low", status: str = "applied") -> ConfigChange:
    ch = ConfigChange(actor=actor, run_id=run_id, target_type=target_type, target_id=target_id, action=action,
                      summary=summary[:2000], reason=reason[:2000], risk=risk, before=before, after=after, status=status)
    session.add(ch)
    await session.flush()
    await audit(session, actor, f"config.{target_type}.{action}", target_id, {"change_id": str(ch.id), "summary": summary})
    await bus.publish("config.changed", {"id": str(ch.id), "target_type": target_type, "target_id": target_id,
                                         "action": action, "actor": actor, "summary": summary})
    return ch


# ───────────── تنظیمات ─────────────
async def change_setting(session: AsyncSession, key: str, values: dict[str, Any], *, actor: str, reason: str,
                         run_id: uuid.UUID | None = None, approved: bool = False) -> ConfigChange:
    model = DEFAULTS.get(key)
    if model is None:
        raise ChangeError(f"بخش تنظیمات «{key}» وجود ندارد. بخش‌ها: {', '.join(DEFAULTS)}", "invalid")
    row = await session.get(SettingKV, key)
    before = model.model_validate(row.value if row else {}).model_dump(mode="json")
    merged = _deep_merge(before, values)
    try:
        after = model.model_validate(merged).model_dump(mode="json")
    except Exception as e:  # noqa: BLE001
        raise ChangeError(f"مقادیر نامعتبر برای «{key}»: {e}", "invalid") from e
    if after == before:
        raise ChangeError("تغییری ایجاد نشد (مقادیر جدید با فعلی یکسان است).", "noop")
    pr = policy.check_setting(key, before, after, actor)
    if pr.decision == "forbidden":
        raise ChangeError(pr.reason, "forbidden")
    if pr.decision == "approval" and not approved:
        raise ChangeError(pr.reason, "approval")
    if key == "ai":
        await _validate_ai(session, after)
    if row is None:
        session.add(SettingKV(key=key, value=after))
    else:
        row.value = after
    await session.flush()
    diff = {k: after[k] for k in after if after.get(k) != before.get(k)}
    ch = await _record(session, actor=actor, target_type="setting", target_id=key, action="update",
                       summary=f"تغییر تنظیمات «{key}»: " + ", ".join(sorted(diff))[:300], reason=reason,
                       before=before, after=after, run_id=run_id, risk="medium" if pr.loosened else "low")
    await bus.publish("settings.changed", {"key": key})
    return ch


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


async def _validate_ai(session: AsyncSession, ai: dict[str, Any]) -> None:
    smart = ai.get("smart") or {}
    for key in ("manager_model_ids", "worker_model_ids"):
        ids = smart.get(key) or []
        if len(ids) > 3:
            raise ChangeError("حداکثر ۳ مدل برای هر سطح مجاز است.", "invalid")
        for mid in ids:
            if not mid:
                continue
            m = await session.get(AIModel, _uuid(mid))
            if m is None:
                raise ChangeError(f"مدل {mid} وجود ندارد.", "invalid")
    from ..model_select import ROLE_CAPS

    for role, mid in (ai.get("pro") or {}).items():
        if not mid:
            continue
        m = await session.get(AIModel, _uuid(mid))
        if m is None:
            raise ChangeError(f"مدل نقش «{role}» وجود ندارد.", "invalid")
        cap = ROLE_CAPS.get(role)
        if cap and not (m.capabilities or {}).get(cap):
            raise ChangeError(f"مدل «{m.display_name or m.model_name}» قابلیت «{cap}» لازم برای نقش «{role}» را ندارد.",
                              "invalid")


def _uuid(v: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(v))
    except ValueError as e:
        raise ChangeError(f"شناسه نامعتبر: {v}", "invalid") from e


# ───────────── مدل ─────────────
async def change_model(session: AsyncSession, model_id: str | uuid.UUID, changes: dict[str, Any], *, actor: str,
                       reason: str, run_id: uuid.UUID | None = None) -> ConfigChange:
    m = await session.get(AIModel, _uuid(model_id))
    if m is None:
        raise ChangeError("مدل یافت نشد.", "invalid")
    allowed = {"enabled", "display_name", "capabilities", "context_window", "price_in", "price_out", "price_cache_read",
               "price_cache_write", "pricing_source", "health", "cooldown_until"}
    bad = set(changes) - allowed
    if bad:
        raise ChangeError(f"فیلدهای غیرمجاز برای مدل: {', '.join(bad)}", "invalid")
    before = snap(m, MODEL_FIELDS)
    now = utcnow().isoformat()
    for k, v in changes.items():
        if k == "capabilities":
            caps = dict(m.capabilities or {})
            meta = dict(m.capability_meta or {})
            for cap, val in (v or {}).items():
                caps[cap] = bool(val)
                meta[cap] = {"value": bool(val), "source": "manual" if actor == "owner" else "manager", "at": now}
            m.capabilities, m.capability_meta = caps, meta
        elif k.startswith("price_"):
            setattr(m, k, Decimal(str(v)))
            m.pricing_checked_at = utcnow()
        elif k == "health" and v == "ok":
            m.health, m.consecutive_failures, m.cooldown_until = "ok", 0, None
        else:
            setattr(m, k, v)
    if any(k.startswith("price_") for k in changes) and "pricing_source" not in changes:
        m.pricing_source = "manual" if actor == "owner" else "manager_unverified"
    after = snap(m, MODEL_FIELDS)
    if after == before:
        raise ChangeError("تغییری ایجاد نشد.", "noop")
    m.updated_at = utcnow()
    return await _record(session, actor=actor, target_type="model", target_id=str(m.id), action="update",
                         summary=f"مدل «{m.display_name or m.model_name}»: " + ", ".join(sorted(changes)), reason=reason,
                         before=before, after=after, run_id=run_id)


async def change_provider_enabled(session: AsyncSession, provider_id: str, enabled: bool, *, actor: str, reason: str,
                                  run_id: uuid.UUID | None = None) -> ConfigChange:
    p = await session.get(AIProvider, _uuid(provider_id))
    if p is None:
        raise ChangeError("ارائه‌دهنده یافت نشد.", "invalid")
    if p.enabled == enabled:
        raise ChangeError("تغییری ایجاد نشد.", "noop")
    before = {"enabled": p.enabled}
    p.enabled = enabled
    p.updated_at = utcnow()
    return await _record(session, actor=actor, target_type="provider", target_id=str(p.id), action="enable" if enabled else "disable",
                         summary=f"ارائه‌دهنده «{p.name}» {'فعال' if enabled else 'غیرفعال'} شد", reason=reason,
                         before=before, after={"enabled": enabled}, run_id=run_id)


async def change_tool_enabled(session: AsyncSession, tool_ref: str, enabled: bool, *, actor: str, reason: str,
                              run_id: uuid.UUID | None = None) -> ConfigChange:
    q = select(InstalledTool).where(InstalledTool.name == tool_ref)
    t = (await session.execute(q)).scalar_one_or_none()
    if t is None:
        try:
            t = await session.get(InstalledTool, uuid.UUID(tool_ref))
        except ValueError:
            t = None
    if t is None:
        raise ChangeError(f"ابزار نصب‌شده «{tool_ref}» یافت نشد (ابزارهای داخلی همیشه فعال‌اند).", "invalid")
    if enabled and t.status == "error":
        raise ChangeError(f"ابزار «{t.name}» در وضعیت خطاست: {t.last_error}. ابتدا به‌روزرسانی یا Rollback لازم است.", "invalid")
    if t.enabled == enabled:
        raise ChangeError("تغییری ایجاد نشد.", "noop")
    before = {"enabled": t.enabled, "status": t.status}
    t.enabled = enabled
    t.status = "ready" if enabled else "disabled"
    return await _record(session, actor=actor, target_type="tool", target_id=t.name, action="enable" if enabled else "disable",
                         summary=f"ابزار «{t.name}» {'فعال' if enabled else 'غیرفعال'} شد", reason=reason,
                         before=before, after={"enabled": t.enabled, "status": t.status}, run_id=run_id)


# ───────────── کارمند ─────────────
async def upsert_employee(session: AsyncSession, data: dict[str, Any], *, actor: str, reason: str,
                          run_id: uuid.UUID | None = None) -> tuple[Employee, ConfigChange]:
    name = (data.get("name") or "").strip()
    if not name:
        raise ChangeError("نام کارمند لازم است.", "invalid")
    e = (await session.execute(select(Employee).where(Employee.name == name))).scalar_one_or_none()
    before = snap(e, EMPLOYEE_FIELDS) if e else None
    if e is None:
        e = Employee(name=name, created_by=actor)
        session.add(e)
    for f in EMPLOYEE_FIELDS:
        if f in data and data[f] is not None and f != "name":
            v = data[f]
            if f == "model_ids":
                for mid in v:
                    if await session.get(AIModel, _uuid(mid)) is None:
                        raise ChangeError(f"مدل {mid} برای کارمند وجود ندارد.", "invalid")
                v = [str(x) for x in v][:3]
            if f == "max_cost_usd":
                v = Decimal(str(v))
            setattr(e, f, v)
    await session.flush()
    after = snap(e, EMPLOYEE_FIELDS)
    if before == after:
        raise ChangeError("تغییری ایجاد نشد.", "noop")
    ch = await _record(session, actor=actor, target_type="employee", target_id=str(e.id),
                       action="create" if before is None else "update",
                       summary=f"کارمند «{e.name}» ({e.title}) {'ساخته' if before is None else 'به‌روزرسانی'} شد",
                       reason=reason, before=before, after=after, run_id=run_id)
    return e, ch


async def delete_employee(session: AsyncSession, name: str, *, actor: str, reason: str,
                          run_id: uuid.UUID | None = None) -> ConfigChange:
    e = (await session.execute(select(Employee).where(Employee.name == name))).scalar_one_or_none()
    if e is None:
        raise ChangeError("کارمند یافت نشد.", "invalid")
    before = snap(e, EMPLOYEE_FIELDS)
    eid = str(e.id)
    await session.delete(e)
    await session.flush()
    return await _record(session, actor=actor, target_type="employee", target_id=eid, action="delete",
                         summary=f"کارمند «{name}» حذف شد", reason=reason, before=before, after=None, run_id=run_id)


# ───────────── Rollback ─────────────
@dataclass
class RollbackResult:
    reverted: list[str]
    message: str


async def rollback(session: AsyncSession, change_id: str | uuid.UUID, *, actor: str,
                   reason: str = "بازگردانی") -> RollbackResult:
    ch = await session.get(ConfigChange, _uuid(change_id))
    if ch is None:
        raise ChangeError("تغییر یافت نشد.", "invalid")
    if ch.status == "rolled_back":
        return RollbackResult([], "این تغییر قبلاً بازگردانده شده است.")
    if actor != "owner" and ch.target_type == "setting" and ch.target_id in policy.OWNER_ONLY_SETTINGS:
        raise ChangeError("بازگردانی تنظیمات امنیتی فقط با کارفرماست.", "forbidden")
    newer = (await session.execute(
        select(ConfigChange).where(ConfigChange.target_type == ch.target_type, ConfigChange.target_id == ch.target_id,
                                   ConfigChange.created_at > ch.created_at, ConfigChange.status != "rolled_back",
                                   ConfigChange.action != "rollback")
        .order_by(ConfigChange.created_at.desc()))).scalars().all()
    reverted = []
    for c in [*newer, ch]:
        await _restore(session, c)
        c.status = "rolled_back"
        c.rolled_back_at = utcnow()
        c.rolled_back_by = actor
        reverted.append(str(c.id))
    await _record(session, actor=actor, target_type=ch.target_type, target_id=ch.target_id, action="rollback",
                  summary=f"بازگردانی {len(reverted)} تغییر: {ch.summary[:200]}", reason=reason,
                  before=ch.after, after=ch.before, status="verified")
    if ch.target_type == "setting":
        await bus.publish("settings.changed", {"key": ch.target_id})
    return RollbackResult(reverted, f"{len(reverted)} تغییر بازگردانده شد.")


async def _restore(session: AsyncSession, c: ConfigChange) -> None:
    before = c.before
    if c.target_type == "setting":
        row = await session.get(SettingKV, c.target_id)
        if before is None:
            if row:
                await session.delete(row)
        elif row is None:
            session.add(SettingKV(key=c.target_id, value=before))
        else:
            row.value = before
    elif c.target_type == "model":
        m = await session.get(AIModel, _uuid(c.target_id))
        if m and before:
            for k, v in before.items():
                if k in ("price_in", "price_out", "price_cache_read", "price_cache_write"):
                    v = Decimal(str(v or 0))
                if k == "cooldown_until" and isinstance(v, str):
                    v = datetime.fromisoformat(v)
                setattr(m, k, v)
    elif c.target_type == "provider":
        p = await session.get(AIProvider, _uuid(c.target_id))
        if p and before:
            p.enabled = before.get("enabled", p.enabled)
    elif c.target_type == "tool":
        t = (await session.execute(select(InstalledTool).where(InstalledTool.name == c.target_id))).scalar_one_or_none()
        if t and before:
            t.enabled, t.status = before.get("enabled", t.enabled), before.get("status", t.status)
    elif c.target_type == "employee":
        e = await session.get(Employee, _uuid(c.target_id))
        if before is None:
            if e:
                await session.delete(e)
        else:
            if e is None:
                e = Employee(id=_uuid(c.target_id), name=before["name"])
                session.add(e)
            for k, v in before.items():
                setattr(e, k, Decimal(str(v)) if k == "max_cost_usd" else v)
    await session.flush()
