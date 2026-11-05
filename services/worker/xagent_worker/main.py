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
from datetime import datetime, timedelta

import psutil
from sqlalchemy import delete, func, select

from xagent_agent.dream import run_dream, summarize_conversation
from xagent_agent.pipeline import AgentRunner
from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Conversation, DreamReport, Run, SystemMetric
from xagent_core.redact import install_log_scrubber, open_secrets
from xagent_core.settings_store import DreamSettings, get_setting
from xagent_tools import installer

from .inbox import process_event, reconcile_inbox
from .inbox import record_event_failure as inbox_failure
from .scheduler import (
    after_routine_run,
    after_task_run,
    claim_scheduled_dream,
    reconcile_runs,
    settle_terminal_runs,
    tick_routines,
    tick_tasks,
)
from .telegram import TelegramBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
install_log_scrubber()
log = logging.getLogger("xagent.worker")
CONTROL_STREAM = "xagent:control"
CONTROL_STALE_MS = 3_600_000  # فرمان بدون ACK پس از یک ساعت «رهاشده» تلقی می‌شود
SCHEDULER_TICK_SECONDS = 15
# حلقه‌های ضروری و حداکثر فاصله مجاز ضربان (ثانیه) برای «آماده» بودن Worker
ESSENTIAL_LOOPS = {"runs": 30, "inbox": 30, "control": 30, "scheduler": 90, "metrics": 120}


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
        # پذیرش محدود: حداکثر دو برابر ظرفیت اجرا در حافظه (در حال اجرا + منتظر)؛ بقیه در Redis Stream می‌مانند
        self.intake_limit = max(2, self.settings.max_concurrent_runs * 2)
        self.run_jobs: dict[uuid.UUID, float] = {}
        self.dream_task: asyncio.Task | None = None
        self.control_sem = asyncio.Semaphore(2)
        self.loops: dict[str, dict] = {}  # نظارت حلقه‌های ضروری: آخرین ضربان و خطا

    def beat(self, name: str) -> None:
        self.loops.setdefault(name, {})["beat"] = utcnow().isoformat()

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
                await after_task_run(run_id, self.telegram)
                async with session_scope() as session:
                    run = await session.get(Run, run_id)
                    if run and run.conversation_id:
                        conv = await session.get(Conversation, run.conversation_id)
                        if conv and conv.message_count > 40 and conv.message_count % 10 == 0:
                            await summarize_conversation(session, conv.id)
            except Exception:  # noqa: BLE001
                log.exception("خطای پس‌پردازش %s", run_id)

    def intake_free(self) -> int:
        return max(0, self.intake_limit - len(self.run_jobs))

    async def consume_runs(self) -> None:
        r = bus.get_blocking_redis()
        while not self.stopping.is_set():
            try:
                self.beat("runs")
                free = self.intake_free()
                if free <= 0:
                    # ظرفیت پر: پیام تازه از صف برداشته نمی‌شود (حافظه و وظایف منتظر محدود می‌مانند)
                    await asyncio.sleep(0.5)
                    continue
                # بازپس‌گیری پیام‌های بی‌پاسخ Workerهای ازکارافتاده
                with contextlib.suppress(Exception):
                    claimed = await r.xautoclaim(bus.RUN_STREAM, bus.RUN_GROUP, self.id, min_idle_time=600_000,
                                                 start_id="0-0", count=min(10, free))
                    for msg_id, fields in (claimed[1] if claimed else []):
                        await self._dispatch(msg_id, fields)
                free = self.intake_free()
                if free <= 0:
                    continue
                resp = await r.xreadgroup(bus.RUN_GROUP, self.id, {bus.RUN_STREAM: ">"}, count=min(4, free), block=5000)
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

        if run_id in self.run_jobs:  # همین Run هم‌اکنون در این Worker پذیرفته شده (صف‌گذاری دوباره تطبیق)
            await r.xack(bus.RUN_STREAM, bus.RUN_GROUP, msg_id)
            return
        self.run_jobs[run_id] = asyncio.get_running_loop().time()
        with contextlib.suppress(Exception):  # نشانه پذیرش: reconcile_runs این Run منتظر را دوباره در صف نمی‌گذارد
            await r.set(f"xagent:run:intake:{run_id}", self.id, ex=900)

        async def job():
            try:
                await self._execute(run_id)
            finally:
                self.run_jobs.pop(run_id, None)
                with contextlib.suppress(Exception):
                    await r.delete(f"xagent:run:intake:{run_id}")
                await r.xack(bus.RUN_STREAM, bus.RUN_GROUP, msg_id)

        t = asyncio.create_task(job())
        self.active.add(t)
        t.add_done_callback(self.active.discard)

    async def _handle_inbox(self, r, msg_id: str, fields: dict) -> None:  # noqa: ANN001
        """ACK فقط پس از پردازش پایدار یا ثبت پایدار شکست نهایی (Dead-letter)؛ در غیر این صورت پیام در
        فهرست Pending می‌ماند تا بازپس‌گیری دوره‌ای دوباره تلاش کند."""
        try:
            event_id = uuid.UUID(fields["event_id"])
        except (KeyError, ValueError):
            await r.xack(bus.INBOX_STREAM, bus.RUN_GROUP, msg_id)  # پیام بدشکل: قابل پردازش نیست
            return
        try:
            await process_event(event_id, self.telegram)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("خطای پردازش رویداد %s", event_id)
            try:
                terminal = await inbox_failure(event_id, e)
            except Exception:  # noqa: BLE001 — حتی ثبت شکست ممکن نشد (DB): بدون ACK، دوباره تلاش می‌شود
                log.exception("ثبت شکست رویداد %s ممکن نشد", event_id)
                return
            if not terminal:
                return
        await r.xack(bus.INBOX_STREAM, bus.RUN_GROUP, msg_id)

    async def consume_inbox(self) -> None:
        r = bus.get_blocking_redis()
        while not self.stopping.is_set():
            try:
                self.beat("inbox")
                # بازپس‌گیری رویدادهای تأییدنشده (شکست قبلی یا Worker ازکارافتاده) با فاصله ۶۰ ثانیه
                with contextlib.suppress(Exception):
                    claimed = await r.xautoclaim(bus.INBOX_STREAM, bus.RUN_GROUP, self.id, min_idle_time=60_000,
                                                 start_id="0-0", count=20)
                    for msg_id, fields in (claimed[1] if claimed else []):
                        await self._handle_inbox(r, msg_id, fields)
                resp = await r.xreadgroup(bus.RUN_GROUP, self.id, {bus.INBOX_STREAM: ">"}, count=20, block=5000)
                for _stream, messages in resp or []:
                    for msg_id, fields in messages:
                        await self._handle_inbox(r, msg_id, fields)
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
                self.beat("control")
                # فرمانی که Worker قبلی پذیرفت ولی تمام نکرد کورکورانه دوباره اجرا نمی‌شود (نصب/به‌روزرسانی/رویا
                # ممکن است نیمه‌کاره اثر گذاشته باشد): به کارفرما اعلام و سپس ACK می‌شود.
                with contextlib.suppress(Exception):
                    claimed = await r.xautoclaim(CONTROL_STREAM, bus.RUN_GROUP, self.id, min_idle_time=CONTROL_STALE_MS,
                                                 start_id="0-0", count=5)
                    for msg_id, fields in (claimed[1] if claimed else []):
                        await self._control_abandoned(fields)
                        await r.xack(CONTROL_STREAM, bus.RUN_GROUP, msg_id)
                resp = await r.xreadgroup(bus.RUN_GROUP, self.id, {CONTROL_STREAM: ">"}, count=5, block=5000)
                for _stream, messages in resp or []:
                    for msg_id, fields in messages:
                        # ACK فقط پس از پایان فرمان (موفق یا شکست ثبت‌شده)؛ وظیفه ردگیری و هنگام توقف منتظر می‌ماند
                        t = asyncio.create_task(self._control_job(r, msg_id, fields))
                        self.active.add(t)
                        t.add_done_callback(self.active.discard)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("خطای کانال کنترلی")
                await asyncio.sleep(2)

    async def _control_job(self, r, msg_id: str, fields: dict) -> None:  # noqa: ANN001
        async with self.control_sem:
            ok = await self._control(fields)
        if ok is not None:
            await r.xack(CONTROL_STREAM, bus.RUN_GROUP, msg_id)

    async def _control_abandoned(self, fields: dict) -> None:
        from xagent_core import inbox as owner_inbox

        cmd = fields.get("cmd") or "?"
        async with session_scope() as session:
            await owner_inbox.post(session, kind="error", severity="important",
                                   title=f"فرمان «{cmd}» ممکن است ناتمام مانده باشد",
                                   body="Worker پیش از پایان این فرمان متوقف شد. برای جلوگیری از اثر تکراری، "
                                        "خودکار دوباره اجرا نشد؛ وضعیت ابزارها/رویا را بررسی و در صورت نیاز دوباره درخواست کنید.",
                                   dedupe_key=f"control-abandoned:{fields.get('id') or hash(json.dumps(fields, sort_keys=True))}",
                                   data={"cmd": cmd})

    async def _control(self, fields: dict) -> bool | None:
        """True موفق، False شکست ثبت‌شده؛ هر دو ACK می‌شوند (فرمان کامل شد). لغو (توقف Worker) → None، بدون ACK."""
        cmd = fields.get("cmd")
        payload = json.loads(fields.get("payload") or "{}")
        try:
            async with session_scope() as session:
                if cmd == "install":
                    await installer.install(session, payload["source"], payload.get("ref"), payload.get("name"),
                                            actor=payload.get("actor", "owner"),
                                            env=open_secrets(payload.get("env_sealed") or payload.get("env") or {}) or None)
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
            return True
        except asyncio.CancelledError:
            return None
        except Exception as e:  # noqa: BLE001
            log.exception("خطای فرمان کنترلی %s", cmd)
            try:
                from xagent_core import inbox as owner_inbox

                async with session_scope() as session:
                    await owner_inbox.post(session, kind="error", severity="important", title=f"فرمان «{cmd}» ناموفق بود",
                                           body=f"{type(e).__name__}: {str(e)[:400]}", data={"cmd": cmd})
            except Exception:  # noqa: BLE001
                log.exception("ثبت شکست فرمان کنترلی ممکن نشد")
            return False

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
                try:  # اعلان‌های تلگرام فقط پس از Commit تراکنش گزارش (F14)
                    from xagent_core import inbox as inbox_mod

                    await inbox_mod.dispatch_pending(self.telegram)
                except Exception:  # noqa: BLE001
                    log.exception("ارسال اعلان‌های صف‌شده ناموفق بود")
                n = await tick_routines(self.telegram) + await tick_tasks(self.telegram)
                if n:
                    log.info("%d روتین زمان‌بندی شد", n)
                # پایش کارهای واگذارشده به Cursor Cloud Agents — فقط HTTP، بدون هیچ فراخوانی LLM
                try:
                    from xagent_tools.cursor_agents import poll_due

                    await poll_due()
                except Exception:  # noqa: BLE001
                    log.exception("پایش Cursor ناموفق بود")
                if loop.time() - last_reconcile > 60:
                    last_reconcile = loop.time()
                    await reconcile_runs()
                    # A3: Run پایانی بدون تسویه روتین/تسک؛ A4: رویداد ثبت‌شده‌ای که به صف نرسید
                    await settle_terminal_runs(telegram=self.telegram)
                    await reconcile_inbox()
                if loop.time() - last_dream_check > 60:
                    last_dream_check = loop.time()
                    await self.maybe_start_dream()
            except Exception:  # noqa: BLE001
                log.exception("خطای زمان‌بند")
            self.beat("scheduler")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stopping.wait(), timeout=SCHEDULER_TICK_SECONDS)

    async def maybe_start_dream(self) -> bool:
        """رویای شبانه در وظیفه جداگانه و نظارت‌شده اجرا می‌شود تا تیک زمان‌بند (روتین/تسک/بازیابی) منتظر آن نماند.
        حداکثر یک رویا هم‌زمان؛ Claim یکتای شبانه و سقف زمان/هزینه رویا بدون تغییر حفظ شده است."""
        if self.dream_task is not None and not self.dream_task.done():
            return False
        lease = await bus.acquire_lease("dream", 3_600_000)
        if not lease:
            return False
        try:
            report_id = await claim_scheduled_dream()
        except Exception:  # noqa: BLE001
            await bus.release_lease("dream", lease)
            raise
        if not report_id:
            await bus.release_lease("dream", lease)
            return False
        self.dream_task = asyncio.create_task(self._run_scheduled_dream(report_id, lease))
        self.active.add(self.dream_task)
        self.dream_task.add_done_callback(self.active.discard)
        return True

    async def _run_scheduled_dream(self, report_id, lease: str) -> None:  # noqa: ANN001
        try:
            async with session_scope() as cfg_session:
                dream_cfg = await get_setting(cfg_session, "dream", DreamSettings)
            async with asyncio.timeout(dream_cfg.max_runtime_seconds):
                async with session_scope() as session:
                    rep = await run_dream(session, report_id=report_id)
                    log.info("رویا: %s", rep.status)
        except Exception as exc:  # noqa: BLE001
            # The original claim was committed in a separate transaction, so failures cannot create a retry storm.
            self.loops.setdefault("dream", {})["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            async with session_scope() as failure_session:
                report = await failure_session.get(DreamReport, report_id)
                if report and report.status == "running":
                    report.status = "failed"
                    report.finished_at = utcnow()
                    report.summary = f"Dream failed after claim: {type(exc).__name__}"
            log.exception("Dream failed (slot %s)", report_id)
        finally:
            await bus.release_lease("dream", lease)

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
                self.beat("metrics")
                await self.publish_loops()
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

    async def supervise(self, name: str, fn, max_restarts: int = 20) -> None:  # noqa: ANN001
        """نظارت حلقه ضروری: خطای غیرمنتظره ثبت، با Backoff محدود دوباره راه‌اندازی، و پس از سقف «failed» اعلام
        می‌شود تا سلامت Worker «کامل» گزارش نشود (Heartbeat به‌تنهایی دلیل سلامت نیست)."""
        st = self.loops.setdefault(name, {})
        st.update(status="running", restarts=st.get("restarts", 0))
        delay = 1.0
        while not self.stopping.is_set():
            try:
                await fn()
                if self.stopping.is_set():
                    break
                raise RuntimeError("حلقه بدون توقف Worker پایان یافت")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                st["restarts"] = st.get("restarts", 0) + 1
                st["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
                st["last_error_at"] = utcnow().isoformat()
                log.exception("حلقه ضروری %s از کار افتاد (بار %d)", name, st["restarts"])
                if st["restarts"] >= max_restarts:
                    st["status"] = "failed"
                    await self.publish_loops()
                    return
                st["status"] = "restarting"
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), timeout=delay)
                delay = min(delay * 2, 60.0)
                st["status"] = "running"
        st["status"] = "stopped"

    def readiness(self) -> dict:
        """آمادگی عملکردی (نه فقط زنده‌بودن): هر حلقه ضروری باید اجرا و اخیراً ضربان داشته باشد."""
        now = utcnow()
        degraded = []
        for name, limit in ESSENTIAL_LOOPS.items():
            st = self.loops.get(name) or {}
            beat = st.get("beat")
            age = (now - datetime.fromisoformat(beat)).total_seconds() if beat else None
            if st.get("status") not in ("running", None) or age is None or age > limit:
                degraded.append({"loop": name, "status": st.get("status") or "unknown", "beat_age_s": age,
                                 "last_error": st.get("last_error")})
        return {"ready": not degraded, "degraded": degraded, "intake": {
            "jobs": len(self.run_jobs), "limit": self.intake_limit,
            "oldest_wait_s": (asyncio.get_running_loop().time() - min(self.run_jobs.values())) if self.run_jobs else 0}}

    async def publish_loops(self) -> None:
        with contextlib.suppress(Exception):
            await bus.get_redis().set("xagent:worker:readiness",
                                      json.dumps({**self.readiness(), "loops": self.loops, "worker": self.id,
                                                  "at": utcnow().isoformat()}, default=str), ex=120)

    async def run(self) -> None:
        await bus.ensure_groups()
        await bus.get_redis().set("xagent:worker:heartbeat", utcnow().isoformat(), ex=120)
        await self.recover()
        await self.telegram.start()
        tasks = [asyncio.create_task(self.supervise(name, fn)) for name, fn in (
            ("runs", self.consume_runs), ("inbox", self.consume_inbox), ("control", self.consume_control),
            ("scheduler", self.scheduler_loop), ("metrics", self.metrics_loop), ("settings", self.settings_watch),
            ("selfheal", self.selfheal_loop))]
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
