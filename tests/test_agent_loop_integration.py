"""独立 PostgreSQL + 真实 LangGraph + 输入敏感模拟模型；不代表真实语义验收。"""
import asyncio
from copy import deepcopy
import json
import os
from uuid import uuid4

import pytest
from backend.agent_dispatch import create_runtime, DispatchRuntime
from backend.agent_contract import AgentConfig, AgentRequest
from backend.agent_loop.contracts import LoopConfig
from backend.agent_loop.store import LoopStore
from backend.agent_store import AgentError
from backend.review_contract import ReviewConfig
from backend.main import public_event
from test_stage6_integration import environment, submit

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要显式隔离PostgreSQL')]


async def env(**extra):
    return await environment(runtime_version='agent-loop-v1',loop_config=LoopConfig(),
        review_config=ReviewConfig(id='loop-integration-post',postcheck=True),**extra)


async def run(store,s,tid,cls=None):
    task=await store.task(tid)
    await (cls(s,task) if cls else create_runtime(s,task)).run()
    return await store.task(tid),await store.db.trace(tid)


async def start(s,store,question='ORD-006订单状态'):
    sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,question)
    return sid,tid


async def test_order_vertical_slice_and_public_reconnect():
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['reviewed'] is True,trace['result']
    assert trace['result']['runtime_version']=='agent-loop-v1'
    assert len([e for e in trace['events'] if e['kind']=='route'])==1
    assert len([c for c in trace['model_calls'] if c['role']=='intent'])==1
    kinds=[e['kind'] for e in trace['events']]
    for kind in ['agent_decision','observation_applied','candidate_created','review_result','answer_release','done']:
        assert kind in kinds
    loop=await LoopStore(create_runtime(s,task)).load()
    assert trace['result']['answer']==loop['candidate']['answer']
    assert loop['phase']=='terminal'
    public=[public_event(e['kind'],e['payload']) for e in trace['events'] if e['kind']!='done']
    assert loop['candidate']['answer'] not in json.dumps(public,ensure_ascii=False)
    assert task['budget']['models']==5 and task['budget']['tools']==1
    assert all(c.get('operation_key') and c.get('reservation_id') for c in trace['model_calls'] if c['role'].startswith('agent_'))


@pytest.mark.parametrize('fault',['incomplete','finding','wrong_identity','wrong_amount'])
async def test_review_gate_never_leaks(monkeypatch,fault):
    from backend.agent_loop import runtime as module
    original=module.structured
    async def invoke(adapter,role,payload,schema,maximum,record,mock,system_prompt=None):
        if role=='agent_review':
            if fault=='incomplete':mock=dict(mock,complete=False)
            if fault=='finding':mock=dict(mock,findings=[dict(kind='unsupported_claim',block_id='b1',goal_id=None,evidence_refs=[],detail='不得发布')])
            if fault=='wrong_identity':mock=dict(mock,candidate_id='wrong')
        if role=='agent_draft' and fault=='wrong_amount':
            mock=deepcopy(mock);mock['blocks'][0]['text']='预计退款 99999 元'
        return await original(adapter,role,payload,schema,maximum,record,mock,system_prompt)
    monkeypatch.setattr(module,'structured',invoke)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert not (trace.get('result') or {}).get('reviewed')
    assert not any(e['kind']=='answer_release' for e in trace['events'])
    assert '99999' not in json.dumps([public_event(e['kind'],e['payload']) for e in trace['events']])


async def test_tool_durable_result_resume_does_not_repeat(monkeypatch):
    original=LoopStore.transition;crashed=False
    async def crash(self,old,new,events):
        nonlocal crashed
        if not crashed and any(k=='observation_applied' for k,p in events):
            crashed=True
            raise AgentError('fixture_after_tool_result_before_apply')
        return await original(self,old,new,events)
    monkeypatch.setattr(LoopStore,'transition',crash)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='failed'
    before=task['budget']
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace
    assert task['budget']['tools']==before['tools']==1
    assert task['budget']['models']>before['models']
    assert task['budget']['reserved_tokens']>before['reserved_tokens']
    assert len([e for e in trace['events'] if e['kind']=='tool_start'])==1
    assert len([e for e in trace['events'] if e['kind']=='observation_applied'])==1
    assert any(e['kind']=='operation_reused' for e in trace['events'])


