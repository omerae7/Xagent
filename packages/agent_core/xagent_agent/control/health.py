"""سلامت عملیاتی مدل‌ها: ثبت موفقیت/شکست، طبقه‌بندی خطا و دوره خنک‌شدن برای Fallback معتبر.

به‌روزرسانی‌ها در تراکنش کوتاه و مستقل انجام می‌شوند تا قفل ردیف مدل در طول اجرای طولانی یک Run
نگه داشته نشود (جلوگیری از انتظار/بن‌بست بین اجراهای هم‌زمان و تفویض به کارمند).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import AIModel

from .errors import ErrorClass, classify


async def record_success(session: AsyncSession, model_id: uuid.UUID) -> None:
    cur = await session.get(AIModel, model_id)
    now = utcnow()
    if cur is not None and cur.health == "ok" and not cur.consecutive_failures and cur.last_ok_at \
            and now - cur.last_ok_at < timedelta(minutes=10):
        return
    async with session_scope() as s2:
        m = await s2.get(AIModel, model_id)
        if m is None:
            return
        m.health, m.consecutive_failures, m.cooldown_until, m.last_ok_at = "ok", 0, None, now
    if cur is not None:
        await session.refresh(cur)


async def record_failure(session: AsyncSession, model_id: uuid.UUID, message: str,
                         status: int | None = None) -> ErrorClass:
    ec = classify(message, status)
    async with session_scope() as s2:
        m = await s2.get(AIModel, model_id)
        if m is None:
            return ec
        m.consecutive_failures = (m.consecutive_failures or 0) + 1
        m.last_error = message[:1000]
        m.last_error_class = ec.code
        if not ec.retryable or m.consecutive_failures >= 3:
            m.health = "failing"
            if ec.cooldown_minutes:
                m.cooldown_until = utcnow() + timedelta(minutes=ec.cooldown_minutes * min(m.consecutive_failures, 4))
        else:
            m.health = "degraded"
            if ec.cooldown_minutes and m.consecutive_failures >= 2:
                m.cooldown_until = utcnow() + timedelta(minutes=ec.cooldown_minutes)
        label = m.display_name or m.model_name
        health = m.health
        from .selfheal import open_incident

        await open_incident(s2, "model", str(m.id), ec.code, f"{label}: {message[:400]}")
    cur = await session.get(AIModel, model_id)
    if cur is not None:
        await session.refresh(cur)
    await bus.publish("model.health", {"id": str(model_id), "health": health, "error_class": ec.code, "label": label})
    return ec


def in_cooldown(m: AIModel) -> bool:
    return bool(m.cooldown_until and m.cooldown_until > utcnow())
