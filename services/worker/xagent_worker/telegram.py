"""پل تلگرام شخصی (Userbot) با Telethon — پروتکل رسمی MTProto.

- دریافت رویدادمحور پیام‌ها و ذخیره در صندوق رویداد (بدون فراخوانی LLM)
- فرمان دادن کارفرما از «Saved Messages» خودش (گفتگوی امن مالک)
- پاسخ‌های ایجنت با پیشوند 🤖 تا دوباره پردازش نشوند
- تایید/رد کارت‌های مجوز با پیام «تایید xxxx» / «رد xxxx» (قطعی)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy import select

from xagent_core import bus
from xagent_core.config import get_settings
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Approval, Conversation, EventInbox, Message, Run, SettingKV
from xagent_core.security import decrypt_secret
from xagent_core.settings_store import TelegramSettings, get_setting
from xagent_core.timeutil import jalali_str

log = logging.getLogger("xagent.telegram")
AGENT_PREFIX = "🤖"
_APPROVAL_RE = re.compile(r"^(تایید|تأیید|approve|رد|deny)\s+([0-9a-f]{6,36})$", re.IGNORECASE)


class TelegramBridge:
    def __init__(self) -> None:
        self.client: Any = None
        self.connected = False
        self.me: Any = None
        self.cfg = TelegramSettings()
        self._lock = asyncio.Lock()

    async def _publish_status(self, error: str | None = None) -> None:
        status = {"connected": self.connected, "error": error, "at": utcnow().isoformat()}
        if self.me is not None:
            status["me"] = {"id": self.me.id, "username": getattr(self.me, "username", None),
                            "name": " ".join(filter(None, [self.me.first_name, getattr(self.me, "last_name", None)]))}
        await bus.get_redis().set("xagent:telegram:status", json.dumps(status, ensure_ascii=False))
        await bus.publish("telegram.status", status)

    async def start(self) -> None:
        async with self._lock:
            await self.stop()
            async with session_scope() as session:
                self.cfg = await get_setting(session, "telegram", TelegramSettings)
                secret = await session.get(SettingKV, "telegram_secret")
            if not self.cfg.enabled or secret is None or not self.cfg.api_id:
                await self._publish_status()
                return
            from telethon import TelegramClient, events
            from telethon.sessions import StringSession

            sess = decrypt_secret(base64.b64decode(secret.value.get("session", "")))
            api_hash = decrypt_secret(base64.b64decode(secret.value.get("api_hash", "")))
            if not sess or not api_hash:
                await self._publish_status("نشست تلگرام قابل رمزگشایی نیست؛ دوباره وارد شوید")
                return
            self.client = TelegramClient(StringSession(sess), self.cfg.api_id, api_hash, device_model="X Agent",
                                         system_version="Linux", app_version="1.0", lang_code="fa",
                                         auto_reconnect=True, connection_retries=None)
            try:
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise RuntimeError("نشست تلگرام منقضی شده است")
                self.me = await self.client.get_me()
                self.client.add_event_handler(self._on_message, events.NewMessage())
                self.connected = True
                await self._publish_status()
                log.info("تلگرام متصل شد: %s", getattr(self.me, "username", self.me.id))
            except Exception as e:  # noqa: BLE001
                self.connected = False
                log.warning("اتصال تلگرام ناموفق: %s", e)
                await self._publish_status(str(e))

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.client = None
        self.connected = False

    # ── دریافت رویدادمحور ──
    async def _on_message(self, event) -> None:  # noqa: ANN001
        try:
            msg = event.message
            chat = await event.get_chat()
            sender = await event.get_sender()
            is_self_chat = event.is_private and msg.chat_id == self.me.id
            text = msg.message or ""
            if msg.out and not is_self_chat:
                return  # پیام‌های ارسالی کارفرما به دیگران رویداد نیستند
            if text.startswith(AGENT_PREFIX):
                return
            username = getattr(chat, "username", None)
            payload = {
                "chat_id": msg.chat_id,
                "chat_title": getattr(chat, "title", None) or " ".join(
                    filter(None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)])),
                "chat_username": username,
                "sender_id": getattr(sender, "id", None),
                "sender_username": getattr(sender, "username", None),
                "sender_name": " ".join(filter(None, [getattr(sender, "first_name", None),
                                                      getattr(sender, "last_name", None)])) if sender else None,
                "message_id": msg.id,
                "text": text[:8000],
                "date": msg.date.astimezone(UTC).isoformat() if msg.date else None,
                "is_private": event.is_private,
                "sender_is_bot": bool(getattr(sender, "bot", False)),
                "is_owner_command": is_self_chat and self.cfg.owner_chat == "me",
                "has_media": bool(msg.media),
                "geo": {"lat": msg.geo.lat, "long": msg.geo.long} if getattr(msg, "geo", None) else None,
            }
            async with session_scope() as session:
                ev = EventInbox(source="telegram", kind="message", payload=payload,
                                dedup_key=f"tg:{msg.chat_id}:{msg.id}")
                session.add(ev)
                try:
                    await session.flush()
                except Exception:  # noqa: BLE001 — تکراری
                    await session.rollback()
                    return
                ev_id = ev.id
            await bus.enqueue_inbox(ev_id)
        except Exception as e:  # noqa: BLE001
            log.exception("خطا در پردازش پیام تلگرام: %s", e)

    # ── عملیات ──
    async def _entity(self, chat: str | int):
        if isinstance(chat, str):
            c = chat.strip()
            if c in ("me", "self"):
                return "me"
            if re.fullmatch(r"-?\d+", c):
                return await self.client.get_entity(int(c))
            return await self.client.get_entity(c)
        return await self.client.get_entity(chat)

    def _allowed(self, chat: str | int) -> bool:
        if not self.cfg.allowlist:
            return True
        c = str(chat).lstrip("@").lower()
        return c in ("me", "self") or c in {a.lstrip("@").lower() for a in self.cfg.allowlist}

    async def send(self, chat: str | int, text: str, file_path: str | None = None,
                   reply_to: int | None = None) -> dict[str, Any]:
        if not self.connected:
            raise RuntimeError("تلگرام متصل نیست")
        if not self._allowed(chat):
            raise PermissionError(f"گفتگوی «{chat}» در فهرست مجاز (Allowlist) تلگرام نیست")
        entity = await self._entity(chat)
        body = text
        if entity == "me":
            body = f"{AGENT_PREFIX} {text}"
        if file_path:
            p = (get_settings().workspace_dir / file_path.lstrip("/")).resolve()
            if get_settings().workspace_dir.resolve() not in p.parents or not p.is_file():
                raise PermissionError("فایل باید در فضای کاری ایجنت باشد")
            m = await self.client.send_file(entity, str(p), caption=body or None, reply_to=reply_to)
        else:
            m = await self.client.send_message(entity, body, reply_to=reply_to)
        return {"message_id": m.id, "chat_id": m.chat_id}

    async def notify_owner(self, text: str) -> None:
        if self.connected:
            await self.client.send_message("me", f"{AGENT_PREFIX} {text}")

    async def recent(self, chat: str | int, limit: int = 15) -> list[dict[str, Any]]:
        entity = await self._entity(chat)
        out = []
        async for m in self.client.iter_messages(entity, limit=min(limit, 100)):
            sender = await m.get_sender()
            out.append({
                "id": m.id,
                "date": jalali_str(m.date) if m.date else "",
                "sender": "من" if m.out else (getattr(sender, "first_name", None) or getattr(sender, "title", "?")),
                "text": m.message or ("[رسانه]" if m.media else ""),
            })
        out.reverse()
        return out

    async def dialogs(self, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        async for d in self.client.iter_dialogs(limit=min(limit, 100)):
            out.append({"id": d.id, "title": d.name, "username": getattr(d.entity, "username", None),
                        "unread": d.unread_count})
        return out

    async def download_media(self, chat_id: int, message_id: int) -> Path | None:
        msg = await self.client.get_messages(chat_id, ids=message_id)
        if not msg or not msg.media:
            return None
        folder = get_settings().uploads_dir / "telegram"
        folder.mkdir(parents=True, exist_ok=True)
        path = await self.client.download_media(msg, file=str(folder))
        return Path(path) if path else None


async def handle_owner_command(bridge: TelegramBridge, payload: dict[str, Any]) -> bool:
    """پیام کارفرما در Saved Messages: تایید/رد مجوز یا ایجاد اجرای مورد اعتماد."""
    text = (payload.get("text") or "").strip()
    if not text:
        return False
    m = _APPROVAL_RE.match(text)
    async with session_scope() as session:
        if m:
            verb, prefix = m.group(1).lower(), m.group(2).lower()
            rows = (await session.execute(select(Approval).where(Approval.status == "pending"))).scalars().all()
            target = next((a for a in rows if str(a.id).startswith(prefix)), None)
            if target is None:
                await bridge.notify_owner("درخواست مجوزی با این شناسه پیدا نشد.")
                return True
            target.status = "approved" if verb in ("تایید", "تأیید", "approve") else "denied"
            target.decided_at = utcnow()
            target.decided_by = "telegram"
            run = await session.get(Run, target.run_id)
            pending_left = [a for a in rows if a.run_id == target.run_id and a.id != target.id]
            if run is not None and run.status == "waiting_approval" and not pending_left:
                run.status = "queued"
            await session.commit()
            if run is not None and not pending_left:
                await bus.enqueue_run(run.id, "approval")
            await bridge.notify_owner(f"{'✅ تایید' if target.status == 'approved' else '⛔ رد'} شد: {target.tool_name}")
            return True
        conv = (await session.execute(select(Conversation).where(
            Conversation.channel == "telegram", Conversation.external_ref == "owner"))).scalar_one_or_none()
        if conv is None:
            conv = Conversation(title="تلگرام کارفرما", channel="telegram", external_ref="owner")
            session.add(conv)
            await session.flush()
        msg = Message(conversation_id=conv.id, role="user", content=text, meta={"channel": "telegram"})
        session.add(msg)
        conv.message_count += 1
        conv.updated_at = utcnow()
        run = Run(conversation_id=conv.id, trigger="telegram", input=text,
                  state={"trusted": True, "principal": "owner", "requested_by": "telegram:saved_messages", "reply_to": "me"})
        session.add(run)
        await session.flush()
        msg.run_id = run.id
        await session.commit()
        await bus.enqueue_run(run.id)
    return True