async def test_cancel_after_review_before_publish(monkeypatch):
    original=LoopStore.publish
    async def cancel(self,state):
        await self.host.store.cancel(self.host.tid,self.host.task['customer_id'])
        return await original(self,state)
    monkeypatch.setattr(LoopStore,'publish',cancel)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='cancelled'
    assert not any(e['kind'] in {'answer_release','done'} for e in trace['events'])


async def test_budget_not_reset_on_resume_and_revision_rejected(monkeypatch):
    from backend.agent_loop.runtime import LoopRuntime
    original=LoopRuntime.draft;crashed=False
    async def crash(self,state):
        nonlocal crashed
        if not crashed:
            crashed=True
            raise AgentError('fixture_before_draft')
        return await original(self,state)
    monkeypatch.setattr(LoopRuntime,'draft',crash)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid);before=task['budget']
    with pytest.raises(AgentError,match='revision_change'):
        await submit(store,s,sid,'恢复',facts={'order_id':'ORD-001'},task_id=tid,action='resume')
    with pytest.raises(AgentError,match='scope_change'):
        await submit(store,s,sid,'现在替我查询物流',task_id=tid,action='resume')
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    resumed=await store.task(tid)
    assert resumed['budget']==before
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace


async def test_budget_exit_does_not_publish():
    s,db,store=await env()
    s=s.model_copy(update={'agent_config':AgentConfig(id='loop-low-'+str(uuid4()),strategy='dynamic',experiment=True,request_models=3)})
    from backend.db import Database
    from backend.agent_store import AgentStore
    store=AgentStore(Database(s));sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert trace['result']['reason']=='unresolved_budget'
    assert trace['result']['evidence']==[] and not trace['result']['reviewed']
    assert task['budget']['models']<=3


async def test_refund_initial_evidence_chain():
    s,db,store=await env();sid,tid=await start(s,store,'ORD-006退款金额为什么只能退这么多？')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['reviewed'],trace['result']
    assert '158.19' in trace['result']['answer']
    assert task['budget']['models']<=12 and task['budget']['reserved_tokens']<=60000
    observed=[e['payload']['tool'] for e in trace['events'] if e['kind']=='observation_applied']
    assert observed[0]=='get_order' and observed[-1]=='calculate_refund'


@pytest.mark.parametrize('point',['decision','draft','review'])
async def test_model_result_durable_before_state_resume(monkeypatch,point):
    original=LoopStore.transition;crashed=False
    event={'decision':'agent_decision','draft':'candidate_created','review':'review_result'}[point]
    async def crash(self,old,new,events):
        nonlocal crashed
        if not crashed and any(k==event for k,p in events):
            crashed=True
            raise AgentError('fixture_after_model_result_before_state')
        return await original(self,old,new,events)
    monkeypatch.setattr(LoopStore,'transition',crash)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='failed'
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace
    assert task['budget']['models']==5 and task['budget']['tools']==1


@pytest.mark.parametrize('mutation',['candidate','bundle','goal','revision'])
async def test_stale_publication_binding_rejected(monkeypatch,mutation):
    from psycopg.types.json import Jsonb
    original=LoopStore.publish
    async def mutate(self,state):
        value=deepcopy(state)
        if mutation=='candidate':value['candidate']['answer']='未审篡改正文'
        if mutation=='bundle':value['bundle']['items'][0]['text']='证据被换掉'
        if mutation=='goal':value['goal_version']+=1
        if mutation=='revision':value['revision']+=1
        async with self.host.db.connection() as c:
            await c.execute('UPDATE agent_loop_states SET state=%s WHERE task_id=%s',(Jsonb(value),self.host.tid))
        return await original(self,state)
    monkeypatch.setattr(LoopStore,'publish',mutate)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='failed' and not trace['result']
    assert not any(e['kind']=='answer_release' for e in trace['events'])


