"""تست‌های پذیرش عمومی موتور اجرای مأموریت (بخش ۲۱ سند ممیزی).

همه با سرویس‌های ساختگی (مدل، مرورگر، تلگرام، API) و ساعت مجازی؛ منطق داخلی را اثبات می‌کنند، نه اتصال واقعی.
A مرورگر عمومی · B دو کارمند هم‌زمان · C تسک موعددار · E خرابی API و روش جایگزین · F عملیات حساس مرورگر ·
G حجم بالای رویداد · H شکست ثبت پس از ارسال · I مأموریت چندکارمند · J Crash میانه مأموریت و بازیابی
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from autonomy_helpers import FakeBrowser, FakePage, FakeTelegram, body_text, called, tool_names
from sqlalchemy import func, select

from xagent_core.db import session_scope, utcnow
from xagent_core.models import (
    Approval,
    EventInbox,
    InboxItem,
    Mission,
    MissionAction,
    Routine,
    Run,
    RunStep,
    Task,
)
from xagent_core.timeutil import set_fake_now


def _script(mark: str, steps):
    """مدل شبیه‌ساز گام‌به‌گام: steps(body, done_calls) → (ابزار، آرگومان) | متن | None."""
    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        return steps(body, called(body))
    return handler


async def _chat(app_client, runner, text: str) -> Run:
    r = await app_client.post("/api/chat/send", json={"text": text})
    assert r.status_code == 200, r.text
    rid = uuid.UUID(r.json()["run"]["id"])
    await runner.execute(rid)
    async with session_scope() as s:
        return await s.get(Run, rid)


async def _steps(run_id) -> list[RunStep]:
    async with session_scope() as s:
        return list((await s.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.idx))).scalars())


# ── A: مأموریت عمومی مرورگر — DOM بدون بینایی غیرضروری ──
async def test_A_general_browser_extraction(app_client, configured_model, runner, mock_llm, monkeypatch):
    from xagent_tools.builtin import computer

    page = FakePage(last_page=3)

    async def fake_page(*_):
        return page
    monkeypatch.setattr(computer, "_page", fake_page)

    def steps(body, done):
        names = tool_names(body)
        if "browser_open" not in names:
            return ("tools_load", {"groups": ["browser"]})
        if not any(n == "browser_open" for n, _ in done):
            return ("browser_open", {"url": "https://catalog.example/list"})
        if page.page_no < page.last_page:
            return ("browser_click", {"ref": 1})
        if not any(n == "browser_read" for n, _ in done):
            return ("browser_read", {})
        return "قیمت نهایی محصول هدف ۴۲ دلار است (از صفحه ۳ استخراج شد)."

    h = _script("سناریو-A", steps)
    mock_llm.handlers.insert(0, h)
    try:
        run = await _chat(app_client, runner, "این سایت آزمایشی را باز کن، قیمت محصول هدف را استخراج کن و گزارش بده (سناریو-A)")
    finally:
        mock_llm.handlers.remove(h)
    assert run.status == "succeeded", run.error
    tools = [s.tool_name for s in await _steps(run.id) if s.kind == "tool"]
    assert "browser_screenshot" not in tools and "desktop_screenshot" not in tools, "بینایی بی‌دلیل استفاده شد"
    assert "۴۲" in (run.result or "") and float(run.cost_usd) < 0.5


# ── B: دو کارمند هم‌زمان در دو تب مستقل ──
async def test_B_two_concurrent_browser_missions_are_isolated(app_client, configured_model, mock_llm, monkeypatch):
    from xagent_agent.pipeline import AgentRunner
    from xagent_tools.builtin import computer

    fb = FakeBrowser()
    monkeypatch.setattr(computer, "_browser", fb)

    async def noop():
        return None
    monkeypatch.setattr(computer, "_guard", noop)

    def make(site: str, mark: str):
        def steps(body, done):
            if "browser_open" not in tool_names(body):
                return ("tools_load", {"groups": ["browser"]})
            if not any(n == "browser_open" for n, _ in done):
                return ("browser_open", {"url": f"https://{site}.example/"})
            if not any(n == "browser_click" for n, _ in done):
                return ("browser_click", {"ref": 1})  # «View» — اثر عادی
            return f"گزارش {site}: انجام شد"
        return _script(mark, steps)

    hs = [make("site-one", "سناریو-B1"), make("site-two", "سناریو-B2")]
    mock_llm.handlers[:0] = hs
    try:
        ids = []
        for mark in ("سناریو-B1", "سناریو-B2"):
            r = await app_client.post("/api/chat/send", json={"text": f"مرورگر را باز کن و صفحه را ببین ({mark})"})
            ids.append(uuid.UUID(r.json()["run"]["id"]))
        await asyncio.gather(AgentRunner("b1").execute(ids[0]), AgentRunner("b2").execute(ids[1]))
    finally:
        for h in hs:
            mock_llm.handlers.remove(h)
    async with session_scope() as s:
        runs = [await s.get(Run, i) for i in ids]
    assert all(r.status == "succeeded" for r in runs), [(r.status, r.error) for r in runs]
    assert "site-one" in runs[0].result and "site-two" in runs[1].result
    for pg in fb.contexts[0].pages + []:
        hosts = {c.split("|")[0].split("/")[2] for c in pg.clicks}
        assert len(hosts) <= 1, f"کلیک‌های دو مأموریت در یک تب مخلوط شد: {pg.clicks}"
    assert fb.contexts[0].pages == [], "تب‌ها پس از پایان اجرا آزاد نشدند"


# ── C: تسک موعددار «فردا ساعت ۹:۳۰» با ساعت مجازی ──
async def test_C_scheduled_report_task_runs_at_due_time(app_client, configured_model, runner, mock_llm):
    from xagent_worker.scheduler import after_task_run, tick_tasks

    tz = "Asia/Tehran"
    from xagent_core.timeutil import get_zone

    local_now = datetime.now(UTC).astimezone(get_zone(tz))
    due_local = (local_now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    due = due_local.astimezone(UTC)

    def chat_steps(body, done):
        if not any(n == "task_create" for n, _ in done):
            return ("task_create", {"title": "گزارش وضعیت تسک‌ها", "description": "فهرست تسک‌های باز و سررسیدها را خلاصه کن"})
        return "ثبت شد؛ فردا ساعت ۹:۳۰ انجام می‌شود."

    def task_steps(body):
        if "گزارش وضعیت تسک‌ها" not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        done = called(body)
        if "task_list" in tool_names(body) and not any(n == "task_list" for n, _ in done):
            return ("task_list", {})
        return "گزارش: ۲ تسک باز، یکی با سررسید امروز."

    hc = _script("سناریو-C", chat_steps)
    mock_llm.handlers[:0] = [hc, task_steps]
    try:
        run = await _chat(app_client, runner, "فردا ساعت ۹:۳۰ گزارش وضعیت تسک‌ها را تهیه کن و نتیجه را برایم بگذار (سناریو-C)")
        assert run.status == "succeeded", run.error
        async with session_scope() as s:
            t = (await s.execute(select(Task).where(Task.title == "گزارش وضعیت تسک‌ها").order_by(Task.created_at.desc()))).scalars().first()
            tid = t.id
            assert t.kind == "agent" and t.status == "todo" and abs((t.start_at - due).total_seconds()) < 1
        set_fake_now(due - timedelta(minutes=1))
        assert await tick_tasks() == 0, "پیش از موعد اجرا شد"
        set_fake_now(due + timedelta(seconds=5))
        assert await tick_tasks() == 1
        assert await tick_tasks() == 0, "همان موعد دوبار اجرا شد"
        async with session_scope() as s:
            trun = (await s.execute(select(Run).where(Run.task_id == tid))).scalar_one()
        await runner.execute(trun.id)
        await after_task_run(trun.id)
    finally:
        set_fake_now(None)
        for h in (hc, task_steps):
            mock_llm.handlers.remove(h)
    async with session_scope() as s:
        t = await s.get(Task, tid)
        assert t.status == "done" and t.last_run_id == trun.id and "۲ تسک باز" in (t.result_summary or "")
        assert (await s.execute(select(InboxItem).where(InboxItem.run_id == trun.id, InboxItem.kind == "report"))).first()


# ── E: خرابی API → روش جایگزین مجاز و تأیید نتیجه ──
async def test_E_broken_api_falls_back_to_browser(app_client, configured_model, runner, mock_llm, monkeypatch):
    from xagent_tools import net
    from xagent_tools.builtin import computer

    page = FakePage(last_page=1)

    async def fake_page(*_):
        return page
    monkeypatch.setattr(computer, "_page", fake_page)

    async def broken(method, url, **kw):
        return net.SafeResponse(503, url, __import__("httpx").Headers({"content-type": "text/plain"}), b"Service Unavailable")
    monkeypatch.setattr(net, "safe_request", broken)

    def steps(body, done):
        if not any(n == "http_request" for n, _ in done) and "http_request" in tool_names(body):
            return ("http_request", {"method": "GET", "url": "https://api.catalog.example/items"})
        if "browser_open" in tool_names(body) and not any(n == "browser_open" for n, _ in done):
            return ("browser_open", {"url": "https://api.catalog.example/items"})
        return "از طریق مرورگر: قیمت نهایی محصول هدف ۴۲ دلار است."

    h = _script("سناریو-E", steps)
    mock_llm.handlers.insert(0, h)
    try:
        run = await _chat(app_client, runner, "از API سایت کاتالوگ قیمت محصول را بگیر و گزارش بده http (سناریو-E)")
    finally:
        mock_llm.handlers.remove(h)
    titles = [s.title for s in await _steps(run.id)]
    assert run.status == "succeeded", run.error
    assert any("روش جایگزین" in t for t in titles), titles
    assert page.url.endswith("/items") and "۴۲" in run.result


# ── F: عملیات حساس مرورگر بر اساس اثر ──
async def test_F_effect_aware_browser_policy(app_client, configured_model, runner, mock_llm, monkeypatch):
    from xagent_tools.builtin import computer

    fb = FakeBrowser()
    monkeypatch.setattr(computer, "_browser", fb)

    async def noop():
        return None
    monkeypatch.setattr(computer, "_guard", noop)
    order = [1, 2, 5, 4]  # View, Edit, Pay (ممنوع)، Delete (نیازمند اجازه)

    def steps(body, done):
        if "browser_open" not in tool_names(body):
            return ("tools_load", {"groups": ["browser"]})
        if not any(n == "browser_open" for n, _ in done):
            return ("browser_open", {"url": "https://shop.example/account"})
        clicks = [a["ref"] for n, a in done if n == "browser_click"]
        for ref in order:
            if ref not in clicks:
                return ("browser_click", {"ref": ref})
        return "پایان"

    h = _script("سناریو-F", steps)
    mock_llm.handlers.insert(0, h)
    try:
        run = await _chat(app_client, runner, "در مرورگر صفحه حساب را باز کن و دکمه‌ها را امتحان کن (سناریو-F)")
    finally:
        mock_llm.handlers.remove(h)
    clicked = [c.split("|")[1] for p in fb.contexts[0].pages for c in p.clicks]
    assert clicked == ['[data-xa-id="1"]', '[data-xa-id="2"]'], f"فقط View و Edit باید اجرا شوند: {clicked}"
    assert run.status == "waiting_approval"
    async with session_scope() as s:
        aps = (await s.execute(select(Approval).where(Approval.run_id == run.id))).scalars().all()
        steps_ = await _steps(run.id)
    assert [a.category for a in aps] == ["web_irreversible"]
    assert any("پرداخت" in (st.title or "") and st.status == "blocked" for st in steps_), "پرداخت باید ممنوع ثبت شود"


# ── G: حجم بالای رویداد بدون حذف بی‌صدا (۱۰، ۳۰، ۳۱، ۵۰۰، ۱۰۰۰ + Timestamp برابر) ──
@pytest.mark.parametrize("n", [10, 30, 31, 500, 1000])
async def test_G_high_volume_events_accounted(app_client, n):
    from xagent_tools.watch import precheck

    t0 = utcnow() - timedelta(seconds=30)
    chat = 900000 + n
    async with session_scope() as s:
        r = Routine(name=f"g-{n}", instruction="x", trigger_type="interval", interval_seconds=60, status="active",
                    starts_at=t0, watch={"source": "telegram", "chats": [str(chat)], "private_only": True},
                    watch_state={}, run_count=0)
        s.add(r)
        await s.flush()
        rid = r.id
        same_ts = utcnow() - timedelta(seconds=10)
        for i in range(n):  # نیمی با Timestamp یکسان، بعضی ربات (فیلتر عمدی)
            bot = i % 10 == 9
            s.add(EventInbox(source="telegram", kind="message", dedup_key=f"tg:{chat}:{i}",
                             created_at=same_ts if i % 2 else same_ts + timedelta(microseconds=i),
                             payload={"chat_id": chat, "chat_username": str(chat), "sender_id": chat, "message_id": i + 1,
                                      "text": f"m{i}", "is_private": True, "sender_is_bot": bot}))
    fake = FakeTelegram()
    delivered, totals = [], {"received": 0, "delivered": 0, "filtered": 0}
    for _ in range(200):
        async with session_scope() as s:
            r = await s.get(Routine, rid)
            res = await precheck(s, r, fake)
            for k in totals:
                totals[k] += res.stats.get(k, 0)
            assert res.stats["received"] == res.stats["delivered"] + res.stats["filtered"]
            delivered += [e["message_id"] for e in res.events]
            r.watch_state = res.state
        if not res.events and not res.stats.get("pending"):
            break
    expected = [i + 1 for i in range(n) if i % 10 != 9]
    assert sorted(delivered) == expected and len(delivered) == len(set(delivered))
    assert totals["received"] >= n and totals["filtered"] >= n - len(expected)


# ── H: ارسال خارجی موفق ولی ثبت پایدار شکست بخورد ──
async def test_H_send_succeeds_but_commit_fails_is_not_resent(app_client, monkeypatch):
    from xagent_tools import missions
    from xagent_tools.base import ToolContext
    from xagent_tools.builtin.telegram import telegram_send

    async with session_scope() as s:
        m = Mission(title="H", owner="admin", status="active", actions_count=0, expires_at=utcnow() + timedelta(hours=1),
                    scope={"tools": ["telegram_send"], "telegram": {"reply": True, "reply_to": ["ali"]}})
        s.add(m)
        await s.flush()
        mid = str(m.id)
    fake = FakeTelegram()
    real_mark = missions.outbox_mark

    async def failing_mark(aid, status, detail=None):
        if status == "sent":
            raise ConnectionError("دیتابیس پس از ارسال در دسترس نیست")
        await real_mark(aid, status, detail)

    state = {"event_payload": {"new_messages": [{"chat": "ali", "chat_id": 1, "message_id": 5}]}}
    async with session_scope() as s:
        ctx = ToolContext(session=s, run_id=uuid.uuid4(), conversation_id=None, timezone="Asia/Tehran", trusted_owner=True,
                          sandbox=None, publish=None, extra={"telegram": fake, "mission_id": mid, "run_state": state,
                                                             "tool_call_id": "c1"})
        monkeypatch.setattr(missions, "outbox_mark", failing_mark)
        with pytest.raises(ConnectionError):
            await telegram_send(ctx, {"chat": "ali", "text": "سلام", "reply_to": 5})
        monkeypatch.setattr(missions, "outbox_mark", real_mark)
        res = await telegram_send(ctx, {"chat": "ali", "text": "سلام", "reply_to": 5})  # بازیابی/تکرار فراخوانی
    assert len(fake.sent) == 1 and res.data.get("uncertain"), "پس از شکست ثبت، پیام دوباره ارسال شد"


# ── I: مأموریت چندکارمند با شواهد واقعی هر مرحله ──
async def test_I_manager_delegates_and_reports_from_evidence(app_client, configured_model, runner, mock_llm):
    for name, title in (("researcher_i", "پژوهشگر"), ("analyst_i", "تحلیلگر")):
        r = await app_client.post("/api/control/employees", json={"name": name, "title": title, "instructions": title,
                                                                    "tool_groups": ["memory"]})
        assert r.status_code == 200, r.text

    def manager(body, done):
        if "delegate_task" not in tool_names(body):
            return ("tools_load", {"groups": ["admin"]})
        d = [a for n, a in done if n == "delegate_task"]
        if not d:
            return ("delegate_task", {"employee": "researcher_i", "task": "داده فروش فصل را استخراج کن [I-R]"})
        if len(d) == 1:
            prev = [m["content"] for m in body["messages"] if m.get("role") == "tool"][-1]
            return ("delegate_task", {"employee": "analyst_i", "task": f"این داده را تحلیل کن [I-A]: {prev[:200]}"})
        outs = [m["content"] for m in body["messages"] if m.get("role") == "tool"]
        return "گزارش نهایی بر اساس گزارش کارمندان:\n" + "\n---\n".join(outs[-2:])

    def employees(body):
        t = body_text(body)
        if body.get("response_format"):
            return None
        if "[I-A]" in t:
            return "تحلیل: رشد ۲۰٪ نسبت به فصل قبل"
        if "[I-R]" in t:
            return "داده: فروش فصل ۱۲۰ واحد"
        return None

    hm = _script("سناریو-I", manager)
    mock_llm.handlers[:0] = [hm, employees]
    try:
        run = await _chat(app_client, runner, "گزارش فروش فصل را با کمک پژوهشگر و تحلیلگر تهیه کن (سناریو-I)")
    finally:
        for h in (hm, employees):
            mock_llm.handlers.remove(h)
    assert run.status == "succeeded", run.error
    async with session_scope() as s:
        kids = (await s.execute(select(Run).where(Run.parent_run_id == run.id).order_by(Run.created_at))).scalars().all()
    assert len(kids) == 2 and all(k.status == "succeeded" for k in kids)
    assert "۱۲۰" in run.result and "۲۰٪" in run.result, "گزارش نهایی از شواهد مراحل ساخته نشد"
    assert float(run.cost_usd) >= sum(float(k.cost_usd) for k in kids), "هزینه کارمندان به مأموریت والد منتسب نشد"


# ── J: Crash میانه مأموریت چندمرحله‌ای؛ اقدام بیرونی تکرار نمی‌شود ──
async def test_J_worker_crash_mid_mission_resumes_without_repeating(app_client, configured_model, mock_llm, monkeypatch):
    from xagent_agent.pipeline import AgentRunner
    from xagent_tools import registry
    from xagent_worker.scheduler import reconcile_runs

    fake = FakeTelegram()
    crashed = {"n": 0}
    real = registry.BUILTIN_BY_NAME["task_list"].handler

    async def crashing(ctx, a):
        if crashed["n"] == 0:
            crashed["n"] += 1
            raise asyncio.CancelledError()  # کشته‌شدن Worker پس از گام اول
        return await real(ctx, a)

    monkeypatch.setattr(registry.BUILTIN_BY_NAME["task_list"], "handler", crashing)

    def steps(body, done):
        names = [n for n, _ in done]
        if "telegram_send" not in names:
            return ("telegram_send", {"chat": "me", "text": "گزارش مرحله اول"})
        if "task_list" not in names:
            return ("task_list", {})
        return "هر دو مرحله انجام شد."

    h = _script("سناریو-J", steps)
    mock_llm.handlers.insert(0, h)
    async with session_scope() as s:
        run = Run(trigger="chat", input="مرحله اول پیام بده، بعد تسک‌ها را ببین (سناریو-J)", status="queued",
                  state={"trusted": True, "principal": "owner", "approved": {}, "route": {
                      "complexity": "simple", "risk": "low", "path": "worker", "command": None, "needs_vision": False,
                      "needs_audio": False, "tool_groups": ["telegram", "tasks"], "memory_layers": [], "reasons": []}})
        s.add(run)
        await s.flush()
        rid = run.id
    from xagent_core.models import HarnessRule
    async with session_scope() as s:
        rule = (await s.execute(select(HarnessRule).where(HarnessRule.category == "external_message"))).scalar_one()
        old_mode, rule.mode = rule.mode, "auto"
    try:
        with pytest.raises(asyncio.CancelledError):
            await AgentRunner("j1", telegram=fake).execute(rid)
        async with session_scope() as s:
            assert (await s.get(Run, rid)).status == "running", "وضعیت پس از Crash باید «در حال اجرا»ی یتیم باشد"
        assert await reconcile_runs(stale_after_seconds=0) >= 1
        await AgentRunner("j2", telegram=fake).execute(rid)
    finally:
        mock_llm.handlers.remove(h)
        async with session_scope() as s:
            (await s.execute(select(HarnessRule).where(HarnessRule.category == "external_message"))).scalar_one().mode = old_mode
    async with session_scope() as s:
        run = await s.get(Run, rid)
    assert run.status == "succeeded", run.error
    assert len(fake.sent) == 1, f"اقدام بیرونی پس از بازیابی تکرار شد: {len(fake.sent)}"
    assert float(run.cost_usd) <= 0.5


_ = (json, func, MissionAction)


# ── D: مسیریاب مدل بر اساس سیاست، کیفیت، هزینه مورد انتظار، تأخیر، سلامت و بودجه ──
async def test_D_model_router_policies(app_client, configured_model):
    from decimal import Decimal

    from xagent_agent.providers.registry import load_model
    from xagent_agent.router import TaskProfile, route
    from xagent_core.models import AIModel, UsageEvent

    async with session_scope() as s:
        base = await s.get(AIModel, uuid.UUID(configured_model))
        specs = {  # نام: (قیمت ورودی، خروجی، کیفیت، ابزار، سلامت، تأخیر، نرخ موفقیت)
            "d-cheap-slow": (0.1, 0.4, 0.55, True, "ok", 9000, 0.95),
            "d-fast-mid": (0.5, 2.0, 0.72, True, "ok", 800, 0.98),
            "d-expensive": (15, 75, 0.95, True, "ok", 3000, 0.99),
            "d-broken": (0.05, 0.1, 0.9, True, "failing", 500, 0.2),
            "d-notools": (0.01, 0.02, 0.6, False, "ok", 400, 0.99),
        }
        ids = {}
        for name, (pin, pout, q, tools, health, lat, ok) in specs.items():
            m = AIModel(provider_id=base.provider_id, model_name=f"{name}-{uuid.uuid4().hex[:4]}", display_name=name,
                        enabled=True, capabilities={"tools": tools}, capability_meta={"quality": q}, context_window=128000,
                        price_in=Decimal(str(pin)), price_out=Decimal(str(pout)), pricing_source="manual", health=health)
            s.add(m)
            await s.flush()
            ids[name] = m.id
            for i in range(20):
                s.add(UsageEvent(model_id=m.id, model_name=m.model_name, purpose="execution", latency_ms=lat,
                                 success=i < int(ok * 20), attempt_status="confirmed"))
    async with session_scope() as s:
        models = [await load_model(s, ids[n]) for n in specs]
        simple = TaskProfile(complexity="simple", need_caps=("tools",), calls=3)
        complex_ = TaskProfile(complexity="complex", need_caps=("tools",), calls=6)
        eco, d_eco = await route(s, models, "economy", simple)
        bal, d_bal = await route(s, models, "balanced", simple)
        qual, d_q = await route(s, models, "quality", complex_)
        capped, d_cap = await route(s, models, "quality", TaskProfile(complexity="complex", need_caps=("tools",),
                                                                        calls=6, budget_usd=0.05))
    label = lambda ms: [m.label for m in ms]  # noqa: E731
    assert label(eco)[0] == "d-cheap-slow", d_eco
    assert label(bal)[0] == "d-fast-mid", d_bal
    assert label(qual)[0] == "d-expensive", d_q
    assert "d-expensive" not in label(capped) and label(capped)[0] == "d-fast-mid", d_cap
    for ms in (eco, bal, qual, capped):
        assert "d-broken" not in label(ms) and "d-notools" not in label(ms), "Provider خراب یا مدل بدون ابزار انتخاب شد"
    assert d_cap["reasons"] and any("بودجه" in r for r in d_cap["reasons"])
    assert all(c["price"] == "verified" for c in d_bal["candidates"])


async def test_observability_breakdown_and_run_metrics(app_client, configured_model, runner):
    run = await _chat(app_client, runner, "سلام")
    m = (await app_client.get(f"/api/runs/{run.id}/metrics")).json()
    assert m["llm_calls"] >= 1 and m["models"] and m["models"][0]["calls"] >= 1
    assert abs(sum(x["cost_verified"] + x["cost_estimated"] for x in m["models"]) - m["cost_usd"]) < 1e-6
    for dim in ("model", "provider", "employee", "routine", "mission", "purpose", "day", "tool"):
        r = await app_client.get(f"/api/usage/breakdown?dimension={dim}&days=7")
        assert r.status_code == 200, (dim, r.text)
        assert isinstance(r.json()["items"], list)
