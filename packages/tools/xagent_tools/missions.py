"""مجوز مأموریت محدود (Scoped Mission Authorization) — اجرای قطعی سمت سرور.

اصول:
- مجوز فقط در اجرای کارفرمای احرازشده ساخته می‌شود (principal=owner سمت سرور)؛ پیام تلگرام، صفحه وب یا
  فایل نمی‌تواند مجوز بسازد یا گسترش دهد.
- اقدام بیرونی (پاسخ تلگرام) فقط به گفتگوهای مجاز و فقط در پاسخ به پیام جدید همان نوبت مجاز است؛
  با کلید یکتا از پاسخ تکراری جلوگیری و با سقف نرخ از حلقه پاسخ میان ربات‌ها جلوگیری می‌شود.
- مجوز پاسخ‌گویی اجازه ارسال فایل، پرداخت، تغییر تنظیمات یا ابزار مدیریتی نمی‌دهد (این ابزارها اصلاً
  در اجرای مأموریت بارگذاری نمی‌شوند).
- مجوز منقضی/لغوشده هیچ اقدامی را مجاز نمی‌کند؛ همه اقدامات در mission_actions ثبت می‌شوند.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from xagent_core.db import utcnow
from xagent_core.models import Mission, MissionAction

READ_TOOLS = {"telegram_recent", "memory_search", "notify_owner"}
REPLY_TOOL = "telegram_send"
DEFAULT_REPLIES_PER_CHAT_HOUR = 6


def norm(v: Any) -> str:
    return str(v).strip().lstrip("@").lower()


def build_scope(watch: dict[str, Any], authorization: dict[str, Any] | None,
                telegram_allowlist: list[str] | None = None) -> dict[str, Any]:
    """محدوده صریح مأموریت از برنامه پایش و درخواست کارفرما (هیچ ابزار حساس دیگری اضافه نمی‌شود)."""
    auth = authorization or {}
    tools = sorted(READ_TOOLS)
    tg: dict[str, Any] = {"read": watch.get("source") == "telegram", "reply": False, "reply_to": [],
                          "reply_only_to_senders": True, "private_only": bool(watch.get("private_only", True)),
                          "allow_files": False,
                          "max_replies_per_chat_hour": int(auth.get("max_replies_per_chat_hour")
                                                           or DEFAULT_REPLIES_PER_CHAT_HOUR)}
    if auth.get("reply"):
        reply_to = [norm(x) for x in (auth.get("reply_to") or watch.get("chats") or telegram_allowlist or [])]
        tg.update({"reply": True, "reply_to": sorted({x for x in reply_to if x})})
        tools.append(REPLY_TOOL)
    return {"kind": f"{watch.get('source', 'custom')}_watch", "tools": sorted(set(tools)), "telegram": tg,
            "out_of_scope": "ask" if auth.get("out_of_scope") == "ask" else "report",
            "instructions": str(auth.get("reply_instructions") or "")[:1500]}


def describe_scope(scope: dict[str, Any]) -> str:
    tg = scope.get("telegram") or {}
    parts = ["خواندن پیام‌های جدید تلگرام" if tg.get("read") else "خواندن داده‌های پایش"]
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


def _senders(run_state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in ((run_state.get("event_payload") or {}).get("new_messages") or []):
        for k in ("chat", "chat_id"):
            if ev.get(k) is not None:
                out.setdefault(norm(ev[k]), []).append(ev)
    return out


async def check_call(session: AsyncSession, mission: Mission | None, tool_name: str, args: dict[str, Any],
                     run_state: dict[str, Any]) -> tuple[str, str]:
    """('allow'|'deny'|'ask'|'out_of_scope', دلیل) — پیش از Harness و پیش از اجرای ابزار."""
    if (err := mission_valid(mission)) is not None:
        return "deny", err
    scope = mission.scope or {}
    if tool_name not in set(scope.get("tools") or []):
        return "deny", f"ابزار «{tool_name}» در محدوده مجوز این مأموریت نیست"
    if tool_name != REPLY_TOOL:
        return "allow", "خواندن در محدوده مأموریت"
    tg = scope.get("telegram") or {}
    if not tg.get("reply"):
        return "deny", "این مأموریت مجوز ارسال پیام ندارد"
    if args.get("file") and not tg.get("allow_files"):
        return "deny", "مجوز مأموریت ارسال فایل را شامل نمی‌شود"
    target = norm(args.get("chat", ""))
    allowed = set(tg.get("reply_to") or [])
    senders = _senders(run_state)
    in_scope = target in allowed and (not tg.get("reply_only_to_senders", True) or target in senders)
    if not in_scope:
        policy = scope.get("out_of_scope", "report")
        return ("ask" if policy == "ask" else "out_of_scope",
                f"گفتگوی «{target}» خارج از محدوده پاسخ‌گویی مجاز این مأموریت است")
    limit = int(tg.get("max_replies_per_chat_hour") or DEFAULT_REPLIES_PER_CHAT_HOUR)
    recent = (await session.execute(select(func.count()).select_from(MissionAction).where(
        MissionAction.mission_id == mission.id, MissionAction.target == target, MissionAction.status == "sent",
        MissionAction.ts >= utcnow() - timedelta(hours=1)))).scalar_one()
    if recent >= limit:
        return "deny", f"سقف نرخ پاسخ ({limit} در ساعت) برای «{target}» پر شده است (جلوگیری از حلقه پاسخ)"
    return "allow", "پاسخ در محدوده مجوز مأموریت"


def infer_reply_to(args: dict[str, Any], run_state: dict[str, Any]) -> str | None:
    if args.get("reply_to"):
        return str(args["reply_to"])
    evs = _senders(run_state).get(norm(args.get("chat", "")), [])
    return str(evs[-1]["message_id"]) if evs and evs[-1].get("message_id") is not None else None


def reply_key(mission_id: Any, chat: Any, ref: str | None) -> str | None:
    return f"tg-reply:{mission_id}:{norm(chat)}:{ref}" if ref else None


async def already_done(session: AsyncSession, key: str | None) -> bool:
    if not key:
        return False
    return (await session.execute(select(MissionAction.id).where(
        MissionAction.dedupe_key == key, MissionAction.status == "sent"))).first() is not None


async def record(session: AsyncSession, *, mission_id: Any, routine_id: Any, run_id: Any, action: str,
                 target: str, ref: str | None, status: str, key: str | None = None,
                 detail: dict[str, Any] | None = None) -> bool:
    """ثبت حسابرسی اقدام؛ False یعنی کلید تکراری بود."""
    stmt = insert(MissionAction).values(
        id=uuid.uuid4(), ts=utcnow(), mission_id=mission_id, routine_id=routine_id, run_id=run_id, action=action,
        target=norm(target)[:160], ref=ref, dedupe_key=key if status == "sent" else None, status=status,
        detail=detail or {})
    if key and status == "sent":
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
    res = await session.execute(stmt.returning(MissionAction.id))
    ok = res.scalar_one_or_none() is not None
    if ok and status == "sent" and mission_id:
        m = await session.get(Mission, uuid.UUID(str(mission_id)))
        if m is not None:
            m.actions_count = (m.actions_count or 0) + 1
    return ok
