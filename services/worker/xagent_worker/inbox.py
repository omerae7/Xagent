"""پردازش رویدادمحور صندوق رویدادها — کاملاً قطعی و بدون LLM.

مدل هوش مصنوعی فقط زمانی فراخوانی می‌شود که:
- کارفرما در گفتگوی مالک فرمان داده باشد، یا
- یک روتین رویدادمحور با فیلتر قطعی (منبع، گفتگو، کلیدواژه، regex) با رویداد تطبیق داشته باشد.
بقیه رویدادها فقط ثبت و در صورت امکان به CRM متصل می‌شوند.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Conversation, EventInbox, Routine, Run
from xagent_core.settings_store import TelegramSettings, get_setting
from xagent_core.text import normalize_fa, truncate
from xagent_crm import contact_by_telegram, log_interaction

from .telegram import TelegramBridge, handle_owner_command

log = logging.getLogger("xagent.inbox")


def routine_matches(r: Routine, source: str, payload: dict[str, Any]) -> bool:
    f = r.event_filter or {}
    if f.get("source") and f["source"] != source:
        return False
    chats = [str(c).lstrip("@").lower() for c in f.get("chat_ids", []) or []]
    if chats:
        cands = {str(payload.get("chat_id", "")).lower(), str(payload.get("chat_username") or "").lower(),
                 str(payload.get("sender_username") or "").lower(), str(payload.get("sender_id", "")).lower()}
        if not cands & set(chats):
            return False
    text = normalize_fa(payload.get("text") or "")
    kws = [normalize_fa(k) for k in f.get("keywords", []) or [] if k]
    if kws and not any(k in text for k in kws):
        return False
    if f.get("regex"):
        try:
            if not re.search(f["regex"], payload.get("text") or "", re.IGNORECASE):
                return False
        except re.error:
            return False
    if f.get("private_only") and not payload.get("is_private"):
        return False
    return True


async def process_event(event_id: uuid.UUID, bridge: TelegramBridge | None) -> str:
    async with session_scope() as session:
        ev = await session.get(EventInbox, event_id)
        if ev is None or ev.status != "new":
            return "skip"
        payload = ev.payload or {}
        decision: dict[str, Any] = {"actions": []}

        if ev.source == "telegram" and payload.get("is_owner_command"):
            ev.status, ev.processed_at = "routed", utcnow()
            decision["actions"].append("owner_command")
            ev.decision = decision
            await session.commit()
            if bridge is not None:
                await handle_owner_command(bridge, payload)
            return "owner_command"

        cfg = await get_setting(session, "telegram", TelegramSettings)
        if ev.source == "telegram" and cfg.auto_link_crm and payload.get("is_private"):
            contact = await contact_by_telegram(session, payload.get("sender_id"), payload.get("sender_username"))
            if contact is not None:
                await log_interaction(session, contact.id, truncate(payload.get("text") or "[رسانه]", 300),
                                      channel="telegram", direction="in", content=payload.get("text") or "")
                decision["actions"].append(f"crm:{contact.id}")

        routines = (await session.execute(select(Routine).where(
            Routine.trigger_type == "event", Routine.enabled.is_(True), Routine.paused.is_(False),
            Routine.circuit_state != "open"))).scalars().all()
        stop = await bus.is_stop_all()
        created: list[uuid.UUID] = []
        for r in routines:
            if stop or not routine_matches(r, ev.source, payload):
                continue
            lease = await bus.acquire_lease(f"routine-event:{r.id}:{ev.id}", 60_000)
            if lease is None:
                continue
            conv = (await session.execute(select(Conversation).where(
                Conversation.channel == "routine", Conversation.external_ref == str(r.id)))).scalar_one_or_none()
            if conv is None:
                conv = Conversation(title=f"روتین: {r.name}", channel="routine", external_ref=str(r.id))
                session.add(conv)
                await session.flush()
            run = Run(conversation_id=conv.id, routine_id=r.id, trigger="event", input=r.instruction,
                      state={"trusted": False, "principal": "external", "requested_by": f"event:{r.id}",
                             "event_payload": payload,
                             "max_cost": float(r.max_cost_usd) if r.max_cost_usd else None,
                             "max_steps": r.max_steps})
            session.add(run)
            await session.flush()
            r.last_run_at = utcnow()
            r.run_count += 1
            created.append(run.id)
            decision["actions"].append(f"routine:{r.id}")
        ev.status = "routed" if created else ("processed" if decision["actions"] else "ignored")
        ev.decision = decision
        ev.processed_at = utcnow()
    for rid in created:
        await bus.enqueue_run(rid, "event")
    if created or decision["actions"]:
        await bus.publish("event.processed", {"id": str(event_id), "status": ev.status, "actions": decision["actions"]})
    return ev.status
