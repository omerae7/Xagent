"""پیش‌بررسی قطعی روتین‌ها (مسیر سبک: Scheduler → Deterministic Check → Conditional LLM).

خواندن رویداد جدید بدون مدل انجام می‌شود. اگر چیزی تغییر نکرده باشد اجرا با نتیجه NO_CHANGE و
بدون هیچ فراخوانی مدل تمام می‌شود؛ فقط وقتی رویداد واقعی وجود دارد مدل با کانتکست فشرده صدا زده می‌شود.

منابع:
- telegram: پیام‌های دریافتی پل MTProto که رویدادمحور در events_inbox ثبت شده‌اند (بدون Polling تلگرام).
- url: دریافت امن صفحه (محافظ SSRF) و مقایسه اثرانگشت متن با نوبت قبل.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import EventInbox, Routine
from xagent_core.text import truncate

MAX_EVENTS = 30
SEEN_KEEP = 300


@dataclass
class WatchResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    error_code: str | None = None
    summary: str = ""


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
    raise ValueError("منبع پایش باید telegram یا url باشد")


def _key(ev_payload: dict[str, Any]) -> set[str]:
    return {str(ev_payload.get(k)).lstrip("@").lower() for k in ("chat_id", "chat_username", "sender_id",
                                                                   "sender_username") if ev_payload.get(k)}


async def _telegram(session: AsyncSession, r: Routine, w: dict[str, Any], telegram: Any) -> WatchResult:
    if telegram is None or not getattr(telegram, "connected", False):
        return WatchResult(error="تلگرام متصل نیست یا نشست آن منقضی شده است؛ از «تنظیمات ← تلگرام» دوباره وصل کنید.",
                           error_code="telegram_disconnected")
    st = dict(r.watch_state or {})
    seen = list(st.get("seen", []))
    seen_set = set(seen)
    since = st.get("cursor")
    since_dt = datetime.fromisoformat(since) if since else (r.starts_at or r.created_at)
    q = select(EventInbox).where(EventInbox.source == "telegram", EventInbox.kind == "message",
                                 EventInbox.created_at <= utcnow())
    if since_dt is not None:
        q = q.where(EventInbox.created_at >= since_dt)
    rows = (await session.execute(q.order_by(EventInbox.created_at, EventInbox.id).limit(500))).scalars().all()
    chats = set(w.get("chats") or [])
    events: list[dict[str, Any]] = []
    cursor = since
    for ev in rows:
        p = ev.payload or {}
        eid = str(ev.id)
        cursor = ev.created_at.isoformat()
        if eid in seen_set:
            continue
        seen.append(eid)
        seen_set.add(eid)
        if p.get("is_owner_command"):
            continue
        if w.get("private_only", True) and not p.get("is_private"):
            continue
        if p.get("sender_is_bot") and not w.get("include_bots"):
            continue  # جلوگیری از حلقه پاسخ میان دو ربات
        if chats and not (_key(p) & chats):
            continue
        if len(events) < MAX_EVENTS:
            events.append({
                "event_id": eid, "chat_id": p.get("chat_id"), "chat": p.get("chat_username") or p.get("chat_id"),
                "sender": p.get("sender_name") or p.get("sender_username"), "message_id": p.get("message_id"),
                "text": truncate(p.get("text") or ("[رسانه]" if p.get("has_media") else ""), 1500),
                "date": p.get("date"), "is_private": p.get("is_private"),
            })
    new_state = {**st, "seen": seen[-SEEN_KEEP:], "cursor": cursor}
    return WatchResult(events=events, state=new_state,
                       summary=f"{len(events)} پیام جدید" if events else "پیام جدیدی نیست")


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
    return WatchResult(error="برنامه پایش نامعتبر است", error_code="invalid_watch")
