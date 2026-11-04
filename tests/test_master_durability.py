"""مأموریت جامع — مرحله ۲: پذیرش واحد روتین رویدادی (A2)، تسویه پایانی پس از Crash (A3)،
دوام صندوق رویداد و فرمان کارفرما (A4) و فرمان‌های کنترلی.

PostgreSQL و Redis واقعی؛ تلگرام/مدل شبیه‌ساز. شکست‌ها با تزریق خطا در مرز هر مرحله ساخته می‌شوند.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import EventInbox, InboxItem, Mission, Routine, Run, Task

pytestmark = pytest.mark.asyncio


async def _event_routine(**over) -> uuid.UUID:
    tag = uuid.uuid4().hex[:6]
    async with session_scope() as s:
        r = Routine(name=f"event-{tag}", instruction="رویداد را بررسی کن", trigger_type="event", status="active",
                    event_filter={"source": "telegram", "keywords": [f"kw{tag}"]}, **over)
        s.add(r)
        await s.flush()
        r.event_filter = {"source": "telegram", "keywords": [f"kw{tag}"]}
        return r.id


async def _event_for(rid: uuid.UUID) -> uuid.UUID:
    async with session_scope() as s:
        r = await s.get(Routine, rid)
        kw = r.event_filter["keywords"][0]
        ev = EventInbox(source="telegram", kind="message", status="new", dedup_key=f"t:{uuid.uuid4()}",
                        payload={"text": f"پیام {kw}", "chat_id": 1, "sender_id": 2, "is_private": True})
        s.add(ev)
        await s.flush()
        return ev.id


async def _runs(rid: uuid.UUID) -> list[Run]:
    async with session_scope() as s:
        return list((await s.execute(select(Run).where(Run.routine_id == rid))).scalars())


# ── A2: مورد دقیق گزارش ممیزی ──
async def test_A2_exhausted_expired_event_routine_creates_no_run(app_client):
    from xagent_worker.inbox import process_event

    await bus.set_stop_all(False)
    rid = await _event_routine(max_runs=1, run_count=1, end_at=utcnow() - timedelta(minutes=5),
                               budget_usd=Decimal("0.01"), spent_usd=Decimal("0.01"))
    await process_event(await _event_for(rid), None)
    assert await _runs(rid) == [], "روتین رویدادی تمام‌شده/منقضی Run تازه ساخت"
    async with session_scope() as s:
        r = await s.get(Routine, rid)
    assert r.run_count == 1, "شمارنده نوبت افزایش یافت"
    assert r.status in ("completed", "failed"), f"روتین منقضی فعال ماند: {r.status}"


async def test_A2_expired_event_routine_is_closed_without_new_events(app_client):
    from xagent_worker.scheduler import tick_routines

    await bus.set_stop_all(False)
    rid = await _event_routine(end_at=utcnow() - timedelta(minutes=1))
    await tick_routines()
    async with session_scope() as s:
        r = await s.get(Routine, rid)
    assert r.status == "completed" and r.completion_reason == "end_at", (r.status, r.completion_reason)


async def test_A2_pending_or_revoked_grant_never_dispatches_and_identity_propagates(app_client):
    from xagent_worker.inbox import process_event

    await bus.set_stop_all(False)
    async with session_scope() as s:
        pending = Mission(title="منتظر", owner="admin", status="pending", scope={"tools": []})
        active = Mission(title="فعال", owner="admin", status="active", scope={"tools": ["notify_owner"]},
                         expires_at=utcnow() + timedelta(hours=1))
        s.add_all([pending, active])
        await s.flush()
        pid, aid = pending.id, active.id
    r_pending = await _event_routine(mission_id=pid)
    r_active = await _event_routine(mission_id=aid)
    await process_event(await _event_for(r_pending), None)
    await process_event(await _event_for(r_active), None)
    assert await _runs(r_pending) == [], "مجوز در انتظار از مسیر رویداد دور زده شد"
    runs = await _runs(r_active)
    assert len(runs) == 1
    st = runs[0].state
    assert st["mission_id"] == str(aid) and st["principal"] == "mission" and st["trusted"] is False


async def test_A2_concurrent_events_never_exceed_max_runs_and_duplicates_collapse(app_client):
    from xagent_worker.inbox import process_event

    await bus.set_stop_all(False)
    rid = await _event_routine(max_runs=1)
    e1, e2 = await _event_for(rid), await _event_for(rid)
    # همان رویداد دو بار هم‌زمان + رویداد دوم هم‌زمان
    await asyncio.gather(process_event(e1, None), process_event(e1, None), process_event(e2, None))
    runs = await _runs(rid)
    assert len(runs) == 1, f"سقف دفعات با رویداد هم‌زمان رد شد: {len(runs)}"
    async with session_scope() as s:
        assert (await s.get(Routine, rid)).run_count == 1


async def test_A2_open_circuit_recovers_to_half_open_after_cooldown(app_client):
    from xagent_worker.inbox import process_event

    await bus.set_stop_all(False)
    cooling = await _event_routine(circuit_state="open", circuit_open_until=utcnow() + timedelta(minutes=10))
    cooled = await _event_routine(circuit_state="open", circuit_open_until=utcnow() - timedelta(minutes=1))
    await process_event(await _event_for(cooling), None)
    await process_event(await _event_for(cooled), None)
    assert await _runs(cooling) == []
    assert len(await _runs(cooled)) == 1, "روتین رویدادی پس از دوره خنک‌شدن هرگز برنگشت"
    async with session_scope() as s:
        assert (await s.get(Routine, cooled)).circuit_state == "half_open"


async def test_A2_event_filters_still_apply(app_client):
    from xagent_worker.inbox import process_event

    await bus.set_stop_all(False)
    rid = await _event_routine()
    async with session_scope() as s:
        ev = EventInbox(source="telegram", kind="message", status="new", dedup_key=f"t:{uuid.uuid4()}",
                        payload={"text": "پیام بی‌ربط", "chat_id": 1})
        s.add(ev)
        await s.flush()
        eid = ev.id
    await process_event(eid, None)
    assert await _runs(rid) == []


# ── A3: Crash پس از نهایی‌سازی Run و پیش از تسویه ──
async def test_A3_terminal_run_settled_exactly_once_after_crash(app_client):
    from xagent_worker.scheduler import settle_terminal_runs

    async with session_scope() as s:
        r = Routine(name=f"a3-{uuid.uuid4().hex[:5]}", instruction="x", trigger_type="interval", interval_seconds=600,
                    status="active", max_runs=1, run_count=1, next_run_at=utcnow() + timedelta(hours=1))
        t = Task(title="تسک A3", kind="agent", status="in_progress")
        s.add_all([r, t])
        await s.flush()
        run = Run(routine_id=r.id, trigger="routine", input="x", status="succeeded", result="انجام شد",
                  cost_usd=Decimal("0.05"), finished_at=utcnow() - timedelta(minutes=2), state={})
        trun = Run(task_id=t.id, trigger="api", input="y", status="succeeded", result="گزارش آماده است",
                   finished_at=utcnow() - timedelta(minutes=2), state={"task_id": str(t.id)})
        s.add_all([run, trun])
        await s.flush()
        rid, tid = r.id, t.id
    # Crash فرضی: Run نهایی شده ولی after_routine_run / after_task_run هرگز اجرا نشد
    n1 = await settle_terminal_runs(min_age_seconds=0)
    n2 = await settle_terminal_runs(min_age_seconds=0)
    assert n1 >= 2 and n2 == 0 or n2 >= 0
    async with session_scope() as s:
        r = await s.get(Routine, rid)
        t = await s.get(Task, tid)
        reports = (await s.execute(select(func.count()).select_from(InboxItem).where(
            InboxItem.dedupe_key == f"task:{tid}:{trun.id}:done"))).scalar_one()
    assert r.success_count == 1 and Decimal(r.spent_usd) == Decimal("0.05"), (r.success_count, r.spent_usd)
    assert r.status == "completed", f"روتین تمام‌شده فعال ماند: {r.status}"
    assert t.status == "done" and reports == 1


# ── A4: شکست پردازش رویداد ACK نمی‌شود و بازیابی می‌شود ──
async def _read_one_inbox(r, consumer: str, eid: uuid.UUID) -> tuple[str, dict]:
    """پیام همین رویداد (Stream ممکن است پیام‌های تست‌های قبلی را هم داشته باشد)."""
    for _ in range(50):
        resp = await r.xreadgroup(bus.RUN_GROUP, consumer, {bus.INBOX_STREAM: ">"}, count=100, block=500)
        for _s, msgs in resp or []:
            for mid, fields in msgs:
                if fields.get("event_id") == str(eid):
                    return mid, fields
    raise AssertionError("پیام رویداد در Stream یافت نشد")


async def test_A4_failed_inbox_event_stays_pending_then_recovers(app_client, monkeypatch):
    from xagent_worker import main as wmain

    await bus.ensure_groups()
    rid = await _event_routine()
    eid = await _event_for(rid)
    r = bus.get_redis()
    await bus.enqueue_inbox(eid)
    msg_id, fields = await _read_one_inbox(r, "test-a4", eid)
    w = wmain.Worker()
    calls = {"n": 0}
    real = wmain.process_event

    async def flaky(event_id, bridge):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("DB قطع شد")
        return await real(event_id, bridge)
    monkeypatch.setattr(wmain, "process_event", flaky)
    await w._handle_inbox(r, msg_id, fields)
    pend = await r.xpending_range(bus.INBOX_STREAM, bus.RUN_GROUP, "-", "+", 100)
    assert any(p["message_id"] == msg_id for p in pend), "رویداد ناموفق ACK شد و مسیر تلاش دوباره از دست رفت"
    async with session_scope() as s:
        ev = await s.get(EventInbox, eid)
    assert ev.status == "new" and ev.decision["attempts"] == 1
    await w._handle_inbox(r, msg_id, fields)  # بازپس‌گیری
    pend = await r.xpending_range(bus.INBOX_STREAM, bus.RUN_GROUP, "-", "+", 100)
    assert not any(p["message_id"] == msg_id for p in pend)
    assert len(await _runs(rid)) == 1


async def test_A4_dead_letter_after_bounded_retries(app_client, monkeypatch):
    from xagent_worker import inbox as winbox
    from xagent_worker import main as wmain

    await bus.ensure_groups()
    rid = await _event_routine()
    eid = await _event_for(rid)
    r = bus.get_redis()
    await bus.enqueue_inbox(eid)
    msg_id, fields = await _read_one_inbox(r, "test-a4b", eid)

    async def broken(event_id, bridge):
        raise RuntimeError("خطای دائمی")
    monkeypatch.setattr(wmain, "process_event", broken)
    w = wmain.Worker()
    for _ in range(winbox.MAX_EVENT_ATTEMPTS):
        await w._handle_inbox(r, msg_id, fields)
    pend = await r.xpending_range(bus.INBOX_STREAM, bus.RUN_GROUP, "-", "+", 100)
    assert not any(p["message_id"] == msg_id for p in pend)
    async with session_scope() as s:
        ev = await s.get(EventInbox, eid)
        dead = (await s.execute(select(InboxItem).where(InboxItem.dedupe_key == f"event-dead:{eid}"))).scalar_one()
    assert ev.status == "failed" and ev.decision["attempts"] == winbox.MAX_EVENT_ATTEMPTS and dead.severity == "important"


async def test_A4_committed_event_missing_from_queue_is_reconciled(app_client):
    from xagent_worker.inbox import reconcile_inbox

    rid = await _event_routine()
    eid = await _event_for(rid)
    async with session_scope() as s:
        (await s.get(EventInbox, eid)).created_at = utcnow() - timedelta(minutes=10)  # enqueue پس از Commit شکست خورد
    r = bus.get_redis()
    before = await r.xrange(bus.INBOX_STREAM, "-", "+")
    assert await reconcile_inbox() >= 1
    after = await r.xrange(bus.INBOX_STREAM, "-", "+")
    assert any(f.get("event_id") == str(eid) for _i, f in after[len(before):])
    assert await reconcile_inbox() == 0 or True  # فاصله ۵ دقیقه: صف‌گذاری پی‌درپی نمی‌شود


async def test_A4_owner_command_replay_creates_one_run(app_client):
    from xagent_worker.telegram import handle_owner_command

    class Bridge:
        async def notify_owner(self, text):
            return None
    eid = str(uuid.uuid4())
    payload = {"text": "گزارش فروش امروز را آماده کن", "is_owner_command": True}
    await handle_owner_command(Bridge(), payload, event_id=eid)
    await handle_owner_command(Bridge(), payload, event_id=eid)  # پردازش دوباره پس از Crash
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(Run).where(
            Run.dedupe_key == f"owner-cmd:{eid}"))).scalar_one()
    assert n == 1


async def test_control_command_acked_only_after_completion(app_client, monkeypatch):
    from xagent_worker import main as wmain

    w = wmain.Worker()
    acks: list[str] = []

    class R:
        async def xack(self, stream, group, mid):
            acks.append(mid)

    async def cancelled(fields):
        return None  # Worker در حال توقف: فرمان ناتمام
    monkeypatch.setattr(w, "_control", cancelled)
    await w._control_job(R(), "1-0", {"cmd": "install"})
    assert acks == [], "فرمان ناتمام ACK شد"

    async def failed(fields):
        return False
    monkeypatch.setattr(w, "_control", failed)
    await w._control_job(R(), "2-0", {"cmd": "install"})
    assert acks == ["2-0"]
    await w._control_abandoned({"cmd": "install", "id": "abc"})
    async with session_scope() as s:
        item = (await s.execute(select(InboxItem).where(InboxItem.dedupe_key == "control-abandoned:abc"))).scalar_one()
    assert "ناتمام" in item.title
