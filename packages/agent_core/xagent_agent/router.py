"""مسیریاب مدل آگاه از قابلیت، هزینه و کیفیت (Capability/Cost/Quality-Aware Model Router).

هدف: کمترین «هزینه کل مورد انتظار یک اجرای موفق»، با رعایت کیفیت، تأخیر، سلامت و بودجه کارفرما — نه صرفاً
ارزان‌ترین قیمت هر توکن. مدل ارزان ولی کم‌کیفیت با تلاش‌های تکراری و بازیابی ممکن است گران‌تر تمام شود.

  هزینه هر فراخوانی  = (ورودی تخمینی × قیمت ورودی + خروجی تخمینی × قیمت خروجی) / ۱e6
  احتمال موفقیت       = قابلیت اطمینان مشاهده‌شده × تابع کیفیت (وزن کیفیت در کار پیچیده بیشتر)
  هزینه مورد انتظار  = هزینه هر فراخوانی × تعداد فراخوانی مورد انتظار ÷ احتمال موفقیت

سیاست‌ها: economy (کمترین هزینه مورد انتظار با حداقل کیفیت)، balanced (تعادل موفقیت/هزینه/تأخیر)، quality
(بیشترین کیفیت در محدوده بودجه)، custom (دقیقاً زنجیره و ترتیب تعیین‌شده کارفرما — رفتار قبلی).
قیمت نامعتبر هرگز صفر فرض نمی‌شود (قیمت محافظه‌کارانه Stop-Loss)؛ سقف بودجه کارفرما فیلتر سخت است.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import AIModel, UsageEvent

from .providers.registry import CATALOG, BoundModel

POLICIES = ("economy", "balanced", "quality", "custom")
TIER_QUALITY = {"manager": 0.85, "worker": 0.6}


@dataclass
class TaskProfile:
    complexity: str = "simple"  # simple | complex
    need_caps: tuple[str, ...] = ()
    est_in: int = 4000
    est_out: int = 600
    calls: int = 3
    budget_usd: float | None = None  # باقی‌مانده بودجه این کار (سقف سخت)
    min_context: int = 0


@dataclass
class Candidate:
    model: BoundModel
    quality: float
    reliability: float
    latency_ms: float
    cost_call: float
    p_success: float
    expected_cost: float
    price_estimated: bool
    samples: int
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"model": self.model.label, "quality": round(self.quality, 2), "reliability": round(self.reliability, 2),
                "latency_ms": int(self.latency_ms), "cost_per_call": round(self.cost_call, 6),
                "p_success": round(self.p_success, 2), "expected_cost": round(self.expected_cost, 6),
                "price": "estimated" if self.price_estimated else "verified", "samples": self.samples,
                "notes": self.notes}


def _catalog_tier(name: str) -> str | None:
    for prov in CATALOG.values():
        for m in prov.get("models", []):
            if m.get("name") == name:
                return m.get("tier")
    return None


async def _stats(session: AsyncSession, ids: list[Any]) -> dict[Any, tuple[int, float, float]]:
    """(تعداد، نرخ موفقیت، میانگین تأخیر) هر مدل در ۷ روز اخیر از دفتر مصرف واقعی."""
    if not ids:
        return {}
    rows = (await session.execute(
        select(UsageEvent.model_id, func.count(), func.avg(cast(UsageEvent.success, Integer)),
               func.avg(UsageEvent.latency_ms))
        .where(UsageEvent.model_id.in_(ids), UsageEvent.ts >= utcnow() - timedelta(days=7),
               UsageEvent.attempt_status.notin_(["reserved", "legacy"]))
        .group_by(UsageEvent.model_id))).all()
    return {mid: (int(n), float(ok or 0), float(lat or 0)) for mid, n, ok, lat in rows}


async def score(session: AsyncSession, models: list[BoundModel], profile: TaskProfile) -> list[Candidate]:
    rows = {r.id: r for r in (await session.execute(select(AIModel).where(AIModel.id.in_([m.id for m in models])))).scalars()}
    stats = await _stats(session, [m.id for m in models])
    now = utcnow()
    out: list[Candidate] = []
    for m in models:
        row = rows.get(m.id)
        notes: list[str] = []
        if any(not m.has(c) for c in profile.need_caps):
            continue
        if profile.min_context and m.context_window and m.context_window < profile.min_context:
            continue
        if row is not None and (row.health == "failing" or (row.cooldown_until and row.cooldown_until > now)):
            continue  # Provider/مدل خراب یا در دوره خنک‌شدن
        meta = m.capability_meta or {}
        quality = float(meta.get("quality") or TIER_QUALITY.get(_catalog_tier(m.model_name) or "", 0.6))
        n, ok_rate, lat = stats.get(m.id, (0, 0.0, 0.0))
        # میانگین بیزی: پیش‌فرض ۰٫۹ با وزن ۵ نمونه، تا چند خطای اتفاقی مدل تازه را حذف نکند
        reliability = (0.9 * 5 + ok_rate * n) / (5 + n)
        if row is not None and row.health == "degraded":
            reliability *= 0.7
            notes.append("کیفیت سرویس کاهش‌یافته")
        latency = lat if n else (2500.0 if quality >= 0.8 else 1500.0)
        cost_call = float(m.cost(profile.est_in, profile.est_out, 0, 0))
        w = 0.55 if profile.complexity == "complex" else 0.25
        p = max(0.05, min(0.99, reliability * ((1 - w) + w * quality)))
        expected = cost_call * profile.calls / p
        if m.price_estimated:
            notes.append("قیمت معتبر ندارد؛ با قیمت محافظه‌کارانه برآورد شد")
        out.append(Candidate(m, quality, reliability, latency, cost_call, p, expected, m.price_estimated, n, notes))
    return out


def rank(cands: list[Candidate], policy: str, profile: TaskProfile) -> tuple[list[Candidate], list[str]]:
    reasons: list[str] = []
    pool = cands
    if profile.budget_usd is not None:
        within = [c for c in pool if c.cost_call * profile.calls <= profile.budget_usd]
        dropped = len(pool) - len(within)
        if dropped:
            reasons.append(f"{dropped} مدل به‌دلیل عبور از بودجه باقی‌مانده ({profile.budget_usd:.4f}$) کنار گذاشته شد")
        pool = within
    if not pool:
        return [], reasons + ["هیچ مدل سالم و دارای قابلیت لازم در محدوده بودجه نیست"]
    if policy == "economy":
        floor = [c for c in pool if c.quality >= 0.5] or pool
        ordered = sorted(floor, key=lambda c: (c.expected_cost, c.latency_ms))
        reasons.append("سیاست اقتصادی: کمترین هزینه مورد انتظار یک اجرای موفق با حداقل کیفیت ۰٫۵")
    elif policy == "quality":
        ordered = sorted(pool, key=lambda c: (-c.quality, -c.reliability, c.expected_cost))
        reasons.append("سیاست کیفیت: بیشترین کیفیت و اطمینان در محدوده بودجه")
    else:
        max_cost = max(c.expected_cost for c in pool) or 1.0
        max_lat = max(c.latency_ms for c in pool) or 1.0
        wq = 1.4 if profile.complexity == "complex" else 1.0

        def utility(c: Candidate) -> float:
            return wq * c.p_success - 0.5 * (c.expected_cost / max_cost) - 0.25 * (c.latency_ms / max_lat)

        ordered = sorted(pool, key=utility, reverse=True)
        reasons.append("سیاست متعادل: بیشینه احتمال موفقیت با جریمه هزینه مورد انتظار و تأخیر"
                       + (" (وزن کیفیت بیشتر برای کار پیچیده)" if profile.complexity == "complex" else ""))
    return ordered, reasons


async def route(session: AsyncSession, models: list[BoundModel], policy: str,
                profile: TaskProfile) -> tuple[list[BoundModel], dict[str, Any]]:
    cands = await score(session, models, profile)
    ordered, reasons = rank(cands, policy, profile)
    decision = {"policy": policy, "profile": {"complexity": profile.complexity, "calls": profile.calls,
                                              "budget_usd": profile.budget_usd, "caps": list(profile.need_caps)},
                "reasons": reasons, "candidates": [c.summary() for c in ordered[:5]],
                "chosen": ordered[0].model.label if ordered else None}
    return [c.model for c in ordered], decision
