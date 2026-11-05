"""History text, display names and a model statement are not delivery receipts."""

import pytest
from autonomy_helpers import FakeTelegram

from xagent_tools.builtin.telegram import _reconcile

pytestmark = pytest.mark.asyncio


async def test_old_identical_text_is_not_proof_of_an_uncertain_send():
    bridge = FakeTelegram()
    bridge.history["synthetic"] = [{"id": 42, "sender": "من", "out": True, "text": "same text"}]
    assert not await _reconcile(bridge, "synthetic", "same text")


async def test_known_receipt_identifies_the_actual_outgoing_message():
    bridge = FakeTelegram()
    bridge.history["synthetic"] = [{"id": 42, "sender": "من", "out": True, "text": "same text"}]
    assert await _reconcile(bridge, "synthetic", "same text", {"message_id": 42})


@pytest.mark.parametrize(
    "out,mid,text", [(False, 42, "same text"), (True, 43, "same text"), (True, 42, "different")]
)
async def test_incoming_display_name_or_different_receipt_cannot_confirm(out, mid, text):
    bridge = FakeTelegram()
    bridge.history["synthetic"] = [{"id": mid, "sender": "من", "out": out, "text": text}]
    assert not await _reconcile(bridge, "synthetic", "same text", {"message_id": 42})


@pytest.mark.parametrize("status", ["sent", "unknown"])
@pytest.mark.parametrize("known_receipt", [False, True])
async def test_durable_outbox_needs_a_real_receipt_before_verified_completion(
    app_client, status, known_receipt
):
    from types import SimpleNamespace

    from xagent_core.db import session_scope
    from xagent_core.models import Run
    from xagent_tools import missions
    from xagent_tools.builtin.telegram import telegram_send

    async with session_scope() as s:
        run = Run(trigger="chat", input="synthetic outbox")
        s.add(run)
        await s.flush()
        rid = run.id
    key = f"tg-send:{rid}:same-call"
    _, aid = await missions.outbox_reserve(
        key,
        mission_id=None,
        routine_id=None,
        run_id=rid,
        action="telegram_send",
        target="synthetic",
        ref=None,
        detail={"text": "same text"},
    )
    await missions.outbox_mark(
        aid, status, {"message_id": 42} if known_receipt else {"error": "synthetic timeout"}
    )
    bridge = FakeTelegram()
    bridge.history["synthetic"] = [{"id": 42, "sender": "من", "out": True, "text": "same text"}]
    async with session_scope() as s:
        ctx = SimpleNamespace(
            session=s, run_id=rid, extra={"telegram": bridge, "tool_call_id": "same-call", "run_state": {}}
        )
        result = await telegram_send(ctx, {"chat": "synthetic", "text": "same text"})
        assert result.ok is known_receipt
        assert (result.verified is True) is known_receipt
        if known_receipt:
            assert result.data["message_id"] == 42
        else:
            assert result.data.get("uncertain") or result.data.get("outcome_uncertain")
    assert bridge.sent == [], "reconciliation must never send a second message"
