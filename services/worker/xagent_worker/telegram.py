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
import uuid
from datetime import UTC
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from xagent_core import approvals, bus
from xagent_core import inbox as inbox_mod
from xagent_core.config import get_settings
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Conversation, EventInbox, Message, Run, SettingKV
from xagent_core.security import decrypt_secret
from xagent_core.settings_store import TelegramSettings, get_setting
from xagent_core.timeutil import jalali_str

log = logging.getLogger("xagent.telegram")
AGENT_PREFIX = "🤖"
_APPROVAL_RE = re.compile(r"^(تایید|تأیید|approve|رد|deny|لغو|revoke)\s+([0-9a-f-]{6,36})$", re.IGNORECASE)


class IngestSpool:
    """صف بازیابی پایدار و محدود رویدادهای دریافتی تلگرام (F12) — جایگزین صف حافظه فرایند.

    هر رویداد با شناسه یکتای خود (dedup) رمزشده روی دیسک می‌ماند تا پس از قطعی دیتابیس یا ری‌استارت Worker
    دوباره ثبت شود (تکراری بی‌اثر است). سرریز بی‌صدا نیست: بازه پیام‌های ثبت‌نشده به‌عنوان «شکاف» ذخیره و پس از
    بازگشت دیتابیس برای بررسی دستی به صندوق کارفرما گزارش می‌شود. بازپخش بالادست تلگرام تضمین نمی‌شود.
    """

    def __init__(self, folder: Path, max_items: int = 5000) -> None:
        self.folder, self.max_items = folder, max_items
        self.file, self.gaps_file = folder / "events.jsonl", folder / "gaps.jsonl"

    def _read(self, path: Path) -> list[dict[str, Any]]:
        from xagent_core.security import decrypt_secret

        if not path.exists():
            return []
        out = []
        for line in path.read_text().splitlines():
            plain = decrypt_secret(line.encode()) if line.strip() else None
            if plain:
                try:
                    out.append(json.loads(plain))
                except ValueError:
                    continue
        return out

    def _write(self, path: Path, rows: list[dict[str, Any]]) -> None:
        from xagent_core.security import encrypt_secret

        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(encrypt_secret(json.dumps(r, ensure_ascii=False)).decode() + "\n" for r in rows))
        tmp.chmod(0o600)
        tmp.replace(path)

    def items(self) -> list[dict[str, Any]]:
        return self._read(self.file)

    def __len__(self) -> int:
        return len(self.items())

    def add(self, payload: dict[str, Any], dedup: str) -> bool:
        rows = self.items()
        if any(r.get("dedup") == dedup for r in rows):
            return True
        if len(rows) >= self.max_items:
            self.record_gap(payload, dedup)
            return False
        self._write(self.file, [*rows, {"dedup": dedup, "payload": payload, "at": utcnow().isoformat()}])
        return True

    def remove(self, done: set[str]) -> None:
        self._write(self.file, [r for r in self.items() if r.get("dedup") not in done])

    def record_gap(self, payload: dict[str, Any], dedup: str) -> None:
        gaps = self._read(self.gaps_file)
        chat, mid = payload.get("chat_id"), payload.get("message_id")
        g = next((x for x in gaps if x.get("chat_id") == chat), None)
        if g is None:
            gaps.append({"chat_id": chat, "chat_title": payload.get("chat_title"), "first_id": mid, "last_id": mid,
                         "count": 1, "since": utcnow().isoformat()})
        else:
            g["count"] += 1
            if isinstance(mid, int):
                g["first_id"] = min(x for x in (g.get("first_id"), mid) if isinstance(x, int))
                g["last_id"] = max(x for x in (g.get("last_id"), mid) if isinstance(x, int))
        self._write(self.gaps_file, gaps)

    def gaps(self) -> list[dict[str, Any]]:
        return self._read(self.gaps_file)

    def clear_gaps(self) -> None:
        self._write(self.gaps_file, [])


