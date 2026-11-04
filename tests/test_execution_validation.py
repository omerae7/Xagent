"""Truthful completion: real runner/settlement and filesystem, scripted model responses."""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import evaluation_reply

from xagent_core.config import get_settings
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Episode, Routine, Run, Task
from xagent_tools.base import ToolResult
from xagent_tools.registry import BUILTIN_BY_NAME
from xagent_worker.main import Worker


async def _execute(mock_llm, configured_model, *, response="verified", evaluator=None, complex_task=False,
                   empty_answer=False, routine=False):
    marker = "VALIDATION-" + uuid.uuid4().hex
    path = f"validation/{marker}.txt"
    tool_name = "not_a_real_tool" if response == "unknown_tool" else "file_write"

    def handler(body):
        messages = body.get("messages", [])
        text = json.dumps(messages, ensure_ascii=False)
        if marker not in text:
            return None
        if body.get("response_format"):
            return evaluator if evaluator is not None else evaluation_reply(messages[-1]["content"])
        if response != "no_call" and not any(m.get("role") == "tool" for m in messages):
            if response == "load_only":
                return ("tools_load", {"groups": ["files"]})
            return (tool_name, {"path": "../refused.txt" if response == "unsafe_path" else path,
                                "content": "verified content"})
        return "" if empty_answer else "Done, the file was created successfully."

    state = {
        "trusted": True, "principal": "owner",
        "route": {"complexity": "complex" if complex_task else "simple", "risk": "low", "path": "worker",
                  "tool_groups": ["files"], "memory_layers": []},
        "goal": {"goal": f"Create file {path}", "success_criteria": [f"File {path} exists with verified content"]},
        "plan": {"steps": [{"title": "Create requested file", "tool_hint": "file_write"}],
                 "tool_groups": ["files"], "memory_layers": []},
    }
    async with session_scope() as s:
        task = Task(title=marker, kind="agent", status="in_progress")
        s.add(task)
        await s.flush()
        routine_id = None
        if routine:
            row = Routine(name=marker, instruction="Create requested file", trigger_type="once",
                          run_at=utcnow(), run_count=1, max_runs=1)
            s.add(row)
            await s.flush()
            routine_id = row.id
        run = Run(task_id=task.id, routine_id=routine_id, input=f"Create a file {marker}", trigger="api", state=state)
        s.add(run)
        await s.flush()
        run_id, task_id = run.id, task.id
    mock_llm.handlers.insert(0, handler)
    try:
        # Executes the production Run -> finalization -> Task settlement path.
        await Worker()._execute(run_id)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        return await s.get(Run, run_id), await s.get(Task, task_id), get_settings().workspace_dir / path


@pytest.mark.parametrize("result", [
    ToolResult(False, "write failed", verified=False),
    ToolResult(True, "unconfirmed write"),
    ToolResult(True, "verification failed", verified=False),
    None,
    {"ok": True, "output": "not a ToolResult"},
    ToolResult("true", "malformed success", verified=True),
    ToolResult(True, None, verified=True),
    ToolResult(True, "", verified=True),
])
async def test_failed_missing_or_malformed_tool_cannot_complete_task(
        result, app_client, configured_model, mock_llm, monkeypatch):
    async def bad_handler(ctx, args):
        return result

    monkeypatch.setattr(BUILTIN_BY_NAME["file_write"], "handler", bad_handler)
    run, task, path = await _execute(mock_llm, configured_model)
    assert run.status == "failed", run.result
    assert task.status == "failed"
    assert run.stop_reason == "validation"
    assert run.state["validation"]["passed"] is False
    assert "file was created successfully" not in run.result
    assert not path.exists()
    async with session_scope() as s:
        from sqlalchemy import select
        episode = (await s.execute(select(Episode).where(Episode.run_id == run.id))).scalar_one()
        assert episode.status == "failed"


@pytest.mark.parametrize("response", ["no_call", "unknown_tool", "load_only"])
async def test_action_claim_without_execution_evidence_fails(response, app_client, configured_model, mock_llm):
    run, task, path = await _execute(mock_llm, configured_model, response=response)
    assert run.status == "failed"
    assert task.status == "failed"
    assert run.stop_reason == "validation"
    assert not path.exists()


async def test_real_filesystem_permission_failure_propagates(app_client, configured_model, mock_llm):
    # No tool is mocked: the real path guard refuses the write before touching disk.
    run, task, path = await _execute(mock_llm, configured_model, response="unsafe_path")
    assert run.status == task.status == "failed"
    assert run.state["evidence"][0]["ok"] is False
    assert not path.exists()


async def test_validation_failure_reaches_routine_and_task_settlement(app_client, configured_model, mock_llm):
    run, task, _ = await _execute(mock_llm, configured_model, response="unsafe_path", routine=True)
    assert run.status == task.status == "failed"
    async with session_scope() as s:
        routine = await s.get(Routine, run.routine_id)
        assert routine.status == "failed"
        assert routine.failure_count == 1 and routine.success_count == 0


async def test_conversation_does_not_require_a_tool(app_client, configured_model):
    response = await app_client.post("/api/chat/send", json={"text": "سلام"})
    run_id = uuid.UUID(response.json()["run"]["id"])
    await Worker()._execute(run_id)
    async with session_scope() as s:
        run = await s.get(Run, run_id)
        assert run.status == "succeeded" and run.steps_count == 0


async def test_unvalidated_early_return_cannot_bypass_finalization_gate(
        app_client, configured_model, mock_llm, monkeypatch):
    from xagent_agent.pipeline import AgentRunner
    monkeypatch.setattr(AgentRunner, "_pipeline", AsyncMock(return_value="Done, successfully completed."))
    run, task, path = await _execute(mock_llm, configured_model)
    assert run.status == task.status == "failed"
    assert run.stop_reason == "validation" and not path.exists()


