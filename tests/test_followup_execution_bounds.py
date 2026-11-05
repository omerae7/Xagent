"""Do not spend tokens retrying denied or externally uncertain effects."""

import pytest

from xagent_core.db import session_scope
from xagent_core.models import Run
from xagent_tools.base import Tool, ToolResult, obj


@pytest.mark.parametrize('mode', ['denied', 'uncertain'])
async def test_execution_stops_without_unbounded_recovery(app_client, configured_model, mock_llm, monkeypatch, mode):
    from autonomy_helpers import body_text

    from xagent_agent import pipeline
    mark = f'BOUNDED-{mode}'
    executions, calls = [], []
    async def effect(ctx, args):
        executions.append(1)
        return ToolResult(True, 'Protocol accepted the synthetic action; no independent receipt')
    tool = Tool('bounded_fx', 'Synthetic controlled effect', obj({}), effect, 'tasks',
                category='payment' if mode == 'denied' else 'routine_change')
    original = pipeline.select_tools
    async def selected(*args, **kw):
        return [*(await original(*args, **kw)), tool]
    monkeypatch.setattr(pipeline, 'select_tools', selected)
    def handler(body):
        if mark not in body_text(body) or not body.get('tools') or body.get('response_format'):
            return None
        calls.append(1)
        return ('bounded_fx', {})  # intentionally uncooperative model keeps asking
    async with session_scope() as s:
        run = Run(trigger='chat', input=f'کار را اجرا کن ({mark})', status='queued', state={'trusted': True})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await pipeline.AgentRunner('bounds').execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    print(f'BOUND_METRIC mode={mode} model_calls={len(calls)} effects={len(executions)}')
    assert len(calls) <= (4 if mode == 'denied' else 1), len(calls)
    assert len(executions) == (0 if mode == 'denied' else 1)
    async with session_scope() as s:
        run = await s.get(Run, rid)
        assert run.status == 'failed' and run.state.get('abort_reason'), (run.status, run.error)


def test_unverified_result_keeps_evidence_and_uncertainty():
    from xagent_agent.validation import checked_result
    tool = Tool('effect', '', obj({}), None, 'tasks', category='routine_change')
    settled = checked_result(tool, {}, ToolResult(True, 'Protocol OK', {'request_id': 'synthetic'}))
    assert not settled.ok and settled.data.get('outcome_uncertain') is True
    assert settled.data['request_id'] == 'synthetic'
