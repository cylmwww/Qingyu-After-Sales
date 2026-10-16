import asyncio
import os
from uuid import uuid4

import pytest

from backend.agent import AgentRuntime, setup_agent, checkpointer
from backend.agent_contract import AgentRequest, AgentConfig
from backend.agent_store import AgentStore, AgentError
from backend.business_tools import BusinessService, response
from backend.config import Settings
from backend.db import Database
from backend.ingest import ingest
from backend.models import ModelAdapter

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要显式隔离PostgreSQL')]


async def environment(strategy='dynamic', **extra):
    s=Settings(_env_file=None,database_url=os.environ['TEST_DATABASE_URL'],rag_mode='mock',
        agent_config=AgentConfig(id='test-'+strategy,strategy=strategy,experiment=strategy!='auto'), **extra)
    db=Database(s); await db.migrate(); await setup_agent(db)
    index=await db.current_index()
    compatible=False
    if index:
        try:
            db.check_index(index);compatible=True
        except Exception:
            pass
    if not compatible:
        adapter=ModelAdapter(s)
        try: await ingest(s,db,adapter)
        finally: await adapter.close()
    return s,db,AgentStore(db)


async def submit(store, s, sid, question, facts=None, **extra):
    body=AgentRequest(session_id=sid,request_id=uuid4(),question=question,facts=facts or {},**extra)
    tid,launch=await store.submit(body,'CUST-001',BusinessService(snapshot_id=s.business_snapshot_id))
    return tid,body


async def run(store, s, tid, runtime=AgentRuntime):
    await runtime(s,await store.task(tid)).run()
    return await store.task(tid),await store.db.trace(tid)


@pytest.mark.parametrize('strategy',['fast','fixed','dynamic'])
async def test_three_paths_and_locked_configuration(strategy):
    s,db,store=await environment(strategy)
    sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'ORD-006退款金额为什么只能退这么多？')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['path']==strategy
    assert trace['result']['reviewed'] is False
    assert trace['result']['evidence']
    tools=[e['payload']['tool'] for e in trace['events'] if e['kind']=='tool_result']
    if strategy=='fast': assert tools==['search_knowledge']
    else:
        assert tools[-1]=='calculate_refund'
        calc=[e['payload']['result'] for e in trace['events'] if e['kind']=='tool_result' and e['payload']['tool']=='calculate_refund'][0]
        assert calc['data']['total_estimated_cents']==15819
    assert task['budget']['models']<=12 and task['budget']['tools']<=10


async def test_idempotency_concurrent_session_and_cross_customer():
    s,db,store=await environment()
    sid=await store.create_session('CUST-001')
    body=AgentRequest(session_id=sid,request_id=uuid4(),question='ORD-014物流在哪里')
    results=await asyncio.gather(*[store.submit(body,'CUST-001',BusinessService()) for _ in range(3)])
    assert len({r[0] for r in results})==1 and sum(r[1] for r in results)==1
    tid=results[0][0]
    with pytest.raises(AgentError,match='idempotency_conflict'):
        await store.submit(body.model_copy(update={'question':'不同问题'}),'CUST-001',BusinessService())
    with pytest.raises(AgentError,match='session_busy'):
        await submit(store,s,sid,'ORD-001物流')
    with pytest.raises(AgentError,match='task_not_found'):
        await store.task(tid,'CUST-002')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed' and trace['result']['reason']=='order_access_denied'
    assert all('"data": null' in e['text'] for e in trace['result']['evidence'])
    assert '无权查询' in trace['result']['answer']


async def test_clarification_revision_and_order_switch():
    s,db,store=await environment()
    sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'退款金额是多少？')
    task,trace=await run(store,s,tid)
    assert task['status']=='waiting'
    assert trace['result']['missing']==['order_id']
    frozen=task['frozen']
    tid2,_=await submit(store,s,sid,'ORD-006',{'packaging_intact':False},task_id=tid,action='clarify')
    assert tid2==tid
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['reason']=='eligibility_not_confirmed'
    assert task['revision']==1 and task['frozen']==frozen
    tid,_=await submit(store,s,sid,'ORD-013退款金额？')
    task,trace=await run(store,s,tid)
    assert set(task['facts'])=={'order_id'}
    assert trace['result']['reason']==''
    assert any(e['payload'].get('result',{}).get('data',{}).get('total_estimated_cents')==6790 for e in trace['events'] if e['kind']=='tool_result' and e['payload']['result']['data'])


async def test_late_results_cannot_pollute_after_cancel():
    s,db,store=await environment()
    sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'ORD-001物流')
    task=await store.task(tid)
    await store.operation(tid,task['attempt'],0,'late','tool',{})
    await store.cancel(tid,'CUST-001')
    newer,_=await submit(store,s,sid,'ORD-006退款进度')
    with pytest.raises(AgentError,match='stale_attempt'):
        await store.result(tid,task['attempt'],0,'late',{'secret':'late'})
    with pytest.raises(AgentError,match='stale_attempt'):
        await store.event(tid,task['attempt'],'tool_result',{'late':True})
    assert newer!=tid and (await store.task(newer))['status']=='running'
    await store.cancel(newer,'CUST-001')


