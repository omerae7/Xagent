"""Native delivery ambiguity must stop the objective, even with a standing grant."""

import uuid

import pytest
from autonomy_helpers import FakeTelegram, body_text
from sqlalchemy import select

from xagent_agent import harness
from xagent_agent.pipeline import AgentRunner
from xagent_agent.validation import checked_result
from xagent_core.db import session_scope
from xagent_core.models import Approval, Run
from xagent_tools.base import Tool, ToolResult, obj


def test_legacy_uncertainty_is_canonical_and_never_a_recoverable_failure():
    tool = Tool("synthetic", "", obj({}), None, "tasks", category="routine_change")
    result = checked_result(tool, {}, ToolResult(False, "effect may have happened", {"uncertain": True}))
    assert result.data.get("outcome_uncertain") is True
    assert result.outcome.value == "UNKNOWN_EFFECT"


@pytest.mark.asyncio
async def test_native_send_timeout_stops_before_another_model_call(app_client, configured_model, mock_llm):
    marker = "NATIVE-UNCERTAIN-" + uuid.uuid4().hex[:8]
    fake = FakeTelegram()
    fake.fail_after_send = 1
    calls = []

    def handler(body):
        if marker not in body_text(body) or not body.get("tools") or body.get("response_format"):
            return None
        calls.append(1)
        return ("telegram_send", {"chat": "uncertain-synthetic", "text": "Controlled local effect"})

    async with session_scope() as s:
        rules = await harness.load_rules(s)
        previous = (rules["external_message"].mode, rules["external_message"].conditions)
        rules["external_message"].mode = "ask"
        rules["external_message"].conditions = {}
        r = Run(
            trigger="chat",
            input="تلگرام را بررسی کن " + marker,
            state={"trusted": True, "requested_by": "admin"},
        )
        s.add(r)
        await s.flush()
        rid = r.id
    mock_llm.handlers.insert(0, handler)
    try:
        runner = AgentRunner("native-unknown", telegram=fake)
        await runner.execute(rid)
        async with session_scope() as s:
            ap = (
                await s.execute(select(Approval).where(Approval.run_id == rid, Approval.status == "pending"))
            ).scalar_one()
            aid = ap.id
        assert (
            await app_client.post(f"/api/approvals/{aid}", json={"decision": "approve", "scope": "standing"})
        ).status_code == 200
        before = len(calls)
        await runner.execute(rid)
        assert len(fake.sent) == 1, f"uncertain send was repeated: {len(fake.sent)} effects"
        assert len(calls) == before, "model was invoked again after an unknown external effect"
        async with session_scope() as s:
            run = await s.get(Run, rid)
            assert run.status == "failed"
            assert run.state["evidence"][-1]["outcome"] == "UNKNOWN_EFFECT"
            assert run.state["validation"]["passed"] is False
    finally:
        mock_llm.handlers.remove(handler)
        async with session_scope() as s:
            rule = (await harness.load_rules(s))["external_message"]
            rule.mode, rule.conditions = previous
