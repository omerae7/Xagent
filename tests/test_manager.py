"""تست مدیر خودگردان: تغییر واقعی تنظیمات با فرمان زبان طبیعی، سیاست امنیتی، کارت اجازه، Rollback،
کشف و آزمون مدل، Fallback در شکست API، خوددرمانی، کارمند و تفویض، و فراخوانی چندمرحله‌ای ابزار."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from xagent_core.db import session_scope
from xagent_core.models import AIModel, ConfigChange, Employee, Run, RunStep, SettingKV, Task


async def _send(client, text: str) -> dict:
    r = await client.post("/api/chat/send", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


async def _run(runner, run_id: str) -> Run:
    await runner.execute(uuid.UUID(run_id))
    async with session_scope() as s:
        return await s.get(Run, uuid.UUID(run_id))


async def _tools_called(run_id) -> list[str]:
    async with session_scope() as s:
        return [n for (n,) in (await s.execute(select(RunStep.tool_name).where(
            RunStep.run_id == run_id, RunStep.kind == "tool").order_by(RunStep.idx))).all() if n]


async def test_manager_changes_setting_persistently_and_rolls_back(app_client, configured_model, runner, mock_llm):
    mock_llm.scenarios["نام خودت را عوض کن"] = [
        ("settings_update", {"section": "general", "values": {"agent_name": "آلفا"}, "reason": "درخواست کارفرما"})]
    data = await _send(app_client, "نام خودت را عوض کن به آلفا")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    assert await _tools_called(run.id) == ["settings_update"]
    async with session_scope() as s:
        assert (await s.get(SettingKV, "general")).value["agent_name"] == "آلفا"
        ch = (await s.execute(select(ConfigChange).where(ConfigChange.run_id == run.id))).scalar_one()
        assert ch.actor == "manager" and ch.before["agent_name"] != "آلفا"
    r = await app_client.post(f"/api/control/changes/{ch.id}/rollback")
    assert r.status_code == 200, r.text
    async with session_scope() as s:
        assert (await s.get(SettingKV, "general")).value["agent_name"] == ch.before["agent_name"]


async def test_manager_cannot_touch_security(app_client, configured_model, runner, mock_llm):
    mock_llm.scenarios["فهرست مجاز تلگرام را باز کن"] = [
        ("settings_update", {"section": "telegram", "values": {"allowlist": ["@anyone"]}, "reason": "x"})]
    data = await _send(app_client, "فهرست مجاز تلگرام را باز کن")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "failed" and run.stop_reason == "validation"
    async with session_scope() as s:
        step = (await s.execute(select(RunStep).where(RunStep.run_id == run.id, RunStep.kind == "tool", RunStep.tool_name == "settings_update"))).scalar_one()
        assert step.status == "error" and "ممنوع" in step.detail["output"]
        tg = await s.get(SettingKV, "telegram")
        assert "@anyone" not in (tg.value.get("allowlist") or [])


async def test_loosening_limits_requires_owner_approval(app_client, configured_model, runner, mock_llm):
    mock_llm.scenarios["سقف هزینه را بیشتر کن"] = [
        ("settings_update", {"section": "stoploss", "values": {"max_cost_per_task_usd": 3.0}, "reason": "کار سنگین"})]
    data = await _send(app_client, "سقف هزینه را بیشتر کن")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "waiting_approval"
    pend = [a for a in (await app_client.get("/api/approvals")).json()["items"] if a["run_id"] == str(run.id)]
    assert pend and pend[0]["category"] == "security_limits"
    async with session_scope() as s:
        assert (await s.get(SettingKV, "stoploss")).value["max_cost_per_task_usd"] != 3.0
    await app_client.post(f"/api/approvals/{pend[0]['id']}", json={"decision": "approve"})
    run = await _run(runner, str(run.id))
    assert run.status == "succeeded", run.error
    async with session_scope() as s:
        assert (await s.get(SettingKV, "stoploss")).value["max_cost_per_task_usd"] == 3.0
    sl = (await app_client.get("/api/settings")).json()["settings"]["stoploss"]
    sl["max_cost_per_task_usd"] = 0.5
    await app_client.put("/api/settings/stoploss", json=sl)


async def test_discovery_probe_and_auto_assignment(app_client, configured_model, runner, mock_llm):
    providers = (await app_client.get("/api/settings/ai/providers")).json()["providers"]
    pid = providers[0]["id"]
    r = await app_client.post(f"/api/control/providers/{pid}/sync")
    assert r.status_code == 200, r.text
    assert "mock-discovered-1" in r.json()["created"]
    async with session_scope() as s:
        disc = (await s.execute(select(AIModel).where(AIModel.model_name == "mock-discovered-1"))).scalar_one()
        emb = (await s.execute(select(AIModel).where(AIModel.model_name == "text-embedding-mock"))).scalar_one()
        assert disc.source == "discovered" and not disc.enabled and disc.pricing_source == "unknown"
        assert emb.capabilities.get("embedding") is True and emb.capability_meta["embedding"]["source"] == "heuristic"
    # مدیر با فرمان طبیعی مدل را آزمون، فعال و در زنجیره کارمند قرار می‌دهد
    mock_llm.scenarios["مدل جدید را آزمون کن"] = [
        ("model_probe", {"model": "mock-discovered-1", "reason": "ارزیابی"}),
        ("routing_set", {"worker": ["mock-discovered-1", "mock-model"], "reason": "مدل آزمون‌شده"}),
    ]
    data = await _send(app_client, "مدل جدید را آزمون کن و برای کارمند تنظیم کن")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    assert await _tools_called(run.id) == ["model_probe", "routing_set"]
    async with session_scope() as s:
        disc = (await s.execute(select(AIModel).where(AIModel.model_name == "mock-discovered-1"))).scalar_one()
        assert disc.enabled and disc.capabilities["tools"] is True and disc.capabilities["vision"] is True
        assert disc.capability_meta["tools"]["source"] == "probe"
        ai = (await s.get(SettingKV, "ai")).value
        assert ai["smart"]["worker_model_ids"][0] == str(disc.id)


async def test_api_failure_fallback_and_self_heal(app_client, configured_model, runner, mock_llm):
    r = await app_client.post("/api/settings/ai/providers", json={
        "kind": "custom", "name": "Broken", "base_url": mock_llm.base_url.replace("/v1", "/fail/v1"), "api_key": "sk-bad-12345"})
    pid = r.json()["id"]
    m = (await app_client.post("/api/settings/ai/models", json={
        "provider_id": pid, "model_name": "broken-model", "capabilities": {"tools": True}})).json()
    ai = (await app_client.get("/api/settings")).json()["settings"]["ai"]
    good = ai["smart"]["manager_model_ids"][-1]
    ai["smart"]["manager_model_ids"] = [m["id"], good]
    ai["smart"]["manager_enabled"] = [True, True]
    ai["smart"]["worker_model_ids"] = [m["id"], configured_model]
    ai["smart"]["worker_enabled"] = [True, True]
    assert (await app_client.put("/api/settings/ai", json=ai)).status_code == 200
    data = await _send(app_client, "سلام بعد از خرابی")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    async with session_scope() as s:
        bm = await s.get(AIModel, uuid.UUID(m["id"]))
        assert bm.health == "failing" and bm.last_error_class == "auth" and bm.cooldown_until is not None
    diag = (await app_client.post("/api/control/diagnose")).json()
    assert any(f["class"] == "auth" for f in diag["findings"])
    rep = (await app_client.post("/api/control/heal")).json()
    assert rep["applied"], rep
    async with session_scope() as s:
        ai2 = (await s.get(SettingKV, "ai")).value
        assert ai2["smart"]["worker_model_ids"][0] != m["id"] and ai2["smart"]["manager_model_ids"][0] != m["id"]
        ch = (await s.execute(select(ConfigChange).where(ConfigChange.actor == "self_heal"))).scalars().first()
        assert ch is not None and ch.status == "verified"


async def test_employee_delegation_end_to_end(app_client, configured_model, runner, mock_llm):
    mock_llm.scenarios["یک کارمند پیگیری بساز"] = [
        ("employee_upsert", {"name": "followup", "title": "مسئول پیگیری", "instructions": "پیگیری مشتریان",
                             "tool_groups": ["tasks"], "models": ["mock-model"], "reason": "نیاز کارفرما"}),
        ("delegate_task", {"employee": "followup", "task": "وظیفه ثبت پیگیری: یک تسک برای تماس با مشتری بساز",
                           "expected_output": "شناسه تسک"}),
    ]
    data = await _send(app_client, "یک کارمند پیگیری بساز و کار ثبت پیگیری را به او بسپار")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    assert await _tools_called(run.id) == ["employee_upsert", "delegate_task"]
    async with session_scope() as s:
        emp = (await s.execute(select(Employee).where(Employee.name == "followup"))).scalar_one()
        child = (await s.execute(select(Run).where(Run.parent_run_id == run.id))).scalar_one()
        assert child.employee_id == emp.id and child.status == "succeeded"
        assert emp.stats["runs"] == 1 and emp.stats["success"] == 1
        assert "task_create" in await _tools_called(child.id)
        assert (await s.execute(select(Task).where(Task.title == "تماس با مشتری"))).scalars().first() is not None


async def test_multi_step_tool_calling(app_client, configured_model, runner, mock_llm):
    mock_llm.scenarios["گزارش چندمرحله‌ای"] = [
        ("task_create", {"title": "گام اول"}),
        ("file_write", {"path": "multi/report.txt", "content": "گزارش"}),
        ("task_list", {}),
    ]
    data = await _send(app_client, "یک گزارش چندمرحله‌ای با تسک و فایل بساز")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    assert await _tools_called(run.id) == ["task_create", "file_write", "task_list"]


async def test_overview_tells_agent_its_capabilities(app_client, configured_model, runner, mock_llm):
    mock_llm.scenarios["وضعیت خودت را بررسی کن"] = [("system_overview", {"section": "all"})]
    data = await _send(app_client, "وضعیت خودت را بررسی کن")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded"
    async with session_scope() as s:
        step = (await s.execute(select(RunStep).where(RunStep.run_id == run.id, RunStep.kind == "tool", RunStep.tool_name == "system_overview"))).scalar_one()
        assert "sk-" not in step.detail["output"]
        # خروجی کامل ابزار (لاگ مرحله تا ۱۲۰۰ نویسه کوتاه می‌شود و با داده بیشتر بخش‌های بعدی را نشان نمی‌دهد)
        out = next(m["content"] for m in (await s.get(type(run), run.id)).state["transcript"]
                   if m.get("role") == "tool" and m.get("tool_name") == "system_overview")
        assert "زنجیره مدیر" in out and "ابزارهای داخلی" in out and "sk-" not in out
    # سیستم‌پرامپت شامل نقشه توانایی‌هاست
    sys_msgs = [c for c in mock_llm.calls if isinstance(c, dict) and c.get("messages")]
    assert any("خودشناسی و توانایی‌ها" in (c["messages"][0].get("content") or "") for c in sys_msgs)


async def test_unverified_tool_capability_is_auto_probed(app_client, configured_model, runner, mock_llm):
    pid = (await app_client.get("/api/settings/ai/providers")).json()["providers"][0]["id"]
    m = (await app_client.post("/api/settings/ai/models", json={"provider_id": pid, "model_name": "mock-unprobed"})).json()
    assert m["capabilities"] == {}
    ai = (await app_client.get("/api/settings")).json()["settings"]["ai"]
    ai["smart"]["worker_model_ids"] = [m["id"]]
    ai["smart"]["worker_enabled"] = [True]
    await app_client.put("/api/settings/ai", json=ai)
    data = await _send(app_client, "یک فایل یادداشت بساز")
    run = await _run(runner, data["run"]["id"])
    assert run.status == "succeeded", run.error
    async with session_scope() as s:
        mm = await s.get(AIModel, uuid.UUID(m["id"]))
        assert mm.capabilities.get("tools") is True and mm.capability_meta["tools"]["source"] == "probe"
    ai["smart"]["worker_model_ids"] = [configured_model]
    await app_client.put("/api/settings/ai", json=ai)
