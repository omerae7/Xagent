"""پیش‌بررسی قطعی روتین‌ها (مسیر سبک: Scheduler → Deterministic Check → Conditional LLM).

خواندن رویداد جدید بدون مدل انجام می‌شود. اگر چیزی تغییر نکرده باشد اجرا با نتیجه NO_CHANGE و
بدون هیچ فراخوانی مدل تمام می‌شود؛ فقط وقتی رویداد واقعی وجود دارد مدل با کانتکست فشرده صدا زده می‌شود.

منابع (قرارداد Adapter یکسان: وضعیت اتصال، رویداد جدید، Cursor، خطا، بدون تغییر، بدون تکرار):
- telegram: پیام‌های دریافتی پل MTProto که رویدادمحور در events_inbox ثبت شده‌اند (بدون Polling تلگرام).
- inbox: هر منبع رویداد دیگر که در events_inbox ثبت می‌شود (webhook، system، MCP، تغییر فایل) با همان Cursor پایدار.
- url: دریافت امن صفحه (محافظ SSRF) و مقایسه اثرانگشت متن با نوبت قبل.

مصرف پایدار رویدادها (Durable Consumption):
- Cursor ترکیبی (created_at, id) — ترتیب قطعی حتی با Timestamp برابر.
- صفحه‌بندی؛ هر نوبت حداکثر MAX_EVENTS رویداد واجد شرایط تحویل می‌شود (Backpressure)؛ Cursor فقط تا آخرین
  رویداد «تحویل‌شده یا عمداً فیلترشده» جلو می‌رود؛ رویداد تحویل‌نشده هرگز تأیید (Ack) نمی‌شود.
- Cursor فقط پس از موفقیت نوبت ذخیره می‌شود (Pipeline)؛ پس از خطا همان رویدادها دوباره تحویل می‌شوند و
  پاسخ تکراری با صندوق خروجی مهار می‌شود.
- حساب‌رسی هر نوبت: received = delivered + filtered (+ pending در صفحه‌های بعد).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import EventInbox, Routine
from xagent_core.text import truncate

MAX_EVENTS = 30
PAGE = 200
SEEN_KEEP = 300  # فقط برای مهاجرت از قالب Cursor قدیمی
ZERO_ID = "00000000-0000-0000-0000-000000000000"


@dataclass
class WatchResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    summary: str = ""
    stats: dict[str, int] = field(default_factory=dict)


def _ids(values: Any) -> list[str]:
    out = []
    for v in values or []:
        v = str(v).strip().lstrip("@").lower()
        if v:
            out.append(v)
    return out[:200]


def normalize_watch(raw: dict[str, Any] | None) -> dict[str, Any]:
    """اعتبارسنجی برنامه پیش‌بررسی؛ فقط کلیدهای شناخته‌شده نگه داشته می‌شوند."""
    if not raw:
        return {}
    src = str(raw.get("source", "")).lower()
    if src == "telegram":
        return {"source": "telegram", "chats": _ids(raw.get("chats") or raw.get("chat_ids")),
                "private_only": bool(raw.get("private_only", True)),
                "include_bots": bool(raw.get("include_bots", False))}
    if src == "url":
        url = str(raw.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            raise ValueError("برای پایش صفحه نشانی http(s) معتبر لازم است")
        return {"source": "url", "url": url[:2000], "contains": str(raw.get("contains") or "")[:200]}
    if src == "inbox":
        name = str(raw.get("inbox_source") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_\-]{2,24}", name) or name == "telegram":
            raise ValueError("برای پایش صندوق رویداد، inbox_source معتبر (مثل webhook) لازم است")
        return {"source": "inbox", "inbox_source": name, "kinds": [str(k)[:32] for k in (raw.get("kinds") or [])][:10]}
    raise ValueError("منبع پایش باید telegram، inbox یا url باشد")


def _key(ev_payload: dict[str, Any]) -> set[str]:
    return {str(ev_payload.get(k)).lstrip("@").lower() for k in ("chat_id", "chat_username", "sender_id",
                                                                   "sender_username") if ev_payload.get(k)}


def _cursor_from_state(st: dict[str, Any], fallback: datetime | None) -> tuple[datetime | None, uuid.UUID, set[str]]:
    """Cursor پایدار؛ سازگار با قالب قدیمی (ISO + فهرست seen) برای روتین‌های در حال اجرا هنگام ارتقا."""
    cur = st.get("cursor")
    if isinstance(cur, list) and len(cur) == 2:
        return datetime.fromisoformat(cur[0]), uuid.UUID(cur[1]), set()
    if isinstance(cur, str):  # قالب قدیمی: از همان زمان با Seen قبلی (بدون از دست دادن رویداد)
        return datetime.fromisoformat(cur), uuid.UUID(ZERO_ID), set(st.get("seen", []))
    return fallback, uuid.UUID(ZERO_ID), set()


async def consume_inbox(session: AsyncSession, r: Routine, source: str,
                        accept: Callable[[EventInbox], dict[str, Any] | None], limit: int = MAX_EVENTS) -> WatchResult:
    """مصرف پایدار رویدادهای یک منبع از events_inbox با Cursor ترکیبی و Ack فقط برای تحویل‌شده/فیلترشده.

    accept(ev) → dict رویداد برای تحویل، یا None اگر عمداً فیلتر شود.
    """
    st = dict(r.watch_state or {})
    since, since_id, legacy_seen = _cursor_from_state(st, r.starts_at or r.created_at)
    now = utcnow()
    base = select(EventInbox).where(EventInbox.source == source, EventInbox.created_at <= now)
    if since is not None:
        base = base.where(tuple_(EventInbox.created_at, EventInbox.id) > tuple_(since, since_id))
    delivered: list[dict[str, Any]] = []
    stats = {"received": 0, "delivered": 0, "filtered": 0, "pending": 0}
    cur_ts, cur_id = since, since_id
    full = False
    q = base
    while not full:
        rows = (await session.execute(q.order_by(EventInbox.created_at, EventInbox.id).limit(PAGE))).scalars().all()
        if not rows:
            break
        for ev in rows:
            if len(delivered) >= limit:
                full = True
                break
            stats["received"] += 1
            item = None if str(ev.id) in legacy_seen else accept(ev)
            if item is None:
                stats["filtered"] += 1
            else:
                delivered.append(item)
                stats["delivered"] += 1
            cur_ts, cur_id = ev.created_at, ev.id  # Ack فقط تا آخرین رویداد بررسی‌شده (نه فراتر)
        if len(rows) < PAGE or full:
            break
        q = base.where(tuple_(EventInbox.created_at, EventInbox.id) > tuple_(cur_ts, cur_id))
    if cur_ts is not None:
        stats["pending"] = int((await session.execute(select(func.count()).select_from(EventInbox).where(
            EventInbox.source == source, EventInbox.created_at <= now,
            tuple_(EventInbox.created_at, EventInbox.id) > tuple_(cur_ts, cur_id)))).scalar_one())
    new_state = {k: v for k, v in st.items() if k != "seen"}
    if cur_ts is not None:
        new_state["cursor"] = [cur_ts.isoformat(), str(cur_id)]
    summary = f"{len(delivered)} رویداد جدید" if delivered else "رویداد جدیدی نیست"
    if stats["pending"]:
        summary += f" (+{stats['pending']} در صف نوبت‌های بعد)"
    return WatchResult(events=delivered, state=new_state, summary=summary, stats=stats)


async def _telegram(session: AsyncSession, r: Routine, w: dict[str, Any], telegram: Any) -> WatchResult:
    if telegram is None or not getattr(telegram, "connected", False):
        return WatchResult(error="تلگرام متصل نیست یا نشست آن منقضی شده است؛ از «تنظیمات ← تلگرام» دوباره وصل کنید.",
                           error_code="telegram_disconnected")
    chats = set(w.get("chats") or [])

    def accept(ev: EventInbox) -> dict[str, Any] | None:
        p = ev.payload or {}
        if ev.kind != "message" or p.get("is_owner_command"):
            return None
        if w.get("private_only", True) and not p.get("is_private"):
            return None
        if p.get("sender_is_bot") and not w.get("include_bots"):
            return None  # جلوگیری از حلقه پاسخ میان دو ربات
        if chats and not (_key(p) & chats):
            return None
        return {
            "event_id": str(ev.id), "chat_id": p.get("chat_id"), "chat": p.get("chat_username") or p.get("chat_id"),
            "sender_id": p.get("sender_id"), "sender": p.get("sender_name") or p.get("sender_username"),
            "message_id": p.get("message_id"),
            "text": truncate(p.get("text") or ("[رسانه]" if p.get("has_media") else ""), 1500),
            "date": p.get("date"), "is_private": p.get("is_private"),
        }

    return await consume_inbox(session, r, "telegram", accept)


async def _inbox(session: AsyncSession, r: Routine, w: dict[str, Any]) -> WatchResult:
    kinds = set(w.get("kinds") or [])

    def accept(ev: EventInbox) -> dict[str, Any] | None:
        if kinds and ev.kind not in kinds:
            return None
        return {"event_id": str(ev.id), "kind": ev.kind, "data": ev.payload or {}}

    return await consume_inbox(session, r, w["inbox_source"], accept)


def _page_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


async def _url(r: Routine, w: dict[str, Any]) -> WatchResult:
    from . import net

    try:
        resp = await net.safe_request("GET", w["url"], timeout=20, max_bytes=1_500_000)
    except net.UnsafeDestination as e:
        return WatchResult(error=f"نشانی پایش مجاز نیست: {e}", error_code="unsafe_url")
    except Exception as e:  # noqa: BLE001
        return WatchResult(error=f"دریافت صفحه ناموفق بود: {type(e).__name__}", error_code="fetch_failed")
    if resp.status_code >= 400:
        return WatchResult(error=f"صفحه پاسخ {resp.status_code} داد", error_code="http_error")
    text = _page_text(resp.text)
    if w.get("contains"):
        idx = text.find(w["contains"])
        text = text[max(0, idx - 300): idx + 700] if idx >= 0 else ""
    digest = hashlib.sha256(text.encode()).hexdigest()
    st = dict(r.watch_state or {})
    prev = st.get("hash")
    new_state = {**st, "hash": digest, "excerpt": truncate(text, 400)}
    if prev is None:
        return WatchResult(state=new_state, summary="خط مبنای صفحه ثبت شد")
    if prev == digest:
        return WatchResult(state=new_state, summary="صفحه تغییری نکرده است")
    return WatchResult(events=[{"url": w["url"], "before": st.get("excerpt", ""), "after": truncate(text, 3000)}],
                       state=new_state, summary="محتوای صفحه تغییر کرده است")


async def precheck(session: AsyncSession, r: Routine, telegram: Any = None) -> WatchResult:
    w = r.watch or {}
    if w.get("source") == "telegram":
        return await _telegram(session, r, w, telegram)
    if w.get("source") == "url":
        return await _url(r, w)
    if w.get("source") == "inbox":
        return await _inbox(session, r, w)
    return WatchResult(error="برنامه پایش نامعتبر است", error_code="invalid_watch")
