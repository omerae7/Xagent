"""تست‌های بازتولید ممیزی مهندسی (دور پنجم) — روی Commit مبنا 06cae3d شکست می‌خورند.

بخش‌های سند: ۴ (Scope خواندن)، ۵ (اعتبار پاسخ)، ۶ (ازدست‌رفتن رویداد)، ۷ (ارسال با نتیجه نامشخص)،
۸ (تداخل تب مرورگر)، ۹ (عملیات حساس مرورگر)، ۱۱ (تسک موعددار)، ۱۸ (خطای DB در دریافت تلگرام).
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from autonomy_helpers import FakeBrowser, FakeTelegram, body_text, called, tool_names
from sqlalchemy import func, select

from xagent_core.db import session_scope, utcnow
from xagent_core.models import EventInbox, Mission, MissionAction, Routine, Run

SCOPE = {"tools": ["telegram_recent", "telegram_send", "notify_owner"],
         "telegram": {"read": True, "read_chats": ["ali"], "reply": True, "reply_to": ["ali", "sara"],
                      "reply_only_to_senders": True, "max_replies_per_chat_hour": 6, "allow_files": False},
         "out_of_scope": "report"}


async def _mission(**over) -> Mission:
    async with session_scope() as s:
        m = Mission(title="آزمون Scope", owner="admin", status="active", scope={**SCOPE, **over}, actions_count=0,
                    expires_at=utcnow() + timedelta(hours=1))
        s.add(m)
        await s.flush()
        return m


def _state(*events) -> dict:
    return {"event_payload": {"source": "telegram", "new_messages": [
        {"event_id": str(uuid.uuid4()), "chat": c, "chat_id": cid, "sender": c, "message_id": mid, "text": "سلام"}
        for c, cid, mid in events]}}


# ── بخش ۴: Scope خواندن ──
async def test_s4_read_tool_cannot_read_other_chat(app_client):
    from xagent_tools.missions import check_call

    m = await _mission()
    async with session_scope() as s:
        mission = await s.get(Mission, m.id)
        verdict, why = await check_call(s, mission, "telegram_recent", {"chat": "bob"}, _state(("ali", 11, 5)))
    assert verdict != "allow", f"خواندن گفتگوی خارج از محدوده مجاز شد: {why}"


async def test_s4_mission_run_never_sees_out_of_scope_chat_data(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    m = await _mission()
    fake = FakeTelegram()
    fake.history["bob"] = [{"id": 1, "date": "", "sender": "bob", "text": "B-SECRET-محرمانه"}]
    mark = "S4-READ-SCOPE"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if not any(n == "telegram_recent" for n, _ in called(body)):
            return ("telegram_recent", {"chat": "bob", "limit": 5})
        return "تمام."

    async with session_scope() as s:
        run = Run(trigger="routine", input=f"گزارش بده ({mark})", status="queued",
                  state={"trusted": True, "principal": "mission", "mission_id": str(m.id)})
        s.add(run)
        await s.flush()
        rid = run.id
    before = len(mock_llm.calls)
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("s4", telegram=fake).execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    later = json.dumps(mock_llm.calls[before:], ensure_ascii=False)
    assert "B-SECRET" not in later, "داده گفتگوی خارج از محدوده به مدل تحویل داده شد"
    async with session_scope() as s:
        blocked = (await s.execute(select(MissionAction).where(MissionAction.mission_id == m.id,
                                                               MissionAction.status == "blocked"))).scalars().all()
        assert blocked, "علت مسدودسازی در Audit ثبت نشد"
        assert (await s.get(Mission, m.id)).scope == {**SCOPE}, "مجوز مأموریت تغییر کرد"


async def test_s4_mission_memory_is_not_global(app_client):
    from xagent_agent.pipeline import AgentRunner

    runner = AgentRunner("s4m")
    m = await _mission(tools=["memory_search", "telegram_send"])
    async with session_scope() as s:
        run = Run(trigger="routine", input="x", state={"trusted": True, "principal": "mission", "mission_id": str(m.id)})
        s.add(run)
        await s.flush()
        scopes = runner.memory_scopes_for_run(run, None, {"mission_id": str(m.id)})
    assert scopes == [f"mission:{m.id}"], f"مأموریت به حافظه سراسری کارفرما دسترسی دارد: {scopes}"


# ── بخش ۵: اعتبار پاسخ به پیام جدید ──
@pytest.mark.parametrize("args,ok", [
    ({"chat": "ali", "reply_to": 5}, True),                 # پیام جدید مجاز همان نوبت
    ({"chat": "ali", "reply_to": 4}, False),                # پیام قدیمی (در رویدادهای این نوبت نیست)
    ({"chat": "ali", "reply_to": 999}, False),              # شناسه ساختگی
    ({"chat": "ali"}, False),                               # ارسال مستقل بدون reply (مجوز جدا لازم دارد)
    ({"chat": "sara", "reply_to": 5}, False),               # شناسه پیام ali با گفتگوی دیگر (تغییر chat)
    ({"chat": "eve", "reply_to": 7}, False),                # مخاطب خارج از محدوده
])
async def test_s5_reply_target_must_match_new_allowed_message(app_client, args, ok):
    from xagent_tools.missions import check_call

    m = await _mission()
    state = _state(("ali", 11, 5), ("sara", 12, 6), ("eve", 13, 7))
    async with session_scope() as s:
        verdict, why = await check_call(s, await s.get(Mission, m.id), "telegram_send", {**args, "text": "x"}, state)
    assert (verdict == "allow") is ok, f"{args} → {verdict}: {why}"


# ── بخش ۶: ازدست‌رفتن رویداد ──
async def _routine_with_watch(t0) -> uuid.UUID:
    async with session_scope() as s:
        r = Routine(name=f"w-{uuid.uuid4().hex[:6]}", instruction="x", trigger_type="interval", interval_seconds=60,
                    status="active", starts_at=t0, watch={"source": "telegram", "chats": [], "private_only": True},
                    watch_state={}, run_count=0)
        s.add(r)
        await s.flush()
        return r.id


@pytest.mark.parametrize("n", [31, 120])
async def test_s6_no_event_silently_lost_beyond_batch(app_client, n):
    from xagent_tools.watch import precheck

    t0 = utcnow() - timedelta(seconds=5)
    rid = await _routine_with_watch(t0)
    fake = FakeTelegram()
    chat = 700000 + n
    for i in range(n):
        await fake.incoming(chat, f"bulk{n}", f"پیام {i}", i + 1)
    delivered: list[int] = []
    for _ in range(10):
        async with session_scope() as s:
            r = await s.get(Routine, rid)
            res = await precheck(s, r, fake)
            delivered += [e["message_id"] for e in res.events if e.get("chat_id") == chat]
            r.watch_state = res.state  # تأیید پس از پردازش موفق
        if not res.events:
            break
    assert sorted(delivered) == list(range(1, n + 1)), f"{n - len(set(delivered))} رویداد بی‌صدا حذف شد"


# ── بخش ۷: ارسال موفق ولی نتیجه نامشخص؛ نباید کورکورانه دوباره ارسال شود ──
async def test_s7_unknown_outcome_is_not_blindly_resent(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    m = await _mission()
    fake = FakeTelegram()
    fake.fail_after_send = 1
    mark = "S7-OUTBOX"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if not any(n == "telegram_send" for n, _ in called(body)):
            return ("telegram_send", {"chat": "ali", "text": "پاسخ", "reply_to": 5})
        return "تمام."

    mock_llm.handlers.insert(0, handler)
    try:
        for _ in range(2):  # نوبت دوم همان رویداد را دوباره پردازش می‌کند
            async with session_scope() as s:
                run = Run(trigger="routine", input=f"پاسخ بده ({mark})", status="queued",
                          state={"trusted": True, "principal": "mission", "mission_id": str(m.id), **_state(("ali", 11, 5))})
                s.add(run)
                await s.flush()
                rid = run.id
            await AgentRunner("s7", telegram=fake).execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    assert len(fake.sent) == 1, f"پیام با نتیجه نامشخص {len(fake.sent)} بار ارسال شد"
    async with session_scope() as s:
        st = (await s.execute(select(MissionAction.status).where(MissionAction.mission_id == m.id,
                                                                 MissionAction.target == "ali"))).scalars().all()
    assert "unknown" in st, f"نتیجه نامشخص ثبت نشد: {st}"


# ── بخش ۸: تداخل تب مرورگر بین دو اجرا ──
async def test_s8_browser_tabs_are_isolated_per_run(monkeypatch):
    from xagent_tools.base import ToolContext
    from xagent_tools.builtin import computer

    fb = FakeBrowser()
    monkeypatch.setattr(computer, "_browser", fb)

    async def noop():
        return None
    monkeypatch.setattr(computer, "_guard", noop)

    def ctx(run):
        return ToolContext(session=None, run_id=run, conversation_id=None, timezone="Asia/Tehran", trusted_owner=True,
                           sandbox=None, publish=None, extra={"approved": set()})
    a, b = ctx(uuid.uuid4()), ctx(uuid.uuid4())
    await computer.browser_open(a, {"url": "https://site-a.example/form"})
    await computer.browser_open(b, {"url": "https://site-b.example/data"})
    await computer.browser_click(a, {"ref": 1})
    pages = fb.contexts[0].pages
    clicks = [c for p in pages for c in p.clicks]
    assert clicks and all(c.startswith("https://site-a.example") for c in clicks), f"کلیک اجرای A در تب اشتباه: {clicks}"


# ── بخش ۹: عملیات حساس مرورگر بر اساس اثر ──
@pytest.mark.parametrize("label,ref,category", [("Pay", 5, "payment"), ("Delete", 4, "web_irreversible"),
                                                 ("Send", 3, "external_message")])
async def test_s9_sensitive_click_is_escalated(monkeypatch, label, ref, category):
    from xagent_tools.base import ToolContext
    from xagent_tools.builtin import computer
    from xagent_tools.errors import EscalateCategory

    fb = FakeBrowser()
    monkeypatch.setattr(computer, "_browser", fb)

    async def noop():
        return None
    monkeypatch.setattr(computer, "_guard", noop)
    c = ToolContext(session=None, run_id=uuid.uuid4(), conversation_id=None, timezone="Asia/Tehran", trusted_owner=True,
                    sandbox=None, publish=None, extra={"approved": {"desktop_control"}})
    await computer.browser_open(c, {"url": "https://shop.example/cart"})
    with pytest.raises(EscalateCategory) as exc:
        await computer.browser_click(c, {"ref": ref})
    assert exc.value.category == category
    assert not any(p.clicks for p in fb.contexts[0].pages), f"کلیک حساس «{label}» بدون مجوز انجام شد"


# ── بخش ۱۱: تسک موعددار اجرایی ایجنت ──
async def test_s11_due_agent_task_is_executed_and_finalized(app_client, configured_model, runner):
    from xagent_worker.scheduler import after_task_run, tick_tasks

    due = utcnow() - timedelta(seconds=5)
    r = await app_client.post("/api/tasks", json={"title": "گزارش وضعیت تسک‌ها", "kind": "agent",
                                                   "start_at": due.isoformat()})
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert await tick_tasks() == 1
    async with session_scope() as s:
        run = (await s.execute(select(Run).where(Run.task_id == uuid.UUID(tid)))).scalar_one()
    await runner.execute(run.id)
    await after_task_run(run.id)
    t = (await app_client.get("/api/tasks")).json()
    task = next(x for x in t["items"] if x["id"] == tid)
    assert task["status"] == "done" and task.get("last_run_id") == str(run.id)


# ── بخش ۱۸: خطای واقعی DB در دریافت تلگرام ≠ رویداد تکراری ──
async def test_s18_db_error_on_receive_is_not_treated_as_duplicate(app_client, monkeypatch):
    from xagent_worker import telegram as tgmod

    bridge = tgmod.TelegramBridge()
    bridge.me = SimpleNamespace(id=1)
    msg = SimpleNamespace(chat_id=4242, id=77, message="سلام", out=False, media=None, date=None, geo=None)
    sender = SimpleNamespace(id=4242, username="dbfail", first_name="db", last_name=None, bot=False)
    event = SimpleNamespace(message=msg, is_private=True, get_chat=lambda: _aw(sender), get_sender=lambda: _aw(sender))
    real_scope = tgmod.session_scope
    calls = {"n": 0}

    def flaky_scope():
        calls["n"] += 1
        if calls["n"] == 1:
            from sqlalchemy.exc import OperationalError
            raise OperationalError("INSERT", {}, Exception("connection refused"))
        return real_scope()

    monkeypatch.setattr(tgmod, "session_scope", flaky_scope)
    await bridge._on_message(event)
    monkeypatch.setattr(tgmod, "session_scope", real_scope)
    await bridge.flush_pending()
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(EventInbox).where(EventInbox.dedup_key == "tg:4242:77"))).scalar_one()
    assert n == 1, "پیام هنگام خطای دیتابیس بی‌صدا از دست رفت"


async def _aw(v):
    return v


_ = tool_names  # helper import kept for future scenarios
