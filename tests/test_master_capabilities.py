"""مأموریت جامع — مرحله ۶: آمادگی واقعی قابلیت‌ها (A7)، کشف و اجرای Python در Sandbox (F1/F2)،
مسیریابی بر اساس کیفیت انجام تأییدشده (ریسک ۱۵).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from autonomy_helpers import body_text, called, tool_names
from sqlalchemy import select

from xagent_core.db import session_scope, utcnow
from xagent_core.models import AIModel, AIProvider, Employee, InstalledTool, Run, RunStep, UsageEvent
from xagent_core.security import encrypt_secret

pytestmark = pytest.mark.asyncio


def _keys(manifest: dict, state: str) -> set[str]:
    return {c["key"] for c in manifest[state]}


# ── A7: نام نوع Sandbox دلیل آمادگی نیست ──
async def test_A7_desktop_without_cdp_is_not_ready(app_client, monkeypatch):
    from xagent_agent import manifest as mf

    mf.invalidate_probes()

    async def down():
        return {"ok": False}

    async def unknown():
        raise TimeoutError("probe")
    monkeypatch.setattr(mf, "probe_browser", down)
    monkeypatch.setattr(mf, "probe_desktop", unknown)
    async with session_scope() as s:
        m = await mf.build_manifest(s, sandbox_kind="desktop")
    assert "browser" not in _keys(m, "ready") and "browser" in _keys(m, "needs_setup")
    assert "desktop" in _keys(m, "unverified"), "نتیجه نامعلوم کاوش «آماده» گزارش شد"
    mf.invalidate_probes()
    async with session_scope() as s:
        runner = await mf.build_manifest(s, sandbox_kind="runner")
    assert "browser" in _keys(runner, "unsupported") and "browser" not in _keys(runner, "ready")
    mf.invalidate_probes()

    async def up():
        return {"ok": True}
    monkeypatch.setattr(mf, "probe_browser", up)
    monkeypatch.setattr(mf, "probe_desktop", up)
    async with session_scope() as s:
        ok = await mf.build_manifest(s, sandbox_kind="desktop")
    assert {"browser", "desktop"} <= _keys(ok, "ready")
    mf.invalidate_probes()


async def test_A7_disabled_provider_tool_and_employee_restrictions(app_client, monkeypatch):
    from xagent_agent import manifest as mf

    mf.invalidate_probes()

    async def up():
        return {"ok": True}
    monkeypatch.setattr(mf, "probe_browser", up)
    monkeypatch.setattr(mf, "probe_desktop", up)
    tag = uuid.uuid4().hex[:6]
    async with session_scope() as s:
        # پاک‌سازی: فقط مدل‌های ارائه‌دهنده غیرفعال این تست وجود داشته باشد تا اثرش دیده شود
        prov = AIProvider(name=f"off-{tag}", kind="custom", base_url="http://127.0.0.1:1/v1",
                          api_key_enc=encrypt_secret("x"), enabled=False)
        s.add(prov)
        await s.flush()
        s.add(AIModel(provider_id=prov.id, model_name=f"vision-only-{tag}", enabled=True,
                      capabilities={"vision": True, "tts": True}))
        s.add(InstalledTool(name=f"tool-{tag}", kind="mcp_stdio", source_url="test", enabled=False, status="ready",
                            config={}, tool_schemas=[]))
        emp = Employee(name=f"e-{tag}", title="پژوهشگر", tool_groups=["web"], enabled=True)
        s.add(emp)
        await s.flush()
        owner = await mf.build_manifest(s, sandbox_kind="desktop")
        employee = await mf.build_manifest(s, sandbox_kind="desktop", employee=emp)
        external = await mf.build_manifest(s, principal="external", sandbox_kind="desktop")
    assert "tts" not in _keys(owner, "ready"), "مدل ارائه‌دهنده غیرفعال آماده گزارش شد"
    detail = next((c["detail"] for c in owner["ready"] if c["key"] == "installed"), "")
    assert f"tool-{tag}" not in detail
    assert "browser" in _keys(employee, "unsupported") and "admin" in _keys(employee, "unsupported")
    assert "admin" in _keys(external, "unsupported") and "host" not in _keys(external, "ready")
    mf.invalidate_probes()


# ── F1/F2: Python در Sandbox — کشف، اجرا (با اجازه) و رد (ممنوع) ──
async def _python_run(app_client, mock_llm, mark: str, text: str):
    from xagent_agent.pipeline import AgentRunner

    seen: list[set[str]] = []

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        seen.append(tool_names(body))
        if "shell_exec" in tool_names(body) and not any(n == "shell_exec" for n, _ in called(body)):
            return ("shell_exec", {"command": "python3 -c \"print(sum([3,5,10])/3)\"", "timeout": 30})
        return "میانگین ۶ است."
    r = await app_client.post("/api/chat/send", json={"text": f"{text} ({mark})"})
    rid = uuid.UUID(r.json()["run"]["id"])
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("py").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        run = await s.get(Run, rid)
        steps = (await s.execute(select(RunStep).where(RunStep.run_id == rid, RunStep.tool_name == "shell_exec",
                                                       RunStep.kind == "tool"))).scalars().all()
    return run, steps, seen


async def test_F1_persian_python_request_is_discovered_and_executed(app_client, configured_model, mock_llm):
    assert (await app_client.put("/api/settings/harness/code_exec", json={"mode": "auto"})).status_code == 200
    try:
        run, steps, seen = await _python_run(app_client, mock_llm, "F1-PY",
                                             "با پایتون میانگین این اعداد را حساب کن: 3, 5, 10")
    finally:
        await app_client.put("/api/settings/harness/code_exec", json={"mode": "ask"})
    assert seen and "shell_exec" in seen[0], "ابزار اجرای Python به مدل ارائه نشد"
    assert steps and steps[0].status == "ok" and "6.0" in (steps[0].detail or {}).get("output", "")
    assert run.status == "succeeded"


async def test_F2_python_denied_by_harness_does_not_execute(app_client, configured_model, mock_llm):
    assert (await app_client.put("/api/settings/harness/code_exec", json={"mode": "forbidden"})).status_code == 200
    try:
        run, steps, _ = await _python_run(app_client, mock_llm, "F2-PY", "run a python script to compute the mean of 3, 5, 10")
    finally:
        await app_client.put("/api/settings/harness/code_exec", json={"mode": "ask"})
    assert not any(st.status == "ok" for st in steps), "اجرای Python ممنوع انجام شد"
    assert run.status != "succeeded"


# ── ریسک ۱۵: موفقیت HTTP جای کیفیت انجام کار تأییدشده را نمی‌گیرد ──
async def test_router_prefers_verified_task_success_over_transport_success(app_client):
    from xagent_agent.providers.registry import load_model
    from xagent_agent.router import TaskProfile, route

    tag = uuid.uuid4().hex[:6]
    async with session_scope() as s:
        prov = AIProvider(name=f"q-{tag}", kind="custom", base_url="http://127.0.0.1:1/v1",
                          api_key_enc=encrypt_secret("x"), enabled=True)
        s.add(prov)
        await s.flush()
        bad = AIModel(provider_id=prov.id, model_name=f"chatty-{tag}", enabled=True, capabilities={"tools": True},
                      price_in=Decimal("0.1"), price_out=Decimal("0.4"), pricing_source="manual")
        good = AIModel(provider_id=prov.id, model_name=f"worker-{tag}", enabled=True, capabilities={"tools": True},
                       price_in=Decimal("0.1"), price_out=Decimal("0.4"), pricing_source="manual")
        s.add_all([bad, good])
        await s.flush()
        now = utcnow()
        for i in range(8):
            for model, status in ((bad, "failed"), (good, "succeeded")):
                run = Run(trigger="api", input="x", status=status, state={})
                s.add(run)
                await s.flush()
                s.add(UsageEvent(ts=now - timedelta(minutes=i), model_id=model.id, model_name=model.model_name,
                                 provider_kind="custom", purpose="execution", run_id=run.id, tokens_in=100,
                                 tokens_out=20, cost_usd=Decimal("0.0001"), success=True, attempt_status="confirmed"))
        await s.flush()
        pool = [await load_model(s, bad.id), await load_model(s, good.id)]
        ordered, decision = await route(s, pool, "balanced", TaskProfile(complexity="complex"))
    assert ordered[0].model_name == f"worker-{tag}", decision
    assert any("نرخ انجام تأییدشده" in n for c in decision["candidates"] for n in c["notes"])