class TelegramBridge:
    def __init__(self, spool_dir: Path | None = None) -> None:
        self.client: Any = None
        self.connected = False
        self.me: Any = None
        self.cfg = TelegramSettings()
        self._lock = asyncio.Lock()
        self.spool = IngestSpool(spool_dir or get_settings().data_dir / "telegram-spool")

    @property
    def _pending(self) -> list[dict[str, Any]]:  # سازگاری: وجود رویداد در صف بازیابی پایدار
        return self.spool.items()

    async def _publish_status(self, error: str | None = None) -> None:
        status = {"connected": self.connected, "error": error, "at": utcnow().isoformat()}
        if self.me is not None:
            status["me"] = {"id": self.me.id, "username": getattr(self.me, "username", None),
                            "name": " ".join(filter(None, [getattr(self.me, "first_name", None), getattr(self.me, "last_name", None)]))}
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
            await self._store(payload, f"tg:{msg.chat_id}:{msg.id}")
        except Exception as e:  # noqa: BLE001
            log.exception("خطا در پردازش پیام تلگرام: %s", e)

    async def _insert(self, payload: dict[str, Any], dedup: str) -> uuid.UUID | None:
        """ثبت در EventInbox؛ None یعنی همان رویداد قبلاً ثبت شده است. خطای ذخیره‌سازی بالا می‌رود."""
        try:
            async with session_scope() as session:
                ev = EventInbox(source="telegram", kind="message", payload=payload, dedup_key=dedup)
                session.add(ev)
                try:
                    await session.flush()
                except IntegrityError:  # همان پیام قبلاً ثبت شده است
                    await session.rollback()
                    return None
                return ev.id
        except IntegrityError:
            return None

    async def _store(self, payload: dict[str, Any], dedup: str) -> None:
        """ثبت پایدار رویداد: تکراری (Unique) بی‌صدا رد می‌شود؛ خطای واقعی ذخیره‌سازی در صف بازیابی پایدار می‌ماند."""
        try:
            ev_id = await self._insert(payload, dedup)
        except Exception as e:  # noqa: BLE001 — اتصال/ذخیره‌سازی: نباید بی‌صدا حذف شود
            kept = self.spool.add(payload, dedup)
            n = len(self.spool)
            if kept:
                log.error("ذخیره پیام تلگرام ناموفق؛ در صف بازیابی پایدار ماند (%d در صف): %s", n, type(e).__name__)
                await self._publish_status(f"ذخیره پیام‌های دریافتی موقتاً ناموفق است ({n} در صف بازیابی پایدار)")
            else:
                log.error("صف بازیابی پر است؛ پیام %s ثبت نشد و به‌عنوان شکاف (نیازمند بررسی) ثبت شد", dedup)
                await self._publish_status("صف بازیابی پیام‌های تلگرام پر شد؛ پیام‌های تازه ثبت نشدند (شکاف ثبت شد؛ "
                                           "نیازمند بررسی دستی گفتگوها)")
            return
        if ev_id is not None:
            await bus.enqueue_inbox(ev_id)

    async def flush_pending(self) -> int:
        """ثبت دوباره رویدادهای صف بازیابی پایدار (پس از قطعی دیتابیس یا ری‌استارت)؛ گزارش شکاف‌ها به کارفرما."""
        rows = self.spool.items()
        done: set[str] = set()
        for r in rows:
            try:
                ev_id = await self._insert(r["payload"], r["dedup"])
            except Exception:  # noqa: BLE001 — دیتابیس هنوز در دسترس نیست؛ در صف می‌ماند
                break
            done.add(r["dedup"])
            if ev_id is not None:
                try:
                    await bus.enqueue_inbox(ev_id)
                except Exception:  # noqa: BLE001 — رویداد ثبت شده؛ پایش صندوق رویداد بعداً آن را برمی‌دارد
                    pass
        if done:
            self.spool.remove(done)
        gaps = self.spool.gaps()
        if gaps and len(done) == len(rows):
            try:
                async with session_scope() as session:
                    lines = [f"• {g.get('chat_title') or g.get('chat_id')}: {g.get('count')} پیام "
                             f"(شناسه {g.get('first_id')} تا {g.get('last_id')})" for g in gaps]
                    await inbox_mod.post(session, kind="error", severity="important",
                                         title="پیام‌های تلگرام در زمان قطعی ثبت نشدند (نیازمند بررسی)",
                                         body="صف بازیابی پر شده بود؛ این پیام‌ها پردازش نشده‌اند و بازپخش خودکار "
                                              "تلگرام تضمین نمی‌شود. لطفاً گفتگوها را بررسی کنید:\n" + "\n".join(lines),
                                         dedupe_key=f"tg-gap:{gaps[0].get('since')}")
                self.spool.clear_gaps()
            except Exception:  # noqa: BLE001 — دفعه بعد دوباره گزارش می‌شود
                pass
        return len(done)

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

    async def notify_owner(self, text: str) -> dict[str, Any] | None:
        """ارسال به Saved Messages کارفرما؛ رسید (شناسه پیام) برای ثبت «ارسال شد» برگردانده می‌شود."""
        if not self.connected:
            return None
        m = await self.client.send_message("me", f"{AGENT_PREFIX} {text}")
        return {"message_id": getattr(m, "id", None)}

    async def recent(self, chat: str | int, limit: int = 15) -> list[dict[str, Any]]:
        entity = await self._entity(chat)
        out = []
        async for m in self.client.iter_messages(entity, limit=min(limit, 100)):
            sender = await m.get_sender()
            out.append({
                "id": m.id,
                "out": bool(m.out),
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


async def handle_owner_command(bridge: TelegramBridge, payload: dict[str, Any], event_id: str | None = None) -> bool:
    """پیام کارفرما در Saved Messages: تایید/رد مجوز یا ایجاد اجرای مورد اعتماد."""
    text = (payload.get("text") or "").strip()
    if not text:
        return False
    m = _APPROVAL_RE.match(text)
    async with session_scope() as session:
        if m:
            verb, prefix = m.group(1).lower(), m.group(2).lower()
            target, err = await approvals.find_by_prefix(session, prefix)
            if target is None:
                await bridge.notify_owner(err or "درخواست مجوزی با این شناسه پیدا نشد.")
                return True
            decision = ("approve" if verb in ("تایید", "تأیید", "approve") else
                        "revoke" if verb in ("لغو", "revoke") else "deny")
            d = await approvals.decide(session, target, decision, "telegram", channel="telegram")
            await session.commit()
            if not d.ok:
                await bridge.notify_owner(f"⚠️ {d.message}")
                return True
            if d.requeue is not None:
                await bus.enqueue_run(d.requeue, "approval")
            label = {"approved": "✅ تایید", "denied": "⛔ رد", "revoked": "↩️ لغو تأیید"}.get(target.status, target.status)
            await bridge.notify_owner(f"{label} شد: {target.tool_name}")
            return True
        key = f"owner-cmd:{event_id}" if event_id else None
        if key and (await session.execute(select(Run.id).where(Run.dedupe_key == key))).first():
            return True  # همین فرمان قبلاً به Run تبدیل شده است (پردازش دوباره پس از Crash)
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
        run = Run(conversation_id=conv.id, trigger="telegram", input=text, dedupe_key=key,
                  state={"trusted": True, "principal": "owner", "requested_by": "telegram:saved_messages", "reply_to": "me"})
        session.add(run)
        await session.flush()
        msg.run_id = run.id
        await session.commit()
        await bus.enqueue_run(run.id)
    return True
