"""آزمون کارگزار فرمان میزبان (Host Command Broker) با سرویس واقعی xagent-hostd و باینری‌های جعلی.

اجرای مجاز، رد غیرمجاز، تزریق آرگومان، نشت اسرار، دورزدن کارت اجازه، Restart و سلامت، قفل هم‌زمان،
بازیابی پس از خرابی سرویس میزبان، Dry Run آپدیت و نبود دسترسی Viewer/کارمند.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from xagent_core.db import session_scope
from xagent_core.models import Approval, HostOp

ROOT = Path(__file__).resolve().parents[1]
HOSTD = ROOT / "infra/hostd/xagent-hostd.py"
LEAK = "sk-live-hostd-should-never-leak-0123456789"
SECRET = os.environ["XAGENT_SECRET_KEY"]


def _script(path: Path, body: str) -> str:
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)
    return str(path)


class Hostd:
    def __init__(self, base: Path):
        self.base = base
        self.marks = base / "marks"
        self.marks.mkdir(parents=True, exist_ok=True)
        bins = base / "bin"
        bins.mkdir(exist_ok=True)
        m = self.marks
        self.docker = _script(bins / "docker", f"""
            args="$*"
            case "$args" in
              *" ps "*"--format json"*) svc="${{@: -1}}"; echo '{{"Service":"'"$svc"'","State":"running","Health":"healthy"}}' ;;
              *" ps "*) printf 'SERVICE STATE STATUS\\napi running Up\\nworker running Up\\n' ;;
              *" logs "*) echo "api started"; echo "provider error key={LEAK} token=Bearer abcdefghijklmnop"; echo "env secret {SECRET}" ;;
              *" restart "*) echo "Restarting ${{@: -1}}"; echo "${{@: -1}}" >> {m}/restarts ;;
              *) echo "docker $args" ;;
            esac
        """)
        self.xagentctl = _script(bins / "xagentctl", f"""
            case "$1" in
              status) echo "xagent status: all services running" ;;
              doctor) echo "doctor ok" ;;
              version) echo '{{"version":"0.1.0"}}' ;;
              backup) touch {m}/backup; echo "backup done" ;;
              update) if [[ " $* " == *" --dry-run "* ]]; then touch {m}/dry-run; echo "dry-run: target abc123, no changes";
                      else touch {m}/update-started; sleep "$(cat {m}/update_sleep 2>/dev/null || echo 2)"; touch {m}/update-done; echo updated; fi ;;
              rollback) touch {m}/rollback; echo rolled back ;;
              *) echo "unknown"; exit 2 ;;
            esac
        """)
        self.journal = _script(bins / "journalctl", "echo journal ok\n")
        self.systemctl = _script(bins / "systemctl", "echo active\n")
        (base / "app").mkdir(exist_ok=True)
        (base / "app" / ".env").write_text(f"XAGENT_SECRET_KEY={SECRET}\nOPENAI_API_KEY={LEAK}\nPOSTGRES_PASSWORD=pg-pass-123456\n")
        self.run_dir = base / "run" / "hostd"
        self.conf = base / "hostd.json"
        self.conf.write_text(json.dumps({
            "home": str(base), "run_dir": str(self.run_dir), "env_file": str(base / "app" / ".env"),
            "app_dir": str(base / "app"), "allowed_uids": [os.getuid()], "max_concurrent": 3,
            "audit_log": str(base / "audit.log"),
            "bin": {"docker": self.docker, "xagentctl": self.xagentctl, "journalctl": self.journal, "systemctl": self.systemctl},
        }))
        self.proc: subprocess.Popen | None = None

    @property
    def sock(self) -> str:
        return str(self.run_dir / "hostd.sock")

    def start(self) -> None:
        self.proc = subprocess.Popen([sys.executable, str(HOSTD), "--config", str(self.conf)])
        for _ in range(100):
            if os.path.exists(self.sock):
                try:
                    s = socket.socket(socket.AF_UNIX)
                    s.connect(self.sock)
                    s.close()
                    return
                except OSError:
                    pass
            time.sleep(0.05)
        raise RuntimeError("hostd did not start")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()


@pytest.fixture(scope="module")
def hostd(tmp_path_factory):
    h = Hostd(tmp_path_factory.mktemp("hostd"))
    h.start()
    yield h
    h.kill()


@pytest.fixture(autouse=True)
def _wire(hostd, monkeypatch):
    from xagent_core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "hostd_socket", hostd.sock)
    monkeypatch.setattr(s, "hostd_jobs_dir", str(hostd.run_dir / "jobs"))
    from xagent_api import hostops

    hostops._catalog_cache = (0.0, {})


def _raw(sock: str, req: dict) -> dict:
    c = socket.socket(socket.AF_UNIX)
    c.connect(sock)
    c.sendall(json.dumps(req).encode() + b"\n")
    data = c.makefile().readline()
    c.close()
    return json.loads(data)


def _signed(op: str, args: dict, key_label=b"xagent-hostd-request-v1", secret=SECRET) -> dict:
    from xagent_api.hostops import canonical

    req = {"v": 1, "op": op, "args": args, "ts": int(time.time()), "nonce": secrets.token_hex(16), "actor": "test"}
    req["mac"] = hmac.new(hmac.new(secret.encode(), key_label, hashlib.sha256).digest(), canonical(req), hashlib.sha256).hexdigest()
    return req


async def _owner_run(client, op, args=None, confirm=False, wait=True) -> dict:
    r = await client.post("/api/host/ops", json={"op": op, "args": args or {}, "reason": "آزمون", "confirm": confirm})
    assert r.status_code == 200, r.text
    d = r.json()
    for _ in range(80):
        if not wait or d["status"] not in ("running", "queued"):
            break
        await asyncio.sleep(0.25)
        d = (await client.get(f"/api/host/ops/{d['id']}")).json()
    return d


def _internal(app_client):
    from xagent_api.hostops import internal_token

    return {"X-XAgent-Internal": internal_token()}


# ───────────── اجرای مجاز ─────────────
async def test_allowed_operation_runs_and_is_audited(app_client, hostd):
    d = await _owner_run(app_client, "system.status")
    assert d["status"] == "succeeded" and d["exit_code"] == 0 and "CPU:" in d["output"] and "RAM:" in d["output"]
    d2 = await _owner_run(app_client, "services.status")
    assert d2["status"] == "succeeded" and "api running" in d2["output"]
    async with session_scope() as s:
        row = await s.get(HostOp, uuid.UUID(d["id"]))
        assert row.requested_by == "admin" and row.actor_kind == "owner" and row.level == 1
    audit = (hostd.base / "audit.log").read_text()
    assert '"op": "system.status"' in audit and '"event": "started"' in audit


# ───────────── رد غیرمجاز و تزریق ─────────────
async def test_unknown_and_injected_operations_are_rejected(app_client, hostd):
    assert (await app_client.post("/api/host/ops", json={"op": "shell", "args": {"cmd": "id"}})).status_code == 400
    for args in ({"service": "api; touch /tmp/pwn"}, {"service": "api", "lines": "10 --since=1970"},
                 {"service": "api", "extra": "--privileged"}):
        r = await app_client.post("/api/host/ops", json={"op": "logs.service", "args": args})
        assert r.status_code == 200 and r.json()["status"] == "rejected", (args, r.text)
    # تزریق گزینه به git از طریق ref و گزینه ssh از طریق host
    r = await app_client.post("/api/host/ops", json={"op": "update.check", "args": {"ref": "--upload-pack=touch /tmp/pwn"}})
    assert r.json()["status"] == "rejected"
    r = await app_client.post("/api/host/ops", json={"op": "diag.ping", "args": {"host": "-oProxyCommand=id"}})
    assert r.json()["status"] == "rejected"
    r = await app_client.post("/api/host/ops", json={"op": "diag.http", "args": {"url": "http://127.0.0.1:5432/"}})
    assert r.json()["status"] == "rejected"


async def test_hostd_refuses_unsigned_forged_and_replayed_requests(hostd):
    assert _raw(hostd.sock, {"op": "run", "args": {"op": "system.status", "args": {}}})["code"] == "unauthorized"
    forged = _signed("run", {"op": "system.status", "args": {}}, secret="wrong-secret-0123456789abcdef")
    assert _raw(hostd.sock, forged)["code"] == "unauthorized"
    good = _signed("run", {"op": "system.status", "args": {}})
    assert _raw(hostd.sock, dict(good))["ok"] is True
    assert _raw(hostd.sock, good)["code"] == "unauthorized"  # تکرار همان درخواست (replay)


async def test_root_level_operation_cannot_be_injected_without_grant(hostd):
    # فرمان دلخواه با دسترسی root فقط با مجوز امضاشده کارفرما
    r = _raw(hostd.sock, _signed("run", {"op": "exec.approved", "args": {"argv": ["/usr/bin/touch", str(hostd.marks / "pwned")],
                                                                          "run_as": "root"}}))
    assert r["ok"] is False and r["code"] == "approval_required"
    # مجوز جعلی، مجوز برای آرگومان دیگر، مجوز منقضی
    from xagent_api.hostops import sign_grant

    args = {"argv": ["/usr/bin/touch", str(hostd.marks / "pwned")], "run_as": "root"}
    bad = sign_grant("exec.approved", args, minutes=5, approval_id=None, by="x")
    bad["mac"] = "0" * 64
    assert _raw(hostd.sock, _signed("run", {"op": "exec.approved", "args": args, "grant": bad}))["code"] == "approval_required"
    other = sign_grant("exec.approved", {"argv": ["/usr/bin/true"], "run_as": "root"}, minutes=5, approval_id=None, by="x")
    assert _raw(hostd.sock, _signed("run", {"op": "exec.approved", "args": args, "grant": other}))["code"] == "approval_required"
    expired = sign_grant("exec.approved", args, minutes=-1, approval_id=None, by="x")
    assert _raw(hostd.sock, _signed("run", {"op": "exec.approved", "args": args, "grant": expired}))["code"] == "approval_required"
    await asyncio.sleep(0.3)
    assert not (hostd.marks / "pwned").exists()


# ───────────── اسرار ─────────────
async def test_output_and_logs_never_contain_secrets(app_client, hostd):
    d = await _owner_run(app_client, "logs.service", {"service": "api", "lines": 50})
    assert d["status"] == "succeeded" and "api started" in d["output"]
    texts = [d["output"], json.dumps(d, ensure_ascii=False)]
    async with session_scope() as s:
        texts.append((await s.get(HostOp, uuid.UUID(d["id"]))).output)
    texts.append((hostd.run_dir / "jobs" / f"{d['job']}.out").read_text())
    texts.append((hostd.base / "audit.log").read_text())
    for t in texts:
        assert LEAK not in t and SECRET not in t and "abcdefghijklmnop" not in t, t[:300]


# ───────────── کارت اجازه ─────────────
async def test_approval_cannot_be_bypassed(app_client, hostd):
    from xagent_api.main import app

    hdr = _internal(app_client)
    run_id = str(uuid.uuid4())
    body = {"op": "xagent.update", "args": {}, "reason": "نسخه جدید", "run_id": run_id, "tool_call_id": "c1", "actor": "manager"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/internal/host/run", json=body)).status_code == 401  # بدون توکن داخلی
        r = await c.post("/api/internal/host/run", json=body, headers=hdr)
        assert r.json()["status"] == "approval_required"
        # ادعای تأیید بدون کارت اجازه
        r = await c.post("/api/internal/host/run", json={**body, "approved": True}, headers=hdr)
        assert r.status_code == 403
        # کارت اجازه‌ای که کارفرما تأیید نکرده (تصمیم‌گیرنده Viewer) یا برای عملیات دیگری است
        data = (await app_client.post("/api/chat/send", json={"text": "آزمون مجوز میزبان"})).json()
        rid = data["run"]["id"]
        async with session_scope() as s:
            s.add(Approval(run_id=uuid.UUID(rid), tool_call_id="c2", category="host_admin", tool_name="host_run",
                           args={"op": "xagent.update", "args": {}}, reason="x", status="approved", decided_by="viewer1"))
            s.add(Approval(run_id=uuid.UUID(rid), tool_call_id="c3", category="host_admin", tool_name="host_run",
                           args={"op": "xagent.rollback", "args": {}}, reason="x", status="approved", decided_by="admin"))
        for cid in ("c2", "c3"):
            r = await c.post("/api/internal/host/run", headers=hdr,
                             json={**body, "run_id": rid, "tool_call_id": cid, "approved": True})
            assert r.status_code == 403, r.text
        assert not (hostd.marks / "update-started").exists()
        # کارت درست کارفرما: یک‌بار اجرا
        from xagent_core.db import utcnow

        async with session_scope() as s:
            s.add(Approval(run_id=uuid.UUID(rid), tool_call_id="c4", category="host_admin", tool_name="host_run",
                           args={"op": "xagent.update", "args": {}}, reason="x", status="approved", decided_by="admin",
                           decided_at=utcnow()))
        (hostd.marks / "update_sleep").write_text("0")
        r = await c.post("/api/internal/host/run", headers=hdr, json={**body, "run_id": rid, "tool_call_id": "c4", "approved": True})
        assert r.status_code == 200 and r.json()["status"] in ("running", "succeeded") and r.json()["approved_by"] == "admin"
        r2 = await c.post("/api/internal/host/run", headers=hdr, json={**body, "run_id": rid, "tool_call_id": "c4", "approved": True})
        assert r2.status_code == 403  # یک‌بارمصرف
    for _ in range(40):
        if (hostd.marks / "update-done").exists():
            break
        await asyncio.sleep(0.2)
    assert (hostd.marks / "update-done").exists()
    (hostd.marks / "update-started").unlink()
    (hostd.marks / "update-done").unlink()


# ───────────── Restart و سلامت + سقف تعداد ─────────────
async def test_manager_restart_with_health_check_and_rate_limit(app_client, hostd):
    from xagent_api.main import app

    hdr = _internal(app_client)
    body = {"op": "service.restart", "args": {"service": "runner"}, "reason": "پاسخ نمی‌دهد", "actor": "manager"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(3):
            r = await c.post("/api/internal/host/run", json=body, headers=hdr)
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["status"] == "succeeded", d
            assert "سالم است" in d["output"] and "Restarting runner" in d["output"]
        r = await c.post("/api/internal/host/run", json=body, headers=hdr)
        assert r.status_code == 429  # سقف سطح ۲ در ساعت
    assert (hostd.marks / "restarts").read_text().split().count("runner") == 3


# ───────────── قفل هم‌زمان ─────────────
async def test_concurrent_modifying_operations_are_locked(app_client, hostd):
    (hostd.marks / "update_sleep").write_text("3")
    up = await _owner_run(app_client, "xagent.update", confirm=True, wait=False)
    assert up["status"] == "running"
    bk = (await app_client.post("/api/host/ops", json={"op": "xagent.backup", "args": {}})).json()
    assert bk["status"] == "failed" and "قفل" in (bk["error"] or "")
    assert not (hostd.marks / "backup").exists()
    ro = await _owner_run(app_client, "xagent.status")  # عملیات فقط‌خواندنی هم‌زمان مجاز است
    assert ro["status"] == "succeeded"
    for _ in range(60):
        d = (await app_client.get(f"/api/host/ops/{up['id']}")).json()
        if d["status"] != "running":
            break
        await asyncio.sleep(0.25)
    assert d["status"] == "succeeded"
    # سطح ۳ از پنل بدون تأیید صریح دوم اجرا نمی‌شود
    assert (await app_client.post("/api/host/ops", json={"op": "xagent.rollback", "args": {}})).status_code == 428


# ───────────── بازیابی پس از خرابی سرویس میزبان ─────────────
async def test_operation_survives_hostd_crash_and_reports_unavailability(app_client, hostd):
    (hostd.marks / "update_sleep").write_text("2")
    up = await _owner_run(app_client, "xagent.update", confirm=True, wait=False)
    assert up["status"] == "running"
    hostd.kill()  # خرابی کامل سرویس میزبان در میانه کار
    r = await app_client.post("/api/host/ops", json={"op": "system.status", "args": {}})
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert r.json()["status"] == "failed" and "در دسترس نیست" in r.json()["error"]
    await asyncio.sleep(2.5)
    hostd.start()
    for _ in range(40):
        d = (await app_client.get(f"/api/host/ops/{up['id']}")).json()
        if d["status"] != "running":
            break
        await asyncio.sleep(0.25)
    assert d["status"] == "succeeded", d  # کار در Session مستقل ادامه یافت و نتیجه ثبت شد
    # اجراکننده‌ای که بمیرد «گم‌شده» علامت می‌خورد
    (hostd.marks / "update_sleep").write_text("30")
    up2 = await _owner_run(app_client, "xagent.update", confirm=True, wait=False)
    st = json.loads((hostd.run_dir / "jobs" / f"{up2['job']}.json").read_text())
    for _ in range(20):
        st = json.loads((hostd.run_dir / "jobs" / f"{up2['job']}.json").read_text())
        if st.get("pid"):
            break
        await asyncio.sleep(0.1)
    os.killpg(int(st["pid"]), signal.SIGKILL)
    await asyncio.sleep(6)
    from xagent_api.hostops import hostd_call

    await hostd_call("job.status", {"job": up2["job"]})
    d2 = (await app_client.get(f"/api/host/ops/{up2['id']}")).json()
    assert d2["status"] == "lost", d2


# ───────────── Dry Run ─────────────
async def test_update_dry_run_makes_no_changes(app_client, hostd):
    for f in ("update-started", "dry-run"):
        (hostd.marks / f).unlink(missing_ok=True)
    from xagent_api.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/internal/host/run", headers=_internal(app_client),
                         json={"op": "update.check", "args": {}, "reason": "بررسی نسخه", "actor": "manager"})
    d = r.json()
    assert d["status"] == "succeeded" and "no changes" in d["output"]
    assert (hostd.marks / "dry-run").exists() and not (hostd.marks / "update-started").exists()


# ───────────── Viewer و کارمند ─────────────
async def test_viewer_and_employee_have_no_host_access(app_client, hostd):
    from test_security_fixes import _ctx, _viewer_client

    from xagent_agent.control import host_tools

    viewer = await _viewer_client()
    try:
        for method, url, body in (("GET", "/api/host/ops", None), ("GET", "/api/host/catalog", None),
                                  ("POST", "/api/host/ops", {"op": "system.status"}), ("POST", "/api/host/stop", {})):
            r = await viewer.request(method, url, json=body)
            assert r.status_code == 403, (url, r.status_code)
    finally:
        await viewer.aclose()
    async with session_scope() as s:
        with pytest.raises(PermissionError):
            await host_tools.host_run(_ctx(s, principal="employee", employee="ops"), {"op": "system.status", "reason": "x"})
        with pytest.raises(PermissionError):
            await host_tools.host_run(_ctx(s, trusted=False, principal="external"), {"op": "system.status", "reason": "x"})
        from xagent_agent.control import changes

        with pytest.raises(changes.ChangeError):
            await changes.upsert_employee(s, {"name": "hostops", "tool_groups": ["host"]}, actor="manager", reason="x")


# ───────────── مسیر کامل مدیر: ابزار ← API داخلی ← میزبان ─────────────
async def test_manager_tool_runs_real_host_operation(app_client, configured_model, runner, mock_llm, hostd, monkeypatch):
    from xagent_agent.control import host_tools
    from xagent_api.main import app
    from xagent_core.models import Run, RunStep

    monkeypatch.setattr(host_tools, "client_factory",
                        lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"))
    mock_llm.scenarios["وضعیت سرور را بررسی کن"] = [("tools_load", {"groups": ["host"]}),
                                                     ("host_run", {"op": "system.status", "reason": "درخواست کارفرما"})]
    try:
        data = (await app_client.post("/api/chat/send", json={"text": "وضعیت سرور را بررسی کن"})).json()
        await runner.execute(uuid.UUID(data["run"]["id"]))
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(data["run"]["id"]))
            steps = (await s.execute(select(RunStep).where(RunStep.run_id == run.id, RunStep.tool_name == "host_run"))).scalars().all()
            op = (await s.execute(select(HostOp).where(HostOp.run_id == run.id))).scalars().first()
        assert run.status == "succeeded", run.error
        assert steps and steps[-1].status == "ok"
        assert op is not None and op.status == "succeeded" and op.actor_kind == "manager" and "CPU:" in op.output
    finally:
        mock_llm.scenarios.pop("وضعیت سرور را بررسی کن", None)
