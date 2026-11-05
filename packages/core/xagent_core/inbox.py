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
    force_telegram: bool = False,
    telegram_text: str | None = None,
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
    await _deliver(session, item, telegram, force_telegram, telegram_text)
    return item


async def _deliver(session: AsyncSession, item: InboxItem, telegram: Any, force_telegram: bool = False,
                   telegram_text: str | None = None) -> None:
    """ثبت «قصد تحویل» در همان تراکنش گزارش (F14): هیچ ارسال بیرونی داخل تراکنشی که ممکن است برگردد انجام
    نمی‌شود. ارسال تلگرام پس از Commit با dispatch_pending و ماشین وضعیت پایدار همان ردیف انجام می‌شود؛
    اگر تراکنش برگردد، گزارش و قصد ارسالش با هم از بین می‌روند و اعلانی ارسال نمی‌شود."""
    delivery: dict[str, Any] = {"panel": "live"}
    try:
        await bus.publish("inbox.created", ser_item(item))
    except Exception:  # noqa: BLE001
        delivery["panel"] = "stored"
    try:
        cfg = await get_setting(session, "reports", ReportSettings)
        need = cfg.telegram_min_severity
        wanted = force_telegram or (need != "off" and SEVERITY_ORDER.get(item.severity, 0) >= SEVERITY_ORDER.get(need, 9))
        if wanted:
            delivery["telegram"] = "pending"
            if telegram_text:
                delivery["telegram_text"] = truncate(scrub(telegram_text), 1200)
    except Exception as e:  # noqa: BLE001 — شکست ثبت قصد اعلان نتیجه اصلی را مخدوش نمی‌کند
        log.warning("ثبت قصد اعلان تلگرام ناموفق: %s", type(e).__name__)
    item.delivery = delivery


def _receipt_ok(res: Any) -> bool:
    mid = res.get("message_id") if isinstance(res, dict) else None
    return (isinstance(mid, int) and not isinstance(mid, bool) and mid > 0) or (isinstance(mid, str) and mid.isdigit())


async def dispatch_pending(telegram: Any, limit: int = 50) -> int:
    """ارسال اعلان‌های تلگرام ثبت‌شده پس از Commit. وضعیت در خود ردیف (Outbox): pending → sending → sent|failed|unknown.

    «sending» یافت‌شده در دور بعد یعنی Crash در میانه ارسال → unknown (بدون تکرار کورکورانه). بدون رسید معتبر
    هم «sent» ثبت نمی‌شود. اتصال نبودن → not_connected (بدون ارسال)."""
    from .db import session_scope

    if telegram is None or not getattr(telegram, "connected", False):
        return 0
    sent = 0
    async with session_scope() as s:
        rows = (await s.execute(select(InboxItem).where(InboxItem.delivery["telegram"].astext.in_(["pending", "sending"]))
                                .order_by(InboxItem.ts).limit(limit).with_for_update(skip_locked=True))).scalars().all()
        claim = []
        for it in rows:
            d = dict(it.delivery or {})
            if d.get("telegram") == "sending":  # تلاش قبلی در میانه قطع شد: شاید ارسال شده باشد
                d["telegram"] = "unknown: interrupted"
            else:
                d["telegram"] = "sending"
                claim.append(it.id)
            it.delivery = d
    for iid in claim:
        async with session_scope() as s:
            it = await s.get(InboxItem, iid)
            if it is None:
                continue
            d = dict(it.delivery or {})
            text = d.pop("telegram_text", None) or f"{it.title}\n{truncate(it.body, 900)}"
            try:
                res = await telegram.notify_owner(text)
                d["telegram"] = "sent" if _receipt_ok(res) else "unknown: missing_receipt"
                sent += d["telegram"] == "sent"
            except Exception as e:  # noqa: BLE001
                ambiguous = isinstance(e, (TimeoutError, ConnectionResetError)) or "timeout" in type(e).__name__.lower()
                d["telegram"] = f"{'unknown' if ambiguous else 'failed'}: {type(e).__name__}"
            it.delivery = d
    return sent
