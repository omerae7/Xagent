"""ممیزی جرم‌شناختی — مرحله ۵، F15: تسویه Run پایانی قدیمی بدون نقطه‌کور سن ایجاد، با صفحه‌بندی و پیشرفت تضمینی."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import update

from xagent_core.db import session_scope, utcnow
from xagent_core.models import Routine, Run


async def _routine_run(created_days_ago: int, finished_minutes_ago: int = 10) -> uuid.UUID:
    async with session_scope() as s:
        rt = Routine(name=f"F15 {uuid.uuid4().hex[:6]}", instruction="x", trigger_type="interval", interval_seconds=3600)
        s.add(rt)
        await s.flush()
        run = Run(trigger="routine", input="x", status="succeeded", routine_id=rt.id, state={},
                  finished_at=utcnow() - timedelta(minutes=finished_minutes_ago))
        s.add(run)
        await s.flush()
        rid = run.id
    async with session_scope() as s:  # created_at با server default؛ عقب بردن صریح برای شبیه‌سازی Run قدیمی
        await s.execute(update(Run).where(Run.id == rid).values(created_at=utcnow() - timedelta(days=created_days_ago)))
    return rid


async def _settled(rid) -> bool:
    async with session_scope() as s:
        run = await s.get(Run, rid)
        return (run.state or {}).get("routine_settled") == run.status


async def test_F15_old_terminal_run_is_settled_regardless_of_creation_age(app_client):
    from xagent_worker.scheduler import settle_terminal_runs

    old = await _routine_run(created_days_ago=45)
    await settle_terminal_runs()
    assert await _settled(old), "Run پایانی تسویه‌نشده به‌دلیل قدیمی بودن تاریخ ایجاد هرگز تسویه نشد"


async def test_F15_failing_run_does_not_block_progress_and_is_tracked(app_client, monkeypatch):
    from xagent_worker import scheduler

    bad = await _routine_run(created_days_ago=60, finished_minutes_ago=50)  # نخستین در ترتیب
    good1 = await _routine_run(created_days_ago=59, finished_minutes_ago=40)
    good2 = await _routine_run(created_days_ago=58, finished_minutes_ago=30)
    real = scheduler.after_routine_run

    async def flaky(rid, telegram=None):
        if rid == bad:
            raise RuntimeError("settlement backend error")
        return await real(rid, telegram)
    monkeypatch.setattr(scheduler, "after_routine_run", flaky)
    await scheduler.settle_terminal_runs(batch=1)  # صفحه‌بندی محدود: هر صفحه یک Run
    assert await _settled(good1) and await _settled(good2), "Run خراب مانع پیشرفت تسویه بقیه شد"
    async with session_scope() as s:
        st = (await s.get(Run, bad)).state or {}
    assert st.get("settle_errors") == 1 and st.get("settle_next_at"), "خطای تسویه ثبت/ردیابی نشد"
    await scheduler.settle_terminal_runs(batch=1)
    async with session_scope() as s:
        assert ((await s.get(Run, bad)).state or {}).get("settle_errors") == 1, "پیش از موعد عقب‌نشینی دوباره تلاش شد"


async def test_F15_settlement_is_idempotent_across_passes(app_client):
    from xagent_worker.scheduler import settle_terminal_runs

    rid = await _routine_run(created_days_ago=20)
    await settle_terminal_runs()
    async with session_scope() as s:
        run = await s.get(Run, rid)
        count1 = (await s.get(Routine, run.routine_id)).run_count
    await settle_terminal_runs()
    async with session_scope() as s:
        run = await s.get(Run, rid)
        assert (await s.get(Routine, run.routine_id)).run_count == count1, "شمارنده روتین دوباره افزایش یافت"
