"""Read-only implementation audit probes; writes only disposable audit DB/Redis."""
import os, sys, asyncio, json, uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from datetime import timedelta
from decimal import Decimal
ROOT = '/workspace/Xagent'
sys.path[:0] = [f'{ROOT}/{p}' for p in ['packages/core','packages/agent_core','packages/memory','packages/tools','packages/crm','services/api','services/worker']]
# Run only against disposable test services. Supply these through the environment.
for name in ('XAGENT_DATABASE_URL', 'XAGENT_REDIS_URL', 'XAGENT_DATA_DIR',
             'XAGENT_SECRET_KEY', 'XAGENT_OWNER_PASSWORD'):
    if not os.environ.get(name):
        raise RuntimeError(f'Missing isolated audit setting: {name}')
os.environ.update(XAGENT_ENV='test', XAGENT_PROFILE='lite', XAGENT_SANDBOX='local', XAGENT_COOKIE_SECURE='false')
from sqlalchemy import select
from xagent_core import bus
from xagent_core.db import session_scope, utcnow, get_engine, dispose_engine
from xagent_core.models import Routine, Run, EventInbox, Conversation, Message
from xagent_agent.pipeline import AgentRunner

async def main():
    out = {}
    runner = AgentRunner('audit')
    rc = NS(state={'evidence':[{'tool':'telegram_send','ok':False,'verified':False,'summary':'not sent'}]}, session=None)
    with patch.object(runner, '_step', AsyncMock()):
        out['simple_failed_tool_accepted'] = await runner._validate(rc, 'Done, sent it.', False)
        rc.state['goal'] = {'goal':'Send message','success_criteria':['Message exists']}
        with patch('xagent_agent.pipeline.select_models', AsyncMock(return_value=NS(models=[object()]))), patch.object(runner, '_llm', AsyncMock(return_value=NS(response=NS(text='INVALID JSON')))):
            out['complex_invalid_evaluator_accepted'] = await runner._validate(rc, 'Done.', True)
    from xagent_agent.manifest import build_manifest
    from xagent_worker.inbox import process_event
    from xagent_worker.scheduler import reconcile_runs, sweep_lifecycle
    await bus.set_stop_all(False)
    async with session_scope() as s:
        m = await build_manifest(s, sandbox_kind='desktop')
        out['browser_without_desktop_service'] = [x for x in m['ready'] if x['key']=='browser']
        r = Routine(name='audit-event-limits', instruction='audit only', trigger_type='event', event_filter={'source':'audit'}, max_runs=1, run_count=1, budget_usd=Decimal('1'), spent_usd=Decimal('1'), end_at=utcnow()-timedelta(days=1), status='active')
        s.add(r)
        ev = EventInbox(source='audit',kind='message',payload={'text':'audit'},dedup_key='audit-'+uuid.uuid4().hex)
        s.add(ev)
        await s.flush()
        rid,eid = r.id,ev.id
    await process_event(eid,None)
    async with session_scope() as s:
        r = await s.get(Routine,rid)
        runs = (await s.execute(select(Run).where(Run.routine_id==rid))).scalars().all()
        out['expired_exhausted_event_routine']={'run_count':r.run_count,'new_runs':len(runs),'state':runs[0].state if runs else None}
        # Simulate a committed successful run followed by worker crash before settlement.
        r2 = Routine(name='audit-unsettled',instruction='audit',trigger_type='interval',interval_seconds=3600,status='active',run_count=1,max_runs=1,next_run_at=utcnow()+timedelta(hours=1))
        s.add(r2); await s.flush()
        finished = Run(routine_id=r2.id,status='succeeded',input='audit',result='done',finished_at=utcnow(),cost_usd=Decimal('.25'))
        s.add(finished); await s.flush(); r2id=r2.id
    await reconcile_runs()
    async with session_scope() as s:
        await sweep_lifecycle(s,utcnow())
    async with session_scope() as s:
        r2=await s.get(Routine,r2id)
        out['terminal_run_crash_before_settlement']={'routine_status':r2.status,'success_count':r2.success_count,'spent_usd':str(r2.spent_usd)}
        # Event routine with no future events is excluded from the expiry sweep.
        rr=Routine(name='audit-event-expiry',instruction='audit',trigger_type='event',event_filter={'source':'never'},end_at=utcnow()-timedelta(days=1),status='active')
        s.add(rr); await s.flush(); rrid=rr.id
        await sweep_lifecycle(s,utcnow())
        out['event_expiry_sweep_status']=rr.status
        # Working-memory layer accepts first message even when over its token budget.
        c=Conversation(title='audit-context',channel='system');s.add(c);await s.flush()
        s.add(Message(conversation_id=c.id,role='user',content='x '*20000));await s.flush()
        from xagent_memory.working import load_window
        from xagent_core.text import estimate_tokens
        _,msgs=await load_window(s,c.id,budget_tokens=100)
        out['working_memory_budget']={'budget':100,'actual_estimated_tokens':sum(estimate_tokens(x.content) for x in msgs)}
    # Inject processing failure; real Redis must retain retryable work.
    from xagent_worker.main import Worker
    await bus.ensure_groups()
    async with session_scope() as s:
        ev=EventInbox(source='audit-error',kind='message',payload={},dedup_key='audit-error-'+uuid.uuid4().hex)
        s.add(ev);await s.flush();failed_eid=ev.id
    # Use an isolated stream name so previous probe runs cannot consume this message.
    test_stream='audit:inbox:'+uuid.uuid4().hex
    with patch.object(bus,'INBOX_STREAM',test_stream):
        await bus.ensure_groups();await bus.enqueue_inbox(failed_eid)
        w=NS(stopping=asyncio.Event(),id='audit-failure',telegram=None)
        async def broken(*a):
            w.stopping.set()
            raise RuntimeError('injected audit processing failure')
        with patch('xagent_worker.main.process_event',broken):
            await Worker.consume_inbox(w)
        pending=await bus.get_redis().xpending(test_stream,bus.RUN_GROUP)
        async with session_scope() as s:
            ev=await s.get(EventInbox,failed_eid)
            out['inbox_failure_acknowledged']={'pending_count':pending['pending'],'database_status':ev.status}
    # Real HTTP/SSE requests, not ASGITransport, to observe dependency lifetime.
    import uvicorn,httpx,socket
    from xagent_api.main import app
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
    task=asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started: break
        await asyncio.sleep(.05)
    streams=[]
    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',timeout=10) as client:
        login=await client.post('/api/auth/login',json={'username':'admin','password':os.environ['XAGENT_OWNER_PASSWORD']})
        out['http_login_status']=login.status_code
        out['http_health_status']=(await client.get('/api/health')).status_code
        out['http_dashboard_status']=(await client.get('/api/dashboard')).status_code
        await asyncio.sleep(.05)
        before=get_engine().pool.checkedout()
        for _ in range(8):
            streams.append(await client.send(client.build_request('GET','/api/events/stream'),stream=True))
        out['sse_pool_connections']={'before':before,'open_streams':8,'checked_out':get_engine().pool.checkedout()}
        try:
            response=await client.get('/api/health',timeout=1)
            out['health_with_eight_streams']=response.status_code
        except httpx.ReadTimeout:
            out['health_with_eight_streams']='timed out after 1s'
        for stream in streams: await stream.aclose()
    server.should_exit=True
    await task
    print(json.dumps(out,indent=2,ensure_ascii=False))
    await bus.close_redis();await dispose_engine()

if __name__=='__main__': asyncio.run(main())
