"""انتخاب مدل برای هر نقش در حالت اسمارت (مدیر/کارمند با Fallback) و حرفه‌ای (Dropdown هر بخش)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.models import AIModel, AIProvider
from xagent_core.settings_store import AISettings, get_setting

from .providers.registry import BoundModel, load_model
from .router import TaskProfile, route

# نقش‌های حالت حرفه‌ای و قابلیت لازم برای اعتبارسنجی
ROLE_CAPS: dict[str, str | None] = {
    "management": "tools",
    "goal": None,
    "planning": None,
    "execution": "tools",
    "vision": "vision",
    "stt": "stt",
    "tts": "tts",
    "memory": None,
    "evaluation": None,
    "dream": None,
}
ROLE_LABELS = {
    "management": "مدیریت",
    "goal": "هدف‌سنجی",
    "planning": "برنامه‌ریزی",
    "execution": "انجام تسک",
    "vision": "چشم (تصویر)",
    "stt": "گوش (گفتار به متن)",
    "tts": "صدا (متن به گفتار)",
    "memory": "مدیریت حافظه",
    "evaluation": "ارزیابی، تشخیص و اصلاح",
    "dream": "رویا",
}
MANAGER_ROLES = {"management", "goal", "planning", "evaluation", "dream"}


@dataclass
class Selection:
    models: list[BoundModel]
    tier: str  # manager | worker | pro | override | routed
    decision: dict | None = None  # علت انتخاب مسیریاب (برای ثبت و نمایش)

    @property
    def primary(self) -> BoundModel | None:
        return self.models[0] if self.models else None


async def _bind_many(session: AsyncSession, ids: list[str], enabled_flags: list[bool] | None = None) -> list[BoundModel]:
    out: list[BoundModel] = []
    for i, mid in enumerate(ids[:3]):
        if enabled_flags is not None and i < len(enabled_flags) and not enabled_flags[i]:
            continue
        b = await load_model(session, mid)
        if b is not None:
            out.append(b)
    return out


async def _all_enabled(session: AsyncSession) -> list[BoundModel]:
    rows = (await session.execute(
        select(AIModel.id).join(AIProvider, AIProvider.id == AIModel.provider_id)
        .where(AIModel.enabled.is_(True), AIProvider.enabled.is_(True)).order_by(AIModel.created_at))).scalars().all()
    out = []
    for mid in rows:
        b = await load_model(session, mid)
        if b is not None and not (b.capabilities or {}).get("embedding"):
            out.append(b)
    return out


async def _any_with_cap(session: AsyncSession, cap: str) -> list[BoundModel]:
    rows = (
        await session.execute(
            select(AIModel.id)
            .join(AIProvider, AIProvider.id == AIModel.provider_id)
            .where(AIModel.enabled.is_(True), AIProvider.enabled.is_(True))
            .order_by(AIModel.created_at)
        )
    ).scalars().all()
    out = []
    for mid in rows:
        b = await load_model(session, mid)
        if b and b.has(cap):
            out.append(b)
    return out


async def select_models(
    session: AsyncSession,
    role: str,
    tier_hint: str = "worker",
    override_model_id: str | None = None,
    need_cap: str | None = None,
    profile: TaskProfile | None = None,
) -> Selection:
    """برگرداندن زنجیره مدل‌ها (اولی اصلی، بقیه Fallback) برای یک نقش."""
    ai = await get_setting(session, "ai", AISettings)
    cap = need_cap or ROLE_CAPS.get(role)

    def _filter(ms: list[BoundModel]) -> list[BoundModel]:
        if cap in ("vision", "stt", "tts"):
            return [m for m in ms if m.has(cap)]
        if cap == "tools":
            # مدل‌های آزمون‌نشده حذف نمی‌شوند تا چرخه اجرا آن‌ها را خودکار آزمون کند
            return [m for m in ms if m.has("tools") or "tools" not in (m.capability_meta or {})] or ms
        return ms

    if override_model_id and override_model_id != "auto" and role not in ("stt", "tts"):
        b = await load_model(session, override_model_id)
        if b is not None and (cap != "vision" or b.has("vision")):
            return Selection([b], "override")

    if ai.mode == "pro":
        mid = ai.pro.get(role)
        chain: list[BoundModel] = []
        if mid:
            b = await load_model(session, mid)
            if b:
                chain.append(b)
        # Fallback: مدل مدیریت، سپس هر مدل دارای قابلیت
        fallback_id = ai.pro.get("management")
        if fallback_id and fallback_id != mid and cap not in ("vision", "stt", "tts"):
            b = await load_model(session, fallback_id)
            if b:
                chain.append(b)
        chain = _filter(chain)
        if not chain and cap:
            chain = await _any_with_cap(session, cap)
        return Selection(chain, "pro")

    if ai.routing_policy != "custom" and cap not in ("stt", "tts"):
        pool = await _all_enabled(session)
        prof = profile or TaskProfile(complexity="complex" if (role in MANAGER_ROLES or tier_hint == "manager") else "simple")
        if cap and cap not in prof.need_caps and cap in ("tools", "vision"):
            prof.need_caps = (*prof.need_caps, cap)
        ordered, decision = await route(session, pool, ai.routing_policy, prof)
        if ordered:
            return Selection(ordered[:3], "routed", decision)
    managers = await _bind_many(session, ai.smart.manager_model_ids, ai.smart.manager_enabled or None)
    workers = await _bind_many(session, ai.smart.worker_model_ids, ai.smart.worker_enabled or None)
    if role in MANAGER_ROLES or tier_hint == "manager":
        chain, tier = managers + workers, "manager"
    else:
        chain, tier = workers + managers, "worker"
    chain = _filter(chain)
    if not chain and cap:
        chain = await _any_with_cap(session, cap)
    if not chain:
        # هیچ مدل اسمارتی تعریف نشده: اولین مدل فعال
        rows = (await session.execute(select(AIModel.id).where(AIModel.enabled.is_(True)).limit(3))).scalars().all()
        chain = _filter(await _bind_many(session, [str(r) for r in rows]))
    return Selection(chain, tier)

PRO_ROLE_CAPS_DOC = {"vision": "بینایی", "stt": "گفتار به متن", "tts": "تولید صدا", "tools": "فراخوانی ابزار"}
