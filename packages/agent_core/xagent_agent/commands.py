"""فرمان‌های قطعی که بدون فراخوانی مدل اجرا می‌شوند (رویدادمحور و بدون توکن‌سوزی)."""

from __future__ import annotations

from datetime import timedelta

import psutil
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.audit import audit
from xagent_core.models import Routine, Run, UsageEvent
from xagent_core.text import fa_digits
from xagent_core.timeutil import now_utc
from xagent_memory.revisions import undo_last

HELP = """فرمان‌های سریع (بدون مصرف توکن):
• «وضعیت» یا /status — وضعیت سیستم، کارهای فعال و هزینه امروز
• «توقف همه» یا /stop — توقف اضطراری همه کارها و روتین‌ها
• «ادامه همه» یا /resume — ادامه کارها
• «آندو» یا /undo [تعداد] — بازگردانی آخرین یادگیری(ها)
• «راهنما» یا /help — همین راهنما"""


async def run_command(session: AsyncSession, name: str, arg: str, actor: str = "owner") -> str:
    if name == "help":
        return HELP
    if name == "stop_all":
        await bus.set_stop_all(True)
        await audit(session, actor, "system.stop_all")
        await bus.publish("system.stop_all", {"on": True})
        return "⛔ توقف اضطراری فعال شد. همه کارها در اولین نقطه امن متوقف و روتین‌ها معلق شدند. برای ادامه: «ادامه همه»"
    if name == "resume_all":
        await bus.set_stop_all(False)
        await audit(session, actor, "system.resume_all")
        paused = (
            await session.execute(
                update(Run)
                .where(Run.status == "paused", Run.stop_reason == "stop_all")
                .values(status="queued", stop_reason=None)
                .returning(Run.id)
            )
        ).scalars().all()
        await session.flush()
        for rid in paused:
            await bus.enqueue_run(rid, "resume_all")
        await bus.publish("system.stop_all", {"on": False})
        return f"▶️ سیستم دوباره فعال شد. {fa_digits(len(paused))} کار متوقف‌شده ادامه می‌یابد."
    if name == "undo":
        count = max(1, min(int(arg or 1), 20))
        res = await undo_last(session, actor=actor, count=count)
        await audit(session, actor, "memory.undo", None, {"count": count, "reverted": res.reverted})
        return f"↩️ {res.message}"
    if name == "status":
        active = (
            await session.execute(select(func.count()).select_from(Run).where(Run.status.in_(["running", "queued"])))
        ).scalar_one()
        waiting = (
            await session.execute(select(func.count()).select_from(Run).where(Run.status == "waiting_approval"))
        ).scalar_one()
        since = now_utc() - timedelta(hours=24)
        spend, tokens = (
            await session.execute(
                select(
                    func.coalesce(func.sum(UsageEvent.cost_usd), 0),
                    func.coalesce(func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), 0),
                ).where(UsageEvent.ts >= since)
            )
        ).one()
        routines = (
            await session.execute(
                select(func.count()).select_from(Routine).where(Routine.enabled.is_(True), Routine.paused.is_(False))
            )
        ).scalar_one()
        stop = await bus.is_stop_all()
        mem = psutil.virtual_memory().percent
        cpu = psutil.cpu_percent(interval=0.2)
        lines = [
            "📊 وضعیت X Agent",
            f"• حالت: {'⛔ توقف اضطراری' if stop else '✅ فعال'}",
            f"• کارهای در حال اجرا/صف: {fa_digits(active)} | منتظر اجازه: {fa_digits(waiting)}",
            f"• روتین‌های فعال: {fa_digits(routines)}",
            f"• مصرف ۲۴ ساعت: {fa_digits(f'{int(tokens):,}')} توکن | ${float(spend):.4f}",
            f"• بار سیستم: CPU {fa_digits(int(cpu))}٪ | RAM {fa_digits(int(mem))}٪",
        ]
        return "\n".join(lines)
    return "فرمان ناشناخته"