async def test_repeated_result_reused_but_not_progress(monkeypatch):
    from backend.agent_loop import mock
    original=mock.respond
    def repeated(role,payload):
        value=original(role,payload)
        if role=='agent_decision':
            value['assessment_updates']=[]
            value['action']=dict(kind='tool',tool='get_order',arguments={'order_id_ref':'fact:order_id'},targets=['g-request'],
                                 expected_information='重复查询夹具',evidence_basis=[])
        return value
    monkeypatch.setattr(mock,'respond',repeated)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert trace['result']['reason']=='no_progress'
    assert task['budget']['tools']==1
    assert len([e for e in trace['events'] if e['kind']=='observation_applied'])==3
    assert not trace['result']['reviewed']


async def test_api_dispatch_sse_replay_and_frozen_profile():
    import httpx
    from backend.main import create_app
    s,db,store=await env(api_access_token='loop-test-token')
    app=create_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost',
        headers={'Authorization':'Bearer loop-test-token'}) as client:
        sid=(await client.post('/api/agent/sessions')).json()['session_id']
        request=AgentRequest(session_id=sid,request_id=uuid4(),question='ORD-006订单状态')
        result=await client.post('/api/agent/chat',json=request.model_dump(mode='json',exclude_unset=True))
        assert result.status_code==200 and 'event: done' in result.text,result.text
        tid=result.headers['X-Task-ID'];task=await store.task(tid);before=task['budget']
        assert task['frozen']['runtime_version']=='agent-loop-v1'
        trace=await db.trace(tid)
        candidate_event=next(e for e in trace['events'] if e['kind']=='candidate_created')
        assert '"blocks"' not in result.text and 'candidate_answer' not in result.text
        replay=await client.get(f'/api/traces/{tid}/events?after={candidate_event["sequence"]}')
        assert 'event: done' in replay.text and '"blocks"' not in replay.text
        assert (await store.task(tid))['budget']==before
        repeated=await client.post('/api/agent/chat',json=request.model_dump(mode='json',exclude_unset=True))
        assert repeated.headers['X-Task-ID']==tid and (await store.task(tid))['budget']==before


@pytest.mark.parametrize('order_status,next_tool',[('shipped','get_logistics'),('paid','get_after_sales')])
async def test_observation_changes_next_action_in_database(order_status,next_tool):
    class Fixture(DispatchRuntime):
        async def invoke_tool(self,tool,args,state):
            if tool!='get_order':
                raise AgentError('controlled_fixture_stop_after_branch')
            value=await super().invoke_tool(tool,args,state)
            value=deepcopy(value)
            value['data']['order']['status']=order_status
            return value
    s,db,store=await env();sid,tid=await start(s,store,'ORD-006退款金额是多少？')
    task,trace=await run(store,s,tid,Fixture)
    assert task['status']=='failed'
    calls=[e['payload']['tool'] for e in trace['events'] if e['kind']=='tool_start']
    assert calls==['get_order',next_tool]
    decisions=[c for c in trace['model_calls'] if c['role']=='agent_decision']
    payload=json.loads(decisions[-1]['messages'][1]['content'])
    assert order_status in payload['evidence'][0]['text']


@pytest.mark.parametrize('recover',[True,False])
async def test_strict_protocol_feedback_bounded_and_charged(monkeypatch,recover):
    from backend.agent_loop import runtime as module
    original=module.structured
    async def invoke(adapter,role,payload,schema,maximum,record,mock,system_prompt=None):
        if role=='agent_review' and (not recover or 'protocol_feedback' not in payload):
            mock=dict(mock,complete='true')
        return await original(adapter,role,payload,schema,maximum,record,mock,system_prompt)
    monkeypatch.setattr(module,'structured',invoke)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    calls=[c for c in trace['model_calls'] if c['role']=='agent_review']
    assert len(calls)==2 and task['budget']['models']==6
    assert len({c['reservation_id'] for c in calls})==2
    assert bool((trace['result'] or {}).get('reviewed'))==recover
    assert task['status']==('completed' if recover else 'failed')


async def test_known_usage_settled_by_reservation(monkeypatch):
    from backend.agent_loop import runtime as module
    original=module.structured
    async def invoke(adapter,role,payload,schema,maximum,record,mock,system_prompt=None):
        async def known(call):
            await record(dict(call,usage={'total_tokens':25},usage_source='controlled_fixture'))
        return await original(adapter,role,payload,schema,maximum,known,mock,system_prompt)
    monkeypatch.setattr(module,'structured',invoke)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed']
    assert task['budget']['reported_tokens']==100
    reservations=[e['payload'] for e in trace['events'] if e['kind']=='model_reservation']
    intent_reservation=next(r['reserved_tokens'] for r in reservations if r['role']=='intent')
    assert task['budget']['reserved_tokens']==intent_reservation+100


