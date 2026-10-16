import os
import pytest
from backend.agent_loop.contracts import LoopConfig
from backend.review_contract import ReviewConfig
from test_stage6_integration import environment

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要隔离PG')]


async def env(**extra):
    return await environment(runtime_version='agent-loop-v1',loop_config=LoopConfig.read('configs/loops/loop-v10-obligations.v1.10.0.json'),
        review_config=ReviewConfig(id='v10-test',postcheck=True),**extra)


@pytest.mark.parametrize('point',['candidate_created','review_result'])
async def test_v10_durable(monkeypatch,point):
    import test_loop_recovery_v3_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_recovery_crash_reuses_durable_outputs(monkeypatch,point)


@pytest.mark.parametrize('role,event',[('agent_decision','agent_decision'),('agent_draft','candidate_created'),('agent_review','review_result')])
async def test_v10_feedback_durable(monkeypatch,role,event):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_protocol_feedback_result_resume_combination(monkeypatch,role,event)


@pytest.mark.parametrize('name',['test_cancel_after_review_before_publish','test_tool_durable_result_resume_does_not_repeat',
    'test_publish_rechecks_budget_after_review','test_expired_lease_cannot_publish','test_budget_exit_does_not_publish'])
async def test_v10_release_and_resume_guards(monkeypatch,name):
    import test_agent_loop_integration as prior
    monkeypatch.setattr(prior,'env',env)
    fn=getattr(prior,name)
    if name=='test_budget_exit_does_not_publish':await fn()
    else:await fn(monkeypatch)


@pytest.mark.parametrize('mutation',['candidate','bundle','goal','revision'])
async def test_v10_stale_binding(monkeypatch,mutation):
    import test_agent_loop_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_stale_publication_binding_rejected(monkeypatch,mutation)


@pytest.mark.parametrize('after_commit',[False,True])
@pytest.mark.parametrize('boundary',['observation','binding'])
async def test_policy_coverage_recovery_is_applied_once(monkeypatch,after_commit,boundary):
    from backend.agent_loop import mock
    from backend.agent_loop.store import LoopStore
    from backend.agent_dispatch import DispatchRuntime
    from backend.agent_store import AgentError
    from test_stage6_integration import submit
    from test_agent_loop_integration import run
    original=mock.respond
    def respond(role,payload):
        # This window-only fixture supports only the verified material it actually writes.
        # The legacy mock cites the first eight search rows, which now correctly creates
        # eight independent completeness requirements under the v10 contract.
        parents=[e for e in payload['evidence'] if e.get('completeness')=='verified_parent']
        value=original(role,dict(payload,evidence=parents) if parents else payload)
        if role=='agent_decision' and payload.get('condition_gaps') and payload.get('search_mode')=='targeted':
            gap=payload['condition_gaps'][0]
            args=gap['context_arguments'] or dict(mode='targeted',query='无理由期限完整规则',gap_ref='g-request')
            value['assessment_updates']=[]
            value['action']=dict(kind='tool',tool=gap['available_tool'],arguments=args,targets=['g-request'],
                expected_information='读取必要窗口规则',evidence_basis=[])
            if gap['context_arguments']:
                value['evidence_need_updates']=[dict(source='goal:g-request',purpose='dependency',
                    evidence_refs=gap['context_arguments']['chunk_refs'])]
        return value
    monkeypatch.setattr(mock,'respond',respond)
    transition=LoopStore.transition;crashed=False;expected=None
    async def crash(self,old,new,events):
        nonlocal crashed,expected
        reached = (any(k=='observation_applied' and p.get('coverage_improved') for k,p in events)
                   if boundary=='observation' else bool(new.get('evidence_bindings')) and not old.get('evidence_bindings'))
        if not crashed and reached:
            crashed=True;expected=new['coverage_progress'] if boundary=='observation' else new['evidence_bindings']
            if after_commit:await transition(self,old,new,events)
            raise AgentError('fixture_coverage_durable_boundary')
        return await transition(self,old,new,events)
    monkeypatch.setattr(LoopStore,'transition',crash)
    s,db,store=await env();sid=await store.create_session('CUST-001')
    facts=dict(category_id='apparel_footwear',return_reason='no_reason',purchased_at='2026-10-01T10:00:00+08:00',
        signed_at='2026-10-03T10:00:00+08:00',after_sales_applied_at='2026-10-09T10:00:00+08:00')
    tid,_=await submit(store,s,sid,'无理由退货的时间规则是什么？',facts)
    task,trace=await run(store,s,tid)
    assert crashed and task['status']=='failed',trace
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace
    state=await LoopStore(DispatchRuntime(s,task)).load()
    applied=[e['payload'] for e in trace['events'] if e['kind']=='observation_applied']
    ids=[o['operation_key'] for o in applied]
    assert len(ids)==len(set(ids))==len(state['observations'])
    if boundary=='observation':
        assert state['coverage_progress']['epoch']==expected['epoch']
        assert all(state['coverage_progress']['levels'][k]>=v for k,v in expected['levels'].items())
    else:
        assert state['evidence_bindings']==expected
        assert len({b['binding_id'] for b in expected})==len(expected)
    assert len([e for e in trace['events'] if e['kind']=='tool_start'])==task['budget']['tools']
    assert task['budget']['models']==len(trace['model_calls'])


async def test_obligation_release_hash_rechecked(monkeypatch):
    from copy import deepcopy
    from psycopg.types.json import Jsonb
    from backend.agent_loop.store import LoopStore
    from test_agent_loop_integration import start, run
    original=LoopStore.publish
    async def mutate(self,state):
        value=deepcopy(state)
        value['evidence_bindings']=[dict(source='finding:forged')]
        async with self.host.db.connection() as c:
            await c.execute('UPDATE agent_loop_states SET state=%s WHERE task_id=%s',(Jsonb(value),self.host.tid))
        return await original(self,state)
    monkeypatch.setattr(LoopStore,'publish',mutate)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='failed' and not trace['result']
    assert not any(e['kind']=='answer_release' for e in trace['events'])
