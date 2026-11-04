"""تست‌های بازتولید ده سؤال ریشه‌یابی (بخش ۲۰) — ابتدا روی کد قبلی شکست خوردند، سپس اصلاح شدند.

هر تست یک رفتار مورد انتظار را تعریف می‌کند. شماره سؤال در نام تست آمده است.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from autonomy_helpers import FakePage, FakeTelegram, body_text, called, tool_names
from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Routine, Run

SCHEDULE_PHRASES = [
    "تا ده دقیقه آینده، دقیقه‌ای یک بار پیام‌ها را بررسی کن",
    "از الان تا ۱۰ دقیقه آینده هر یک دقیقه یک بار تلگرام من را بررسی کن",
    "هر ۵ دقیقه یک بار قیمت دلار را چک کن و اگر تغییر کرد خبرم کن",
    "for the next 30 minutes check my telegram every 2 minutes",
]


async def _send(client, text: str) -> dict:
    r = await client.post("/api/chat/send", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


# ── سؤال ۳: عبارت «هر یک دقیقه یک بار» باید به ابزار روتین برسد ──
@pytest.mark.parametrize("text", SCHEDULE_PHRASES)
async def test_q3_natural_schedule_loads_routine_tools(text):
    from xagent_agent.classifier import classify
    from xagent_tools.registry import select_tools

    route = classify(text)
    assert "routines" in route.tool_groups, f"گروه روتین برای «{text}» بارگذاری نشد: {route.tool_groups}"
    async with session_scope() as s:
        names = {t.name for t in await select_tools(s, route.tool_groups, text)}
    assert "routine_create" in names


# ── سؤال ۱: درخواست اجرایی زمان‌دار باید ابزار واقعی را به مدل بدهد، نه فقط متن ──
async def test_q1_timed_request_offers_routine_tool_to_model(app_client, configured_model, runner, mock_llm):
    marker = "تست-بازتولید-سؤال-یک"
    seen: list[set[str]] = []

    def handler(body):
        if marker in body_text(body) and body.get("tools"):
            seen.append(tool_names(body))
            return "ثبت شد"
        return None

    mock_llm.handlers.append(handler)
    try:
        data = await _send(app_client, f"تا ۱۰ دقیقه آینده، دقیقه‌ای یک بار پیام‌های تلگرام را بررسی کن ({marker})")
        await runner.execute(uuid.UUID(data["run"]["id"]))
    finally:
        mock_llm.handlers.remove(handler)
    assert seen, "مدل اجرا با ابزار فراخوانی نشد"
    assert any("routine_create" in names for names in seen), f"ابزار ساخت روتین به مدل داده نشد: {seen[-1]}"


# ── سؤال ۲: شناسنامه پویا باید «آماده» را از «نیازمند تنظیم» جدا کند ──
async def test_q2_capability_manifest_separates_ready_from_setup(configured_model):
    from xagent_agent.manifest import build_manifest

    async with session_scope() as s:
        m = await build_manifest(s, principal="owner", telegram=FakeTelegram(connected=False))
    ready = {c["key"] for c in m["ready"]}
    setup = {c["key"] for c in m["needs_setup"]}
    assert "routines" in ready and "web" in ready
    assert "telegram" in setup and "telegram" not in ready, "تلگرامِ متصل‌نشده نباید «آماده» اعلام شود"


# ── سؤال ۷: قفل روتین در همه مسیرهای خروج آزاد شود ──
async def _due_routine(**kw) -> uuid.UUID:
    async with session_scope() as s:
        r = Routine(name=f"r-{uuid.uuid4().hex[:6]}", instruction="وضعیت را بررسی کن", trigger_type="interval",
                    interval_seconds=60, next_run_at=utcnow() - timedelta(seconds=5), **kw)
        s.add(r)
        await s.flush()
        return r.id


async def test_q7_lease_released_when_active_run_skips_slot(app_client):
    from xagent_worker.scheduler import tick_routines

    rid = await _due_routine()
    async with session_scope() as s:
        s.add(Run(routine_id=rid, trigger="routine", input="x", status="running"))
    await tick_routines()
    leftover = await bus.get_redis().exists(f"xagent:lease:routine:{rid}")
    assert not leftover, "Lease روتین پس از ردکردن نوبت (اجرای فعال) آزاد نشد و تا پایان TTL قفل ماند"


async def test_q7_paused_routine_run_releases_lease_and_is_counted(app_client):
    from xagent_worker.scheduler import after_routine_run, tick_routines

    rid = await _due_routine()
    await tick_routines()
    async with session_scope() as s:
        run = (await s.execute(select(Run).where(Run.routine_id == rid))).scalar_one()
        run.status = "paused"
        run.stop_reason = "steps"
        run_id = run.id
    await after_routine_run(run_id)
    assert not await bus.get_redis().exists(f"xagent:lease:routine:{rid}"), "اجرای متوقف‌شده روتین را قفل نگه داشت"
    async with session_scope() as s:
        r = await s.get(Routine, rid)
        assert r.last_status not in (None, "success"), "نتیجه اجرای نیمه‌کاره ثبت نشد"


# ── سؤال ۵ و ۸ (چرخه حیات): مهلت و سقف دفعات در روتین ثبت شوند ──
async def test_q5_routine_accepts_duration_and_max_runs(app_client):
    r = await app_client.post("/api/routines", json={
        "name": "پایش ده‌دقیقه‌ای", "instruction": "پیام‌ها را بررسی کن", "trigger_type": "interval",
        "interval_seconds": 60, "duration_minutes": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("end_at"), "زمان پایان روتین ثبت نشد؛ روتین برای همیشه اجرا می‌شود"
    assert body.get("max_runs") == 10, "قرارداد شمارش (۱۰ بررسی در بازه ۱۰ دقیقه) ثبت نشد"


# ── سؤال ۴: اجرای فوری جزو نوبت‌هاست و زمان‌بند آن را تکرار نمی‌کند ──
async def test_q4_immediate_run_is_not_duplicated_by_scheduler(app_client):
    from xagent_worker.scheduler import tick_routines

    r = await app_client.post("/api/routines", json={
        "name": "پایش فوری", "instruction": "پیام‌ها را بررسی کن", "trigger_type": "interval",
        "interval_seconds": 60, "duration_minutes": 5, "run_now": True})
    assert r.status_code == 200, r.text
    rid = uuid.UUID(r.json()["id"])
    await tick_routines()
    async with session_scope() as s:
        runs = (await s.execute(select(Run).where(Run.routine_id == rid))).scalars().all()
        routine = await s.get(Routine, rid)
    assert len(runs) == 1, f"اجرای فوری انجام نشد یا تکرار شد: {len(runs)} اجرا"
    assert routine.run_count == 1


# ── سؤال ۶: معنای وضعیت‌ها در داشبورد یکسان باشد ──
async def test_q6_dashboard_separates_paused_from_active(app_client):
    async with session_scope() as s:
        s.add(Run(trigger="chat", input="قدیمی", status="paused", stop_reason="steps"))
    d = (await app_client.get("/api/dashboard")).json()
    summary = d["runs"].get("summary")
    assert summary is not None, "داشبورد تعریف واحدی از کار فعال ارائه نمی‌دهد"
    assert summary["paused"] >= 1
    assert summary["active"] == summary["running"] + summary["queued"], "متوقف/منتظر نباید «فعال» شمرده شوند"


async def test_q6_stale_running_run_is_reconciled(app_client):
    """Worker پس از ری‌استارت سریع (Lease قبلی هنوز زنده) اجرای running را برای همیشه رها می‌کرد."""
    from xagent_worker.scheduler import reconcile_runs

    async with session_scope() as s:
        run = Run(trigger="chat", input="یتیم", status="running")
        s.add(run)
        await s.flush()
        rid = run.id
        run.lease_until = utcnow() - timedelta(minutes=10)
    await reconcile_runs(stale_after_seconds=60)
    async with session_scope() as s:
        assert (await s.get(Run, rid)).status == "queued"


# ── سؤال ۸: نتیجه اجرای پس‌زمینه باید در صندوق کارفرما برسد ──
async def test_q8_routine_result_reaches_owner_inbox(app_client, configured_model, runner):
    from xagent_worker.scheduler import after_routine_run, tick_routines

    rid = await _due_routine()
    await tick_routines()
    async with session_scope() as s:
        run_id = (await s.execute(select(Run.id).where(Run.routine_id == rid))).scalar_one()
    await runner.execute(run_id)
    await after_routine_run(run_id)
    r = await app_client.get("/api/inbox")
    assert r.status_code == 200, "صندوق کارفرما وجود ندارد"
    assert any(i.get("run_id") == str(run_id) for i in r.json()["items"]), "گزارش روتین به صندوق کارفرما نرسید"


# ── سؤال ۹: روتین بدون رویداد جدید نباید مدل را صدا بزند ──
async def test_q9_watch_routine_without_new_events_makes_no_llm_call(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner
    from xagent_worker.scheduler import tick_routines

    r = await app_client.post("/api/routines", json={
        "name": "پایش تلگرام", "instruction": "اگر پیام جدیدی آمد خلاصه‌اش را گزارش کن", "trigger_type": "interval",
        "interval_seconds": 60, "watch": {"source": "telegram"}})
    assert r.status_code == 200, r.text
    rid = uuid.UUID(r.json()["id"])
    async with session_scope() as s:
        (await s.get(Routine, rid)).next_run_at = utcnow() - timedelta(seconds=1)
    await tick_routines()
    async with session_scope() as s:
        run_id = (await s.execute(select(Run.id).where(Run.routine_id == rid))).scalars().first()
    before = len(mock_llm.calls)
    await AgentRunner("t-q9", telegram=FakeTelegram()).execute(run_id)
    async with session_scope() as s:
        run = await s.get(Run, run_id)
    assert run.status == "succeeded", run.error
    assert len(mock_llm.calls) == before, f"بدون رویداد جدید {len(mock_llm.calls) - before} فراخوانی مدل انجام شد"


# ── سؤال ۵: مأموریت مرورگر ساده‌نما با بیش از ۶ تعامل نباید نیمه‌کاره بماند ──
async def test_q5_browser_mission_beyond_six_interactions(app_client, configured_model, runner, mock_llm, monkeypatch):
    from xagent_tools.builtin import computer

    page = FakePage(last_page=8)

    async def fake_page():
        return page

    monkeypatch.setattr(computer, "_page", fake_page)
    marker = "کاتالوگ-آزمایشی-۵"

    def handler(body):
        if marker not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        names = tool_names(body)
        if "browser_open" not in names:
            return ("tools_load", {"groups": ["browser"]})
        clicks = [a for n, a in called(body) if n == "browser_click"]
        if not any(n == "browser_open" for n, _ in called(body)):
            return ("browser_open", {"url": "https://catalog.example/list"})
        if page.page_no < page.last_page:
            return ("browser_click", {"ref": 1, "note": f"page-{len(clicks) + 2}"})
        return "قیمت نهایی محصول هدف ۴۲ دلار است."

    mock_llm.handlers.append(handler)
    try:
        data = await _send(app_client, f"مرورگر را باز کن و قیمت محصول هدف را از {marker} بخوان")
        await runner.execute(uuid.UUID(data["run"]["id"]))
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        run = await s.get(Run, uuid.UUID(data["run"]["id"]))
    assert run.status == "succeeded", f"مأموریت مرورگر در {run.steps_count} گام متوقف شد: {run.stop_reason}"
    assert page.page_no == page.last_page
