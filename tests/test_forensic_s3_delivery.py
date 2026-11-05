"""ممیزی جرم‌شناختی — مرحله ۳، F18: رسید تحویل نهایی؛ رسید ناقص یا بدشکل هرگز «ارسال شد» نمی‌شود.

تلگرام شبیه‌ساز است؛ هیچ پیام واقعی ارسال نمی‌شود.
"""

from __future__ import annotations

import pytest
from autonomy_helpers import FakeTelegram, body_text
from sqlalchemy import select

from xagent_core.db import session_scope
from xagent_core.models import MissionAction, Run


class ReceiptTelegram(FakeTelegram):
    def __init__(self, receipt, exc: BaseException | None = None):
        super().__init__()
        self.receipt, self.exc, self.calls = receipt, exc, 0

    async def send(self, chat, text, file_path=None, reply_to=None):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.receipt


async def final_run(mock_llm, tg, mark: str) -> Run:
    from xagent_agent.pipeline import AgentRunner

    def handler(body):
        if mark in body_text(body) and not body.get("response_format"):
            return "پاسخ نهایی آزمون."
        return None
    async with session_scope() as s:
        run = Run(trigger="telegram", input=f"سلام ({mark})", status="queued",
                  state={"trusted": True, "principal": "owner", "reply_to": "me"})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner("s3d", telegram=tg).execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        return await s.get(Run, rid)


@pytest.mark.parametrize("receipt", [{}, {"message_id": None}, {"message_id": "abc"}, {"message_id": -5},
                                     {"message_id": True}, None, "ok", ["42"]])
async def test_F18_missing_or_malformed_final_receipt_is_never_sent(app_client, configured_model, mock_llm, receipt):
    tg = ReceiptTelegram(receipt)
    run = await final_run(mock_llm, tg, f"F18-BAD-{type(receipt).__name__}-{str(receipt)[:6]}")
    d = run.state.get("delivery") or {}
    assert tg.calls == 1 and d.get("status") == "unknown", f"رسید {receipt!r} → {d}"
    async with session_scope() as s:
        row = (await s.execute(select(MissionAction).where(MissionAction.run_id == run.id))).scalar_one()
    assert row.status == "unknown"


async def test_F18_valid_receipt_known_failure_and_not_connected(app_client, configured_model, mock_llm):
    ok = await final_run(mock_llm, ReceiptTelegram({"message_id": 42}), "F18-OK")
    assert ok.state["delivery"]["status"] == "sent" and ok.state["delivery"]["message_id"] == 42
    failed = await final_run(mock_llm, ReceiptTelegram(None, PermissionError("not in allowlist")), "F18-FAIL")
    assert failed.state["delivery"]["status"] == "failed"
    offline = FakeTelegram(connected=False)
    nc = await final_run(mock_llm, offline, "F18-OFF")
    assert nc.state["delivery"]["status"] == "not_connected"


async def test_F18_uncertain_send_is_not_blindly_retried(app_client, configured_model, mock_llm):
    from xagent_agent.pipeline import AgentRunner

    tg = ReceiptTelegram({})
    run = await final_run(mock_llm, tg, "F18-RETRY")
    async with session_scope() as s:
        r = await s.get(Run, run.id)
        r.status = "queued"  # بازیابی/اجرای دوباره همان Run
    mock_llm.handlers.insert(0, lambda body: "پاسخ دوباره." if "F18-RETRY" in body_text(body) else None)
    try:
        await AgentRunner("s3d", telegram=tg).execute(run.id)
    finally:
        mock_llm.handlers.pop(0)
    async with session_scope() as s:
        again = await s.get(Run, run.id)
    assert tg.calls == 1, "ارسال با نتیجه نامعلوم کورکورانه تکرار شد"
    assert again.state["delivery"]["status"] == "pending_reconciliation"


async def test_F18_telegram_send_tool_requires_valid_receipt(app_client):
    from types import SimpleNamespace

    from xagent_tools.builtin import telegram as tgtool

    tg = ReceiptTelegram({"message_id": "abc"})
    async with session_scope() as s:
        ctx = SimpleNamespace(session=s, run_id=__import__("uuid").uuid4(),
                              extra={"telegram": tg, "tool_call_id": "c-f18", "run_state": {}})
        res = await tgtool.telegram_send(ctx, {"chat": "ali", "text": "سلام"})
    assert res.ok is False and res.verified is not True, "رسید بدشکل ابزار ارسال را «تأییدشده» کرد"
