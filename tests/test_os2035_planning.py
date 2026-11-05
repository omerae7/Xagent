"""Objective truth is independent of optional model plans and provider JSON shape."""

from types import SimpleNamespace

import pytest

from xagent_agent.planning import normalize_goal, normalize_plan
from xagent_agent.validation import evidence_issues, objective_report
from xagent_tools.base import ExecutionOutcome, ToolResult


def test_employee_identity_does_not_turn_a_greeting_into_an_external_objective():
    from xagent_agent.validation import execution_required

    run = SimpleNamespace(task_id=None, routine_id=None, employee_id="employee", input="سلام")
    assert not execution_required(run, {"route": {"tool_groups": []}}, "سلام!")
    assert execution_required(run, {"route": {"tool_groups": []}}, "پیام ارسال شد")
    run.input = "سایت را با مرورگر باز کن"
    assert execution_required(run, {"route": {"tool_groups": ["browser"]}}, "در حال بررسی")


@pytest.mark.parametrize("hint", ["memory, episodes", "web, browser", "web", "browser_read", "invented_tool"])
def test_advisory_steps_cannot_reject_verified_routine_creation(hint):
    state = {
        "route": {"intent": {"recurring": True}},
        "plan": {"steps": [{"tool_hint": hint}]},
        "evidence": [{"tool": "routine_create", "ok": True, "verified": True, "requires_verification": True}],
    }
    run = SimpleNamespace(trigger="chat", routine_id=None, input="هر دقیقه تلگرام را بررسی کن و پاسخ بده")
    assert not evidence_issues(state, True)
    assert objective_report(run, state)["status"] == "completed"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "bad",
        {"steps": [None, 3, {"title": 42}], "tool_groups": 3},
        {"steps": "bad", "memory_layers": {"semantic": True}},
    ],
)
def test_malformed_plans_are_bounded_typed_advice(raw):
    plan = normalize_plan(raw, {"browser_read": "browser"})
    assert plan["steps"] and plan["steps"][0]["tool_hint"] == "auto"
    assert plan["tool_groups"] == []


def test_groups_are_normalized_and_real_identifiers_preserved():
    plan = normalize_plan(
        {
            "steps": [
                {"title": "inspect", "tool_hint": "web, browser"},
                {"title": "read", "tool_hint": "browser_read"},
            ],
            "tool_groups": ["browser", None, "fake"],
        },
        {"browser_read": "browser"},
    )
    assert plan["tool_groups"] == ["browser", "web"]
    assert [s["tool_hint"] for s in plan["steps"]] == ["auto", "browser_read"]


def test_future_effects_are_not_creation_success_criteria():
    goal = normalize_goal(
        {"success_criteria": ["Reply to every future message"]}, "create routine", scheduled_creation=True
    )
    assert len(goal["success_criteria"]) == 1
    assert "ذخیره" in goal["success_criteria"][0]


def test_missing_requested_effect_still_fails_without_any_plan():
    run = SimpleNamespace(trigger="chat", routine_id=None, input="reply to @alice")
    state = {"evidence": [{"tool": "file_read", "ok": True, "verified": True}]}
    assert objective_report(run, state)["status"] == "not_completed"
    assert evidence_issues({"evidence": [{"tool": "routine_create", "ok": False, "verified": False}]}, True)


@pytest.mark.parametrize(
    "result,expected",
    [
        (ToolResult(True, "read"), ExecutionOutcome.OBSERVED),
        (ToolResult(True, "stored", verified=True), ExecutionOutcome.VERIFIED),
        (ToolResult(False, "failed", verified=False), ExecutionOutcome.FAILED),
        (
            ToolResult(False, "unknown", {"outcome_uncertain": True}, verified=False),
            ExecutionOutcome.UNKNOWN_EFFECT,
        ),
    ],
)
def test_result_outcome_cannot_be_set_by_model_prose(result, expected):
    assert result.outcome == expected


@pytest.mark.asyncio
async def test_goal_stage_loads_prior_context_before_clarification(app_client):
    from xagent_agent.pipeline import AgentRunner
    from xagent_core.db import session_scope
    from xagent_core.models import Conversation, Message, Run

    async with session_scope() as session:
        conv = Conversation(title="context")
        session.add(conv)
        await session.flush()
        run = Run(conversation_id=conv.id, trigger="chat", input="همان فرم را بررسی کن", state={})
        session.add(run)
        await session.flush()
        session.add(
            Message(conversation_id=conv.id, role="user", content="نشانی فرم https://example.com/editor است")
        )
        session.add(Message(conversation_id=conv.id, role="user", content=run.input, run_id=run.id))
        await session.flush()
        rc = SimpleNamespace(session=session, run=run, state={}, trusted=True)
        result = await AgentRunner("context")._planning_input(rc)
        assert "https://example.com/editor" in result
        assert result.count("همان فرم را بررسی کن") == 1
        assert 'untrusted="true"' in result


def test_existing_owner_control_groups_survive_plan_normalization():
    assert normalize_plan({"tool_groups": ["admin", "host"]}, {})["tool_groups"] == ["admin", "host"]


def test_canonical_unknown_effect_keeps_the_original_objective_unknown():
    state = {
        "evidence": [
            {
                "tool": "telegram_send",
                "ok": False,
                "verified": False,
                "requires_verification": True,
                "outcome": "UNKNOWN_EFFECT",
            }
        ]
    }
    run = SimpleNamespace(input="reply to Alice", trigger="chat", routine_id=None)
    result = objective_report(run, state)
    assert result["status"] == "unknown" and result["unknown"] == ["message_sent"] and result["missing"] == []
