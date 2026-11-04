"""ابزارهای مشترک تست خودمختاری: تلگرام ساختگی، صفحه مرورگر ساختگی و کمک‌کننده‌های مدل شبیه‌ساز.

هیچ‌کدام به تلگرام یا مرورگر واقعی وصل نمی‌شوند؛ فقط رفتار قابل‌کنترل برای آزمون فراهم می‌کنند.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from xagent_core.db import session_scope
from xagent_core.models import EventInbox


class FakeTelegram:
    """جایگزین پل Telethon: پیام‌های ورودی را در صندوق رویداد ثبت و پیام‌های ارسالی را فقط ضبط می‌کند."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.sent: list[dict[str, Any]] = []
        self.owner_notes: list[str] = []
        self.fail_next_sends = 0  # شبیه‌سازی خطای موقت شبکه
        self._mid = 1000

    async def send(self, chat, text: str, file_path: str | None = None, reply_to: int | None = None) -> dict:
        if not self.connected:
            raise RuntimeError("تلگرام متصل نیست")
        if self.fail_next_sends > 0:
            self.fail_next_sends -= 1
            raise ConnectionError("قطع موقت ارتباط با سرور تلگرام (شبیه‌سازی)")
        if file_path:
            raise PermissionError("ارسال فایل در این آزمون مجاز نیست")
        self._mid += 1
        self.sent.append({"chat": str(chat), "text": text, "reply_to": reply_to, "message_id": self._mid})
        return {"message_id": self._mid, "chat_id": chat}

    async def notify_owner(self, text: str) -> None:
        self.owner_notes.append(text)

    async def recent(self, chat, limit: int = 15) -> list[dict]:
        return []

    async def dialogs(self, limit: int = 20) -> list[dict]:
        return []

    async def incoming(self, chat_id: int, username: str, text: str, message_id: int, *, private: bool = True,
                       bot: bool = False, when: datetime | None = None) -> uuid.UUID | None:
        """شبیه‌سازی دریافت پیام از MTProto: همان قالب payload پل واقعی؛ تکراری‌ها رد می‌شوند."""
        payload = {
            "chat_id": chat_id, "chat_title": username, "chat_username": username, "sender_id": chat_id,
            "sender_username": username, "sender_name": username, "message_id": message_id, "text": text,
            "date": (when or datetime.now(UTC)).isoformat(), "is_private": private, "is_owner_command": False,
            "has_media": False, "geo": None, "sender_is_bot": bot,
        }
        async with session_scope() as s:
            ev = EventInbox(source="telegram", kind="message", payload=payload, dedup_key=f"tg:{chat_id}:{message_id}")
            s.add(ev)
            try:
                await s.flush()
            except Exception:  # noqa: BLE001 — تکراری
                await s.rollback()
                return None
            return ev.id


class FakePage:
    """صفحه مرورگر ساختگی برای مأموریت چندمرحله‌ای: هر کلیک «بعدی» یک صفحه جلو می‌رود."""

    def __init__(self, last_page: int = 8, stuck: bool = False) -> None:
        self.url = "about:blank"
        self.page_no = 0
        self.last_page = last_page
        self.stuck = stuck
        self.clicks: list[str] = []

    async def bring_to_front(self) -> None:
        return None

    async def goto(self, url: str, **_: Any):
        self.url = url
        self.page_no = 1
        return SimpleNamespace(status=200)

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def title(self) -> str:
        return f"کاتالوگ — صفحه {self.page_no}"

    async def evaluate(self, js: str, *_: Any):
        if "querySelectorAll" in js:
            return [{"id": 1, "tag": "a", "type": "", "label": "صفحه بعد", "href": "#next"}]
        body = f"فهرست محصولات صفحه {self.page_no}"
        if self.page_no >= self.last_page:
            body += " — قیمت نهایی محصول هدف: 42 دلار"
        return body

    async def click(self, selector: str, timeout: int = 0) -> None:
        self.clicks.append(selector)
        if not self.stuck:
            self.page_no += 1
            self.url = f"https://catalog.example/list?page={self.page_no}"

    async def inner_text(self, selector: str, timeout: int = 0) -> str:
        return await self.evaluate("")


def body_text(body: dict) -> str:
    return " ".join(m["content"] for m in body.get("messages", [])
                    if m.get("role") in ("user", "system") and isinstance(m.get("content"), str))


def tool_names(body: dict) -> set[str]:
    return {t["function"]["name"] for t in body.get("tools", [])}


def called(body: dict) -> list[tuple[str, dict]]:
    out = []
    for m in body.get("messages", []):
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                out.append((c["function"]["name"], json.loads(c["function"]["arguments"] or "{}")))
    return out
