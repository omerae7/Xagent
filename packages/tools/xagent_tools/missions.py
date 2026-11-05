"""مجوز مأموریت محدود (Scoped Mission Authorization) — اجرای قطعی سمت سرور.

اصول:
- مجوز فقط در اجرای کارفرمای احرازشده ساخته می‌شود (principal=owner سمت سرور)؛ پیام تلگرام، صفحه وب یا
  فایل نمی‌تواند مجوز بسازد یا گسترش دهد.
- کنترل در سطح «ابزار + عملیات + منبع + آرگومان»: بارگذاری‌شدن یک ابزار به معنی مجازبودن همه پارامترهایش
  نیست. هر ابزار مأموریت اعتبارسنج اختصاصی دارد؛ ابزاری که اعتبارسنج ندارد رد می‌شود (پیش‌فرض امن).
- خواندن فقط از گفتگوهای مجاز؛ پاسخ فقط به یک پیام جدید و مجاز همان نوبت (Chat + Message ID تطبیق داده
  می‌شود)؛ ارسال مستقل بدون Reply فقط با مجوز جداگانه `send_standalone`.
- حافظه مأموریت فقط Scope خودش (`mission:<id>`) را می‌بیند، مگر کارفرما صریحاً حافظه سراسری را مجاز کند.
- اقدام بیرونی با «صندوق خروجی پایدار» (Transactional Outbox) ثبت می‌شود: رزرو پیش از ارسال در تراکنش
  جدا Commit می‌شود؛ اگر نتیجه ارسال نامشخص بماند، بدون بررسی دوباره ارسال نمی‌شود (ادعای Exactly-once نداریم:
  در نتیجه نامشخص «حداکثر یک‌بار» + گزارش به کارفرما).
- مجوز منقضی/لغوشده هیچ اقدامی را مجاز نمی‌کند؛ همه اقدامات و مسدودسازی‌ها در mission_actions ثبت می‌شوند.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import session_scope, utcnow
from xagent_core.models import Mission, MissionAction

READ_TOOLS = {"telegram_recent", "memory_search", "notify_owner"}
REPLY_TOOL = "telegram_send"
DEFAULT_REPLIES_PER_CHAT_HOUR = 6
# وضعیت‌های صندوق خروجی: reserved → sending → sent | failed (قطعاً ارسال نشد) | unknown (ممکن است ارسال شده باشد)
OUTBOX_FINAL_OK = {"sent", "reconciled"}
OUTBOX_UNCERTAIN = {"reserved", "sending", "unknown"}


def norm(v: Any) -> str:
    return str(v).strip().lstrip("@").lower()


def build_scope(watch: dict[str, Any], authorization: dict[str, Any] | None,
                telegram_allowlist: list[str] | None = None) -> dict[str, Any]:
    """محدوده صریح مأموریت از برنامه پایش و درخواست کارفرما (هیچ ابزار حساس دیگری اضافه نمی‌شود)."""
    auth = authorization or {}
    tools = sorted(READ_TOOLS)
    watch_chats = [norm(x) for x in (watch.get("chats") or [])]
    tg: dict[str, Any] = {"read": watch.get("source") == "telegram", "read_chats": watch_chats, "reply": False,
                          "reply_to": [], "reply_only_to_senders": True, "send_standalone": False,
                          "private_only": bool(watch.get("private_only", True)), "allow_files": False,
                          "max_replies_per_chat_hour": int(auth.get("max_replies_per_chat_hour")
                                                           or DEFAULT_REPLIES_PER_CHAT_HOUR)}
    if auth.get("reply"):
        reply_to = [norm(x) for x in (auth.get("reply_to") or watch.get("chats") or telegram_allowlist or [])]
        tg.update({"reply": True, "reply_to": sorted({x for x in reply_to if x})})
        tools.append(REPLY_TOOL)
    if not tg["read_chats"]:
        tg["read_chats"] = list(tg["reply_to"])  # تاریخچه فقط از گفتگوهای مجاز پاسخ
    return {"kind": f"{watch.get('source', 'custom')}_watch", "tools": sorted(set(tools)), "telegram": tg,
            "memory": "mission", "out_of_scope": "ask" if auth.get("out_of_scope") == "ask" else "report",
            "instructions": str(auth.get("reply_instructions") or "")[:1500]}


def describe_scope(scope: dict[str, Any]) -> str:
    tg = scope.get("telegram") or {}
    parts = ["خواندن پیام‌های جدید تلگرام" if tg.get("read") else "خواندن داده‌های پایش"]
    if tg.get("read_chats"):
        parts.append("تاریخچه فقط از: " + "، ".join(tg["read_chats"]))
    if tg.get("reply"):
        who = "، ".join(tg.get("reply_to") or []) or "(هیچ گفتگویی)"
        parts.append(f"پاسخ فقط به پیام‌های جدید از: {who} (حداکثر {tg.get('max_replies_per_chat_hour')} پاسخ در ساعت به هر گفتگو، بدون ارسال فایل)")
    parts.append("خارج از محدوده: " + ("درخواست تأیید" if scope.get("out_of_scope") == "ask" else "فقط گزارش به کارفرما"))
    return "؛ ".join(parts)


def mission_valid(m: Mission | None) -> str | None:
    if m is None:
        return "مجوز مأموریت یافت نشد"
    if m.status != "active":
        return f"مجوز مأموریت فعال نیست (وضعیت: {m.status})"
    if m.expires_at is not None and utcnow() >= m.expires_at:
        return "مجوز مأموریت منقضی شده است"
    if m.max_actions is not None and (m.actions_count or 0) >= m.max_actions:
        return "سقف تعداد اقدامات این مأموریت پر شده است"
    return None


def _events(run_state: dict[str, Any]) -> list[dict[str, Any]]:
    return list((run_state.get("event_payload") or {}).get("new_messages") or [])


def _event_keys(ev: dict[str, Any]) -> set[str]:
    return {norm(ev[k]) for k in ("chat", "chat_id") if ev.get(k) is not None}


def match_event(run_state: dict[str, Any], chat: Any, reply_to: Any) -> dict[str, Any] | None:
    """پیام جدید همان نوبت که هم گفتگو و هم شناسه پیامش با درخواست یکی است (داده رویداد سمت سرور)."""
    target = norm(chat)
    try:
        mid = int(str(reply_to))
    except (TypeError, ValueError):
        return None
    for ev in _events(run_state):
        if target in _event_keys(ev) and ev.get("message_id") is not None and int(ev["message_id"]) == mid:
            return ev
    return None


Verdict = tuple[str, str]
Validator = Callable[[AsyncSession, Mission, dict[str, Any], dict[str, Any]], Awaitable[Verdict]]


def _out_of_scope(scope: dict[str, Any], why: str) -> Verdict:
    return ("ask" if scope.get("out_of_scope") == "ask" else "out_of_scope"), why


async def _v_recent(session: AsyncSession, m: Mission, args: dict[str, Any], st: dict[str, Any]) -> Verdict:
    scope = m.scope or {}
    allowed = {norm(x) for x in ((scope.get("telegram") or {}).get("read_chats") or [])}
    target = norm(args.get("chat", ""))
    if not target or target not in allowed:
        return _out_of_scope(scope, f"خواندن گفتگوی «{target or '?'}» خارج از محدوده خواندن این مأموریت است")
    return "allow", "خواندن از گفتگوی مجاز"


async def _v_dialogs(session: AsyncSession, m: Mission, args: dict[str, Any], st: dict[str, Any]) -> Verdict:
    if (m.scope or {}).get("telegram", {}).get("list_dialogs"):
        return "allow", "فهرست گفتگوها مجاز است"
    return "deny", "فهرست همه گفتگوهای تلگرام در محدوده این مأموریت نیست"


async def _v_send(session: AsyncSession, m: Mission, args: dict[str, Any], st: dict[str, Any]) -> Verdict:
    scope = m.scope or {}
    tg = scope.get("telegram") or {}
    if not tg.get("reply"):
        return "deny", "این مأموریت مجوز ارسال پیام ندارد"
    if args.get("file") and not tg.get("allow_files"):
        return "deny", "مجوز مأموریت ارسال فایل را شامل نمی‌شود"
    target = norm(args.get("chat", ""))
    if target not in set(tg.get("reply_to") or []):
        return _out_of_scope(scope, f"گفتگوی «{target}» خارج از محدوده پاسخ‌گویی مجاز این مأموریت است")
    if args.get("reply_to") in (None, ""):
        if not tg.get("send_standalone"):
            return "deny", "ارسال مستقل (بدون پاسخ به پیام جدید) نیازمند مجوز جداگانه است؛ reply_to را بده"
    elif match_event(st, target, args.get("reply_to")) is None:
        return "deny", "شناسه پاسخ با هیچ پیام جدید و مجاز همان گفتگو در این نوبت تطبیق ندارد"
    if tg.get("reply_only_to_senders", True) and not tg.get("send_standalone") and not any(
            target in _event_keys(ev) for ev in _events(st)):
        return "deny", "در این نوبت پیام جدیدی از این گفتگو نرسیده است"
    limit = int(tg.get("max_replies_per_chat_hour") or DEFAULT_REPLIES_PER_CHAT_HOUR)
    recent = (await session.execute(select(func.count()).select_from(MissionAction).where(
        MissionAction.mission_id == m.id, MissionAction.target == target,
        MissionAction.status.in_(["sent", "sending", "reserved", "unknown", "reconciled"]),
        MissionAction.ts >= utcnow() - timedelta(hours=1)))).scalar_one()
    if recent >= limit:
        return "deny", f"سقف نرخ پاسخ ({limit} در ساعت) برای «{target}» پر شده است (جلوگیری از حلقه پاسخ)"
    return "allow", "پاسخ به پیام جدید مجاز در محدوده مأموریت"


async def _v_allow(session: AsyncSession, m: Mission, args: dict[str, Any], st: dict[str, Any]) -> Verdict:
    return "allow", "در محدوده مأموریت"


# اعتبارسنج هر ابزار مأموریت؛ memory_search با Scope حافظه مأموریت (در Pipeline) محدود می‌شود
VALIDATORS: dict[str, Validator] = {
    "telegram_recent": _v_recent,
    "telegram_dialogs": _v_dialogs,
    REPLY_TOOL: _v_send,
    "memory_search": _v_allow,
    "notify_owner": _v_allow,
}


async def check_call(session: AsyncSession, mission: Mission | None, tool_name: str, args: dict[str, Any],
                     run_state: dict[str, Any]) -> tuple[str, str]:
    """('allow'|'deny'|'ask'|'out_of_scope', دلیل) — پیش از Harness و پیش از اجرای ابزار، بدون تغییر مجوز."""
    if (err := mission_valid(mission)) is not None:
        return "deny", err
    scope = mission.scope or {}
    if tool_name not in set(scope.get("tools") or []):
        return "deny", f"ابزار «{tool_name}» در محدوده مجوز این مأموریت نیست"
    validator = VALIDATORS.get(tool_name)
    if validator is None:
        return "deny", f"برای ابزار «{tool_name}» اعتبارسنج محدوده تعریف نشده است (پیش‌فرض امن: رد)"
    return await validator(session, mission, args, run_state)


def reply_key(mission_id: Any, chat: Any, ref: str | None) -> str | None:
    return f"tg-reply:{mission_id}:{norm(chat)}:{ref}" if ref else None


async def outbox_reserve(key: str, *, mission_id: Any, routine_id: Any, run_id: Any, action: str, target: str,
                         ref: str | None, detail: dict[str, Any] | None = None) -> tuple[str, uuid.UUID | None]:
    """رزرو پایدار پیش از اقدام بیرونی (تراکنش مستقل و Commit‌شده).

    خروجی: ('reserved', id) — مجاز به ارسال؛ ('done', id) — قبلاً ارسال شده؛
    ('uncertain', id) — نتیجه تلاش قبلی نامشخص است و نباید کورکورانه تکرار شود.
    """
    async with session_scope() as s:
        row = (await s.execute(select(MissionAction).where(MissionAction.dedupe_key == key).with_for_update())).scalar_one_or_none()
        if row is not None:
            if row.status in OUTBOX_FINAL_OK:
                return "done", row.id
            if row.status in OUTBOX_UNCERTAIN:
                return "uncertain", row.id
            row.status, row.ts, row.run_id = "reserved", utcnow(), run_id  # failed قطعی → تلاش مجدد مجاز
            return "reserved", row.id
        rid = uuid.uuid4()
        res = await s.execute(insert(MissionAction).values(
            id=rid, ts=utcnow(), mission_id=mission_id, routine_id=routine_id, run_id=run_id, action=action,
            target=norm(target)[:160], ref=ref, dedupe_key=key, status="reserved", detail=detail or {},
        ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(MissionAction.id))
        if res.scalar_one_or_none() is None:
            return "uncertain", None  # Worker دیگری هم‌زمان رزرو کرد
        return "reserved", rid


async def outbox_mark(action_id: uuid.UUID, status: str, detail: dict[str, Any] | None = None) -> None:
    async with session_scope() as s:
        row = await s.get(MissionAction, action_id)
        if row is None:
            return
        row.status = status
        row.detail = {**(row.detail or {}), **(detail or {})}
        if status in ("sent", "reconciled") and row.mission_id:
            m = await s.get(Mission, row.mission_id)
            if m is not None:
                m.actions_count = (m.actions_count or 0) + 1


async def record(session: AsyncSession, *, mission_id: Any, routine_id: Any, run_id: Any, action: str,
                 target: str, ref: str | None, status: str, key: str | None = None,
                 detail: dict[str, Any] | None = None) -> bool:
    """ثبت حسابرسی (مسدودسازی/خطا) بدون کلید یکتا."""
    await session.execute(insert(MissionAction).values(
        id=uuid.uuid4(), ts=utcnow(), mission_id=mission_id, routine_id=routine_id, run_id=run_id, action=action,
        target=norm(target)[:160], ref=ref, dedupe_key=key, status=status, detail=detail or {}))
    return True
