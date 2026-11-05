"""Real local browser effects; no external accounts, messages or model API calls."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from autonomy_helpers import body_text, called, tool_names
from browser_env import CHROME, reset_computer, start
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(CHROME is None, reason="Chrome/Chromium unavailable")]
FORM = """<!doctype html><title>Synthetic editor</title><h1>Local editor</h1>
<form onsubmit="event.preventDefault(); window.sends=(window.sends||0)+1;
 document.querySelector('#receipt').textContent='Saved locally '+window.sends;">
<input id="draft" name="draft" aria-label="Draft"><button type="submit">Publish</button></form>
<p id="receipt">Not submitted</p>"""

@pytest.fixture(scope="module")
def env():
    e, stop = start({"/editor": (200, {}, FORM)})
    yield e
    stop()

@pytest.fixture
async def comp(env, monkeypatch, tmp_path):
    from xagent_core import bus
    from xagent_tools.builtin import computer
    monkeypatch.setattr(computer, "_cdp_url", lambda: env.cdp)
    # Isolate the starting owner state; keep the real runtime security guard.
    takeover = await bus.is_takeover()
    await bus.set_takeover(False, "synthetic-browser-fixture")
    monkeypatch.setattr(computer, "_identity_path", lambda emp: tmp_path / f"{emp}.state")
    await reset_computer(computer)
    for page in list((await computer._context()).pages):
        if page.url.startswith(env.web):
            await page.close()
    yield computer
    await reset_computer(computer)
    await bus.set_takeover(takeover, "synthetic-browser-fixture")

def ctx(employee=None):
    return SimpleNamespace(run_id=uuid.uuid4(), extra={"employee_id": employee, "approved": set()},
                           vision=None, trusted_owner=employee is None, session=None)

async def test_finished_run_keeps_visible_page_and_reuses_it(comp, env):
    first = ctx()
    assert (await comp.browser_open(first, {"url": env.web + "/editor"})).ok
    page = comp._sessions[str(first.run_id)]
    await page.fill('#draft', 'persistent draft')
    await comp.release_session(first.run_id)
    assert not page.is_closed(), "finishing a Run closed its visible browser tab"
    second = ctx()
    assert (await comp.browser_open(second, {"url": env.web + "/editor"})).ok
    assert comp._sessions[str(second.run_id)] is page
    assert await page.input_value('#draft') == 'persistent draft'

@pytest.mark.parametrize("scenario", ["normal", "reconnect", "denied", "revoked", "expired", "arguments_changed", "call_id_changed", "policy_change"])
async def test_escalated_approval_resumes_exact_browser_effect(app_client, configured_model, mock_llm, comp, env, scenario):
    from xagent_agent.pipeline import AgentRunner
    from xagent_core.db import session_scope
    from xagent_core.models import Approval, Run
    mark = "FOLLOWUP-APPROVAL"
    def handler(body):
        if mark not in body_text(body) or not body.get('tools') or body.get('response_format'):
            return None
        if 'browser_type' not in tool_names(body):
            return ('tools_load', {'groups': ['browser']})
        done = [n for n, _ in called(body)]
        if 'browser_open' not in done:
            return ('browser_open', {'url': env.web + '/editor'})
        if 'browser_type' not in done:
            return ('browser_type', {'selector': '#draft', 'value': 'synthetic payload', 'submit': True,
                                     'expect_text': 'Saved locally 1'})
        return 'The local form was submitted.'
    async with session_scope() as s:
        r = Run(trigger='chat', input=f'با مرورگر فرم را پر کن و ارسال کن ({mark})', status='queued', state={'trusted': True})
        s.add(r)
        await s.flush()
        rid = r.id
    mock_llm.handlers.insert(0, handler)
    runner = AgentRunner('followup-browser')
    try:
        await runner.execute(rid)
        async with session_scope() as s:
            assert (await s.get(Run, rid)).status == 'waiting_approval'
            ap = (await s.execute(select(Approval).where(Approval.run_id == rid, Approval.status == 'pending'))).scalar_one()
            aid = ap.id
        page = comp._sessions[str(rid)]
        assert await page.evaluate('window.sends || 0') == 0
        response = await app_client.post(f'/api/approvals/{aid}', json={'decision': 'deny' if scenario == 'denied' else 'approve'})
        assert response.status_code == 200
        if scenario == 'policy_change':
            response = await app_client.put('/api/settings/harness/external_message', json={
                'mode': 'conditional', 'conditions': {'deny': ['127.0.0.1'], 'deny_mode': 'forbid'}})
            assert response.status_code == 200
        if scenario == 'revoked':
            assert (await app_client.post(f'/api/approvals/{aid}', json={'decision': 'revoke'})).status_code == 200
        if scenario in ('arguments_changed', 'call_id_changed', 'expired'):
            from copy import deepcopy
            from datetime import timedelta

            from xagent_core.db import utcnow
            async with session_scope() as s:
                if scenario == 'expired':
                    (await s.get(Approval, aid)).decided_at = utcnow() - timedelta(days=2)
                else:
                    run = await s.get(Run, rid)
                    state = deepcopy(run.state)
                    pending = state['pending_calls'][0]
                    if scenario == 'arguments_changed':
                        pending['arguments']['value'] = 'unauthorized replacement'
                    else:
                        pending['id'] = 'different-operation'
                    run.state = state
        if scenario == 'reconnect':
            await comp.release_session(rid)
            await reset_computer(comp, cleanup=False)
            # Simulate a fresh Worker/client; browser process/profile remains alive.
            runner = AgentRunner('replacement-worker')
        await runner.execute(rid)
        if scenario == 'reconnect':
            page = next(p for p in (await comp._context()).pages if p.url.endswith('/editor'))
        if scenario in ('denied', 'revoked', 'arguments_changed', 'call_id_changed', 'expired', 'policy_change'):
            assert await page.evaluate('window.sends || 0') == 0
            assert await page.input_value('#draft') == ''
            async with session_scope() as s:
                run = await s.get(Run, rid)
                assert run.status != 'succeeded'
                if scenario == 'expired':
                    assert run.status == 'waiting_approval'
                    cards = (await s.execute(select(Approval).where(Approval.run_id == rid))).scalars().all()
                    assert any(a.status == 'pending' and a.id != aid for a in cards)
            return
        assert not page.is_closed(), 'final answer closed the observed form'
        assert await page.input_value('#draft') == 'synthetic payload', 'valid approval did not resume typing'
        assert await page.evaluate('window.sends || 0') == 1, 'authorized submission must execute exactly once'
        async with session_scope() as s:
            r = await s.get(Run, rid)
            assert r.status == 'succeeded', (r.status, r.error)
        await runner.execute(rid)
        assert await page.evaluate('window.sends || 0') == 1
    finally:
        mock_llm.handlers.remove(handler)
        if scenario == 'policy_change':
            await app_client.put('/api/settings/harness/external_message', json={'mode': 'ask', 'conditions': {}})

async def test_sequential_runs_are_bounded_and_user_tabs_protected(comp, env, monkeypatch):
    bctx = await comp._context()
    user = await bctx.new_page()
    await user.goto(env.web + '/editor')
    pages = len(bctx.pages)
    for _ in range(8):
        c = ctx()
        assert (await comp.browser_open(c, {'url': env.web + '/editor'})).ok
        await comp.release_session(c.run_id)
    assert len(bctx.pages) == pages
    assert not user.is_closed()
    monkeypatch.setattr(comp, 'MAX_IDENTITY_TABS', pages)
    blocked = await comp.browser_open(ctx(), {'url': env.web + '/editor', 'reuse': False})
    assert not blocked.ok and blocked.data['error'] == 'busy'
    assert not user.is_closed()

async def test_owner_connection_restart_recovers_saved_tab_and_draft(comp, env):
    c = ctx()
    c.extra['run_state'] = {}
    await comp.browser_open(c, {'url': env.web + '/editor'})
    page = comp._sessions[str(c.run_id)]
    await page.fill('#draft', 'retained through reconnect')
    await comp.release_session(c.run_id)
    await reset_computer(comp, cleanup=False)
    read = await comp.browser_read(c, {})
    assert read.ok
    assert await comp._sessions[str(c.run_id)].input_value('#draft') == 'retained through reconnect'

async def test_typing_is_verified_and_does_not_submit(comp, env):
    c = ctx()
    await comp.browser_open(c, {'url': env.web + '/editor'})
    result = await comp.browser_type(c, {'selector': '#draft', 'value': 'ordinary draft'})
    page = comp._sessions[str(c.run_id)]
    assert result.ok and result.verified and result.data['field_verified']
    assert await page.input_value('#draft') == 'ordinary draft'
    assert await page.evaluate('window.sends || 0') == 0

async def test_submission_without_receipt_is_uncertain_not_success(comp, env):
    c = ctx()
    c.extra['approved'] = {'external_message'}
    await comp.browser_open(c, {'url': env.web + '/editor', 'reuse': False})
    result = await comp.browser_type(c, {'selector': '#draft', 'value': 'uncertain draft', 'submit': True})
    assert not result.ok and result.verified is False and result.data['outcome_uncertain']
    assert await comp._sessions[str(c.run_id)].evaluate('window.sends') == 1

async def test_live_dom_scope_change_cannot_reuse_approval(comp, env):
    from xagent_tools.errors import EscalateCategory
    c = ctx()
    await comp.browser_open(c, {'url': env.web + '/editor'})
    args = {'selector': '#draft', 'value': 'blocked payload', 'submit': True}
    with pytest.raises(EscalateCategory) as e:
        await comp.browser_type(c, args)
    c.extra['approved'] = {e.value.category}
    c.extra['approval_scopes'] = {e.value.category: e.value.extra_args}
    page = comp._sessions[str(c.run_id)]
    await page.locator('button').evaluate("e => e.textContent = 'Delete account'")
    with pytest.raises(EscalateCategory):
        await comp.browser_type(c, args)
    assert await page.input_value('#draft') != 'blocked payload'
    assert await page.evaluate('window.sends || 0') == 0

async def test_cross_worker_tab_claim_fails_closed_and_releases(comp, env):
    from xagent_core import bus
    c = ctx()
    await comp.browser_open(c, {'url': env.web + '/editor'})
    page = comp._sessions[str(c.run_id)]
    name = comp._tab_leases[str(c.run_id)]
    assert not await bus.acquire_lease(name, 1000, 'other-worker')
    await comp.release_session(c.run_id)
    foreign = await bus.acquire_lease(name, 1000, 'other-worker')
    assert foreign
    try:
        with pytest.raises(comp.BrowserBusy):
            await comp._claim_page(ctx(), page)
    finally:
        await bus.release_lease(name, 'other-worker')
    assert not page.is_closed()

async def test_five_minute_browser_routine_waits_without_tokens_and_settles_once(app_client, configured_model,
                                                                              mock_llm, comp, env, monkeypatch):
    import asyncio
    from datetime import timedelta

    from xagent_agent.pipeline import AgentRunner
    from xagent_core.db import session_scope, utcnow
    from xagent_core.models import Routine, Run
    from xagent_worker import scheduler

    mark = 'FOLLOWUP-FIVE-MINUTE'
    def handler(body):
        if mark not in body_text(body) or not body.get('tools') or body.get('response_format'):
            return None
        if 'browser_open' not in tool_names(body):
            return ('tools_load', {'groups': ['browser']})
        if 'browser_open' not in [n for n, _ in called(body)]:
            return ('browser_open', {'url': env.web + '/editor'})
        return 'صفحه محلی مشاهده شد؛ تغییر تازه‌ای در این آزمون تعریف نشده است.'
    clock = [utcnow()]
    due = clock[0] + timedelta(minutes=5)
    async with session_scope() as s:
        routine = Routine(name=mark, instruction=f'با مرورگر سایت را بررسی کن ({mark})',
                          trigger_type='interval', interval_seconds=300, enabled=True, status='active',
                          starts_at=clock[0], next_run_at=due)
        s.add(routine)
        await s.flush()
        rtid = routine.id
    monkeypatch.setattr(scheduler, 'utcnow', lambda: clock[0])
    before = len(mock_llm.calls)
    await scheduler.tick_routines()
    assert len(mock_llm.calls) == before
    async with session_scope() as s:
        assert not (await s.execute(select(Run).where(Run.routine_id == rtid))).scalars().all()
    clock[0] = due + timedelta(seconds=1)
    await asyncio.gather(scheduler.tick_routines(), scheduler.tick_routines())
    async with session_scope() as s:
        run = (await s.execute(select(Run).where(Run.routine_id == rtid))).scalar_one()
        rid = run.id
    mock_llm.handlers.insert(0, handler)
    try:
        await AgentRunner('scheduled-browser').execute(rid)
        await scheduler.after_routine_run(rid)
        await scheduler.after_routine_run(rid)
    finally:
        mock_llm.handlers.remove(handler)
    async with session_scope() as s:
        run, routine = await s.get(Run, rid), await s.get(Routine, rtid)
        assert run.status == 'succeeded', (run.status, run.error)
        assert run.state['routine_settled'] == run.status
        assert routine.status == 'active' and routine.success_count == 1
        assert routine.next_run_at > clock[0]
        routine.enabled, routine.status = False, 'paused'
    after = len(mock_llm.calls)
    clock[0] += timedelta(minutes=10)
    await scheduler.tick_routines()
    assert len(mock_llm.calls) == after
