"""Regression tests for the final Host Broker, Dream, and updater hardening.

No external network or billable provider is contacted.
"""

from __future__ import annotations

import runpy
import socket
from pathlib import Path

import pytest

from xagent_core import bus
from xagent_core.db import session_scope
from xagent_core.settings_store import DreamSettings, get_setting, set_setting

HOSTD = Path(__file__).resolve().parents[1] / "infra/hostd/xagent-hostd.py"


def test_host_broker_fails_closed_without_secret_and_after_secret_removed(tmp_path):
    mod = runpy.run_path(str(HOSTD))
    cfg = {"env_file": str(tmp_path / ".env"), "run_dir": str(tmp_path)}
    broker = mod["Broker"](cfg)
    with pytest.raises(PermissionError, match="configuration unavailable"):
        broker.keys()
    (tmp_path / ".env").write_text("XAGENT_SECRET_KEY=valid-secret-for-unit-testing-123456789\n")
    request_key, grant_key = broker.keys()
    assert request_key and grant_key and request_key != grant_key
    (tmp_path / ".env").unlink()
    with pytest.raises(PermissionError, match="configuration unavailable"):
        broker.keys()


def test_diag_http_pins_public_dns_and_disables_redirects_and_proxies(monkeypatch):
    mod = runpy.run_path(str(HOSTD))

    def public_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    cfg = {
        "app_dir": "/tmp",
        "bin": {"xagentctl": "/bin/true", "journalctl": "/bin/true",
                "systemctl": "/bin/true", "docker": "/bin/true"},
    }
    level, steps, _timeout = mod["build_steps"](
        cfg, "diag.http", {"url": "https://example.com/path"}
    )
    argv = steps[0][1]
    assert level == 1
    assert argv[0] == "curl" and argv[1] == "-q"
    assert "--noproxy" in argv and argv[argv.index("--noproxy") + 1] == "*"
    assert "--resolve" in argv
    assert argv[argv.index("--resolve") + 1] == "example.com:443:8.8.8.8"
    assert "--max-redirs" in argv and argv[argv.index("--max-redirs") + 1] == "0"
    assert not any(x in argv for x in ("-L", "--location", "--location-trusted"))

    def private_dns(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private_dns)
    with pytest.raises(mod["BadArgs"], match="non-public"):
        mod["_public_http_args"]("https://example.com/")

    def mixed_dns(*_args, **_kwargs):
        return public_dns() + private_dns()

    monkeypatch.setattr(socket, "getaddrinfo", mixed_dns)
    with pytest.raises(mod["BadArgs"], match="non-public"):
        mod["_public_http_args"]("https://example.com/")


@pytest.mark.asyncio
async def test_manual_dream_rejects_disabled_mode_and_stop_all(app_client):
    await bus.set_stop_all(False)
    async with session_scope() as session:
        original = await get_setting(session, "dream", DreamSettings)
        await set_setting(session, "dream", original.model_copy(update={"enabled": False}))
    try:
        response = await app_client.post("/api/dream/run")
        assert response.status_code == 409
        assert "غیرفعال" in response.json()["detail"]
        await bus.set_stop_all(True)
        response = await app_client.post("/api/dream/run")
        assert response.status_code == 409
        assert "توقف اضطراری" in response.json()["detail"]
    finally:
        await bus.set_stop_all(False)
        async with session_scope() as session:
            await set_setting(session, "dream", original)
