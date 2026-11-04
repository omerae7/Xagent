"""میز کار و گزارش‌های ایجنت (Owner Inbox).

- هر گزارش به Run/روتین/مأموریت واقعی متصل است و با dedupe_key فقط یک بار ثبت می‌شود.
- ثبت گزارش در تراکنش جاری انجام می‌شود؛ تحویل جانبی (رویداد زنده، تلگرام کارفرما) خطای خودش را
  فقط در delivery ثبت می‌کند و هرگز نتیجه اصلی Run را خراب نمی‌کند.
- نتیجه «بدون تغییر» (NO_CHANGE) گزارش ساخته نمی‌شود؛ اجرای روتین اما در شمارنده‌ها ثبت می‌شود.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from . import bus
from .models import InboxItem
from .redact import scrub
from .settings_store import ReportSettings, get_setting
from .text import truncate

log = logging.getLogger("xagent.inbox")
SEVERITY_ORDER = {"info": 0, "important": 1, "critical": 2}


def ser_item(it: InboxItem) -> dict[str, Any]:
    return {
        "id": str(it.id), "ts": it.ts.isoformat() if it.ts else None, "kind": it.kind, "severity": it.severity,
        "title": it.title, "body": it.body, "run_id": str(it.run_id) if it.run_id else None,
        "routine_id": str(it.routine_id) if it.routine_id else None,
        "mission_id": str(it.mission_id) if it.mission_id else None,
        "conversation_id": str(it.conversation_id) if it.conversation_id else None,
        "link": it.link, "data": it.data or {}, "delivery": it.delivery or {}, "read": it.read,
        "archived": it.archived,
    }


async def post(
    session: AsyncSession,
    *,
    kind: str,
    title: str,
    body: str = "",
    severity: str = "info",
    run_id: uuid.UUID | None = None,
    routine_id: uuid.UUID | None = None,
    mission_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    link: str | None = None,
    dedupe_key: str | None = None,
    data: dict[str, Any] | None = None,
    telegram: Any = None,
) -> InboxItem | None:
    """ثبت گزارش (یک‌بار برای هر dedupe_key). خروجی None یعنی قبلاً ثبت شده بود."""
    values = {
        "id": uuid.uuid4(), "kind": kind, "severity": severity if severity in SEVERITY_ORDER else "info",
        "title": truncate(scrub(title), 250), "body": truncate(scrub(body or ""), 6000), "run_id": run_id,
        "routine_id": routine_id, "mission_id": mission_id, "conversation_id": conversation_id, "link": link,
        "dedupe_key": dedupe_key, "data": data or {}, "delivery": {},
    }
    from .db import utcnow

    values["ts"] = utcnow()
    stmt = insert(InboxItem).values(**values)
    if dedupe_key:
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    res = await session.execute(stmt.returning(InboxItem.id))
    new_id = res.scalar_one_or_none()
    if new_id is None:
        return None
    item = (await session.execute(select(InboxItem).where(InboxItem.id == new_id))).scalar_one()
    await _deliver(session, item, telegram)
    return item


async def _deliver(session: AsyncSession, item: InboxItem, telegram: Any) -> None:
    delivery: dict[str, Any] = {"panel": "live"}
    try:
        await bus.publish("inbox.created", ser_item(item))
    except Exception:  # noqa: BLE001
        delivery["panel"] = "stored"
    try:
        cfg = await get_setting(session, "reports", ReportSettings)
        need = cfg.telegram_min_severity
        if need != "off" and SEVERITY_ORDER.get(item.severity, 0) >= SEVERITY_ORDER.get(need, 9):
            if telegram is not None and getattr(telegram, "connected", False):
                await telegram.notify_owner(f"{item.title}\n{truncate(item.body, 900)}")
                delivery["telegram"] = "sent"
            else:
                delivery["telegram"] = "not_connected"
    except Exception as e:  # noqa: BLE001 — شکست اعلان نتیجه اصلی را مخدوش نمی‌کند
        delivery["telegram"] = f"failed: {type(e).__name__}"
        log.warning("ارسال اعلان تلگرام ناموفق: %s", type(e).__name__)
    item.delivery = delivery
