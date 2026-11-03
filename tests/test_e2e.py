"""تست انتهابه‌انتها: ورود کارفرما ← پیام چت ← مسیریابی/هدف‌سنجی ← Harness ← ابزار ← اعتبارسنجی ←
ثبت در حافظه و لاگ مصرف. Worker با AgentRunner واقعی شبیه‌سازی می‌شود؛ فقط مدل، شبیه‌ساز است."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from xagent_core import bus
from xagent_core.db import session_scope
from xagent_core.models import Approval, Episode, Experience, Run, RunStep, Task, UsageEvent


async def _send(client, text: str, conversation_id: str | None = None) -> dict:
    r = await client.post("/api/chat/send", json={"text": text, "conversation_id": conversation_id})
    assert r.status_code == 200, r.text
    return r.json()


async def _run(runner, run_id: str) -> Run:
    await runner.execute(uuid.UUID(run_id))
    async with session_scope() as s:
        return await s.get(Run, uuid.UUID(run_id))


async def test_auth_required(app_client):
    import httpx

    from xagent_api.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/api/dashboard")).status_code == 401
        bad = await anon.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
        assert bad.status_code == 401
    # CSRF: کوکی بدون هدر سفارشی رد می‌شود
    r = await app_client.post("/api/conversations", json={}, headers={"X-XAgent": ""})
    assert r.status_code == 403


async def test_deterministic_command_without_llm(app_client, configured_model, mock_llm):
    before = len(mock_llm.calls)
    data = await _send(app_client, "/status")
    assert data["run"]["status"] == "succeeded"
    assert "وضعیت X Agent" in data["reply"]["content"]
    assert len(mock_llm.calls) == before, "فرمان قطعی نباید مدل را صدا بزند"


async def test_simple_chat_worker_path(app_client, configured_model, runner, mock_llm):
    data = await _send(app_client, "سلام")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    assert run.route["complexity"] == "trivial"
    assert run.tokens_in > 0 and run.tokens_out > 0 and float(run.cost_usd) > 0
    r = await app_client.get(f"/api/conversations/{data['conversation']['id']}/messages")
    msgs = r.json()["items"]
    assert msgs[-1]["role"] == "assistant" and "X Agent" in msgs[-1]["content"]


async def test_full_cycle_task_creation(app_client, configured_model, runner):
    """چرخه کامل: پیام ← مسیریابی ← هدف ← برنامه ← حافظه ← ابزار (با Harness خودکار) ← اعتبارسنجی ← یادگیری."""
    data = await _send(app_client, "لطفاً یک تسک مهم برای تماس با مشتری بساز، بررسی کن و بعد گزارش بده")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    assert run.route["path"] == "manager"
    assert run.goal.get("goal")
    async with session_scope() as s:
        kinds = [k for (k,) in (await s.execute(select(RunStep.kind).where(RunStep.run_id == run.id)
                                                .order_by(RunStep.idx))).all()]
        for k in ("route", "goal", "plan", "memory", "llm", "tool", "validate", "answer"):
            assert k in kinds, f"گام {k} ثبت نشده: {kinds}"
        task = (await s.execute(select(Task).where(Task.title == "تماس با مشتری"))).scalars().first()
        assert task is not None and task.priority == "high" and task.due_at is not None
        usage = (await s.execute(select(func.count(), func.sum(UsageEvent.tokens_in), func.sum(UsageEvent.cache_read))
                                 .where(UsageEvent.run_id == run.id))).one()
        assert usage[0] >= 3 and usage[1] > 0 and usage[2] > 0
        ep = (await s.execute(select(Episode).where(Episode.run_id == run.id))).scalar_one()
        assert ep.status == "success" and "task_create" in ep.tools
        exp = (await s.execute(select(Experience).where(Experience.tool_sequence.any("task_create")))).scalars().first()
        assert exp is not None and exp.outcome == "verified_success"
    dash = (await app_client.get("/api/dashboard")).json()
    assert dash["usage"]["today"]["tokens_in"] > 0
    assert "cpu" in dash["system"]


async def test_harness_approval_flow(app_client, configured_model, runner):
    data = await _send(app_client, "به علی یک پیام سلام در تلگرام بفرست")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "waiting_approval"
    pending = (await app_client.get("/api/approvals")).json()["items"]
    ap = next(a for a in pending if a["run_id"] == str(run.id))
    assert ap["category"] == "external_message" and ap["tool_name"] == "telegram_send"
    r = await app_client.post(f"/api/approvals/{ap['id']}", json={"decision": "approve"})
    assert r.status_code == 200
    run = await _run(runner, str(run.id))
    # تلگرام در محیط تست متصل نیست → ابزار صادقانه خطا می‌دهد و ایجنت گزارش می‌کند
    assert run.status == "succeeded"
    async with session_scope() as s:
        a = await s.get(Approval, uuid.UUID(ap["id"]))
        assert a.status == "approved"
        tool_step = (await s.execute(select(RunStep).where(RunStep.run_id == run.id, RunStep.kind == "tool"))).scalars().first()
        assert tool_step.status == "error"


async def test_harness_forbidden_blocks(app_client, configured_model, runner):
    r = await app_client.put("/api/settings/harness/external_message", json={"mode": "forbidden", "rule_text": "هرگز"})
    assert r.status_code == 200
    data = await _send(app_client, "به رضا پیام بده که جلسه لغو شد")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded"
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(Approval).where(Approval.run_id == run.id))).scalar_one()
        assert n == 0
    await app_client.put("/api/settings/harness/external_message", json={"mode": "ask"})
    # پرداخت هرگز نمی‌تواند خودکار شود
    r = await app_client.put("/api/settings/harness/payment", json={"mode": "auto"})
    assert r.status_code == 422


async def test_file_tool_verified(app_client, configured_model, runner):
    data = await _send(app_client, "یک فایل یادداشت بساز")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    from xagent_core.config import get_settings

    assert (get_settings().workspace_dir / "notes/a.txt").read_text(encoding="utf-8") == "سلام دنیا"


async def test_loop_controller_stops_blind_repetition(app_client, configured_model, runner):
    data = await _send(app_client, "این حلقه را روی سایت تست کن")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "failed" and "کنترلر حلقه" in (run.result or "")
    async with session_scope() as s:
        kinds = [k for (k,) in (await s.execute(select(RunStep.kind).where(RunStep.run_id == run.id)))]
        assert kinds.count("tool") <= 4, "کنترلر حلقه باید تکرار کور را متوقف کند"
        assert "recover" in kinds, "باید پیش از توقف، استراتژی تغییر کند/مدیر وارد شود"


async def test_stoploss_pauses_and_resume(app_client, configured_model, runner):
    s = (await app_client.get("/api/settings")).json()["settings"]["stoploss"]
    s["max_steps_simple"] = 1
    await app_client.put("/api/settings/stoploss", json=s)
    data = await _send(app_client, "یک فایل دیگر هم بساز")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "paused" and run.stop_reason == "steps"
    s["max_steps_simple"] = 6
    await app_client.put("/api/settings/stoploss", json=s)
    r = await app_client.post(f"/api/runs/{run.id}/resume", json={"extra_steps": 5})
    assert r.status_code == 200
    run = await _run(runner, str(run.id))
    assert run.status == "succeeded", run.error


async def test_stop_all_and_resume(app_client, configured_model, runner):
    await _send(app_client, "توقف همه")
    assert await bus.is_stop_all()
    data = await _send(app_client, "سلام دوباره")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "paused" and run.stop_reason == "stop_all"
    await _send(app_client, "ادامه همه")
    assert not await bus.is_stop_all()
    run = await _run(runner, str(run.id))
    assert run.status == "succeeded"


async def test_undo_center_api(app_client, configured_model):
    r = await app_client.post("/api/memory/rules", json={"content": "همیشه با لحن رسمی بنویس", "priority": 70})
    assert r.status_code == 200
    revs = (await app_client.get("/api/memory/revisions")).json()["items"]
    rev = next(x for x in revs if x["entity_id"] == r.json()["id"])
    u = await app_client.post(f"/api/memory/revisions/{rev['id']}/undo", json={})
    assert u.status_code == 200 and u.json()["reverted"]
    rules = (await app_client.get("/api/memory/rules")).json()["items"]
    assert all(x["id"] != r.json()["id"] for x in rules)


async def test_usage_and_tasks_endpoints(app_client, configured_model):
    u = (await app_client.get("/api/usage?days=7")).json()
    assert u["daily"] and u["by_model"][0]["tokens_in"] > 0
    t = await app_client.post("/api/tasks", json={"title": "بررسی قرارداد", "priority": "urgent"})
    assert t.status_code == 200
    tid = t.json()["id"]
    assert (await app_client.patch(f"/api/tasks/{tid}", json={"status": "done"})).json()["completed_at"]
    m = (await app_client.get("/api/system/metrics")).json()
    assert 0 <= m["current"]["cpu"] <= 100