async def test_publish_rechecks_budget_after_review(monkeypatch):
    original=LoopStore.publish
    async def exceeded(self,state):
        async with self.host.db.connection() as c:
            await c.execute("UPDATE agent_tasks SET budget=jsonb_set(budget,'{reserved_tokens}','60001') WHERE id=%s",(self.host.tid,))
        return await original(self,state)
    monkeypatch.setattr(LoopStore,'publish',exceeded)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert trace['result']['reason']=='unresolved_budget' and not trace['result']['reviewed']
    assert not any(e['kind']=='answer_release' for e in trace['events'])


async def test_external_response_not_durable_keeps_consumption(monkeypatch):
    from backend.agent_store import AgentStore
    original=AgentStore.result;crashed=False
    async def crash(self,tid,attempt,revision,key,result):
        nonlocal crashed
        if not crashed and ':tool:' in key:
            crashed=True
            raise AgentError('fixture_response_before_durable_result')
        return await original(self,tid,attempt,revision,key,result)
    monkeypatch.setattr(AgentStore,'result',crash)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid);before=task['budget']
    assert task['status']=='failed' and before['tools']==1
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    assert (await store.task(tid))['budget']==before
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'] and task['budget']['tools']==2
    assert any(e['kind']=='possible_reexecution' for e in trace['events'])


@pytest.mark.parametrize('status,code,terminal',[
    ('no_permission','order_access_denied','completed'),
    ('missing_input','order_required','completed'),
    ('no_result','order_not_found','completed'),
    ('permanent_failure','quality_verification_required','completed'),
    ('transient_failure','tool_timeout','failed')])
async def test_tool_failure_semantics(status,code,terminal):
    from backend.business_tools import response
    class Fixture(DispatchRuntime):
        async def invoke_tool(self,tool,args,state):
            return response(status,code=code,missing=['order_id'] if status=='missing_input' else [])
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid,Fixture)
    assert task['status']==terminal,trace
    assert not (trace['result'] or {}).get('reviewed')
    assert not any(e['kind']=='answer_release' for e in trace['events'])


async def test_expired_lease_cannot_publish(monkeypatch):
    original=LoopStore.publish
    async def expire(self,state):
        async with self.host.db.connection() as c:
            await c.execute("UPDATE agent_tasks SET lease_until=now()-interval '1 second' WHERE id=%s",(self.host.tid,))
        return await original(self,state)
    monkeypatch.setattr(LoopStore,'publish',expire)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    await store.recover_expired()
    assert (await store.task(tid))['status']=='interrupted'
    assert not any(e['kind'] in {'done','answer_release'} for e in trace['events'])


@pytest.mark.parametrize('answer_path',[None,'configs/answers/q1-rewrite.v1.0.0.json','configs/answers/q2-parent-related.v1.0.0.json'])
async def test_retrieval_baseline_optional_context_and_auxiliary_ledger(answer_path):
    s,db,store=await env(answer_config_path=answer_path)
    sid,tid=await start(s,store,'鞋盒包装完整需要什么条件？')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert task['budget']['models']==len(trace['model_calls'])
    assert task['budget']['models']<=12 and task['budget']['reserved_tokens']<=60000
    roles=[c['role'] for c in trace['model_calls']]
    assert 'embedding' in roles
    assert ('query_rewrite' in roles)==bool(answer_path and 'q1-rewrite' in answer_path)
    for call in trace['model_calls']:
        if call['role'] in {'embedding','query_rewrite','rerank'}:
            assert ':tool:' in call['operation_key'] and call['reservation_id']
    async with db.connection() as c:
        operations=await (await c.execute("SELECT arguments FROM agent_operations WHERE task_id=%s AND kind='tool'",(tid,))).fetchall()
    assert operations[0]['arguments']['args']['query']=='鞋盒包装完整需要什么条件？'
    assert trace['result']['runtime_version']=='agent-loop-v1'
    if not trace['result']['reviewed']:
        assert trace['result']['evidence']==[]
