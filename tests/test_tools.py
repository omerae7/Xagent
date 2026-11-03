"""تست نصب واقعی ابزار MCP از مخزن git، فراخوانی آن توسط ایجنت با مجوز Harness، به‌روزرسانی و Rollback،
و زمان‌بند روتین‌ها/صندوق رویداد (رویدادمحور بدون LLM)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import EventInbox, InstalledTool, Routine, Run, ToolVersion
from xagent_tools import installer
from xagent_tools.registry import installed_tools

SERVER = textwrap.dedent('''
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("echo")

    @server.tool()
    def echo(text: str) -> str:
        """متن را برمی‌گرداند"""
        return "ECHO:" + text

    if __name__ == "__main__":
        server.run("stdio")
''')


def _make_repo(version: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="mcp-echo-"))
    (d / "server.py").write_text(SERVER.replace("ECHO:", f"ECHO{version}:"), encoding="utf-8")
    (d / "xagent-tool.json").write_text(json.dumps({
        "name": "echo", "description": "ابزار آزمایشی", "kind": "mcp_stdio",
        "command": sys.executable, "args": ["server.py"],
    }), encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run([*git, "-C", str(d), "add", "."], check=True)
    subprocess.run([*git, "-C", str(d), "commit", "-qm", "v1"], check=True)
    return d


def _commit(d: Path, version: str) -> None:
    (d / "server.py").write_text(SERVER.replace("ECHO:", f"ECHO{version}:"), encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "-C", str(d), "commit", "-qam", version], check=True)


async def test_mcp_install_call_update_rollback(app_client, configured_model, runner):
    repo = _make_repo("1")
    async with session_scope() as s:
        out = await installer.install(s, str(repo), name="echo", actor="owner")
        assert out.ok, out.log
        tool = await s.get(InstalledTool, out.tool_id)
        assert tool.status == "ready" and [t["name"] for t in tool.tool_schemas] == ["echo"]
        names = [t.name for t in await installed_tools(s)]
        assert "mcp__echo__echo" in names
        # ایجنت اجازه نصب از مسیر محلی ندارد
        denied = await installer.install(s, str(repo), name="x2", actor="agent")
        assert not denied.ok

    # فراخوانی توسط ایجنت: ابزار خارجی طبق Harness نیازمند اجازه است
    r = await app_client.post("/api/chat/send", json={"text": "با ابزار اکو بنویس درود"})
    run_id = uuid.UUID(r.json()["run"]["id"])
    await runner.execute(run_id)
    pend = [a for a in (await app_client.get("/api/approvals")).json()["items"] if a["run_id"] == str(run_id)]
    assert pend and pend[0]["category"] == "external_tool"
    await app_client.post(f"/api/approvals/{pend[0]['id']}", json={"decision": "approve"})
    await runner.execute(run_id)
    async with session_scope() as s:
        run = await s.get(Run, run_id)
        assert run.status == "succeeded", run.error
        assert "ECHO1:درود" in run.result

    # به‌روزرسانی به نسخه ۲ و سپس Rollback به نسخه ۱
    _commit(repo, "2")
    async with session_scope() as s:
        tool = (await s.execute(select(InstalledTool).where(InstalledTool.name == "echo"))).scalar_one()
        up = await installer.update(s, tool.id)
        assert up.ok, up.log
        versions = (await s.execute(select(ToolVersion).where(ToolVersion.tool_id == tool.id))).scalars().all()
        assert len(versions) == 2
        rb = await installer.rollback(s, tool.id)
        assert rb.ok, rb.message
        await s.refresh(tool)
        active = await s.get(ToolVersion, tool.active_version_id)
        from xagent_tools import mcp_client

        ok, text = await mcp_client.call_tool("echo", tool.config, "echo", {"text": "x"})
        assert ok and text.startswith("ECHO1:"), (active.version, text)


async def test_routine_scheduler_lease_and_circuit_breaker(app_client, configured_model):
    from xagent_worker.scheduler import after_routine_run, tick_routines

    r = await app_client.post("/api/routines", json={
        "name": "گزارش ساعتی", "instruction": "وضعیت تسک‌ها را خلاصه کن", "trigger_type": "interval",
        "interval_seconds": 3600})
    assert r.status_code == 200, r.text
    rid = uuid.UUID(r.json()["id"])
    async with session_scope() as s:
        routine = await s.get(Routine, rid)
        routine.next_run_at = utcnow() - timedelta(seconds=5)
    assert await tick_routines() >= 1
    # تیک دوم: روتین هنوز فعال است/قفل دارد → اجرای تکراری ساخته نمی‌شود
    async with session_scope() as s:
        routine = await s.get(Routine, rid)
        routine.next_run_at = utcnow() - timedelta(seconds=5)
    await tick_routines()
    async with session_scope() as s:
        runs = (await s.execute(select(Run).where(Run.routine_id == rid))).scalars().all()
        assert len(runs) == 1
        run_id = runs[0].id
    # سه خطای پیاپی → مدارقطع‌کن باز می‌شود
    for _ in range(3):
        async with session_scope() as s:
            run = await s.get(Run, run_id)
            run.status = "failed"
        await after_routine_run(run_id)
    async with session_scope() as s:
        routine = await s.get(Routine, rid)
        assert routine.circuit_state == "open" and routine.circuit_open_until is not None
    resumed = await app_client.post(f"/api/routines/{rid}/resume")
    assert resumed.json()["circuit_state"] == "closed"


async def test_event_driven_inbox_no_llm(app_client, configured_model, mock_llm):
    from xagent_worker.inbox import process_event

    r = await app_client.post("/api/routines", json={
        "name": "پاسخ به سفارش", "instruction": "سفارش جدید را در CRM ثبت کن", "trigger_type": "event",
        "event_filter": {"source": "telegram", "keywords": ["سفارش"]}})
    assert r.status_code == 200, r.text
    before = len(mock_llm.calls)
    async with session_scope() as s:
        e1 = EventInbox(source="telegram", kind="message", dedup_key=f"t:{uuid.uuid4()}",
                        payload={"text": "سلام، خوبی؟", "chat_id": 1, "is_private": True})
        e2 = EventInbox(source="telegram", kind="message", dedup_key=f"t:{uuid.uuid4()}",
                        payload={"text": "یک سفارش جدید دارم", "chat_id": 2, "is_private": True})
        s.add_all([e1, e2])
        await s.flush()
        ids = (e1.id, e2.id)
    assert await process_event(ids[0], None) == "ignored"
    assert await process_event(ids[1], None) == "routed"
    assert len(mock_llm.calls) == before, "پردازش رویداد نباید LLM را صدا بزند"
    async with session_scope() as s:
        run = (await s.execute(select(Run).where(Run.trigger == "event").order_by(Run.created_at.desc()))).scalars().first()
        assert run.state["trusted"] is False and run.state["event_payload"]["text"] == "یک سفارش جدید دارم"
    await bus.get_redis().delete("xagent:stop_all")


async def test_web_search_tools_available_without_desktop_or_routing(database):
    """Plain chat and follow-up questions should always receive browser-free web tools."""
    from xagent_tools.registry import select_tools
    async with session_scope() as session:
        chosen = await select_tools(session, [], "خودت بگرد")
        names = {tool.name for tool in chosen}
        assert "web_search" in names
        assert "web_fetch" in names
        # Arbitrary API mutation calls must not be enabled implicitly.
        assert "http_request" not in names
