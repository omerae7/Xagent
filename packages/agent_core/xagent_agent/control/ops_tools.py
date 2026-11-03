"""ابزارهای مدیر برای اداره کامل سیستم با فرمان زبان طبیعی.

هر تغییر از مسیر ChangeService انجام می‌شود (سیاست مجوز + Audit + Rollback) و در صورت امکان
بلافاصله با آزمون واقعی تأیید می‌شود. تنظیمات امنیتی فقط با کارفرماست.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from sqlalchemy import func, or_, select

from xagent_core.models import AIModel, AIProvider, ConfigChange, Employee, InstalledTool
from xagent_core.settings_store import DEFAULTS, AISettings, all_settings, get_setting
from xagent_core.text import truncate
from xagent_tools.base import Tool, ToolContext, ToolResult, b, i, obj, s
from xagent_tools.errors import EscalateCategory

from . import capabilities, changes, selfheal
from .errors import CLASSES


def _actor(ctx: ToolContext) -> str:
    emp = ctx.extra.get("employee_name")
    return f"employee:{emp}" if emp else "manager"


def _guard_trusted(ctx: ToolContext) -> None:
    if not ctx.trusted_owner:
        raise PermissionError("این درخواست از منبع غیرکارفرما آمده است؛ تغییر تنظیمات سیستم مجاز نیست.")


async def resolve_model(session, ref: str) -> AIModel | None:
    ref = (ref or "").strip()
    try:
        m = await session.get(AIModel, uuid.UUID(ref))
        if m:
            return m
    except ValueError:
        pass
    q = ref.split("/")[-1].lower()
    rows = (await session.execute(select(AIModel).where(or_(
        func.lower(AIModel.model_name) == q, func.lower(AIModel.display_name) == ref.lower())))).scalars().all()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        enabled = [r for r in rows if r.enabled]
        return enabled[0] if len(enabled) == 1 else rows[0]
    rows = (await session.execute(select(AIModel).where(func.lower(AIModel.model_name).like(f"%{q}%")))).scalars().all()
    return rows[0] if len(rows) == 1 else None


async def resolve_provider(session, ref: str) -> AIProvider | None:
    try:
        p = await session.get(AIProvider, uuid.UUID(ref))
        if p:
            return p
    except ValueError:
        pass
    rows = (await session.execute(select(AIProvider).where(or_(
        func.lower(AIProvider.name).like(f"%{ref.lower()}%"), AIProvider.kind == ref.lower())))).scalars().all()
    return rows[0] if rows else None


def _model_line(m: AIModel, provider: str) -> str:
    caps = [k for k, v in (m.capabilities or {}).items() if v]
    meta = m.capability_meta or {}
    srcs = {k: (meta.get(k) or {}).get("source", "?") for k in caps}
    cap_txt = ", ".join(f"{k}({srcs[k]})" for k in caps) or "قابلیت تأییدشده‌ای ندارد"
    price = f"${float(m.price_in or 0):g}/${float(m.price_out or 0):g} per 1M [{m.pricing_source}]"
    return (f"- {m.display_name or m.model_name} | id={m.id} | api_id={m.model_name} | {provider} | "
            f"{'فعال' if m.enabled else 'غیرفعال'} | سلامت={m.health}"
            f"{' (' + (m.last_error_class or '') + ')' if m.health in ('failing', 'degraded') else ''} | {cap_txt} | {price}")


# ───────────── ابزارهای خواندنی ─────────────
async def system_overview(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    s_ = ctx.session
    section = a.get("section", "all")
    out: list[str] = []
    providers = {p.id: p for p in (await s_.execute(select(AIProvider))).scalars()}
    if section in ("all", "models"):
        ai = await get_setting(s_, "ai", AISettings)
        models = (await s_.execute(select(AIModel).order_by(AIModel.enabled.desc(), AIModel.model_name))).scalars().all()
        by_id = {str(m.id): m for m in models}
        def chain(ids): return " → ".join((by_id[i].display_name or by_id[i].model_name) if i in by_id else f"?{i[:6]}" for i in ids if i) or "خالی"
        out.append(f"## هوش مصنوعی\nحالت: {ai.mode}\nزنجیره مدیر: {chain(ai.smart.manager_model_ids)}\n"
                   f"زنجیره کارمند: {chain(ai.smart.worker_model_ids)}")
        if ai.mode == "pro":
            out.append("نقش‌ها: " + ", ".join(f"{r}={chain([v]) if v else 'پیش‌فرض'}" for r, v in ai.pro.items()))
        out.append("## ارائه‌دهندگان\n" + "\n".join(
            f"- {p.name} | id={p.id} | نوع={p.kind} | {'فعال' if p.enabled else 'غیرفعال'} | کلید={'دارد' if p.api_key_enc else 'ندارد'}"
            for p in providers.values()) or "هیچ")
        lim = int(a.get("limit", 40))
        out.append(f"## مدل‌ها ({len(models)})\n" + "\n".join(
            _model_line(m, providers[m.provider_id].name if m.provider_id in providers else "?") for m in models[:lim]))
    if section in ("all", "employees"):
        emps = (await s_.execute(select(Employee).order_by(Employee.name))).scalars().all()
        out.append("## کارمندان\n" + ("\n".join(
            f"- {e.name} ({e.title}) | {'فعال' if e.enabled else 'غیرفعال'} | ابزارها={','.join(e.tool_groups) or 'پیش‌فرض'} | "
            f"مدل‌ها={len(e.model_ids)} | آمار={json.dumps(e.stats or {}, ensure_ascii=False)}" for e in emps) or "هنوز کارمندی تعریف نشده"))
    if section in ("all", "tools"):
        from xagent_tools.registry import BUILTIN, GROUP_LABELS

        groups: dict[str, list[str]] = {}
        for t in BUILTIN:
            groups.setdefault(t.group, []).append(t.name)
        out.append("## ابزارهای داخلی (همیشه نصب و فعال)\n" + "\n".join(
            f"- {GROUP_LABELS.get(g, g)} [{g}]: {', '.join(n)}" for g, n in groups.items()))
        inst = (await s_.execute(select(InstalledTool))).scalars().all()
        out.append("## ابزارهای نصب‌شده\n" + ("\n".join(
            f"- {t.name} | {t.kind} | {t.status} | {'فعال' if t.enabled else 'غیرفعال'} | {len(t.tool_schemas or [])} ابزار"
            for t in inst) or "هیچ"))
        try:
            health = await asyncio.wait_for(ctx.sandbox.health(), timeout=4)
            out.append(f"## محیط ایزوله ({health.get('kind')}): {'سالم' if health.get('ok') else 'در دسترس نیست'}")
        except Exception as e:  # noqa: BLE001
            out.append(f"## محیط ایزوله: در دسترس نیست ({type(e).__name__})")
        tg = ctx.extra.get("telegram")
        out.append(f"## تلگرام: {'متصل' if tg is not None and getattr(tg, 'connected', False) else 'متصل نیست'}")
    if section in ("all", "settings"):
        st = await all_settings(s_)
        st.pop("telegram", None)
        out.append("## تنظیمات قابل تغییر توسط مدیر\n" + json.dumps(
            {k: v for k, v in st.items() if k != "ai"}, ensure_ascii=False)[:3000])
    return ToolResult(True, truncate("\n\n".join(out), 14000))


async def system_diagnose(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rep = await selfheal.diagnose(ctx.session, int(a.get("hours", 24)))
    lines = [f"- [{f['severity']}] ({f['class']}) {f['message']} → اقدام: {f['fix']}" for f in rep["findings"]]
    inc = [f"- رخداد {i['class']} روی {i['source']}:{i['target'][:12]} ×{i['count']} [{i['status']}]" for i in rep["incidents"]]
    txt = "## یافته‌ها\n" + ("\n".join(lines) or "مشکلی یافت نشد ✅")
    if inc:
        txt += "\n\n## رخدادهای باز\n" + "\n".join(inc)
    txt += "\n\nراهنمای کلاس‌ها: " + "؛ ".join(f"{k}={v.label}" for k, v in CLASSES.items() if k in {f['class'] for f in rep['findings']})
    return ToolResult(True, txt, {"count": len(rep["findings"])})


async def change_history(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    rows = (await ctx.session.execute(select(ConfigChange).order_by(ConfigChange.created_at.desc())
                                      .limit(int(a.get("limit", 15))))).scalars().all()
    return ToolResult(True, "\n".join(
        f"- {r.created_at:%m-%d %H:%M} | id={r.id} | {r.actor} | {r.status} | {r.summary[:140]}" for r in rows) or "تغییری ثبت نشده")


# ───────────── ابزارهای تغییر (با Audit و Rollback) ─────────────
def _fail(e: changes.ChangeError) -> ToolResult:
    prefix = {"forbidden": "⛔ ممنوع", "invalid": "❌ نامعتبر", "noop": "ℹ️ بدون تغییر"}.get(e.decision, "❌")
    return ToolResult(e.decision == "noop", f"{prefix}: {e}")


async def model_discover(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    ref = a.get("provider", "all")
    provs = (await ctx.session.execute(select(AIProvider).where(AIProvider.enabled.is_(True)))).scalars().all() \
        if ref == "all" else [p for p in [await resolve_provider(ctx.session, ref)] if p]
    if not provs:
        return ToolResult(False, "ارائه‌دهنده‌ای یافت نشد. ارائه‌دهنده و کلید API را کارفرما در تنظیمات ← هوش مصنوعی ثبت می‌کند.")
    lines = []
    for p in provs:
        try:
            r = await capabilities.sync_provider(ctx.session, p)
            lines.append(f"- {p.name}: {r['found']} مدل قابل دسترس؛ {len(r['created'])} مدل تازه ثبت شد (غیرفعال تا آزمون)"
                         + (f": {', '.join(r['created'][:25])}" if r["created"] else "")
                         + (f"؛ با این کلید دیده نمی‌شوند: {', '.join(r['not_visible_with_key'][:10])}" if r["not_visible_with_key"] else ""))
            if r["created"]:
                await changes._record(ctx.session, actor=_actor(ctx), target_type="provider", target_id=str(p.id),
                                      action="discover", summary=f"کشف {len(r['created'])} مدل تازه از {p.name}",
                                      reason=a.get("reason", "کشف مدل"), before=None, after={"created": r["created"][:100]},
                                      run_id=ctx.run_id, status="verified")
        except capabilities.DiscoveryError as e:
            lines.append(f"- {p.name}: ❌ {e}")
    return ToolResult(True, "\n".join(lines))


async def model_probe(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    m = await resolve_model(ctx.session, a["model"])
    if m is None:
        return ToolResult(False, f"مدل «{a['model']}» یافت نشد. از system_overview شناسه دقیق را بردار.")
    before = changes.snap(m, changes.MODEL_FIELDS)
    rep = await capabilities.probe_model(ctx.session, str(m.id), run_id=ctx.run_id)
    if a.get("enable_if_ok", True) and rep.get("ok") and not m.enabled:
        m.enabled = True
    after = changes.snap(m, changes.MODEL_FIELDS)
    if after != before:
        await changes._record(ctx.session, actor=_actor(ctx), target_type="model", target_id=str(m.id), action="probe",
                              summary=f"آزمون عملی «{m.display_name or m.model_name}»: " + json.dumps(
                                  {k: v.get('ok') for k, v in rep.get('results', {}).items()}, ensure_ascii=False),
                              reason=a.get("reason", "آزمون قابلیت"), before=before, after=after, run_id=ctx.run_id,
                              status="verified" if rep.get("ok") else "verification_failed")
    return ToolResult(bool(rep.get("ok")), json.dumps(rep, ensure_ascii=False)[:3000], verified=bool(rep.get("ok")))


async def model_update(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    m = await resolve_model(ctx.session, a["model"])
    if m is None:
        return ToolResult(False, f"مدل «{a['model']}» یافت نشد.")
    ch_ = {k: a[k] for k in ("enabled", "display_name", "context_window", "price_in", "price_out", "price_cache_read",
                             "price_cache_write", "pricing_source") if a.get(k) is not None}
    if a.get("reset_health"):
        ch_["health"] = "ok"
    try:
        ch = await changes.change_model(ctx.session, m.id, ch_, actor=_actor(ctx), reason=a.get("reason", ""), run_id=ctx.run_id)
    except changes.ChangeError as e:
        return _fail(e)
    return ToolResult(True, f"✅ {ch.summary} (change_id={ch.id}, قابل بازگردانی)", verified=True)


async def routing_set(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    ai = (await get_setting(ctx.session, "ai", AISettings)).model_dump(mode="json")
    patch: dict[str, Any] = {}
    if a.get("mode"):
        patch["mode"] = a["mode"]
    for key, field in (("manager", "manager_model_ids"), ("worker", "worker_model_ids")):
        refs = a.get(key)
        if refs is None:
            continue
        ids = []
        for r in refs[:3]:
            m = await resolve_model(ctx.session, r)
            if m is None:
                return ToolResult(False, f"مدل «{r}» یافت نشد.")
            if not m.enabled:
                return ToolResult(False, f"مدل «{m.display_name or m.model_name}» غیرفعال است؛ ابتدا با model_probe آزمون و فعال کن.")
            if not (m.capabilities or {}).get("tools"):
                return ToolResult(False, f"مدل «{m.display_name or m.model_name}» قابلیت ابزار تأییدشده ندارد؛ "
                                         "بدون آن کارمند/مدیر نمی‌تواند هیچ کاری انجام دهد. ابتدا model_probe را اجرا کن.")
            ids.append(str(m.id))
        patch.setdefault("smart", dict(ai["smart"]))
        patch["smart"][field] = ids
        patch["smart"][field.replace("model_ids", "enabled")] = [True] * len(ids)
    if a.get("pro"):
        pro = dict(ai["pro"])
        for role, r in a["pro"].items():
            m = await resolve_model(ctx.session, r) if r else None
            if r and m is None:
                return ToolResult(False, f"مدل «{r}» برای نقش {role} یافت نشد.")
            pro[role] = str(m.id) if m else None
        patch["pro"] = pro
    if not patch:
        return ToolResult(False, "هیچ تغییری مشخص نشده است.")
    try:
        ch = await changes.change_setting(ctx.session, "ai", patch, actor=_actor(ctx), reason=a.get("reason", ""),
                                          run_id=ctx.run_id)
    except changes.ChangeError as e:
        return _fail(e)
    return ToolResult(True, f"✅ {ch.summary} (change_id={ch.id})", verified=True)


async def settings_update(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    values = a.get("values")
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            return ToolResult(False, "values باید یک شیء JSON باشد.")
    approved = "security_limits" in ctx.extra.get("approved", set())
    try:
        ch = await changes.change_setting(ctx.session, a["section"], values or {}, actor=_actor(ctx),
                                          reason=a.get("reason", ""), run_id=ctx.run_id, approved=approved)
    except changes.ChangeError as e:
        if e.decision == "approval":
            raise EscalateCategory("security_limits", str(e), {"section": a["section"], "values": values}) from e
        return _fail(e)
    return ToolResult(True, f"✅ {ch.summary} (change_id={ch.id})", verified=True)


async def tool_toggle(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    try:
        ch = await changes.change_tool_enabled(ctx.session, a["tool"], bool(a["enabled"]), actor=_actor(ctx),
                                               reason=a.get("reason", ""), run_id=ctx.run_id)
    except changes.ChangeError as e:
        return _fail(e)
    return ToolResult(True, f"✅ {ch.summary} (change_id={ch.id})", verified=True)


async def provider_toggle(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    p = await resolve_provider(ctx.session, a["provider"])
    if p is None:
        return ToolResult(False, "ارائه‌دهنده یافت نشد.")
    try:
        ch = await changes.change_provider_enabled(ctx.session, str(p.id), bool(a["enabled"]), actor=_actor(ctx),
                                                   reason=a.get("reason", ""), run_id=ctx.run_id)
    except changes.ChangeError as e:
        return _fail(e)
    return ToolResult(True, f"✅ {ch.summary} (change_id={ch.id})", verified=True)


async def employee_upsert(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    data = {k: a.get(k) for k in ("name", "title", "description", "instructions", "tool_groups", "memory_scope",
                                  "enabled", "max_steps", "max_cost_usd")}
    if a.get("models"):
        ids = []
        for r in a["models"][:3]:
            m = await resolve_model(ctx.session, r)
            if m is None:
                return ToolResult(False, f"مدل «{r}» یافت نشد.")
            if not (m.capabilities or {}).get("tools"):
                return ToolResult(False, f"مدل «{m.display_name or m.model_name}» قابلیت ابزار تأییدشده ندارد.")
            ids.append(str(m.id))
        data["model_ids"] = ids
    try:
        e, ch = await changes.upsert_employee(ctx.session, data, actor=_actor(ctx), reason=a.get("reason", ""),
                                              run_id=ctx.run_id)
    except changes.ChangeError as e:
        return _fail(e)
    return ToolResult(True, f"✅ {ch.summary} (change_id={ch.id})", {"employee_id": str(e.id)}, verified=True)


async def employee_delete(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    try:
        ch = await changes.delete_employee(ctx.session, a["name"], actor=_actor(ctx), reason=a.get("reason", ""),
                                           run_id=ctx.run_id)
    except changes.ChangeError as e:
        return _fail(e)
    return ToolResult(True, f"✅ {ch.summary} (change_id={ch.id})", verified=True)


async def change_rollback(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    try:
        res = await changes.rollback(ctx.session, a["change_id"], actor=_actor(ctx), reason=a.get("reason", "بازگردانی توسط مدیر"))
    except changes.ChangeError as e:
        return _fail(e)
    return ToolResult(True, f"↩️ {res.message}", {"reverted": res.reverted}, verified=True)


async def self_heal(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    _guard_trusted(ctx)
    rep = await selfheal.heal(ctx.session, actor=_actor(ctx), dry_run=bool(a.get("dry_run")))
    return ToolResult(True, json.dumps(rep, ensure_ascii=False)[:3000])


async def delegate_task(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    from ..employees import delegate

    return await delegate(ctx, a["employee"], a["task"], a.get("expected_output", ""))


async def tools_load(ctx: ToolContext, a: dict[str, Any]) -> ToolResult:
    # در pipeline پردازش می‌شود؛ این تابع فقط پاسخ پیش‌فرض است
    return ToolResult(True, "گروه‌های ابزار بارگذاری شدند: " + ", ".join(a.get("groups", [])))


CFG = "system_config"
ADMIN_TOOLS = [
    Tool("system_overview", "نمای کامل وضعیت خودت: مدل‌ها (شناسه، سلامت، قابلیت تأییدشده و منبع، قیمت)، زنجیره مدیر/کارمند، "
         "ارائه‌دهندگان، کارمندان، ابزارها، محیط ایزوله، تلگرام و تنظیمات. قبل از هر تغییر سیستم از این استفاده کن.",
         obj({"section": s("all|models|employees|tools|settings"), "limit": i("حداکثر مدل")}), system_overview, "admin",
         label="نمای سیستم"),
    Tool("system_diagnose", "عیب‌یابی قطعی همه بخش‌ها: خطاهای مدل/API/ابزار/تنظیمات با طبقه‌بندی و اقدام پیشنهادی.",
         obj({"hours": i("بازه بررسی به ساعت")}), system_diagnose, "admin", label="عیب‌یابی سیستم"),
    Tool("self_heal", "اجرای اصلاحات خودکار کم‌ریسک (جابه‌جایی مدل‌های خراب، غیرفعال‌سازی ابزار خطادار) با ثبت و قابلیت بازگردانی.",
         obj({"dry_run": b("فقط گزارش بدون اعمال")}), self_heal, "admin", category=CFG, action="heal", label="خوددرمانی"),
    Tool("model_discover", "کشف مدل‌های واقعی قابل دسترس با کلید API ارائه‌دهنده (بدون ساختن شناسه یا قیمت).",
         obj({"provider": s("نام/شناسه ارائه‌دهنده یا all"), "reason": s("دلیل")}), model_discover, "admin",
         category=CFG, action="discover", label="کشف مدل"),
    Tool("model_probe", "آزمون عملی مدل (گفتگو، فراخوانی ابزار، بینایی) و ثبت قابلیت‌های تأییدشده؛ در صورت موفقیت فعال می‌شود.",
         obj({"model": s("شناسه/نام مدل"), "enable_if_ok": b("فعال‌سازی پس از موفقیت"), "reason": s("دلیل")}, ["model"]),
         model_probe, "admin", category=CFG, action="probe", label="آزمون مدل"),
    Tool("model_update", "تغییر مدل: فعال/غیرفعال، نام نمایشی، پنجره کانتکست، قیمت (با pricing_source معتبر) یا بازنشانی سلامت.",
         obj({"model": s("شناسه/نام"), "enabled": b("فعال"), "display_name": s("نام"), "context_window": i("کانتکست"),
              "price_in": {"type": "number"}, "price_out": {"type": "number"}, "price_cache_read": {"type": "number"},
              "pricing_source": s("official_docs|provider_api|owner|unverified"), "reset_health": b("بازنشانی سلامت"),
              "reason": s("دلیل")}, ["model", "reason"]), model_update, "admin", category=CFG, action="update",
         label="ویرایش مدل"),
    Tool("routing_set", "تعیین حالت (smart/pro) و زنجیره مدل‌های مدیر و کارمند (حداکثر ۳، به ترتیب Fallback) یا مدل هر نقش حرفه‌ای.",
         obj({"mode": s("smart|pro"), "manager": {"type": "array", "items": {"type": "string"}},
              "worker": {"type": "array", "items": {"type": "string"}},
              "pro": {"type": "object", "description": "نقش→مدل: management, goal, planning, execution, vision, stt, tts, memory, evaluation, dream"},
              "reason": s("دلیل")}, ["reason"]), routing_set, "admin", category=CFG, action="routing", label="مسیریابی مدل‌ها"),
    Tool("settings_update", "تغییر تنظیمات: general (نام، شخصیت، منطقه زمانی، تقویم)، ai (دما، embedding، صدا)، stoploss، "
         "routine_safety، dream. سخت‌ترکردن محدودیت آزاد است؛ شل‌کردن آن اجازه کارفرما می‌خواهد. بخش‌های امنیتی ممنوع‌اند.",
         obj({"section": s("general|ai|stoploss|routine_safety|dream"),
              "values": {"type": "object", "description": "فقط فیلدهای تغییرکرده"}, "reason": s("دلیل")},
             ["section", "values", "reason"]), settings_update, "admin", category=CFG, action="settings",
         label="تغییر تنظیمات"),
    Tool("tool_toggle", "فعال/غیرفعال کردن ابزار نصب‌شده (MCP/GitHub).",
         obj({"tool": s("نام ابزار"), "enabled": b("فعال"), "reason": s("دلیل")}, ["tool", "enabled"]), tool_toggle,
         "admin", category=CFG, action="tool", label="روشن/خاموش ابزار"),
    Tool("provider_toggle", "فعال/غیرفعال کردن ارائه‌دهنده (کلید و نشانی فقط توسط کارفرما).",
         obj({"provider": s("نام/شناسه"), "enabled": b("فعال"), "reason": s("دلیل")}, ["provider", "enabled"]),
         provider_toggle, "admin", category=CFG, action="provider", label="روشن/خاموش ارائه‌دهنده"),
    Tool("employee_upsert", "ساخت یا ویرایش کارمند هوش مصنوعی با نقش، دستورالعمل، مدل‌ها (دارای ابزار) و گروه‌های ابزار.",
         obj({"name": s("نام یکتا (انگلیسی/فارسی کوتاه)"), "title": s("عنوان شغلی"), "description": s("شرح"),
              "instructions": s("دستورالعمل کاری"), "models": {"type": "array", "items": {"type": "string"}},
              "tool_groups": {"type": "array", "items": {"type": "string"},
                              "description": "web, browser, files, tasks, routines, crm, telegram, memory, episodes, system, desktop"},
              "memory_scope": s("shared|private"), "enabled": b("فعال"), "max_steps": i("سقف گام"),
              "max_cost_usd": {"type": "number"}, "reason": s("دلیل")}, ["name"]), employee_upsert, "admin",
         category=CFG, action="employee", label="مدیریت کارمند"),
    Tool("employee_delete", "حذف کارمند.", obj({"name": s("نام"), "reason": s("دلیل")}, ["name"]), employee_delete,
         "admin", category=CFG, action="employee", label="حذف کارمند"),
    Tool("delegate_task", "سپردن یک زیرکار به کارمند؛ کارمند با مدل و ابزارهای خودش انجام می‌دهد و نتیجه، هزینه و مراحل را گزارش می‌کند تا ارزیابی کنی.",
         obj({"employee": s("نام کارمند"), "task": s("شرح کامل کار"), "expected_output": s("خروجی مورد انتظار")},
             ["employee", "task"]), delegate_task, "admin", label="تفویض به کارمند"),
    Tool("change_history", "فهرست آخرین تغییرات تنظیمات (توسط تو، کارمندان، خوددرمانی یا کارفرما).",
         obj({"limit": i("تعداد")}), change_history, "admin", label="سابقه تغییرات"),
    Tool("change_rollback", "بازگردانی یک تغییر تنظیمات (و تغییرات جدیدتر همان هدف).",
         obj({"change_id": s("شناسه تغییر"), "reason": s("دلیل")}, ["change_id"]), change_rollback, "admin",
         category=CFG, action="rollback", label="بازگردانی تغییر"),
]

LOAD_TOOL = Tool(
    "tools_load",
    "بارگذاری گروه‌های ابزار بیشتر برای ادامه همین کار: admin (اداره سیستم)، web، browser، desktop، files، tasks، routines، "
    "crm، telegram، memory، episodes، system، installed.",
    obj({"groups": {"type": "array", "items": {"type": "string"}}}, ["groups"]), tools_load, "core", label="بارگذاری ابزار",
)

ALL_SECTIONS = list(DEFAULTS)
