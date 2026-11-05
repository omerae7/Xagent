"""ممیزی جرم‌شناختی — مرحله ۵، F14: مرز تراکنش اعلان کارفرما (قصد پایدار، ارسال پس از Commit، بدون تکرار کورکورانه).

تلگرام شبیه‌ساز است (FakeTelegram)؛ هیچ پیام واقعی ارسال نمی‌شود.
"""

from __future__ import annotations

import uuid

from autonomy_helpers import FakeTelegram, body_text, called
from sqlalchemy import select

from xagent_core import inbox
from xagent_core.db import get_sessionmaker, session_scope
from xagent_core.models import InboxItem, Run


async def test_F14_rolled_back_transaction_never_notifies_owner(app_client):
    tg = FakeTelegram()
    key = f"f14-rb-{uuid.uuid4().hex[:6]}"
    async with get_sessionmaker()() as s:
        await inbox.post(s, kind="error", severity="critical", title="F14 گزارشی که برمی‌گردد", dedupe_key=key, telegram=tg)
        await s.rollback()  # تراکنش گزارش برگشت
    await inbox.dispatch_pending(tg)
    assert not any("F14 گزارشی که برمی‌گردد" in n for n in tg.owner_notes), \
        "اعلان تلگرام برای گزارشی ارسال شد که تراکنشش برگشت خورد"


async def test_F14_committed_intent_is_sent_once_with_receipt(app_client):
    tg = FakeTelegram()
    key = f"f14-ok-{uuid.uuid4().hex[:6]}"
    async with session_scope() as s:
        item = await inbox.post(s, kind="error", severity="critical", title="F14 گزارش پایدار", dedupe_key=key, telegram=tg)
        iid = item.id
    assert not any("F14 گزارش پایدار" in n for n in tg.owner_notes), "ارسال داخل تراکنش (پیش از Commit) انجام شد"
    await inbox.dispatch_pending(tg)
    await inbox.dispatch_pending(tg)
    assert len([n for n in tg.owner_notes if "F14 گزارش پایدار" in n]) == 1
    async with session_scope() as s:
        assert (await s.get(InboxItem, iid)).delivery["telegram"] == "sent"


async def test_F14_interrupted_or_receiptless_send_is_unknown_not_retried(app_client):
    tg = FakeTelegram()
    async with session_scope() as s:
        a = await inbox.post(s, kind="error", severity="critical", title="F14 قطع‌شده", dedupe_key=f"f14-i-{uuid.uuid4()}",
                             telegram=tg)
        a.delivery = {**a.delivery, "telegram": "sending"}  # Crash در میانه ارسال قبلی
        aid = a.id
    await inbox.dispatch_pending(tg)
    async with session_scope() as s:
        assert (await s.get(InboxItem, aid)).delivery["telegram"].startswith("unknown")
    assert not any("F14 قطع‌شده" in n for n in tg.owner_notes), "ارسال نامعلوم کورکورانه تکرار شد"

    class NoReceipt(FakeTelegram):
        async def notify_owner(self, text):
            self.owner_notes.append(text)
            return None
    nr = NoReceipt()
    async with session_scope() as s:
        b = await inbox.post(s, kind="error", severity="critical", title="F14 بدون رسید", dedupe_key=f"f14-n-{uuid.uuid4()}",
                             telegram=nr)
        bid = b.id
    await inbox.dispatch_pending(nr)
    async with session_scope() as s:
        assert (await s.get(InboxItem, bid)).delivery["telegram"] == "unknown: missing_receipt"


async def test_F14_approval_notification_only_after_commit(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    tg = FakeTelegram()
    mark = "F14-APPROVAL"

    def handler(body):
        if mark not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        if not any(n == "file_delete" for n, _ in called(body)):
            if "file_delete" not in {t["function"]["name"] for t in body.get("tools", [])}:
                return ("tools_load", {"groups": ["files"]})
            return ("file_delete", {"path": "notes/f14.txt"})
        return "گزارش."
    async with session_scope() as s:
        run = Run(trigger="chat", input=f"فایل notes/f14.txt را حذف کن ({mark})", status="queued", state={"trusted": True})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("s5n", telegram=tg).execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        assert (await s.get(Run, rid)).status == "waiting_approval"
        item = (await s.execute(select(InboxItem).where(InboxItem.run_id == rid, InboxItem.kind == "approval"))).scalar_one()
    ap_id = str(item.data["approval_id"])[:8]
    mine = lambda: [n for n in tg.owner_notes if ap_id in n]  # noqa: E731 — فقط اعلان همین آزمون
    assert mine() == [], "اعلان تأیید داخل تراکنش اجرا (پیش از Commit) ارسال شد"
    await inbox.dispatch_pending(tg)
    assert len(mine()) == 1 and "تایید" in mine()[0]
    async with session_scope() as s:
        assert (await s.get(InboxItem, item.id)).delivery["telegram"] == "sent"
