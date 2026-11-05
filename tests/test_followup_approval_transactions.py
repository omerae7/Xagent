"""Approval decisions across real PostgreSQL transactions (synthetic operations)."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from xagent_core import approvals
from xagent_core.db import session_scope, utcnow
from xagent_core.models import Approval, Run

pytestmark = pytest.mark.asyncio

async def records(cards=1, status='waiting_approval'):
    async with session_scope() as s:
        run = Run(trigger='chat', input='synthetic approval', status=status, state={'trusted': True})
        s.add(run)
        await s.flush()
        aps = [Approval(run_id=run.id, tool_call_id=f'call-{i}', tool_name='browser_type',
                        category='web_irreversible', args={}) for i in range(cards)]
        s.add_all(aps)
        await s.flush()
        return run.id, [a.id for a in aps]

async def decision(aid, action='approve'):
    async with session_scope() as s:
        a = await s.get(Approval, aid)
        return await approvals.decide(s, a, action, 'synthetic-owner')

async def test_duplicate_callbacks_have_one_committed_decision(app_client):
    rid, ids = await records()
    a, b = await asyncio.gather(decision(ids[0]), decision(ids[0]))
    assert sum(d.ok for d in (a, b)) == 1
    assert sum(d.requeue == rid for d in (a, b)) == 1

async def test_simultaneous_distinct_cards_do_not_lose_wakeup(app_client):
    rid, ids = await records(cards=2)
    decisions = await asyncio.gather(*(decision(aid) for aid in ids))
    assert all(d.ok for d in decisions)
    assert sum(d.requeue == rid for d in decisions) == 1
    async with session_scope() as s:
        assert (await s.get(Run, rid)).status == 'queued'

@pytest.mark.parametrize('status', ['succeeded', 'cancelled', 'failed'])
async def test_late_approval_never_requeues_terminal_run(app_client, status):
    rid, ids = await records(status=status)
    result = await decision(ids[0])
    assert result.requeue is None
    async with session_scope() as s:
        assert (await s.get(Run, rid)).status == status

@pytest.mark.parametrize('action', ['deny', 'revoke'])
async def test_denied_or_revoked_decision_remains_blocking(app_client, action):
    _, ids = await records()
    if action == 'revoke':
        assert (await decision(ids[0])).ok
    assert (await decision(ids[0], action)).ok
    async with session_scope() as s:
        assert (await s.get(Approval, ids[0])).status == ('denied' if action == 'deny' else 'revoked')

async def test_binding_separates_run_arguments_category_and_observed_target():
    from xagent_agent.pipeline import AgentRunner
    from xagent_tools.builtin.computer import TOOLS
    tool = next(t for t in TOOLS if t.name == 'browser_type')
    rc = SimpleNamespace(run=SimpleNamespace(id=uuid.uuid4(), employee_id=None),
                         state={'approval_scopes': {'cid': {'web_irreversible': {'url': 'https://example.test/a'}}}})
    args = {'selector': '#draft', 'value': 'synthetic', 'submit': True}
    bound = AgentRunner._approval_args(rc, 'cid', 'web_irreversible', args)
    sig = AgentRunner._binding(rc, tool, 'web_irreversible', bound)
    for other in [dict(args, value='changed'), dict(args, selector='#other')]:
        assert AgentRunner._binding(rc, tool, 'web_irreversible', AgentRunner._approval_args(rc, 'cid', 'web_irreversible', other)) != sig
    assert AgentRunner._binding(rc, tool, 'web_irreversible', AgentRunner._approval_args(rc, 'other-cid', 'web_irreversible', args)) != sig
    assert AgentRunner._binding(rc, tool, 'login', bound) != sig
    rc.run.id = uuid.uuid4()
    assert AgentRunner._binding(rc, tool, 'web_irreversible', bound) != sig

async def test_expired_matching_approval_requests_new_card(app_client, monkeypatch):
    from xagent_agent.pipeline import AgentRunner, Paused
    from xagent_tools.builtin.computer import TOOLS
    tool = next(t for t in TOOLS if t.name == 'browser_type')
    rid, ids = await records()
    await decision(ids[0])
    async with session_scope() as s:
        a = await s.get(Approval, ids[0])
        a.decided_at = utcnow() - timedelta(days=2)
        rc = SimpleNamespace(run=await s.get(Run, rid), state={}, session=s)
        runner = AgentRunner('expiry-test')
        async def step(*args, **kwargs):
            return None
        monkeypatch.setattr(runner, '_step', step)
        with pytest.raises(Paused):
            await runner._harness_decide(rc, 'call-0', tool, 'web_irreversible', {}, {})
        await s.flush()
        rows = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalars().all()
        assert sorted(a.status for a in rows) == ['expired', 'pending']
