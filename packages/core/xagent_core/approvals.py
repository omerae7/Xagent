"""تصمیم یکسان روی درخواست‌های اجازه برای همه کانال‌ها (داشبورد و تلگرام).

- تأیید فقط روی درخواست «در انتظار» و تا سقف عمر کارت (PENDING_TTL)؛ کارت قدیمی منقضی می‌شود، نه تأیید.
- لغو (revoke) تأیید قبلی ممکن است؛ اجرای در جریان، پیش از اثر، وضعیت تازه پایگاه داده را می‌خواند.
- مدت اعتبار تأیید برای اجرا (APPROVED_TTL) در لحظه اثر دوباره سنجیده می‌شود.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .audit import audit
from .db import utcnow
from .models import Approval, Run

PENDING_TTL = timedelta(hours=72)
APPROVED_TTL = timedelta(hours=24)
MIN_PREFIX = 8


@dataclass
class Decision:
    ok: bool
    message: str
    approval: Approval | None = None
    requeue: uuid.UUID | None = None
    conflict: bool = False


async def decide(session: AsyncSession, a: Approval, decision: str, actor: str, note: str | None = None,
                 channel: str = "panel") -> Decision:
    """decision: approve | deny | revoke. فراخواننده Commit و صف‌کردن دوباره Run را انجام می‌دهد."""
    await session.refresh(a)  # وضعیت تازه، نه شیء کهنه نشست
    if decision == "revoke":
        if a.status != "approved":
            return Decision(False, "فقط تأیید معتبر قابل لغو است", a, conflict=True)
        a.status, a.decided_at, a.decided_by = "revoked", utcnow(), actor
        a.decision_note = note
        await audit(session, actor, "approval.revoked", a.tool_name,
                    {"category": a.category, "run_id": str(a.run_id), "channel": channel})
        return Decision(True, "تأیید لغو شد؛ عملیات بدون تأیید تازه اجرا نمی‌شود.", a)
    if a.status != "pending":
        return Decision(False, "قبلاً تصمیم‌گیری شده است", a, conflict=True)
    if decision == "approve" and utcnow() - a.created_at > PENDING_TTL:
        # کارت اجازه قدیمی ممکن است با وضعیت فعلی همخوان نباشد؛ تأیید آن عملیاتی را مجاز نمی‌کند
        a.status, a.decided_at, a.decided_by = "expired", utcnow(), actor
        await audit(session, actor, "approval.expired", a.tool_name, {"run_id": str(a.run_id), "channel": channel})
        return Decision(False, "این درخواست اجازه منقضی شده است؛ کار را دوباره درخواست کنید", a, conflict=True)
    a.status = "approved" if decision == "approve" else "denied"
    a.decided_at, a.decided_by, a.decision_note = utcnow(), actor, note
    await audit(session, actor, f"approval.{a.status}", a.tool_name,
                {"category": a.category, "run_id": str(a.run_id), "channel": channel})
    run = await session.get(Run, a.run_id)
    remaining = (await session.execute(select(func.count()).select_from(Approval).where(
        Approval.run_id == a.run_id, Approval.status == "pending"))).scalar_one()
    requeue = None
    if run is not None and remaining == 0:
        if run.status == "waiting_approval":
            run.status = "queued"
        requeue = run.id
    return Decision(True, "ثبت شد", a, requeue=requeue)


async def find_by_prefix(session: AsyncSession, prefix: str) -> tuple[Approval | None, str | None]:
    """یافتن کارت در انتظار با پیشوند شناسه (تلگرام)؛ پیشوند کوتاه یا مبهم رد می‌شود."""
    prefix = prefix.lower()
    if len(prefix) < MIN_PREFIX:
        return None, f"شناسه باید دست‌کم {MIN_PREFIX} نویسه باشد."
    rows = (await session.execute(select(Approval).where(Approval.status.in_(["pending", "approved"])))).scalars().all()
    hits = [a for a in rows if str(a.id).startswith(prefix)]
    if not hits:
        return None, "درخواست مجوزی با این شناسه پیدا نشد."
    if len(hits) > 1:
        return None, "این شناسه به بیش از یک درخواست می‌خورد؛ شناسه کامل‌تر بفرستید."
    return hits[0], None
