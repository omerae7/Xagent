"""Worker پس‌زمینه X Agent.

وظایف هم‌زمان:
- مصرف صف Runها (Redis Streams + Consumer Group) با محدودیت هم‌زمانی متناسب با سخت‌افزار
- بازیابی Runهای معلق پس از ری‌استارت (بدون از دست رفتن وضعیت)
- زمان‌بند روتین‌ها، مدارقطع‌کن و رویای شبانه
- صندوق رویدادها (تلگرام و ...) — رویدادمحور و بدون LLM
- فرمان‌های کنترلی (نصب/به‌روزرسانی/Rollback ابزار، رویا)
- نمونه‌برداری بار سیستم و Heartbeat
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import uuid
from datetime import timedelta

import psutil
from sqlalchemy import delete, func, select

from xagent_agent.dream import run_dream, summarize_conversation
from xagent_agent.pipeline import AgentRunner
from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Conversation, DreamReport, Run, SystemMetric
from xagent_core.redact import install_log_scrubber
from xagent_core.settings_store import DreamSettings, get_setting
from xagent_tools import installer

from .inbox import process_event
from .scheduler import after_routine_run, claim_scheduled_dream, reconcile_runs, tick_routines
from .telegram import TelegramBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_scrubber()
log = logging.getLogger("xagent.worker")
CONTROL_STREAM = "xagent:control"


class Worker:
    def __init__(self) -> None:
        self.id = f"{socket.gethostname()}-{os.getpid()}"
        self.settings = get_settings()
        self.telegram = TelegramBridge()
        self.runner = AgentRunner(self.id, telegram=self.telegram)
        self.sem = asyncio.Semaphore(self.settings.max_concurrent_runs)
        self.stopping = asyncio.Event()
        self.heal_now = asyncio.Event()
        self.active: set[asyncio.Task] = set()

    async def recover(self) -> None:
        """Runهایی که Worker قبلی رها کرده (بدون Lease زنده) فوراً دوباره در صف قرار می‌گیرند.

        اجراهایی که Lease قبلی‌شان هنوز منقضی نشده، در reconcile دوره‌ای زمان‌بند بازیابی می‌شوند.
        """
        await reconcile_runs(stale_after_seconds=0)

    async def _execute(self, run_id: uuid.UUID) -> None:
        async with self.sem:
            try:
                status = await self.runner.execute(run_id)
                log.info("اجرای %s → %s", run_id, status)
            except Exception:  # noqa: BLE001
                log.exception("خطای اجرای %s", run_id)
            try:
                await after_routine_run(run_id, self.telegram)
                async with session_scope() as session:
                    run = await session.get(Run, run_id)
                    if run and run.conversation_id:
                        conv = await session.get(Conversation, run.conversation_id)
                        if conv and conv.message_count > 40 and conv.message_count % 10 == 0:
                            await summarize_conversation(session, conv.id)
            except Exception:  # noqa: BLE001
                log.exception("خطای پس‌پردازش %s", run_id)

    async def consume_runs(self) -> None:
        r = bus.get_blocking_redis()
        while not self.stopping.is_set():
            try:
                # بازپس‌گیری پیام‌های بی‌پاسخ Workerهای ازکارافتاده
                with contextlib.suppress(Exception):
                    claimed = await r.xautoclaim(bus.RUN_STREAM, bus.RUN_GROUP, self.id, min_idle_time=600_000,
                                                 start_id="0-0", count=10)
                    for msg_id, fields in (claimed[1] if claimed else []):
                        await self._dispatch(msg_id, fields)
                resp = await r.xreadgroup(bus.RUN_GROUP, self.id, {bus.RUN_STREAM: ">"}, count=4, block=5000)
                for _stream, messages in resp or []:
                    for msg_id, fields in messages:
                        await self._dispatch(msg_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("خطای مصرف صف")
                await asyncio.sleep(2)

    async def _dispatch(self, msg_id: str, fields: dict) -> None:
        r = bus.get_redis()
        try:
            run_id = uuid.UUID(fields["run_id"])
        except (KeyError, ValueError):
            await r.xack(bus.RUN_STREAM, bus.RUN_GROUP, msg_id)
            return

        async def job():
            try:
                await self._execute(run_id)
            finally:
                await r.xack(bus.RUN_STREAM, bus.RUN_GROUP, msg_id)

        t = asyncio.create_task(job())
        self.active.add(t)
        t.add_done_callback(self.active.discard)

    async def consume_inbox(self) -> None:
        r = bus.get_blocking_redis()
        while not self.stopping.is_set():
            try:
                resp = await r.xreadgroup(bus.RUN_GROUP, self.id, {bus.INBOX_STREAM: ">"}, count=20, block=5000)
                for _stream, messages in resp or []:
                    for msg_id, fields in messages:
                        try:
                            await process_event(uuid.UUID(fields["event_id"]), self.telegram)
                        except Exception:  # noqa: BLE001
                            log.exception("خطای پردازش رویداد")
                        await r.xack(bus.INBOX_STREAM, bus.RUN_GROUP, msg_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("خطای صندوق رویداد")
                await asyncio.sleep(2)

    async def consume_control(self) -> None:
        r = bus.get_blocking_redis()
        with contextlib.suppress(Exception):
            await r.xgroup_create(CONTROL_STREAM, bus.RUN_GROUP, id="$", mkstream=True)
        while not self.stopping.is_set():
            try:
                resp = await r.xreadgroup(bus.RUN_GROUP, self.id, {CONTROL_STREAM: ">"}, count=5, block=5000)
                for _stream, messages in resp or []:
                    for msg_id, fields in messages:
                        await r.xack(CONTROL_STREAM, bus.RUN_GROUP, msg_id)
                        asyncio.create_task(self._control(fields))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("خطای کانال کنترلی")
                await asyncio.sleep(2)

    async def _control(self, fields: dict) -> None:
        cmd = fields.get("cmd")
        payload = json.loads(fields.get("payload") or "{}")
        try:
            async with session_scope() as session:
                if cmd == "install":
                    await installer.install(session, payload["source"], payload.get("ref"), payload.get("name"),
                                            actor=payload.get("actor", "owner"), env=payload.get("env") or None)
                elif cmd == "update":
                    await installer.update(session, uuid.UUID(payload["tool_id"]), payload.get("ref"),
                                           actor=payload.get("actor", "owner"))
                elif cmd == "rollback":
                    vid = payload.get("version_id")
                    await installer.rollback(session, uuid.UUID(payload["tool_id"]),
                                             uuid.UUID(vid) if vid else None, actor=payload.get("actor", "owner"))
                elif cmd == "dream":
                    if await bus.is_stop_all():
                        log.warning("اجرای دستی Dream رد شد: STOP_ALL")
                        return
                    cfg = await get_setting(session, "dream", DreamSettings)
                    async with asyncio.timeout(cfg.max_runtime_seconds):
                        await run_dream(session, force=fields.get("force") == "1")
        except Exception:  # noqa: BLE001
            log.exception("خطای فرمان کنترلی %s", cmd)

    async def selfheal_loop(self) -> None:
        """خوددرمانی دوره‌ای (قطعی و بدون LLM): هر ۱۰ دقیقه یا بلافاصله پس از رخداد سلامت مدل."""
        from xagent_agent.control.selfheal import heal

        while not self.stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.heal_now.wait(), timeout=600)
            self.heal_now.clear()
            if await bus.is_stop_all():
                continue
            lease = await bus.acquire_lease("selfheal", 120_000)
            if not lease:
                continue
            try:
                async with session_scope() as session:
                    rep = await heal(session)
                    if rep.get("applied"):
                        log.info("خوددرمانی: %s", rep["applied"])
            except Exception:  # noqa: BLE001
                log.exception("خطای خوددرمانی")
            finally:
                await bus.release_lease("selfheal", lease)

    async def scheduler_loop(self) -> None:
        last_dream_check = 0.0
        last_reconcile = 0.0
        loop = asyncio.get_running_loop()
        while not self.stopping.is_set():
            try:
                if self.telegram._pending:
                    await self.telegram.flush_pending()
                n = await tick_routines(self.telegram)
                if n:
                    log.info("%d روتین زمان‌بندی شد", n)
                if loop.time() - last_reconcile > 60:
                    last_reconcile = loop.time()
                    await reconcile_runs()
                if loop.time() - last_dream_check > 60:
                    last_dream_check = loop.time()
                    lease = await bus.acquire_lease("dream", 3_600_000)
                    if lease:
                        try:
                            report_id = await claim_scheduled_dream()
                            if report_id:
                                try:
                                    async with session_scope() as cfg_session:
                                        dream_cfg = await get_setting(cfg_session, "dream", DreamSettings)
                                    async with asyncio.timeout(dream_cfg.max_runtime_seconds):
                                        async with session_scope() as session:
                                            rep = await run_dream(session, report_id=report_id)
                                            log.info("رویا: %s", rep.status)
                                except Exception as exc:  # noqa: BLE001
                                    # The original claim was committed in a separate
                                    # transaction, so failures cannot create a retry storm.
                                    async with session_scope() as failure_session:
                                        report = await failure_session.get(DreamReport, report_id)
                                        if report and report.status == "running":
                                            report.status = "failed"
                                            report.finished_at = utcnow()
                                            report.summary = f"Dream failed after claim: {type(exc).__name__}"
                                    log.exception("Dream failed (slot %s)", report_id)
                        finally:
                            await bus.release_lease("dream", lease)
            except Exception:  # noqa: BLE001
                log.exception("خطای زمان‌بند")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=15)

    async def metrics_loop(self) -> None:
        psutil.cpu_percent(interval=None)
        prev = psutil.net_io_counters()
        n = 0
        while not self.stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=30)
            try:
                vm, sw = psutil.virtual_memory(), psutil.swap_memory()
                disk = psutil.disk_usage(str(self.settings.data_dir) if self.settings.data_dir.exists() else "/")
                net = psutil.net_io_counters()
                try:
                    load1 = psutil.getloadavg()[0]
                except OSError:
                    load1 = 0.0
                async with session_scope() as session:
                    active = (await session.execute(select(func.count()).select_from(Run).where(
                        Run.status == "running"))).scalar_one()
                    m = SystemMetric(ts=utcnow(), cpu=psutil.cpu_percent(interval=None), mem=vm.percent,
                                     swap=sw.percent, disk=disk.percent, load1=load1,
                                     net_rx=net.bytes_recv - prev.bytes_recv, net_tx=net.bytes_sent - prev.bytes_sent,
                                     active_runs=active)
                    session.add(m)
                    n += 1
                    if n % 120 == 0:
                        await session.execute(delete(SystemMetric).where(SystemMetric.ts < utcnow() - timedelta(days=7)))
                prev = net
                await bus.publish("system.metrics", {"ts": m.ts.isoformat(), "cpu": m.cpu, "mem": m.mem, "swap": m.swap,
                                                     "disk": m.disk, "load1": m.load1, "net_rx": m.net_rx,
                                                     "net_tx": m.net_tx, "active_runs": active})
                await bus.get_redis().set("xagent:worker:heartbeat", utcnow().isoformat(), ex=120)
            except Exception:  # noqa: BLE001
                log.exception("خطای نمونه‌برداری")

    async def settings_watch(self) -> None:
        async for ev in bus.subscribe():
            if self.stopping.is_set():
                break
            if ev.get("type") == "settings.changed" and (ev.get("data") or {}).get("key") == "telegram":
                await self.telegram.start()
            if ev.get("type") == "model.health" and (ev.get("data") or {}).get("health") == "failing":
                self.heal_now.set()

    async def run(self) -> None:
        await bus.ensure_groups()
        await bus.get_redis().set("xagent:worker:heartbeat", utcnow().isoformat(), ex=120)
        await self.recover()
        await self.telegram.start()
        tasks = [asyncio.create_task(c()) for c in (self.consume_runs, self.consume_inbox, self.consume_control,
                                                     self.scheduler_loop, self.metrics_loop, self.settings_watch,
                                                     self.selfheal_loop)]
        log.info("Worker %s آماده است (هم‌زمانی: %d)", self.id, self.settings.max_concurrent_runs)
        await self.stopping.wait()
        for t in tasks:
            t.cancel()
        if self.active:
            log.info("منتظر پایان %d اجرای فعال…", len(self.active))
            await asyncio.wait(self.active, timeout=30)
        await self.telegram.stop()
        await bus.close_redis()


def main() -> None:
    w = Worker()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, w.stopping.set)
    loop.run_until_complete(w.run())


if __name__ == "__main__":
    main()
