"""新契约隔离 PG 回归：真实 LangGraph，所有模型均离线替身。"""
import os
from copy import deepcopy
import json

import pytest

from backend.agent_loop import mock, runtime as module
from backend.agent_loop.contracts import LoopConfig
from backend.agent_loop.store import LoopStore
from backend.agent_dispatch import DispatchRuntime
from backend.agent_store import AgentError
from test_agent_loop_integration import start, run
from test_stage6_integration import environment
from backend.review_contract import ReviewConfig

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要显式隔离 PostgreSQL')]


async def env(**extra):
    return await environment(runtime_version='agent-loop-v1',
        loop_config=LoopConfig.read('configs/loops/loop-v2-bound.v1.2.0.json'),
        review_config=ReviewConfig(id='bound-test-post',postcheck=True),**extra)


@pytest.mark.parametrize('question',['ORD-006订单状态','包装政策咨询',
    '请仅查询并说明 ORD-006 当前订单状态，不需要退款金额、退款流程或物流详情。'])
async def test_new_contract_tool_candidate_review_release(question):
    s,db,store=await env();sid,tid=await start(s,store,question)
    task,trace=await run(store,s,tid)
    assert task['status']=='completed' and trace['result']['reviewed'],trace
    state=await LoopStore(DispatchRuntime(s,task)).load()
    assert state['candidate']['candidate_id']==trace['result']['candidate_id']
    assert state['review']['candidate_id']==state['candidate']['candidate_id']
    assert state['review']['bundle_id']==state['candidate']['bundle_id']
    if '包装' in question:
        event=next(e['payload'] for e in trace['events'] if e['kind']=='agent_decision')
        assert event['arguments']=={'mode':'original'}
        assert state['observations'][0]['query_origin']=='original'
    assert task['budget']['models']==len(trace['model_calls'])


@pytest.mark.parametrize('fault',['fact_evidence','first_targeted'])
async def test_action_feedback_recovery_and_atomic_updates(monkeypatch,fault):
    original=mock.respond
    def respond(role,payload):
        value=original(role,payload)
        if role=='agent_decision' and not payload['observations']:
            if payload.get('action_rejection'):
                feedback=payload['action_rejection']
                assert feedback['atomic_rejection'] and feedback['issues']
                assert not payload['assessments']
                return value
            value['assessment_updates']=[dict(goal_id='g-request',status='open',statement='不应落盘',support_refs=[],conflict_refs=[],unknowns=[])]
            if fault=='fact_evidence':value['action']['evidence_basis']=['fact:order_id']
            else:value['action']['arguments']={'mode':'targeted','query':'改写','gap_ref':'g-request'}
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env();sid,tid=await start(s,store,'包装政策咨询' if fault=='first_targeted' else 'ORD-006订单状态')
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace
    rejected=[e for e in trace['events'] if e['kind']=='action_rejected']
    assert len(rejected)==1 and rejected[0]['payload']['atomic_rejection']
    assert not any('不应落盘' in json.dumps(e['payload'],ensure_ascii=False) for e in trace['events'] if e['kind']=='goal_update')


@pytest.mark.parametrize('feedback_success',[True,False])
async def test_real_extra_field_failure_one_strict_feedback_charged(monkeypatch,feedback_success):
    original=module.structured
    async def invoke(adapter,role,payload,schema,maximum,record,mock,system_prompt=None):
        if role=='agent_decision' and payload['observations']:
            assert all('based_on_state' not in a for a in payload['assessments'].values())
            if not feedback_success or 'protocol_feedback' not in payload:
                mock=deepcopy(mock)
                mock['assessment_updates'][0]['based_on_state']=payload['state_version']
        return await original(adapter,role,payload,schema,maximum,record,mock,system_prompt)
    monkeypatch.setattr(module,'structured',invoke)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['budget']['models']==len(trace['model_calls'])
    assert len([e for e in trace['events'] if e['kind']=='loop_protocol_feedback'])==1
    if feedback_success:
        assert trace['result']['reviewed'] and task['budget']['models']==6
    else:
        assert task['status']=='failed' and task['budget']['models']==4
        assert not any(e['kind']=='answer_release' for e in trace['events'])


@pytest.mark.parametrize('role,event',[('agent_decision','agent_decision'),('agent_draft','candidate_created'),('agent_review','review_result')])
async def test_v2_cr01_feedback_success_durable_resume(monkeypatch,role,event):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_protocol_feedback_result_resume_combination(monkeypatch,role,event)


@pytest.mark.parametrize('rewrite,retries',[(False,0),(True,0),(True,1)])
async def test_v2_cr02_auxiliary_admission_and_actual_ledger(monkeypatch,rewrite,retries):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_optional_hybrid_rerank_and_rewrite_share_ledger(rewrite,retries)


async def test_new_contract_api_and_evaluate(monkeypatch):
    import httpx
    from uuid import uuid4
    from backend.main import create_app
    from scripts.evaluate import evaluation_runtime
    s,db,store=await env()
    from pydantic import SecretStr
    s=s.model_copy(update={'api_access_token':SecretStr('bound-offline-test')})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s)),base_url='http://localhost',
        headers={'Authorization':'Bearer bound-offline-test'}) as client:
        sid=(await client.post('/api/agent/sessions')).json()['session_id']
        result=await client.post('/api/agent/chat',json={'session_id':sid,'request_id':str(uuid4()),'question':'ORD-006订单状态'})
        assert result.status_code==200 and 'event: done' in result.text
        trace=await db.trace(result.headers['X-Task-ID'])
        assert trace['result']['reviewed']
    sid,tid=await start(s,store)
    task=await store.task(tid)
    task,trace=await run(store,s,tid,evaluation_runtime([],task['frozen']))
    assert trace['result']['reviewed']
