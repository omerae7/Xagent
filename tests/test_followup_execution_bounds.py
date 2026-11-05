"""Do not spend tokens retrying denied or externally uncertain effects."""

import uuid

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

@pytest.mark.parametrize('duplicate', [True, False])
async def test_duplicate_tool_call_ids_are_rejected_before_database_effects(app_client, configured_model,
                                                                         mock_llm, monkeypatch, duplicate):
    from autonomy_helpers import body_text, called
    from sqlalchemy import func, select

    from xagent_agent import pipeline
    from xagent_core.models import Task

    mark = f'CALL-ID-{uuid.uuid4().hex[:8]}'
    async def create(ctx, args):
        task = Task(title=mark, status='open')
        ctx.session.add(task)
        await ctx.session.flush()
        await ctx.session.refresh(task)
        return ToolResult(True, 'Synthetic record persisted', {'id': str(task.id)}, verified=True)
    tool = Tool('call_id_fx', 'Synthetic database action', obj({}), create, 'tasks', category='routine_change')
    select_original, process_original = pipeline.select_tools, pipeline.AgentRunner._process_calls
    async def select_tools(*args, **kwargs):
        return [*(await select_original(*args, **kwargs)), tool]
    async def inject_calls(self, rc, pending, *args, **kwargs):
        if pending and pending[0]['name'] == tool.name:
            second = dict(pending[0])
            if not duplicate:
                second['id'] += '-distinct'
            pending = [pending[0], second]
        return await process_original(self, rc, pending, *args, **kwargs)
    monkeypatch.setattr(pipeline, 'select_tools', select_tools)
    monkeypatch.setattr(pipeline.AgentRunner, '_process_calls', inject_calls)
    def handler(body):
        if mark in body_text(body) and body.get('tools') and not body.get('response_format'):
            return (tool.name, {}) if not called(body) else 'The requested work finished.'
        return None
    async with session_scope() as s:
        run = Run(trigger='chat', input=f'کار را انجام بده ({mark})', status='queued', state={'trusted': True})
        s.add(run)
        await s.flush()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await pipeline.AgentRunner('call-id').execute(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        count = (await s.execute(select(func.count()).select_from(Task).where(Task.title == mark))).scalar_one()
        run = await s.get(Run, rid)
        assert count == (0 if duplicate else 2), f'durable database effects: {count}'
        assert run.status == ('failed' if duplicate else 'succeeded'), (run.status, run.error)
