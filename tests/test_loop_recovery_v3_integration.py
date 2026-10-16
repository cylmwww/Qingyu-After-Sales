"""隔离PG验收，模型均为输入敏感替身，不调用供应商。"""
import os
from copy import deepcopy
import pytest
from backend.agent_loop.contracts import LoopConfig, REVIEW_DOMAINS
from backend.agent_loop.store import LoopStore
from backend.agent_dispatch import DispatchRuntime
from backend.agent_store import AgentError
from backend.review_contract import ReviewConfig
from test_agent_loop_integration import start, run
from test_stage6_integration import environment, submit

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要显式隔离PG')]

def execution(status='completed', reason=None):
    return dict(status=status, reason=reason, checked_domains=list(REVIEW_DOMAINS) if status=='completed' else [],
        unchecked_blocks=[] if status=='completed' else ['b1'], unchecked_goals=[], unchecked_findings=[])


async def env(**extra):
    return await environment(runtime_version='agent-loop-v1',loop_config=LoopConfig.read('configs/loops/loop-v3-recovery.v1.3.0.json'),
        review_config=ReviewConfig(id='recovery-test-post',postcheck=True),**extra)

@pytest.mark.parametrize('scenario',['complete_issue','incomplete_issue','empty_recovery','capacity','repeated_incomplete','dynamic_feedback','exhausted'])
async def test_review_execution_paths(monkeypatch,scenario):
    from backend.agent_loop import mock
    original=mock.respond
    def respond(role,payload):
        value=original(role,payload)
        if role=='agent_draft' and scenario in {'complete_issue','incomplete_issue'} and not payload.get('repair'):
            value['blocks'][0]['text']='冻结订单已经取消完成'
        if role=='agent_review':
            if scenario=='incomplete_issue' and not payload['pending_findings']:
                value['execution']=execution('incomplete','not_finished')
            if scenario in {'empty_recovery','repeated_incomplete','capacity'}:
                if scenario!='empty_recovery' or not payload.get('execution_recovery_used'):
                    value['execution']=execution('incomplete','capacity' if scenario=='capacity' else 'not_finished')
            if scenario in {'dynamic_feedback','exhausted'} and (scenario=='exhausted' or 'protocol_feedback' not in payload):
                value['candidate_id']='wrong'
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    state=await LoopStore(DispatchRuntime(s,task)).load()
    if scenario in {'complete_issue','incomplete_issue','empty_recovery','dynamic_feedback'}:
        assert trace['result']['reviewed'],trace
        if scenario in {'complete_issue','incomplete_issue'}:
            assert len(state['candidate_history'])==2 and state['review_history'][-1]['dispositions']
        if scenario=='empty_recovery':assert len(state['candidate_history'])==1 and len(state['review_history'])==2
    elif scenario=='exhausted':
        assert task['status']=='failed' and trace['error']['code']=='loop_protocol_exhausted'
        before=task['budget']['models']
        await submit(store,s,sid,'恢复',task_id=tid,action='resume')
        task,trace=await run(store,s,tid)
        assert task['budget']['models']==before and task['status']=='failed'
    else:
        assert not trace['result']['reviewed']
        assert trace['result']['reason']==('review_incomplete_capacity' if scenario=='capacity' else 'review_execution_recovery_exhausted')
    assert task['budget']['models']==len(trace['model_calls'])

@pytest.mark.parametrize('point',['review_result','candidate_created'])
async def test_recovery_crash_reuses_durable_outputs(monkeypatch,point):
    original=LoopStore.transition;crashed=False
    async def transition(self,old,new,events):
        nonlocal crashed
        if not crashed and any(k==point for k,p in events):
            crashed=True;raise AgentError('fixture_after_durable')
        return await original(self,old,new,events)
    monkeypatch.setattr(LoopStore,'transition',transition)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid);assert task['status']=='failed'
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'] and task['budget']['models']==5


@pytest.mark.parametrize('role,event',[('agent_decision','agent_decision'),('agent_draft','candidate_created'),('agent_review','review_result')])
async def test_v3_feedback_durable_resume(monkeypatch,role,event):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_protocol_feedback_result_resume_combination(monkeypatch,role,event)

@pytest.mark.parametrize('rewrite,retries',[(False,0),(True,0),(True,1)])
async def test_v3_auxiliary_budget(monkeypatch,rewrite,retries):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_optional_hybrid_rerank_and_rewrite_share_ledger(rewrite,retries)


async def test_v3_api_and_evaluation_shared_dispatch(monkeypatch):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_api_repair_and_evaluation_use_shared_runtime(monkeypatch)

async def test_v3_release_authority(monkeypatch):
    import test_agent_loop_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_cancel_after_review_before_publish(monkeypatch)
