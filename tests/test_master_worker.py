"""مأموریت جامع — مرحله ۵: SSE و Pool دیتابیس (A5)، Lease fencing، Dream جدا از زمان‌بند، پذیرش محدود Worker،
نظارت حلقه‌های ضروری و سلامت محدود به زمان.

SSE روی سرور واقعی Uvicorn (همان Event Loop آزمون) با PostgreSQL/Redis واقعی؛ شمار اتصال‌های گرفته‌شده Pool
مستقیم اندازه‌گیری می‌شود، نه فقط کد پاسخ HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
import uuid
from datetime import timedelta

import httpx
import pytest
from autonomy_helpers import body_text, called, tool_names
from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import get_engine, session_scope, utcnow
from xagent_core.models import DreamReport, Routine, Run, User

pytestmark = pytest.mark.asyncio


def _port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@contextlib.asynccontextmanager
async def live_api():
    import uvicorn

    from xagent_api.main import app

    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def _login(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "Test-Password-123"},
                          headers={"X-XAgent": "1"})
    assert r.status_code == 200, r.text
    client.cookies.set("xa_token", r.json()["token"])


# ── G1 / A5: هشت جریان SSE هم‌زمان اتصال دیتابیس نگه نمی‌دارند ──
async def test_A5_sse_streams_do_not_hold_db_connections(app_client, monkeypatch):
    from xagent_api.routers import live

    monkeypatch.setattr(live, "SSE_REAUTH_SECONDS", 3600, raising=False)
    pool = get_engine().sync_engine.pool
    async with live_api() as base, httpx.AsyncClient(base_url=base, timeout=10) as client:
        await _login(client)
        before = pool.checkedout()
        async with contextlib.AsyncExitStack() as stack:
            for _ in range(8):
                resp = await stack.enter_async_context(client.stream("GET", "/api/events/stream"))
                assert resp.status_code == 200
                first = await resp.aiter_text().__anext__()
                assert "retry" in first
            await asyncio.sleep(0.3)
            held = pool.checkedout() - before
            t0 = time.monotonic()
            health = await client.get("/api/health")
            dash = await client.get("/api/dashboard")
            elapsed = time.monotonic() - t0
        assert held <= 1, f"{held} اتصال دیتابیس توسط جریان‌های SSE نگه داشته شد"
        assert health.status_code == 200 and dash.status_code == 200 and elapsed < 3, (health.status_code, elapsed)


async def test_A5_revoked_token_closes_stream_and_resync_is_sent(app_client, monkeypatch):
    from xagent_api.routers import live

    monkeypatch.setattr(live, "SSE_REAUTH_SECONDS", 0.3)
    try:
        async with live_api() as base, httpx.AsyncClient(base_url=base, timeout=15) as client:
            await _login(client)
            async with client.stream("GET", "/api/events/stream") as resp:
                chunks = []
                async for text in resp.aiter_text():
                    chunks.append(text)
                    if "event: resync" in "".join(chunks) and len(chunks) == 1 or len(chunks) == 2:
                        # ابطال همه نشست‌ها (افزایش token_version) در میانه جریان
                        async with session_scope() as s:
                            u = (await s.execute(select(User).where(User.username == "admin"))).scalar_one()
                            u.token_version += 1
                    if "event: auth" in text:
                        break
                joined = "".join(chunks)
            assert "event: resync" in joined, "پس از اتصال، رویداد همگام‌سازی دوباره ارسال نشد"
            assert "revoked" in joined, "جریان پس از ابطال توکن باز ماند"
    finally:
        # app_client جلسه قدیمی را استفاده می‌کند؛ برای تست‌های بعدی دوباره وارد می‌شویم
        r = await app_client.post("/api/auth/login", json={"username": "admin", "password": "Test-Password-123"})
        app_client.cookies.set("xa_token", r.json()["token"])


async def test_health_is_bounded_and_reports_worker_readiness(app_client):
    await bus.get_redis().set("xagent:worker:readiness", json.dumps(
        {"ready": False, "degraded": [{"loop": "scheduler", "status": "failed"}], "intake": {}, "at": "x"}), ex=60)
    r = await app_client.get("/api/health")
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["worker"]["ready"] is False and body["worker"]["degraded"][0]["loop"] == "scheduler"


# ── Lease fencing: Worker کهنه پس از ازدست‌دادن مالکیت اثر تازه نمی‌گذارد و وضعیت پایانی ثبت نمی‌کند ──
async def _file_run(mark: str) -> uuid.UUID:
    async with session_scope() as s:
        run = Run(trigger="chat", input=f"یک فایل بنویس ({mark})", status="queued", state={"trusted": True})
        s.add(run)
        await s.flush()
        return run.id


async def test_lease_stolen_before_tool_blocks_side_effect(app_client, configured_model, mock_llm):
    import redis as sync_redis

    from xagent_agent.pipeline import AgentRunner
    from xagent_core.config import get_settings

    mark = "FENCE-STOLEN"
    rid = await _file_run(mark)
    target = get_settings().workspace_dir / "fence" / f"{rid}.txt"
    rc = sync_redis.Redis.from_url(get_settings().redis_url)

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if "file_write" not in tool_names(body):
            return ("tools_load", {"groups": ["files"]})
        if not any(n == "file_write" for n, _ in called(body)):
            rc.set(f"xagent:lease:run:{rid}", "worker-b:new-owner")  # Worker دیگر مالک شد (Lease منقضی/بازیابی)
            return ("file_write", {"path": f"fence/{rid}.txt", "content": "stale"})
        return "نوشتم."
    mock_llm.handlers.insert(0, handler)
    try:
        status = await AgentRunner("worker-a").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
        rc.delete(f"xagent:lease:run:{rid}")
    assert status == "lease_lost"
    assert not target.exists(), "Worker کهنه پس از ازدست‌دادن Lease فایل نوشت"
    async with session_scope() as s:
        run = await s.get(Run, rid)
    assert run.status not in ("succeeded", "failed"), "Worker کهنه وضعیت پایانی ثبت کرد"


async def test_failed_lease_renewal_cancels_execution(app_client, configured_model, mock_llm, monkeypatch):
    from xagent_agent import pipeline
    from xagent_core.config import get_settings

    monkeypatch.setattr(pipeline, "HEARTBEAT_SECONDS", 0.1)

    async def refuse(*a, **k):
        return False
    monkeypatch.setattr(pipeline.bus, "extend_lease", refuse)
    mark = "FENCE-RENEW"
    rid = await _file_run(mark)
    target = get_settings().workspace_dir / "fence" / f"renew-{rid}.txt"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        time.sleep(0.8)  # در این فاصله تمدید Lease شکست می‌خورد
        if "file_write" not in tool_names(body):
            return ("tools_load", {"groups": ["files"]})
        return ("file_write", {"path": f"fence/renew-{rid}.txt", "content": "stale"})
    mock_llm.handlers.insert(0, handler)
    try:
        status = await pipeline.AgentRunner("worker-a").execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    assert status == "lease_lost" and not target.exists()


# ── ریسک ۱: Dream کند مانع اجرای روتین سررسیده نمی‌شود ──
async def test_slow_dream_does_not_block_due_routine(app_client, monkeypatch):
    from xagent_worker import main as wmain

    await bus.set_stop_all(False)
    async with session_scope() as s:
        rep = DreamReport(started_at=utcnow(), slot_key=f"t:{uuid.uuid4().hex[:20]}", trigger="scheduled", status="running",
                          events_reviewed=0, tokens=0, cost_usd=0, summary="", learnings=[], skills_proposed=[],
                          cleanup={})
        r = Routine(name=f"due-{uuid.uuid4().hex[:5]}", instruction="x", trigger_type="interval", interval_seconds=600,
                    status="active", next_run_at=utcnow() - timedelta(seconds=5))
        s.add_all([rep, r])
        await s.flush()
        rep_id, rid = rep.id, r.id
    await bus.release_lease("dream", (await bus.get_redis().get("xagent:lease:dream")) or "")

    async def claim():
        return rep_id
    dream_started = asyncio.Event()

    async def slow_dream(session, report_id=None, **kw):
        dream_started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(wmain, "claim_scheduled_dream", claim)
    monkeypatch.setattr(wmain, "run_dream", slow_dream)
    monkeypatch.setattr(wmain, "SCHEDULER_TICK_SECONDS", 0.2)
    w = wmain.Worker()
    loop_task = asyncio.create_task(w.scheduler_loop())
    try:
        await asyncio.wait_for(dream_started.wait(), timeout=5)
        dispatched = False
        for _ in range(40):
            async with session_scope() as s:
                if (await s.execute(select(Run.id).where(Run.routine_id == rid))).first():
                    dispatched = True
                    break
            await asyncio.sleep(0.1)
        assert dispatched, "روتین سررسیده تا پایان Dream منتظر ماند"
        assert w.dream_task is not None and not w.dream_task.done()
    finally:
        w.stopping.set()
        loop_task.cancel()
        if w.dream_task:
            w.dream_task.cancel()
        with contextlib.suppress(BaseException):
            await loop_task
        with contextlib.suppress(BaseException):
            await w.dream_task
        await bus.release_lease("dream", (await bus.get_redis().get("xagent:lease:dream")) or "")


# ── ریسک ۳: پذیرش محدود — انبوه کار بیش از ظرفیت در حافظه انباشته نمی‌شود ──
async def test_worker_intake_is_bounded(app_client, monkeypatch):
    from xagent_worker import main as wmain

    await bus.ensure_groups()
    w = wmain.Worker()
    w.intake_limit = 2
    peak = {"n": 0}

    async def slow_execute(run_id):
        peak["n"] = max(peak["n"], len(w.run_jobs))
        await asyncio.sleep(0.6)
    monkeypatch.setattr(w, "_execute", slow_execute)
    async with session_scope() as s:
        ids = []
        for _ in range(8):
            run = Run(trigger="api", input="x", status="queued", state={})
            s.add(run)
            await s.flush()
            ids.append(run.id)
    for rid in ids:
        await bus.enqueue_run(rid, "burst")
    task = asyncio.create_task(w.consume_runs())
    observed = 0
    for _ in range(20):
        await asyncio.sleep(0.1)
        observed = max(observed, len(w.run_jobs))
    w.stopping.set()
    task.cancel()
    with contextlib.suppress(BaseException):
        await task
    for t in list(w.active):
        t.cancel()
    assert observed <= 2 and peak["n"] <= 2, f"پذیرش بیش از ظرفیت: {observed}"


# ── ریسک ۷: حلقه ضروری از کار افتاده تشخیص، دوباره راه‌اندازی و در آمادگی گزارش می‌شود ──
async def test_essential_loop_failure_is_detected_and_restarted(app_client):
    from xagent_worker import main as wmain

    w = wmain.Worker()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("Redis قطع شد")
        w.beat("scheduler")
        await w.stopping.wait()

    async def broken():
        raise RuntimeError("خطای دائمی")
    sup = asyncio.create_task(w.supervise("scheduler", flaky))
    for _ in range(80):
        if calls["n"] >= 3:
            break
        await asyncio.sleep(0.1)
    assert w.loops["scheduler"]["restarts"] == 2 and w.loops["scheduler"]["status"] == "running"
    await w.supervise("inbox", broken, max_restarts=1)
    assert w.loops["inbox"]["status"] == "failed"
    ready = w.readiness()
    assert ready["ready"] is False and any(d["loop"] == "inbox" for d in ready["degraded"])
    w.stopping.set()
    await asyncio.wait_for(sup, timeout=5)