async def test_clarification_is_not_a_completed_task(app_client, configured_model, mock_llm):
    marker = "AMBIGUOUS-" + uuid.uuid4().hex

    def clarify(body):
        if marker in json.dumps(body, ensure_ascii=False) and body.get("response_format"):
            return json.dumps({"goal": "Create the file", "success_criteria": ["File exists"],
                               "ambiguity": "Which destination folder should I use?"})
        return None

    response = await app_client.post("/api/chat/send", json={"text": f"یک فایل بساز، بررسی کن و سپس گزارش بده {marker}"})
    run_id = uuid.UUID(response.json()["run"]["id"])
    async with session_scope() as s:
        task = Task(title=marker, status="in_progress", kind="agent")
        s.add(task)
        await s.flush()
        run = await s.get(Run, run_id)
        run.task_id = task.id
        task_id = task.id
    mock_llm.handlers.insert(0, clarify)
    try:
        await Worker()._execute(run_id)
    finally:
        mock_llm.handlers.remove(clarify)
    async with session_scope() as s:
        run, task = await s.get(Run, run_id), await s.get(Task, task_id)
        assert run.status == "paused" and run.stop_reason == "clarification"
        assert task.status == "waiting" and task.completed_at is None


@pytest.mark.parametrize("evaluator", ["not json", "{}", '{"passed":"false"}', '{"passed":1}',
                                       '{"passed":null}', '{"passed":true,"issues":null}',
                                       '{"passed":true,"issues":[]}',
                                       '{"passed":true,"criteria":[{"index":0,"passed":true,"evidence":["fake"]}]}',
                                       '{"passed":true,"criteria":[{"index":0,"passed":true,"evidence":[]}]}',
                                       '{"passed":false,"issues":["not satisfied"]}'])
async def test_invalid_or_repeated_failed_evaluation_never_becomes_success(
        evaluator, app_client, configured_model, mock_llm):
    run, task, path = await _execute(mock_llm, configured_model, complex_task=True, evaluator=evaluator)
    # The write really happened; evaluation failure must not be represented as a rollback.
    assert path.read_text() == "verified content"
    assert run.status == "failed"
    assert task.status == "failed"
    assert run.state["validation"]["passed"] is False
    assert run.state["recoveries"] == 1
    assert run.stop_reason == "validation"


@pytest.mark.parametrize("complex_task", [False, True])
async def test_verified_execution_completes_run_and_task(app_client, configured_model, mock_llm, complex_task):
    run, task, path = await _execute(mock_llm, configured_model, complex_task=complex_task)
    assert path.read_text() == "verified content"
    assert run.status == "succeeded", run.error
    assert task.status == "done"
    assert run.state["validation"]["passed"] is True
    assert run.state["evidence"][0]["verified"] is True


async def test_empty_answer_cannot_be_replaced_by_success_placeholder(app_client, configured_model, mock_llm):
    run, task, path = await _execute(mock_llm, configured_model, empty_answer=True)
    assert path.read_text() == "verified content"
    assert run.status == task.status == "failed"
    assert run.stop_reason == "validation"


async def test_evaluator_unavailable_fails_closed(monkeypatch):
    from xagent_agent import pipeline
    runner = pipeline.AgentRunner()
    monkeypatch.setattr(runner, "_step", AsyncMock())
    monkeypatch.setattr(pipeline, "select_models", AsyncMock(return_value=SimpleNamespace(models=[])))
    rc = SimpleNamespace(run=SimpleNamespace(task_id=None, routine_id=None, employee_id=None, input="Explain"), session=None,
                         state={"goal": {"success_criteria": ["Explain the answer"]}})
    assert (await runner._validate(rc, "An explanation", True))["passed"] is False


async def test_evaluator_transport_error_fails_closed(monkeypatch):
    from xagent_agent import pipeline
    runner = pipeline.AgentRunner()
    monkeypatch.setattr(runner, "_step", AsyncMock())
    monkeypatch.setattr(pipeline, "select_models", AsyncMock(return_value=SimpleNamespace(models=[object()])))
    monkeypatch.setattr(runner, "_llm", AsyncMock(side_effect=pipeline.ProviderError("offline")))
    rc = SimpleNamespace(run=SimpleNamespace(task_id=None, routine_id=None, employee_id=None, input="Explain"), session=None,
                         state={"goal": {"success_criteria": ["Explain the answer"]}})
    assert (await runner._validate(rc, "An explanation", True))["passed"] is False


def test_missing_result_and_unrelated_success_cannot_satisfy_plan():
    from xagent_agent.validation import evidence_issues
    state = {"plan": {"steps": [{"tool_hint": "telegram_send"}]}, "evidence": [
        {"tool": "file_write", "ok": True, "verified": True},
    ], "transcript": [{"role": "assistant", "tool_calls": [{"id": "missing", "name": "telegram_send"}]}]}
    assert len(evidence_issues(state, True)) == 2
    state["evidence"].append({"tool": "telegram_send", "call_id": "missing", "ok": False, "verified": False})
    assert evidence_issues(state, True)


def test_verified_retry_can_resolve_same_operation_without_erasing_failure_evidence():
    from xagent_agent.validation import evidence_issues
    evidence = [{"tool": "file_write", "signature": "same", "ok": False, "verified": False},
                {"tool": "file_write", "signature": "same", "ok": True, "verified": True}]
    assert not evidence_issues({"evidence": evidence}, True)
    assert evidence[0]["ok"] is False


def test_observation_contract_remains_compatible():
    from xagent_agent.validation import checked_result
    result = ToolResult(True, "No matches", {"count": 0})
    assert checked_result(BUILTIN_BY_NAME["memory_search"], {"query": "audit"}, result) is result
