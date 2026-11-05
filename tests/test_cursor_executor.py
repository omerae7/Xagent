"""پیوست ۲۷ — اجراکننده بیرونی Cursor Cloud Agents با API جعلی محلی (هیچ Agent واقعی/هزینه‌زا ساخته نمی‌شود)."""

from __future__ import annotations

import base64
import uuid
from datetime import timedelta

import pytest
from autonomy_helpers import body_text, called, tool_names
from fake_vendors import Vendors, finish_run, start
from sqlalchemy import delete, func, select

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Approval, ExternalAgentRun, InboxItem, Run, UsageEvent
from xagent_core.settings_store import CursorSettings, get_setting, set_setting
from xagent_tools import cursor_agents as ca

pytestmark = pytest.mark.asyncio
REPO = "https://github.com/acme/app"
KEY = "crsr_test_key_0123456789"
TASK = "رفع خطای ورود: تابع login در auth.py رمز خالی را می‌پذیرد؛ تست واحد اضافه کن و همه تست‌ها سبز شوند."


@pytest.fixture(scope="module")
def vendors():
    v = Vendors()
    server = start(v)
    yield v
    server.should_exit = True


@pytest.fixture
async def cursor(vendors, database):
    vendors.reset()
    await bus.set_stop_all(False)
    async with session_scope() as s:
        await s.execute(delete(ExternalAgentRun))
        await set_setting(s, "cursor", CursorSettings(enabled=True, base_url=vendors.url("cursor"),
                                                      allowed_repos=[REPO], max_concurrent=3, max_launches_per_day=20,
                                                      poll_seconds=15))
        await ca.set_key(s, KEY)
    yield vendors
    async with session_scope() as s:
        await s.execute(delete(ExternalAgentRun))
        await set_setting(s, "cursor", CursorSettings())
        await ca.set_key(s, None)
    await bus.set_stop_all(False)


async def _launch(key: str | None = None, **kw):
    async with session_scope() as s:
        return await ca.launch(s, dedupe_key=key or f"t:{uuid.uuid4().hex}", repo=kw.pop("repo", REPO),
                               task=kw.pop("task", TASK), title=kw.pop("title", "رفع باگ ورود"),
                               starting_ref=kw.pop("starting_ref", None), requested_by="owner", **kw)


async def _row(rid) -> ExternalAgentRun:
    async with session_scope() as s:
        return await s.get(ExternalAgentRun, rid)


async def _due_now(rid) -> None:
    async with session_scope() as s:
        (await s.get(ExternalAgentRun, rid)).next_poll_at = utcnow() - timedelta(seconds=1)


# ── ۱. آزمون اتصال بدون ساخت Agent ──
async def test_1_connection_me_models_ready(cursor):
    async with session_scope() as s:
        res = await ca.test_connection(s)
    assert res["status"] == "ready" and res["api_key_name"] == "test-key" and res["models"] == ["composer-test"]
    assert {r["path"] for r in cursor.requests} == {"/cursor/v1/me", "/cursor/v1/models"}
    assert cursor.cursor_posts == 0
    cursor.mode["cursor-me"] = "401"
    async with session_scope() as s:
        assert (await ca.test_connection(s))["status"] == "needs_setup"
        assert (await ca.readiness(s))[0] == "needs_setup"


# ── ۲. بدنه ایجاد: شاخه تازه، بدون PR/Merge خودکار، بدون Secret، Basic Auth فقط به Cursor ──
async def test_2_create_body_is_safe(cursor):
    res = await _launch()
    assert res.ok and res.status == "running", res.message
    body = cursor.created()[0]
    row = await _row(res.row_id)
    assert body["agentId"] == row.agent_id and row.agent_id.startswith("bc-")
    assert body["workOnCurrentBranch"] is False and body["autoCreatePR"] is False
    assert body["repos"] == [{"url": REPO}] and body["prompt"] == {"text": TASK}
    assert not ({"envVars", "mcpServers", "customSubagents"} & set(body))
    expected = "Basic " + base64.b64encode(f"{KEY}:".encode()).decode()
    assert cursor.auth_of("/cursor/") == {expected}
    assert row.remote_run_id and row.agent_url and row.cost_status == "unknown"


