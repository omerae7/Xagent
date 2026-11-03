"""تست دیمن کامپیوتر ایجنت: احراز توکن، اجرای فرمان ایزوله، Takeover و پل MCP (stdio ← HTTP)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from test_tools import _make_repo

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def agentd(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    home = tmp_path_factory.mktemp("agent-home")
    env = {**os.environ, "DESKTOP_TOKEN": "tok-123", "AGENTD_PORT": str(port), "HOME": str(home)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "infra/desktop/agentd.py")], env=env)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def runnerd(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    home = tmp_path_factory.mktemp("runner-home")
    env = {**os.environ, "DESKTOP_TOKEN": "tok-123", "AGENTD_PORT": str(port),
           "HOME": str(home), "RUNNER_MODE": "1"}
    proc = subprocess.Popen([sys.executable, str(ROOT / "infra/desktop/agentd.py")], env=env)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=5)


def test_auth_exec_and_takeover(agentd, runnerd):
    assert httpx.get(f"{agentd}/health").json()["ok"]
    assert httpx.post(f"{runnerd}/exec", json={"cmd": "echo hi"}).status_code == 401
    h = {"Authorization": "Bearer tok-123"}
    assert httpx.post(f"{agentd}/exec", json={"cmd": "echo hi"}, headers=h).status_code == 404
    assert httpx.post(f"{agentd}/mcp/register", json={}, headers=h).status_code == 404
    r = httpx.post(f"{runnerd}/exec", json={"cmd": "echo سلام && echo $XAGENT_SECRET_KEY"}, headers=h).json()
    assert r["code"] == 0 and r["stdout"].splitlines()[0] == "سلام"
    assert "test-secret" not in r["stdout"], "اسرار سیستم نباید به محیط ایزوله نشت کند"
    r = httpx.post(f"{runnerd}/exec", json={"cmd": "sleep 5", "timeout": 1}, headers=h, timeout=10).json()
    assert r["timed_out"]
    httpx.post(f"{agentd}/takeover", json={"on": True}, headers=h)
    assert httpx.post(f"{agentd}/input", json={"action": "key", "keys": "a"}, headers=h).status_code == 423
    httpx.post(f"{agentd}/takeover", json={"on": False}, headers=h)


async def test_mcp_bridge_through_desktop_sandbox(agentd, runnerd, monkeypatch):
    from xagent_core.config import get_settings
    from xagent_tools import mcp_client, sandbox

    repo = _make_repo("D")
    s = get_settings()
    monkeypatch.setattr(s, "desktop_url", agentd)
    monkeypatch.setattr(s, "desktop_token", "tok-123")
    monkeypatch.setenv("XAGENT_RUNNER_URL", runnerd)
    monkeypatch.setenv("XAGENT_RUNNER_TOKEN", "tok-123")
    monkeypatch.setattr(sandbox, "_sandbox", sandbox.DesktopSandbox())
    try:
        cfg = {"transport": "stdio", "command": sys.executable, "args": ["server.py"], "cwd": str(repo)}
        tools = await mcp_client.list_tools("echo-desk", cfg)
        assert [t["name"] for t in tools] == ["echo"]
        # دو فراخوانی پشت‌سرهم روی همان فرایند پایدار (initialize تکراری کش می‌شود)
        for word in ("یک", "دو"):
            ok, text = await mcp_client.call_tool("echo-desk", cfg, "echo", {"text": word})
            assert ok and text == f"ECHOD:{word}"
    finally:
        monkeypatch.setattr(sandbox, "_sandbox", None)
