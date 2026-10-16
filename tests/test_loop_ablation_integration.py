"""隔离PG真实调度；模型为受控夹具，验证消融确实改变行为。"""
import os
import pytest
from backend.agent_loop.contracts import LoopConfig
from backend.review_contract import ReviewConfig
from backend.agent_loop.store import LoopStore
from backend.agent_dispatch import create_runtime
from test_stage6_integration import environment
from test_agent_loop_integration import start, run

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要隔离PG')]


async def env(group):
    return await environment(runtime_version='agent-loop-v1',
        loop_config=LoopConfig.read(f'configs/loops/loop-{group.lower()}-ablation.v1.7.0.json'),
        review_config=ReviewConfig(id='ablation-test',postcheck=True))


@pytest.mark.parametrize('group', ['E2','E3','E4','E5'])
async def test_profiles_publish_with_honest_review_flag(group):
    s,db,store=await env(group); _,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['outcome']=='full_answer',trace['result']
    assert trace['result']['reviewed'] == (group in {'E4','E5'})
    roles=[c['role'] for c in trace['model_calls']]
    assert roles.count('agent_review') == (1 if group in {'E4','E5'} else 0)
    assert roles.count('agent_decision') == (1 if group=='E2' else 2)
    assert trace['result']['review_id'] is not None if group in {'E4','E5'} else trace['result']['review_id'] is None


@pytest.mark.parametrize('group', ['E4','E5'])
async def test_failed_review_stops_e4_but_e5_repairs(monkeypatch,group):
    from backend.agent_loop import mock
    original=mock.respond
    def respond(role,payload):
        value=original(role,payload)
        if role=='agent_draft' and not payload.get('repair'):
            value['blocks'][0]['text']='冻结订单已取消完成'
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env(group); _,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    state=await LoopStore(create_runtime(s,task)).load()
    if group=='E4':
        assert trace['result']['outcome']=='handoff'
        assert trace['result']['reason']=='ablation_review_withheld'
        assert len(state['candidate_history'])==len(state['review_history'])==1
        assert state['repair_episodes']==0
    else:
        assert trace['result']['reviewed'],trace
        assert len(state['candidate_history'])==len(state['review_history'])==2
        assert state['repair_episodes']==1


@pytest.mark.parametrize('group', ['E2','E3'])
async def test_unreviewed_still_blocks_program_violation(monkeypatch,group):
    from backend.agent_loop import mock
    original=mock.respond
    def respond(role,payload):
        value=original(role,payload)
        if role=='agent_draft': value['blocks'][0]['text']='预计退款 99999 元'
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env(group); _,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert not any(e['kind']=='answer_release' for e in trace['events'])
    assert '99999' not in (trace.get('result') or {}).get('answer','')


@pytest.mark.parametrize('group', ['E2','E3'])
async def test_cancel_before_unreviewed_publish(monkeypatch,group):
    original=LoopStore.publish
    async def cancel_then_publish(self,state):
        h=self.host
        await h.store.cancel(h.tid,h.task['customer_id'])
        return await original(self,state)
    monkeypatch.setattr(LoopStore,'publish',cancel_then_publish)
    s,db,store=await env(group); _,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='cancelled'
    assert not any(e['kind']=='answer_release' for e in trace['events'])
