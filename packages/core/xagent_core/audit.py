"""ثبت لاگ حسابرسی، اعلان‌ها و رویدادهای حافظه رویدادی."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import bus
from .models import AuditLog, Notification


async def audit(
    session: AsyncSession,
    actor: str,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(AuditLog(actor=actor, action=action, target=target, detail=detail or {}, ip=ip))
    await session.flush()


async def notify(
    session: AsyncSession,
    title: str,
    body: str = "",
    level: str = "info",
    link: str | None = None,
) -> Notification:
    n = Notification(title=title, body=body, level=level, link=link)
    session.add(n)
    await session.flush()
    await bus.publish(
        "notification",
        {"id": str(n.id), "title": title, "body": body, "level": level, "link": link},
    )
    return n
