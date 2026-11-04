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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from xagent_core import bus
from xagent_core.db import session_scope, utcnow
from xagent_core.models import EventInbox, Mission, Routine
from xagent_core.settings_store import TelegramSettings, get_setting
from xagent_core.text import normalize_fa, truncate
from xagent_crm import contact_by_telegram, log_interaction
from xagent_tools.lifecycle import (
    active_run,
    admission_block,
    claim_occurrence,
    finalize_routine,
    pause_for_budget,
)

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


MAX_EVENT_ATTEMPTS = 5


async def record_event_failure(event_id: uuid.UUID, error: BaseException) -> bool:
    """ثبت پایدار شکست پردازش رویداد؛ True یعنی سقف تلاش پر شد و رویداد «failed» (Dead-letter) شد.

    تا پیش از آن رویداد «new» می‌ماند و پیام Stream تأیید (ACK) نمی‌شود تا بازپس‌گیری/تطبیق دوباره پردازش کند.
    """
    from xagent_core import inbox as owner_inbox

    async with session_scope() as session:
        ev = await session.get(EventInbox, event_id, with_for_update=True)
        if ev is None or ev.status != "new":
            return True
        d = dict(ev.decision or {})
        d["attempts"] = int(d.get("attempts", 0)) + 1
        d["last_error"] = truncate(f"{type(error).__name__}: {error}", 300)
        d["last_attempt_at"] = utcnow().isoformat()
        ev.decision = d
        if d["attempts"] < MAX_EVENT_ATTEMPTS:
            return False
        ev.status, ev.processed_at = "failed", utcnow()
        await owner_inbox.post(session, kind="error", severity="important",
                               title=f"پردازش رویداد {ev.source} پس از {d['attempts']} تلاش ناموفق ماند",
                               body=f"{d['last_error']}\nرویداد حذف نشده و در صندوق رویدادها با وضعیت «failed» باقی است.",
                               dedupe_key=f"event-dead:{ev.id}", data={"event_id": str(ev.id)})
        return True


async def reconcile_inbox(min_age_seconds: int = 120, limit: int = 200) -> int:
    """رویدادهای ثبت‌شده‌ای که هرگز به صف نرسیدند یا پیامشان از دست رفت دوباره در صف قرار می‌گیرند.

    پردازش idempotent است (قفل سطری + وضعیت new + کلید یکتای نوبت)، پس صف‌گذاری دوباره اقدام تکراری نمی‌سازد.
    """
    now = utcnow()
    ids: list[uuid.UUID] = []
    async with session_scope() as session:
        rows = (await session.execute(select(EventInbox).where(
            EventInbox.status == "new", EventInbox.created_at <= now - timedelta(seconds=min_age_seconds))
            .order_by(EventInbox.created_at).limit(limit).with_for_update(skip_locked=True))).scalars().all()
        for ev in rows:
            d = dict(ev.decision or {})
            last = d.get("requeued_at")
            if last and (now - datetime.fromisoformat(last)).total_seconds() < 300:
                continue
            d["requeued_at"] = now.isoformat()
            ev.decision = d
            ids.append(ev.id)
    for eid in ids:
        await bus.enqueue_inbox(eid)
    if ids:
        log.info("%d رویداد بی‌صف دوباره در صف قرار گرفت", len(ids))
    return len(ids)


async def process_event(event_id: uuid.UUID, bridge: TelegramBridge | None) -> str:
    async with session_scope() as session:
        # قفل سطری: دو مصرف‌کننده هم‌زمان (تحویل دوباره/بازپس‌گیری) یک رویداد را دوبار پردازش نمی‌کنند
        ev = await session.get(EventInbox, event_id, with_for_update=True)
        if ev is None or ev.status != "new":
            return "skip"
        payload = ev.payload or {}
        decision: dict[str, Any] = {**(ev.decision or {}), "actions": []}

        if ev.source == "telegram" and payload.get("is_owner_command"):
            # فرمان ابتدا اجرا و سپس «routed» ثبت می‌شود؛ Handler idempotent است (Run یکتا با کلید رویداد)،
            # پس Crash بین این دو فقط به اجرای دوباره همان فرمان بدون Run تکراری می‌انجامد.
            if bridge is not None:
                await handle_owner_command(bridge, payload, event_id=str(ev.id))
            ev.status, ev.processed_at = "routed", utcnow()
            decision["actions"].append("owner_command")
            ev.decision = decision
            return "owner_command"

        cfg = await get_setting(session, "telegram", TelegramSettings)
        if ev.source == "telegram" and cfg.auto_link_crm and payload.get("is_private"):
            contact = await contact_by_telegram(session, payload.get("sender_id"), payload.get("sender_username"))
            if contact is not None:
                await log_interaction(session, contact.id, truncate(payload.get("text") or "[رسانه]", 300),
                                      channel="telegram", direction="in", content=payload.get("text") or "")
                decision["actions"].append(f"crm:{contact.id}")

        candidates = (await session.execute(select(Routine.id).where(
            Routine.trigger_type == "event", Routine.status == "active"))).scalars().all()
        stop = await bus.is_stop_all()
        created: list[uuid.UUID] = []
        now = utcnow()
        for rid in candidates:
            if stop:
                break
            # قفل سطری: رویدادهای هم‌زمان برای یک روتین سریالی می‌شوند (سقف دفعات/بودجه یک‌بار شمرده می‌شود)
            r = await session.get(Routine, rid, with_for_update=True, populate_existing=True)
            if r is None or r.status != "active" or not routine_matches(r, ev.source, payload):
                continue
            block = await admission_block(session, r, now)
            if block is not None:
                reason, final = block
                decision["actions"].append(f"routine_skipped:{r.id}:{reason}")
                if final is not None and await active_run(session, r.id) is None:
                    await finalize_routine(session, r, now, reason, status=final if final != "completed" else None)
                elif reason == "budget":
                    await pause_for_budget(session, r)
                continue
            mission = await session.get(Mission, r.mission_id) if r.mission_id else None
            # نوبت پایدار رویداد: همان رویداد هرگز دو Run برای یک روتین نمی‌سازد (حتی پس از Crash/تحویل دوباره)
            run_id = await claim_occurrence(
                session, r, now, trigger="event", key=f"routine:{r.id}:event:{ev.id}",
                extra_state={"trusted": False, "principal": "mission" if mission is not None else "external",
                             "requested_by": f"event:{r.id}", "event_payload": payload, "event_id": str(ev.id)})
            if run_id is None:
                decision["actions"].append(f"routine_duplicate:{r.id}")
                continue
            created.append(run_id)
            decision["actions"].append(f"routine:{r.id}")
        ev.status = "routed" if created else ("processed" if decision["actions"] else "ignored")
        ev.decision = decision
        ev.processed_at = utcnow()
    for rid in created:
        await bus.enqueue_run(rid, "event")
    if created or decision["actions"]:
        await bus.publish("event.processed", {"id": str(event_id), "status": ev.status, "actions": decision["actions"]})
    return ev.status