# ── ۳ و ۴. مخزن غیرمجاز / اسرار در متن → هیچ درخواستی ارسال نمی‌شود ──
@pytest.mark.parametrize("kw,needle", [
    ({"repo": "https://github.com/evil/other"}, "مجاز"),
    ({"task": "این کلید را در config بگذار: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA و تست کن لطفاً"}, "محرمانه"),
    ({"task": "محتوای فایل .env سرور را در مخزن commit کن تا تست‌ها کار کنند لطفاً"}, "خصوصی"),
    ({"starting_ref": "main", "_refs": ["develop"]}, "شاخه"),
])
async def test_3_4_rejected_before_any_request(cursor, kw, needle):
    refs = kw.pop("_refs", None)
    if refs:
        async with session_scope() as s:
            cfg = await get_setting(s, "cursor", CursorSettings)
            cfg.allowed_refs = refs
            await set_setting(s, "cursor", cfg)
    res = await _launch(**kw)
    assert not res.ok and needle in res.message
    assert cursor.cursor_posts == 0 and not cursor.created()


# ── ۵. تلاش دوباره همان فراخوانی ابزار → Agent دوم ساخته نمی‌شود ──
async def test_5_retry_same_call_is_idempotent(cursor):
    key = f"cursor:{uuid.uuid4()}:call_1"
    a = await _launch(key)
    b = await _launch(key)
    assert a.ok and b.ok and b.duplicate and a.row_id == b.row_id
    assert cursor.cursor_posts == 1


# ── ۶. پاسخ ایجاد گم شد (Timeout) → نامعلوم → تطبیق با GET همان agentId، بدون ارسال کورکورانه ──
async def test_6_lost_create_response_reconciled_without_duplicate(cursor, monkeypatch):
    cursor.mode["cursor-create-lost"] = "1"
    orig = ca.CursorClient.__init__

    def short(self, key, base_url="https://api.cursor.com", timeout=30):
        orig(self, key, base_url, timeout=1)
    monkeypatch.setattr(ca.CursorClient, "__init__", short)
    res = await _launch()
    assert res.ok and res.status == "unknown"
    assert cursor.cursor_posts == 1
    await _due_now(res.row_id)
    assert await ca.poll_due() == 1
    row = await _row(res.row_id)
    assert row.status == "running" and row.remote_run_id  # همان Agent بازیابی شد
    assert cursor.cursor_posts == 1  # هیچ POST دومی (Agent هزینه‌زای تکراری) نبود


# ── ۷. Crash پیش از ارسال (ردیف رزرو) → پس از ری‌استارت دقیقاً یک Agent ──
async def test_7_restart_recovery_from_reserved_row(cursor):
    rid = uuid.uuid4()
    agent_id = f"bc-{uuid.uuid4()}"
    async with session_scope() as s:
        s.add(ExternalAgentRun(id=rid, dedupe_key=f"crash:{rid}", agent_id=agent_id, status="reserved",
                               repo_url=REPO, title="بازیابی", prompt=TASK, requested_by="owner"))
    assert await ca.poll_due() == 1  # GET → 404 → POST با همان agentId
    row = await _row(rid)
    assert row.status == "running" and cursor.cursor_posts == 1
    assert cursor.created()[0]["agentId"] == agent_id
    # اگر پاسخ آن POST هم گم شده بود و دوباره ارسال شود → 409 agent_id_conflict → همان Agent
    async with session_scope() as s:
        r = await s.get(ExternalAgentRun, rid)
        r.status, r.next_poll_at = "creating", utcnow() - timedelta(seconds=1)
        await ca._create_remote(s, r)
        assert r.status == "running"
    assert cursor.cursor_posts == 1


