"""ممیزی جرم‌شناختی — مرحله ۳، F08: مسیریابی پاسخ MCP هم‌زمان در پل stdio ← HTTP دیمن (فرایند واقعی agentd).

سرور MCP ساختگی پاسخ‌ها را با تأخیر و به ترتیب معکوس می‌دهد؛ دو «کارمند/اجرا» با شناسه JSON-RPC یکسان
درخواست می‌فرستند. هیچ پاسخی نباید از مرز درخواست‌کننده عبور کند.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.asyncio

FAKE_SERVER = textwrap.dedent('''
    import json, sys, threading, time
    def reply(msg):
        p = msg.get("params") or {}
        if p.get("hang"):
            return
        time.sleep(float(p.get("delay", 0)))
        if p.get("die"):
            sys.stdout.flush(); import os; os._exit(3)
        out = {"jsonrpc": "2.0", "id": msg["id"], "result": {"who": p.get("who"), "echo": p.get("n")}}
        sys.stdout.write(json.dumps(out) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" in msg and msg.get("method"):
            if msg["method"] == "initialize":
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": 1}}) + "\\n")
                sys.stdout.flush()
            else:
                threading.Thread(target=reply, args=(msg,), daemon=True).start()
''')


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    home = tmp_path_factory.mktemp("mcp-home")
    (home / "work").mkdir()
    server = home / "fake_mcp.py"
    server.write_text(FAKE_SERVER)
    env = {**os.environ, "DESKTOP_TOKEN": "tok-mcp", "AGENTD_PORT": str(port), "HOME": str(home), "RUNNER_MODE": "1",
           "MCP_REQUEST_TIMEOUT": "3"}
    proc = subprocess.Popen([sys.executable, str(ROOT / "infra/desktop/agentd.py")], env=env)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    hdr = {"Authorization": "Bearer tok-mcp"}
    r = httpx.post(f"{base}/mcp/register", headers=hdr,
                   json={"name": "fx", "config": {"command": sys.executable, "args": [str(server)]}})
    assert r.status_code == 200
    yield base, hdr
    proc.terminate()
    proc.wait(timeout=5)


async def call(c, base, hdr, rid, **params):
    r = await c.post(f"{base}/mcp/fx", headers=hdr, json={"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                                         "params": params}, timeout=15)
    return r.json()


async def test_F08_identical_ids_from_concurrent_clients_never_cross(bridge):
    base, hdr = bridge
    async with httpx.AsyncClient() as c:
        # دو کلاینت با شناسه یکسان 1؛ پاسخ اولی دیرتر می‌رسد (ترتیب معکوس)
        a, b = await asyncio.gather(call(c, base, hdr, 1, who="employee-A", n="A1", delay=1.0),
                                    call(c, base, hdr, 1, who="employee-B", n="B1", delay=0.1))
    assert a.get("result", {}).get("who") == "employee-A", f"پاسخ کلاینت A اشتباه/گم شد: {a}"
    assert b.get("result", {}).get("who") == "employee-B", f"پاسخ کلاینت B اشتباه/گم شد: {b}"
    assert a["id"] == 1 and b["id"] == 1  # شناسه اصلی هر کلاینت به خودش برمی‌گردد


async def test_F08_many_concurrent_reordered_requests_stay_isolated(bridge):
    base, hdr = bridge
    async with httpx.AsyncClient() as c:
        res = await asyncio.gather(*[call(c, base, hdr, 7, who=f"run-{i}", n=i, delay=(10 - i) * 0.05)
                                     for i in range(10)])
    assert [r.get("result", {}).get("who") for r in res] == [f"run-{i}" for i in range(10)], res


async def test_F08_timeout_and_server_restart_do_not_leak_responses(bridge):
    base, hdr = bridge
    async with httpx.AsyncClient() as c:
        hung = await call(c, base, hdr, 5, who="hang", hang=True)
        assert "error" in hung, "درخواست بی‌پاسخ خطای Timeout نداد"
        ok = await call(c, base, hdr, 5, who="after-timeout", n=1)
        assert ok.get("result", {}).get("who") == "after-timeout", ok
        died = await call(c, base, hdr, 9, who="dies", die=True)
        assert "error" in died
        again = await call(c, base, hdr, 9, who="after-restart", n=2)
        assert again.get("result", {}).get("who") == "after-restart", f"سرور پس از خروج دوباره راه نیفتاد: {again}"
