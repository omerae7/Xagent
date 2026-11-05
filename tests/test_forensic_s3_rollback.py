"""ممیزی جرم‌شناختی — مرحله ۳، F19: Rollback ابزار نصب‌شده با پیکربندی اجرایی همان نسخه و آمادگی صادقانه.

اتصال MCP با تابع ساختگی شبیه‌سازی می‌شود؛ هیچ ابزار واقعی نصب یا اجرا نمی‌شود.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from xagent_core.db import session_scope
from xagent_core.models import InstalledTool, ToolVersion

V1 = {"transport": "stdio", "command": "node", "args": ["/t/v1/index.js"], "cwd": "/t/v1",
      "env": {"API_TOKEN": "dummy-v1-token-77"}}
V2 = {"transport": "stdio", "command": "/t/v2/.venv/bin/srv", "args": ["--mode", "new"], "cwd": "/t/v2",
      "env": {"API_TOKEN": "dummy-v2-token-88", "NEW_FLAG": "1"}}


async def _tool_with_versions(v1_manifest: dict) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from xagent_core.redact import seal_secrets

    async with session_scope() as s:
        t = InstalledTool(name=f"f19-{uuid.uuid4().hex[:6]}", kind="mcp_stdio", status="ready", enabled=True,
                          config=V2, source_url="https://git.example/t.git")
        s.add(t)
        await s.flush()
        v1 = ToolVersion(tool_id=t.id, version="v1", path="/t/v1", manifest=v1_manifest, test_status="passed")
        s.add(v1)
        await s.flush()
        v2 = ToolVersion(tool_id=t.id, version="v2", path="/t/v2",
                         manifest={"exec_config": seal_secrets(V2), "kind": "mcp_stdio"}, test_status="passed")
        s.add(v2)
        await s.flush()
        v1.created_at = v2.created_at.replace(year=v2.created_at.year - 1)
        t.active_version_id = v2.id
        return t.id, v1.id, v2.id


@pytest.fixture(autouse=True)
async def cleanup_f19_tools():
    yield
    from sqlalchemy import delete

    async with session_scope() as s:  # ابزارهای آزمون دیگر آزمون‌ها (اسکن ابزار mcp__*) را آلوده نکنند
        await s.execute(delete(InstalledTool).where(InstalledTool.name.like("f19-%")))


@pytest.fixture
def mcp_ok(monkeypatch):
    from xagent_tools import installer

    seen: list[dict] = []

    async def list_tools(name, cfg):
        seen.append(cfg)
        return [{"name": "echo", "description": "", "input_schema": {"type": "object"}}]
    monkeypatch.setattr(installer.mcp_client, "list_tools", list_tools)
    return seen


async def test_F19_rollback_restores_version_specific_execution_config(app_client, mcp_ok):
    from xagent_core.redact import seal_secrets
    from xagent_tools import installer

    tid, v1, _ = await _tool_with_versions({"exec_config": seal_secrets(V1), "kind": "mcp_stdio"})
    async with session_scope() as s:
        out = await installer.rollback(s, tid, v1)
        assert out.ok, out.message
        t = await s.get(InstalledTool, tid)
        assert t.config == V1, f"پیکربندی نسخه قبلی از پیکربندی نسخه فعلی ساخته شد: {t.config}"
        assert t.active_version_id == v1 and t.status == "ready"
    assert mcp_ok and mcp_ok[-1]["command"] == "node", "آزمون اتصال با پیکربندی نسخه هدف انجام نشد"


async def test_F19_failed_connection_test_never_reports_ready(app_client, monkeypatch):
    from xagent_core.redact import seal_secrets
    from xagent_tools import installer

    async def broken(name, cfg):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(installer.mcp_client, "list_tools", broken)
    tid, v1, v2 = await _tool_with_versions({"exec_config": seal_secrets(V1), "kind": "mcp_stdio"})
    async with session_scope() as s:
        out = await installer.rollback(s, tid, v1)
        t = await s.get(InstalledTool, tid)
        assert not out.ok, "Rollback با آزمون اتصال ناموفق موفق اعلام شد"
        assert not (t.active_version_id == v1 and t.status == "ready"), "آزمون اتصال ناموفق به «ready» رسید"
        assert t.active_version_id == v2 and t.config == V2, "نسخه فعلی سالم بی‌دلیل کنار گذاشته شد"


async def test_F19_legacy_version_record_is_handled_safely(app_client, mcp_ok):
    from xagent_tools import installer

    tid, v1, _ = await _tool_with_versions({})  # رکورد قدیمی: پیکربندی اجرایی ذخیره نشده
    async with session_scope() as s:
        out = await installer.rollback(s, tid, v1)
        t = await s.get(InstalledTool, tid)
        assert out.ok and t.config["cwd"] == "/t/v1" and "/t/v2" not in json.dumps(t.config)
        assert "legacy" in out.message or "قدیمی" in out.message, "بازسازی تقریبی پیکربندی قدیمی گزارش نشد"


async def test_F19_install_stores_sealed_exec_config_per_version(app_client, mcp_ok, monkeypatch, tmp_path):
    from xagent_tools import installer

    async def fake_detect(path, log):
        return {"kind": "mcp_stdio", "config": {**V1, "cwd": path}, "description": "x", "test": None,
                "harness_category": "external_tool", "manifest": {}}
    monkeypatch.setattr(installer, "_detect_and_install", fake_detect)

    class SB:
        def tool_path(self, name, version):
            return str(tmp_path / name / version)

        async def exec(self, cmd, cwd=None, timeout=60, env=None):
            from types import SimpleNamespace
            return SimpleNamespace(code=0, stdout="abc123def456\n", stderr="", timed_out=False)
    monkeypatch.setattr(installer, "get_sandbox", lambda: SB())

    async def ok_run(log, cmd, cwd=None, timeout=120):
        return True
    monkeypatch.setattr(installer, "_run", ok_run)
    async with session_scope() as s:
        out = await installer.install(s, "https://github.com/example/f19", name=f"f19-install-{uuid.uuid4().hex[:4]}",
                                      env={"API_TOKEN": "dummy-install-token-99"})
        assert out.ok, out.message
        tv = (await s.execute(select(ToolVersion).where(ToolVersion.tool_id == out.tool_id))).scalar_one()
    assert "exec_config" in tv.manifest and "dummy-install-token-99" not in json.dumps(tv.manifest)