async def test_durable_reuse_crash_window_and_explicit_resume():
    s,db,store=await environment('fixed')
    sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'ORD-006退款金额？')
    class CrashAfterTool(AgentRuntime):
        async def execute(self,state):
            value=await super().execute(state)
            raise RuntimeError('模拟结果落盘后检查点前崩溃')
    task,trace=await run(store,s,tid,CrashAfterTool)
    assert task['status']=='failed'
    durable_tools=task['budget']['tools']
    await submit(store,s,sid,'显式恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert any(e['kind']=='operation_reused' for e in trace['events'])
    assert task['budget']['tools']==6
    assert durable_tools==1
    # 单独制造外部调用开始但没有可靠结果的窗口，恢复时记录可能重执行。
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-001物流')
    task=await store.task(tid)
    await store.operation(tid,task['attempt'],0,'window','tool',{})
    async with db.connection() as c:
        await c.execute("UPDATE agent_tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(tid,))
    await store.recover_expired()
    assert (await store.task(tid))['status']=='interrupted'
    await submit(store,s,sid,'显式恢复',task_id=tid,action='resume')
    task=await store.task(tid)
    assert await store.operation(tid,task['attempt'],0,'window','tool',{}) is None
    assert any(e['kind']=='possible_reexecution' for e in (await db.trace(tid))['events'])
    assert task['budget']['active_ms']>=20000
    await store.cancel(tid,'CUST-001')


async def test_budget_timeout_retry_and_no_progress():
    s,db,store=await environment('dynamic')
    s=s.model_copy(update={'agent_config':AgentConfig(id='small-budget',strategy='dynamic',experiment=True,request_tools=1)})
    store=AgentStore(Database(s));sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'ORD-006退款金额？')
    task,trace=await run(store,s,tid)
    assert task['status']=='failed' and trace['error']['code']=='tools_budget_exceeded'
    await store.cancel(tid,'CUST-001')
    s,db,store=await environment('auto')
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'运费政策')
    class Empty(AgentRuntime):
        async def invoke_tool(self,tool,args,state): return response('no_result')
    task,trace=await run(store,s,tid,Empty)
    assert task['status']=='completed'
    assert trace['result']['reason']=='no_progress_exit'
    assert len([e for e in trace['events'] if e['kind']=='replan'])==1
    assert task['frozen']['agent_config']['strategy']=='auto'
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-001物流')
    class Transient(AgentRuntime):
        async def invoke_tool(self,tool,args,state): return response('transient_failure',code='tool_timeout')
    task,trace=await run(store,s,tid,Transient)
    calls=[e for e in trace['events'] if e['kind']=='tool_result']
    assert len(calls)==2 and task['budget']['tools']==2
    assert task['status']=='failed' and trace['error']['code']=='tool_timeout'


async def test_api_auth_sse_reconnect_and_repeated_submit():
    import httpx
    from backend.main import create_app
    s,db,store=await environment('fixed',api_access_token='local-stage6-test')
    app=create_app(s,db)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.post('/api/agent/sessions')).status_code==401
            headers={'Authorization':'Bearer local-stage6-test'}
            sid=(await client.post('/api/agent/sessions',headers=headers)).json()['session_id']
            body={'session_id':sid,'request_id':str(uuid4()),'question':'退款金额是多少？'}
            result=await client.post('/api/agent/chat',headers=headers,json=body)
            assert result.status_code==200 and 'event: clarification' in result.text
            tid=result.headers['X-Task-ID']
            duplicate=await client.post('/api/agent/chat',headers=headers,json=body)
            assert duplicate.headers['X-Task-ID']==tid
            body.update(request_id=str(uuid4()),task_id=tid,action='clarify',facts={'order_id':'ORD-006'})
            resumed=await client.post('/api/agent/chat',headers=headers,json=body)
            assert 'event: done' in resumed.text
            trace=(await client.get('/api/traces/'+tid,headers=headers)).json()
            seq=trace['events'][-1]['sequence']
            assert not (await client.get('/api/traces/'+tid+'/events?after='+str(seq),headers=headers)).text
            assert (await client.get('/api/agent/tasks/'+tid,headers=headers)).json()['status']=='completed'


async def test_executor_cannot_change_plan_or_guess_order():
    s,db,store=await environment('dynamic')
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-006退款金额？')
    class Guess(AgentRuntime):
        async def decision(self,role,payload,schema,limit,mock,key):
            if role=='executor': return {'step_id':'step1','tool':'get_order','arguments':{'order_id':'ORD-014'}}
            return await super().decision(role,payload,schema,limit,mock,key)
    task,trace=await run(store,s,tid,Guess)
    assert task['status']=='completed', trace['error']
    assert not any(c['role']=='executor' for c in trace['model_calls'])
    starts=[e['payload'] for e in trace['events'] if e['kind']=='tool_start']
    business_starts=[e for e in starts if e['tool']!='search_knowledge']
    assert business_starts and all(e['arguments'].get('order_id')=='ORD-006' for e in business_starts)
    assert not any(e['arguments'].get('order_id')=='ORD-014' for e in starts)


