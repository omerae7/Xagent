"""ممیزی جرم‌شناختی — مرحله ۵، F12: دریافت پایدار تلگرام (قطعی دیتابیس، ری‌استارت Worker، سرریز صریح).

پیام‌ها ساختگی‌اند و به حساب تلگرام واقعی وصل نمی‌شود؛ قطعی دیتابیس با جایگزینی session_scope شبیه‌سازی می‌شود.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select

from xagent_core.db import session_scope
from xagent_core.models import EventInbox, InboxItem


async def _aw(v):
    return v


def _event(chat_id: int, mid: int, text: str = "سلام آزمون F12"):
    msg = SimpleNamespace(chat_id=chat_id, id=mid, message=text, out=False, media=None, date=None, geo=None)
    sender = SimpleNamespace(id=chat_id, username=f"u{chat_id}", first_name="f12", last_name=None, bot=False)
    return SimpleNamespace(message=msg, is_private=True, get_chat=lambda: _aw(sender), get_sender=lambda: _aw(sender))


def _db_down(monkeypatch, tgmod):
    from sqlalchemy.exc import OperationalError

    def broken():
        raise OperationalError("INSERT", {}, Exception("connection refused"))
    monkeypatch.setattr(tgmod, "session_scope", broken)


async def _count(dedup: str) -> int:
    async with session_scope() as s:
        return (await s.execute(select(func.count()).select_from(EventInbox).where(EventInbox.dedup_key == dedup))).scalar_one()


async def test_F12_event_survives_db_outage_and_worker_restart(app_client, monkeypatch):
    from xagent_worker import telegram as tgmod

    real = tgmod.session_scope
    bridge = tgmod.TelegramBridge()
    bridge.me = SimpleNamespace(id=1)
    _db_down(monkeypatch, tgmod)
    await bridge._on_message(_event(81201, 5, "متن محرمانه F12"))
    monkeypatch.setattr(tgmod, "session_scope", real)
    restarted = tgmod.TelegramBridge()  # ری‌استارت Worker: حافظه فرایند قبلی از دست رفته است
    await restarted.flush_pending()
    assert await _count("tg:81201:5") == 1, "پیام دریافت‌شده هنگام قطعی دیتابیس با ری‌استارت Worker از دست رفت"
    await restarted.flush_pending()
    assert await _count("tg:81201:5") == 1  # بازپخش دوباره تکراری نمی‌سازد


async def test_F12_spool_is_encrypted_bounded_and_overflow_is_explicit(app_client, monkeypatch):
    from xagent_worker import telegram as tgmod

    real = tgmod.session_scope
    bridge = tgmod.TelegramBridge()
    bridge.me = SimpleNamespace(id=1)
    bridge.spool.max_items = 2
    _db_down(monkeypatch, tgmod)
    for mid in (1, 2, 3, 4):
        await bridge._on_message(_event(81202, mid, f"پیام خصوصی {mid}"))
    raw = bridge.spool.file.read_bytes()
    assert "پیام خصوصی".encode() not in raw, "صف بازیابی متن پیام را رمزنشده روی دیسک نگه داشت"
    assert len(bridge.spool) == 2 and bridge.spool.gaps()[0]["count"] == 2  # سرریز بی‌صدا نیست
    monkeypatch.setattr(tgmod, "session_scope", real)
    await bridge.flush_pending()
    assert await _count("tg:81202:1") == 1 and await _count("tg:81202:2") == 1 and len(bridge.spool) == 0
    async with session_scope() as s:
        item = (await s.execute(select(InboxItem).where(InboxItem.title.like("%ثبت نشدند%"))
                                .order_by(InboxItem.ts.desc()))).scalars().first()
    assert item is not None and "3 تا 4" in item.body and "تضمین نمی‌شود" in item.body, "شکاف برای بررسی گزارش نشد"
    assert bridge.spool.gaps() == []