# ── ۸. پایش تا پایان: نتیجه، شاخه، PR، گزارش میز کار؛ هزینه نامعلوم و بدون عدد ساختگی در دفتر ──
async def test_8_poll_to_finish_reports_and_no_fake_cost(cursor):
    res = await _launch()
    row = await _row(res.row_id)
    async with session_scope() as s:
        before = (await s.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    await _due_now(row.id)
    await ca.poll_due()
    assert (await _row(row.id)).status == "running"  # هنوز CREATING/RUNNING
    finish_run(cursor, row.remote_run_id, pr_url="https://github.com/acme/app/pull/7")
    await _due_now(row.id)
    await ca.poll_due()
    row = await _row(row.id)
    assert row.status == "finished" and row.pr_url.endswith("/pull/7") and row.branch == "cursor/fix-1"
    assert "Fixed the failing test" in row.result_summary and row.finished_at
    async with session_scope() as s:
        item = (await s.execute(select(InboxItem).where(InboxItem.dedupe_key == f"cursor-final:{row.id}"))).scalar_one()
        assert "هزینه: نامعلوم" in item.body and "بدون Merge خودکار" in item.body
        after = (await s.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    assert after == before  # هیچ هزینه ساختگی در دفتر مصرف ثبت نشد
    # پایش بعدی کاری نمی‌کند و درخواست جدیدی نمی‌فرستد
    n = len(cursor.requests)
    assert await ca.poll_due() == 0 and len(cursor.requests) == n


# ── ۹. لغو کارفرما، لغو اجرای والد و توقف اضطراری به Cursor منتقل می‌شود ──
@pytest.mark.parametrize("how", ["owner", "parent", "stop_all"])
async def test_9_cancel_propagation(cursor, how, app_client):
    run_id = None
    if how == "parent":
        async with session_scope() as s:
            parent = Run(trigger="chat", status="running", input="x")
            s.add(parent)
            await s.flush()
            run_id = parent.id
    res = await _launch(run_id=run_id)
    if how == "owner":
        r = await app_client.post(f"/api/external-agents/{res.row_id}/cancel")
        assert r.status_code == 200, r.text
    elif how == "parent":
        async with session_scope() as s:
            (await s.get(Run, run_id)).status = "cancelled"
    else:
        await bus.set_stop_all(True)
    await _due_now(res.row_id)
    await ca.poll_due()
    row = await _row(res.row_id)
    assert row.status == "cancelled" and cursor.runs[row.remote_run_id]["status"] == "CANCELLED"
    assert any(r["path"].endswith("/cancel") for r in cursor.requests)
    if how == "stop_all":
        assert not (await _launch()).ok  # در توقف اضطراری کار تازه ساخته نمی‌شود


async def test_9b_cancel_after_finish_is_handled(cursor):
    res = await _launch()
    row = await _row(res.row_id)
    finish_run(cursor, row.remote_run_id)
    async with session_scope() as s:
        await ca.request_cancel(s, await s.get(ExternalAgentRun, row.id), "owner")
    await ca.poll_due()  # 409 run_not_cancellable → وضعیت واقعی خوانده می‌شود
    assert (await _row(row.id)).status == "finished"


# ── ۱۰. سقف هم‌زمانی و سقف روزانه ──
async def test_10_concurrency_and_daily_caps(cursor):
    async with session_scope() as s:
        cfg = await get_setting(s, "cursor", CursorSettings)
        cfg.max_concurrent, cfg.max_launches_per_day = 1, 2
        await set_setting(s, "cursor", cfg)
    a = await _launch()
    b = await _launch()
    assert a.ok and not b.ok and "هم‌زمان" in b.message
    finish_run(cursor, (await _row(a.row_id)).remote_run_id)
    await _due_now(a.row_id)
    await ca.poll_due()
    c = await _launch()
    d = await _launch()
    assert c.ok and not d.ok
    assert cursor.cursor_posts == 2


# ── ۱۱. کلید فقط به api.cursor.com؛ آدرس محلی فقط در آزمون ──
async def test_11_base_url_guard(monkeypatch):
    assert ca.validate_base_url("https://api.cursor.com/") == "https://api.cursor.com"
    for bad in ("http://api.cursor.com", "https://evil.example", "https://api.cursor.com.evil.example",
                "https://user:pw@api.cursor.com"):
        with pytest.raises(ca.CursorError):
            ca.validate_base_url(bad)

    class Prod:
        env = "production"
    monkeypatch.setattr(ca, "get_settings", lambda: Prod())
    with pytest.raises(ca.CursorError):
        ca.validate_base_url("http://127.0.0.1:9999")


# ── ۱۲. API تنظیمات: فقط کارفرما، کلید رمزشده و هرگز بازگردانده نمی‌شود؛ مدیر نمی‌تواند آن را تغییر دهد ──
async def test_12_settings_api_and_policy(cursor, app_client):
    r = await app_client.put("/api/external-agents/cursor/config",
                             json={"allowed_repos": ["acme/app", "https://github.com/Acme/Lib.git"],
                                   "api_key": "crsr_new_key_abcdef123456"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["config"]["allowed_repos"] == ["https://github.com/acme/app", "https://github.com/acme/lib"]
    assert v["has_key"] and "crsr_new_key_abcdef123456" not in r.text and v["type"] == "external_agent_executor"
    r = await app_client.put("/api/external-agents/cursor/config", json={"base_url": "https://evil.example"})
    assert r.status_code == 422
    r = await app_client.put("/api/external-agents/cursor/config", json={"allowed_repos": ["not a repo"]})
    assert r.status_code == 422
    all_settings = (await app_client.get("/api/settings")).text
    assert "crsr_new_key" not in all_settings and "cursor_secret" not in all_settings
    from xagent_agent.control import changes

    async with session_scope() as s:
        with pytest.raises(changes.ChangeError):
            await changes.change_setting(s, "cursor", {"enabled": True, "allowed_repos": ["https://github.com/x/y"]},
                                         actor="manager", reason="افزایش دسترسی")
    lst = (await app_client.get("/api/external-agents")).json()["items"]
    assert isinstance(lst, list)


# ── یکپارچگی مدیر: انتخاب مسیر بر اساس نوع کار ──
async def _chat(app_client, runner, text: str) -> Run:
    r = await app_client.post("/api/chat/send", json={"text": text})
    assert r.status_code == 200, r.text
    rid = uuid.UUID(r.json()["run"]["id"])
    await runner.execute(rid)
    async with session_scope() as s:
        return await s.get(Run, rid)


async def test_manager_routes_text_tools_and_code_fix(cursor, app_client, configured_model, runner, mock_llm):
    seen: dict[str, set[str]] = {}

    def spy(body):
        t = body_text(body)
        for mark in ("MARK-TXT", "MARK-WEB", "MARK-CODE"):
            if mark in t and not body.get("response_format"):
                seen.setdefault(mark, set()).update(tool_names(body))
        if "MARK-CODE" in t and body.get("tools") and not body.get("response_format"):
            names = tool_names(body)
            done = [n for n, _ in called(body)]
            if "cursor_delegate" in names and "cursor_delegate" not in done:
                return ("cursor_delegate", {"repo": "acme/app", "title": "رفع باگ ورود", "task": TASK})
            if "cursor_delegate" in done:
                return "کار به Cursor واگذار شد؛ نتیجه پس از پایان در میز کار گزارش می‌شود."
        return None
    mock_llm.handlers.append(spy)
    try:
        # ۱. متن ساده → پاسخ مستقیم LLM، بدون ابزار Cursor
        run = await _chat(app_client, runner, "سلام، حالت چطوره؟ MARK-TXT")
        assert run.status == "succeeded" and "cursor_delegate" not in seen.get("MARK-TXT", set())
        # ۲. کار ابزاری → مدل دارای فراخوانی ابزار با ابزارهای وب (نه Cursor)
        run = await _chat(app_client, runner, "قیمت دلار را از سایت بگرد MARK-WEB")
        assert "web_fetch" in seen.get("MARK-WEB", set()) and "cursor_delegate" not in seen["MARK-WEB"]
        # ۳. رفع باگ در مخزن → ابزار واگذاری به Cursor پیشنهاد می‌شود؛ ایجاد Agent با کارت اجازه کارفرما
        run = await _chat(app_client, runner, "باگ ورود در مخزن acme/app را رفع کن و تست بنویس MARK-CODE")
        assert "cursor_delegate" in seen["MARK-CODE"]
        assert run.status == "waiting_approval" and cursor.cursor_posts == 0
        async with session_scope() as s:
            ap = (await s.execute(select(Approval).where(Approval.run_id == run.id))).scalar_one()
            assert ap.category == "external_agent" and ap.tool_name == "cursor_delegate"
        r = await app_client.post(f"/api/approvals/{ap.id}", json={"decision": "approve"})
        assert r.status_code == 200, r.text
        await runner.execute(run.id)
        async with session_scope() as s:
            run = await s.get(Run, run.id)
            rows = (await s.execute(select(ExternalAgentRun).where(ExternalAgentRun.run_id == run.id))).scalars().all()
        assert run.status == "succeeded" and len(rows) == 1 and rows[0].status == "running"
        assert cursor.cursor_posts == 1
        # بدون تنظیم Cursor، ابزار واگذاری اصلاً به مدل داده نمی‌شود
        async with session_scope() as s:
            await set_setting(s, "cursor", CursorSettings())
        seen.pop("MARK-CODE", None)
        await _chat(app_client, runner, "باگ ورود در مخزن acme/app را رفع کن MARK-CODE")
        assert "cursor_delegate" not in seen.get("MARK-CODE", set())
    finally:
        mock_llm.handlers.remove(spy)