async def test_real_timeout_and_explicit_failure_retry():
    s,db,store=await environment('fixed')
    s=s.model_copy(update={'agent_config':AgentConfig(id='timeout-check',strategy='fixed',experiment=True,business_timeout=.02)})
    store=AgentStore(Database(s));sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-001物流')
    class Slow(AgentRuntime):
        async def invoke_tool(self,tool,args,state):
            await asyncio.sleep(1)
            return await super().invoke_tool(tool,args,state)
    task,trace=await run(store,s,tid,Slow)
    assert task['status']=='failed' and trace['error']['code']=='tool_timeout'
    assert task['budget']['tools']==2
    await submit(store,s,sid,'恢复查询',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert any(e['kind']=='operation_retry_after_failure' for e in trace['events'])


async def test_checkpoint_recovery_across_killed_python_process():
    import subprocess
    import sys
    from pathlib import Path
    s,db,store=await environment('fixed')
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-006退款金额？')
    code='''import asyncio,os,sys
from backend.config import Settings
from backend.db import Database
from backend.agent_store import AgentStore
from backend.agent import AgentRuntime
class Slow(AgentRuntime):
 async def invoke_tool(self,tool,args,state):
  await asyncio.sleep(60)
  return await super().invoke_tool(tool,args,state)
async def main():
 s=Settings(_env_file=None,rag_mode='mock',database_url=os.environ['TEST_DATABASE_URL'])
 task=await AgentStore(Database(s)).task(sys.argv[1])
 await (Slow if sys.argv[2]=='slow' else AgentRuntime)(s,task).run()
asyncio.run(main())
'''
    log=Path('local/artifacts/stage6')/('process-crash-'+tid+'.txt');log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('w',encoding='utf-8') as output:
        proc=subprocess.Popen([sys.executable,'-c',code,tid,'slow'],stdout=output,stderr=subprocess.STDOUT)
        try:
            witnessed=False
            for _ in range(900):
                async with db.connection() as c:
                    row=await (await c.execute("SELECT count(*) AS n FROM agent_operations WHERE task_id=%s AND kind='tool' AND status='started'",(tid,))).fetchone()
                if row['n']:
                    witnessed=True;break
                if proc.poll() is not None:break
                await asyncio.sleep(.1)
            assert witnessed,log.read_text('utf-8')
        finally:
            if proc.poll() is None:proc.kill()
            proc.wait(timeout=10)
        # 模拟租约到期的时钟前移；执行进程确实被终止，不靠同对象内存恢复。
        async with db.connection() as c:
            await c.execute("UPDATE agent_tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(tid,))
        await store.recover_expired()
        assert (await store.task(tid))['status']=='interrupted'
        await submit(store,s,sid,'显式恢复',task_id=tid,action='resume')
        restored=subprocess.Popen([sys.executable,'-c',code,tid,'normal'],stdout=output,stderr=subprocess.STDOUT)
        try:
            for _ in range(900):
                if restored.poll() is not None:break
                await asyncio.sleep(.1)
            assert restored.poll()==0,log.read_text('utf-8')
        finally:
            if restored.poll() is None:restored.kill()
            restored.wait(timeout=10)
    task=await store.task(tid);trace=await db.trace(tid)
    assert task['status']=='completed',trace['error']
    assert any(e['kind']=='possible_reexecution' for e in trace['events'])


async def test_active_budget_consumption_survives_limit_error():
    s,db,store=await environment('fixed')
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-001物流')
    task=await store.task(tid)
    with pytest.raises(AgentError,match='active_time_budget_exceeded'):
        await store.heartbeat(tid,task['attempt'],301000,s)
    assert (await store.task(tid))['budget']['active_ms']==301000


async def test_token_budget_shared_across_explicit_resume():
    s,db,store=await environment('fixed')
    s=s.model_copy(update={'agent_config':AgentConfig(id='task-token-limit',strategy='fixed',experiment=True,token_budget=1000)})
    sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'ORD-006退款进度')
    task=await store.task(tid)
    await store.reserve(tid,task['attempt'],s.agent_config,'models',700)
    await store.finish(tid,task['attempt'],'failed',{},code='injected_process_failure')
    body=AgentRequest(session_id=sid,request_id=uuid4(),question='显式恢复',task_id=tid,action='resume')
    await store.submit(body,'CUST-001',BusinessService())
    resumed=await store.task(tid)
    assert resumed['budget']['request_tokens']==700
    assert resumed['budget']['reserved_tokens']==700
    with pytest.raises(AgentError,match='token_budget_exceeded'):
        await store.reserve(tid,resumed['attempt'],s.agent_config,'models',400)
    saved=await store.task(tid)
    assert saved['budget']['models']==1 and saved['budget']['reserved_tokens']==700
    await store.cancel(tid,'CUST-001')
