"""تشخیص و خوددرمانی: تشخیص خطا ← طبقه‌بندی ← پیشنهاد ← بررسی مجوز ← اصلاح مجاز ← آزمون ← گزارش.

اصلاح خودکار فقط برای اقدامات کم‌ریسک و برگشت‌پذیر (با ConfigChange):
- مدل خراب در زنجیره (کلید/سهمیه/نبود مدل): جابه‌جایی به انتهای زنجیره و جایگزینی با مدل سالم سازگار
- مدل فاقد قابلیت ابزار در نقش اجرا: جایگزینی با مدل سالم دارای ابزار
- ابزار نصب‌شده خطادار: غیرفعال‌سازی
محافظت از حلقه: حداکثر ۶ اصلاح در ساعت و هر هدف حداکثر یک‌بار در ۳۰ دقیقه.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import notify
from xagent_core.db import utcnow
from xagent_core.models import (
    AIModel,
    AIProvider,
    ConfigChange,
    Incident,
    InstalledTool,
    Run,
    RunStep,
    UsageEvent,
)
from xagent_core.settings_store import AISettings, get_setting

from . import changes
from .errors import CLASSES, classify

MAX_FIXES_PER_HOUR = 6


async def open_incident(session: AsyncSession, source: str, target: str, error_class: str, message: str) -> Incident:
    inc = (await session.execute(select(Incident).where(
        Incident.source == source, Incident.target == target, Incident.error_class == error_class))).scalar_one_or_none()
    if inc is None:
        inc = Incident(source=source, target=target, error_class=error_class, message=message[:2000])
        session.add(inc)
    else:
        inc.count += 1
        inc.last_seen_at = utcnow()
        inc.message = message[:2000]
        if inc.status in ("fixed", "ignored"):
            inc.status = "open"
            inc.resolved_at = None
    await session.flush()
    return inc


async def _healthy_with(session: AsyncSession, cap: str | None, exclude: set[str]) -> list[AIModel]:
    rows = (await session.execute(
        select(AIModel).join(AIProvider, AIProvider.id == AIModel.provider_id)
        .where(AIModel.enabled.is_(True), AIProvider.enabled.is_(True)))).scalars().all()
    now = utcnow()
    out = []
    for m in rows:
        if str(m.id) in exclude or m.health == "failing" or (m.cooldown_until and m.cooldown_until > now):
            continue
        if cap and not (m.capabilities or {}).get(cap):
            continue
        out.append(m)
    out.sort(key=lambda m: (m.health != "ok", m.consecutive_failures))
    return out


async def diagnose(session: AsyncSession, hours: int = 24) -> dict[str, Any]:
    """گزارش تشخیصی کامل و قطعی (بدون LLM) از وضعیت سیستم."""
    since = utcnow() - timedelta(hours=hours)
    ai = await get_setting(session, "ai", AISettings)
    findings: list[dict[str, Any]] = []
    models = {str(m.id): m for m in (await session.execute(select(AIModel))).scalars()}

    def label(mid: str) -> str:
        m = models.get(mid)
        return (m.display_name or m.model_name) if m else f"(مدل حذف‌شده {mid[:8]})"

    chains = {"manager": ai.smart.manager_model_ids, "worker": ai.smart.worker_model_ids}
    if ai.mode == "smart":
        for tier, ids in chains.items():
            if not [i for i in ids if i]:
                findings.append({"severity": "high", "class": "config", "target": f"routing:{tier}",
                                 "message": f"هیچ مدلی برای سطح «{'مدیر' if tier == 'manager' else 'کارمند'}» تعیین نشده است.",
                                 "fix": "assign_model"})
            for mid in ids:
                m = models.get(mid)
                if m is None:
                    findings.append({"severity": "high", "class": "config", "target": f"routing:{tier}",
                                     "message": f"مدل {mid[:8]} در زنجیره {tier} وجود ندارد.", "fix": "assign_model"})
                    continue
                if not m.enabled:
                    findings.append({"severity": "medium", "class": "config", "target": str(m.id),
                                     "message": f"مدل «{label(mid)}» در زنجیره {tier} است اما غیرفعال است.", "fix": "assign_model"})
                if m.health == "failing":
                    findings.append({"severity": "high", "class": m.last_error_class or "unknown", "target": str(m.id),
                                     "message": f"مدل «{label(mid)}» خراب است: {CLASSES.get(m.last_error_class or 'unknown', CLASSES['unknown']).label}"
                                                f" — {(m.last_error or '')[:160]}", "fix": "replace_failing_model"})
                if not (m.capabilities or {}).get("tools"):
                    src = ((m.capability_meta or {}).get("tools") or {}).get("source")
                    findings.append({"severity": "high" if tier == "worker" else "medium", "class": "unsupported_tools",
                                     "target": str(m.id),
                                     "message": f"مدل «{label(mid)}» قابلیت ابزار تأییدشده ندارد"
                                                f"{' (هنوز آزمون نشده)' if not src else ''}؛ بدون آن هیچ کاری اجرا نمی‌شود.",
                                     "fix": "probe_or_replace"})
    # خطاهای اخیر مدل‌ها
    errs = (await session.execute(
        select(UsageEvent.model_id, UsageEvent.model_name, func.count(), func.max(UsageEvent.error))
        .where(UsageEvent.ts >= since, UsageEvent.success.is_(False)).group_by(UsageEvent.model_id, UsageEvent.model_name)
    )).all()
    for mid, name, n, err in errs:
        ec = classify(err)
        findings.append({"severity": "medium", "class": ec.code, "target": str(mid), "message":
                         f"{n} خطا در «{name}» ({ec.label}): {(err or '')[:160]}", "fix": ec.action})
    # خطاهای ابزار
    tool_errs = (await session.execute(
        select(RunStep.tool_name, func.count(), func.max(RunStep.title))
        .where(RunStep.kind == "tool", RunStep.status == "error", RunStep.started_at >= since)
        .group_by(RunStep.tool_name))).all()
    for tname, n, _ in tool_errs:
        findings.append({"severity": "low" if n < 3 else "medium", "class": "tool", "target": tname,
                         "message": f"ابزار «{tname}» {n} بار در {hours} ساعت اخیر خطا داد.", "fix": "inspect_tool"})
    broken_tools = (await session.execute(select(InstalledTool).where(InstalledTool.status == "error"))).scalars().all()
    for t in broken_tools:
        findings.append({"severity": "medium", "class": "tool_disabled", "target": t.name,
                         "message": f"ابزار نصب‌شده «{t.name}» در وضعیت خطاست: {(t.last_error or '')[:160]}",
                         "fix": "update_or_rollback_tool"})
    failed_runs = (await session.execute(select(func.count()).select_from(Run).where(
        Run.created_at >= since, Run.status == "failed"))).scalar_one()
    if failed_runs:
        findings.append({"severity": "low", "class": "run", "target": "runs",
                         "message": f"{failed_runs} اجرای ناموفق در {hours} ساعت اخیر.", "fix": "review"})
    open_inc = (await session.execute(select(Incident).where(Incident.status.in_(["open", "escalated"]))
                                      .order_by(Incident.last_seen_at.desc()).limit(20))).scalars().all()
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    return {"findings": findings, "incidents": [
        {"id": str(i.id), "source": i.source, "target": i.target, "class": i.error_class, "count": i.count,
         "status": i.status, "message": i.message[:200]} for i in open_inc]}


async def _recent_fix_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(ConfigChange).where(
        ConfigChange.actor == "self_heal", ConfigChange.created_at >= utcnow() - timedelta(hours=1)))).scalar_one()


async def _recently_fixed(session: AsyncSession, target_id: str) -> bool:
    return bool((await session.execute(select(func.count()).select_from(ConfigChange).where(
        ConfigChange.actor == "self_heal", ConfigChange.target_id == target_id,
        ConfigChange.created_at >= utcnow() - timedelta(minutes=30)))).scalar_one())


async def heal(session: AsyncSession, actor: str = "self_heal", dry_run: bool = False) -> dict[str, Any]:
    """اجرای اصلاحات کم‌ریسک و برگشت‌پذیر؛ هر اصلاح یک ConfigChange قابل Rollback است."""
    report: dict[str, Any] = {"applied": [], "skipped": [], "escalated": []}
    if await _recent_fix_count(session) >= MAX_FIXES_PER_HOUR:
        report["skipped"].append("سقف اصلاح خودکار در ساعت پر شده است (جلوگیری از حلقه).")
        return report
    ai = await get_setting(session, "ai", AISettings)
    if ai.mode != "smart":
        report["skipped"].append("در حالت حرفه‌ای، تخصیص مدل‌ها دستی است؛ فقط گزارش داده می‌شود.")
        return report
    new_ai = ai.model_dump(mode="json")
    changed_reasons: list[str] = []
    for tier, cap in (("manager_model_ids", "tools"), ("worker_model_ids", "tools")):
        ids = [i for i in new_ai["smart"].get(tier, []) if i]
        bad = []
        for mid in ids:
            m = await session.get(AIModel, changes._uuid(mid))
            if m is None or not m.enabled or m.health == "failing" or not (m.capabilities or {}).get(cap):
                bad.append(mid)
        if not bad:
            continue
        good = [i for i in ids if i not in bad]
        if not good:
            candidates = await _healthy_with(session, cap, set(bad))
            if not candidates:
                report["escalated"].append(f"برای «{tier}» هیچ مدل سالم دارای ابزار وجود ندارد؛ یک مدل معتبر اضافه یا آزمون کنید.")
                continue
            good = [str(candidates[0].id)]
        # مدل‌های خراب در انتهای زنجیره (برای بازگشت خودکار پس از رفع)
        new_ids = (good + [b for b in bad if b not in good])[:3]
        if new_ids != new_ai["smart"][tier]:
            new_ai["smart"][tier] = new_ids
            flag_key = "manager_enabled" if tier.startswith("manager") else "worker_enabled"
            new_ai["smart"][flag_key] = [True] * len(new_ids)
            changed_reasons.append(f"{tier}: مدل‌های خراب/فاقد ابزار به انتهای زنجیره رفتند")
    if changed_reasons:
        if dry_run:
            report["applied"].append({"dry_run": True, "reasons": changed_reasons})
        elif not await _recently_fixed(session, "ai"):
            try:
                ch = await changes.change_setting(session, "ai", {"smart": new_ai["smart"]}, actor=actor,
                                                  reason="خوددرمانی: " + "؛ ".join(changed_reasons))
                ch.verification = {"checked": "routing", "note": "زنجیره جدید فقط شامل مدل‌های سالم دارای ابزار در ابتدای زنجیره است"}
                ch.status = "verified"
                report["applied"].append({"change_id": str(ch.id), "summary": ch.summary})
            except changes.ChangeError as e:
                report["skipped"].append(str(e))
    for t in (await session.execute(select(InstalledTool).where(
            InstalledTool.status == "error", InstalledTool.enabled.is_(True)))).scalars().all():
        if dry_run or await _recently_fixed(session, t.name):
            continue
        before = {"enabled": True, "status": t.status}
        t.enabled = False
        ch = await changes._record(session, actor=actor, target_type="tool", target_id=t.name, action="disable",
                                   summary=f"ابزار خطادار «{t.name}» غیرفعال شد", reason="خوددرمانی: ابزار در وضعیت خطا",
                                   before=before, after={"enabled": False, "status": t.status})
        report["applied"].append({"change_id": str(ch.id), "summary": ch.summary})
    # بستن رخدادهای مدل‌هایی که دوباره سالم شده‌اند
    for inc in (await session.execute(select(Incident).where(Incident.status == "open", Incident.source == "model"))).scalars():
        m = await session.get(AIModel, changes._uuid(inc.target)) if inc.target else None
        if m is not None and m.health == "ok":
            inc.status, inc.resolved_at = "fixed", utcnow()
        elif m is not None and report["applied"]:
            inc.status = "fixing"
            inc.actions = [*(inc.actions or []), {"at": utcnow().isoformat(), "applied": report["applied"]}]
        elif m is not None and CLASSES.get(inc.error_class, CLASSES["unknown"]).action == "owner_action":
            if inc.status != "escalated":
                inc.status = "escalated"
                await notify(session, "نیاز به اقدام کارفرما", f"{inc.message[:200]}\n{CLASSES[inc.error_class].label}",
                             level="warning", link="/settings?tab=ai")
    await session.flush()
    if report["applied"]:
        await bus.publish("selfheal.applied", {"count": len(report["applied"])})
    return report
