"""明确三类隔离验收接缝；脚本审核仅工程夹具，不冒充真实模型。"""
import os
from copy import deepcopy
import pytest
from backend.agent_dispatch import DispatchRuntime
from backend.agent_loop.store import LoopStore
from backend.agent_loop.runtime import LoopRuntime
from backend.agent_loop.mechanisms import mechanism_evidence
from backend.agent_store import AgentError
from test_loop_conditions_v4_integration import env
from test_agent_loop_integration import start, run
from test_stage6_integration import submit

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要显式隔离PG')]

@pytest.mark.parametrize('kind',['adaptive_evidence','review_rewrite','review_lookup'])
async def test_separate_mechanism_seams(monkeypatch,kind):
    from backend.agent_loop import mock
    original=mock.respond
    marker='受控首轮总则摘要，不含包装例外。'
    reached=False
    def respond(role,payload):
        nonlocal reached
        value=original(role,payload)
        pending=(payload.get('repair') or {}).get('pending_findings', [])
        lacks=bool(payload['evidence']) and all(e['text']==marker for e in payload['evidence'])
        if role=='agent_draft' and kind=='review_rewrite' and not payload.get('repair'):
            value['blocks'][0]['text']='冻结订单已取消完成';reached=True
        if role=='agent_review' and kind=='review_lookup' and lacks:
            value['findings']=[dict(kind='missing_requirement',block_id=None,goal_id='g-request',evidence_refs=[],detail='需包装例外全文')]
        if role=='agent_decision' and lacks and (kind=='adaptive_evidence' or pending):
            value['assessment_updates']=[dict(goal_id='g-request',status='open',statement='包装例外尚未取得',support_refs=[],conflict_refs=[],unknowns=['包装例外'])]
            value['action']=dict(kind='tool',tool='search_knowledge',targets=['g-request'],expected_information='取得包装例外正文',
                evidence_basis=[e['evidence_id'] for e in payload['evidence']],arguments=dict(mode='targeted',query='包装例外完整条款',gap_ref=pending[0]['finding_id'] if pending else 'g-request'))
        return value
    monkeypatch.setattr(mock,'respond',respond)
    class Fixture(DispatchRuntime):
        async def invoke_tool(self,tool,args,state):
            nonlocal reached
            result=await super().invoke_tool(tool,args,state)
            if kind!='review_rewrite' and tool=='search_knowledge' and args['query']==self.task['question']:
                result=deepcopy(result);result['data']['evidence']=[dict(result['data']['evidence'][0],text=marker)];reached=True
            return result
    stop_original=LoopRuntime.advance;paused=False
    async def pause(self,cursor):
        nonlocal paused
        state=await self.store.load()
        if kind=='review_lookup' and not paused and state['findings'] and state['phase']=='decide':
            paused=True;raise AgentError('fixture_failed_review_checkpoint')
        return await stop_original(self,cursor)
    monkeypatch.setattr(LoopRuntime,'advance',pause)
    s,db,store=await env();sid,tid=await start(s,store,'ORD-006订单状态' if kind=='review_rewrite' else '包装例外条件是什么？')
    task,trace=await run(store,s,tid,Fixture)
    if kind=='review_lookup':
        assert task['status']=='failed' and paused
        checkpoint=await LoopStore(Fixture(s,task)).load()
        assert checkpoint['findings'] and len(checkpoint['review_history'])==1
        await submit(store,s,sid,'恢复',task_id=tid,action='resume')
        task,trace=await run(store,s,tid,Fixture)
    assert trace['result']['reviewed'],trace
    state=await LoopStore(Fixture(s,task)).load()
    async with db.connection() as c:
        operations=await(await c.execute('SELECT key,kind,status,arguments,result FROM agent_operations WHERE task_id=%s',(tid,))).fetchall()
    evidence=mechanism_evidence(state,operations,trace['result'],dict(mechanism=kind,injection_reached=reached,
        origin='offline_controlled',scope='failed_review_resume_segment' if kind=='review_lookup' else 'end_to_end'))
    assert evidence['acceptance']=='mechanism_pass',evidence
    if kind=='adaptive_evidence':assert not evidence['failed_review_seen'] and not evidence['review_lookup']
    if kind=='review_lookup':assert evidence['chains'][-1]['new_evidence_used']
