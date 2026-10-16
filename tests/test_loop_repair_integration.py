"""隔离 PostgreSQL、LangGraph、输入敏感模拟；没有供应商模型调用。"""
import os
from copy import deepcopy

import pytest

from backend.agent_loop.store import LoopStore
from backend.agent_store import AgentError
from test_agent_loop_integration import env, start, run
from test_stage6_integration import submit

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要显式隔离 PostgreSQL')]


@pytest.mark.parametrize('role,event',[('agent_decision','agent_decision'),('agent_draft','candidate_created'),('agent_review','review_result')])
async def test_protocol_feedback_result_resume_combination(monkeypatch,role,event):
    from backend.agent_loop import runtime as module
    invoke_original=module.structured
    transition_original=LoopStore.transition
    crashed=False
    async def invoke(adapter,current,payload,schema,maximum,record,mock,system_prompt=None):
        if current==role:
            if crashed:
                # 决策还有后续合法行动，只禁止重调相同旧输入。
                if current!='agent_decision' or payload['state_version']==first_version[0]:
                    raise AssertionError('durable result must survive provider unavailability')
            if not first_version:
                first_version.append(payload.get('state_version'))
            if 'protocol_feedback' not in payload:
                mock=dict(mock,unexpected='invalid')
        return await invoke_original(adapter,current,payload,schema,maximum,record,mock,system_prompt)
    first_version=[]
    async def transition(self,old,new,events):
        nonlocal crashed
        if not crashed and any(k==event for k,p in events):
            crashed=True
            raise AgentError('fixture_after_protocol_success')
        return await transition_original(self,old,new,events)
    monkeypatch.setattr(module,'structured',invoke)
    monkeypatch.setattr(LoopStore,'transition',transition)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='failed'
    before=task['budget']['models']
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace
    expected=7 if role=='agent_decision' else 6
    assert task['budget']['models']==expected
    if role=='agent_review': assert task['budget']['models']==before
    assert len([e for e in trace['events'] if e['kind']=='candidate_created'])==1


async def test_known_embedding_blocks_before_tool_without_ledger_charge(monkeypatch):
    from backend.agent_loop import runtime as module
    from backend.agent_contract import AgentConfig
    from backend.agent_store import AgentStore
    from backend.db import Database
    monkeypatch.setattr(module,'structured_input_tokens',lambda *a:1)
    s,db,store=await env()
    s=s.model_copy(update={'agent_config':AgentConfig(id='four-models',strategy='dynamic',experiment=True,request_models=5)})
    store=AgentStore(Database(s));sid,tid=await start(s,store,'包装政策是什么')
    task,trace=await run(store,s,tid)
    assert trace['result']['reason']=='unresolved_budget'
    assert task['budget']['models']==2 and task['budget']['tools']==0
    assert not any(c['role']=='embedding' for c in trace['model_calls'])
    check=next(e['payload'] for e in trace['events'] if e['kind']=='budget_admission')
    assert check['required']['models']==3+(s.model_retry_count+1) and check['reservation'] is False


def install_scenario(monkeypatch,scenario):
    """受控接缝依赖输入正文/证据/未解决发现，不按调用轮数驱动。"""
    from backend.agent_loop import mock
    from backend.agent_dispatch import DispatchRuntime
    original=mock.respond
    marker='受控夹具：只有总则摘要，没有包装例外正文。'
    def respond(role,payload):
        value=original(role,payload)
        pending=(payload.get('repair') or {}).get('pending_findings',[])
        lacks=bool(payload['evidence']) and all(e['text']==marker for e in payload['evidence'])
        if role=='agent_draft' and scenario=='rewrite' and not payload.get('repair'):
            value['blocks'][0]['text']='冻结订单已经取消完成'
        if role=='agent_review':
            if scenario=='dispute' and not payload.get('pending_findings'):
                value['findings']=[dict(kind='unsupported_claim',block_id='b1',goal_id=None,evidence_refs=[],detail='受控误报：未找到订单状态依据')]
            if scenario=='lookup' and lacks:
                value['findings']=[dict(kind='missing_requirement',block_id=None,goal_id='g-request',evidence_refs=[],detail='需核实包装例外完整条款')]
        if role=='agent_decision' and lacks and (pending or scenario=='prelookup'):
            value['assessment_updates']=[dict(goal_id='g-request',status='open',statement='摘要不足以确认包装例外',support_refs=[],conflict_refs=[],unknowns=['包装例外'])]
            value['action']=dict(kind='tool',tool='search_knowledge',targets=['g-request'],expected_information='取得包装例外完整正文',
                evidence_basis=[e['evidence_id'] for e in payload['evidence']],arguments={'query':'包装例外完整条款','gap_ref':pending[0]['finding_id'] if pending else 'g-request'})
        return value
    monkeypatch.setattr(mock,'respond',respond)
    class Fixture(DispatchRuntime):
        async def invoke_tool(self,tool,args,state):
            result=await super().invoke_tool(tool,args,state)
            if scenario in {'lookup','prelookup'} and tool=='search_knowledge' and args['query']==self.task['question']:
                result=deepcopy(result)
                assert result['data']['evidence']
                row=dict(result['data']['evidence'][0],text=marker)
                result['data']['evidence']=[row]
            return result
    return Fixture


@pytest.mark.parametrize('scenario',['rewrite','dispute','lookup','prelookup'])
async def test_input_sensitive_closed_loops(monkeypatch,scenario):
    import json
    from backend.main import public_event
    fixture=install_scenario(monkeypatch,scenario)
    s,db,store=await env();sid,tid=await start(s,store,'包装例外条件是什么？' if 'lookup' in scenario else 'ORD-006订单状态')
    task,trace=await run(store,s,tid,fixture)
    assert task['status']=='completed',trace['error']
    assert trace['result']['reviewed'],trace['result']
    state=await LoopStore(fixture(s,task)).load()
    assert state['findings']==[]
    reviews=state['review_history'];candidates=state['candidate_history']
    assert len(reviews)==(1 if scenario=='prelookup' else 2)
    if scenario!='prelookup':
        assert reviews[0]['findings'] and reviews[1]['dispositions']
        assert reviews[0]['registered_findings'][0]['finding_id']==reviews[1]['dispositions'][0]['finding_id']
        assert reviews[1]['dispositions'][0]['status']==('not_applicable' if scenario=='dispute' else 'resolved')
    if scenario=='dispute':
        assert len(candidates)==1
        assert sum(c['role']=='agent_draft' for c in trace['model_calls'])==1
        assert reviews[0]['candidate_id']==reviews[1]['candidate_id'] and reviews[0]['review_id']!=reviews[1]['review_id']
    elif scenario=='rewrite':
        assert len(candidates)==2 and candidates[0]['content_hash']!=candidates[1]['content_hash']
        assert '取消完成' not in trace['result']['answer']
    elif scenario=='lookup':
        assert candidates[0]['bundle_id']!=candidates[1]['bundle_id']
        assert len(state['observations'])==2 and not state['observations'][-1]['reused']
        assert state['observations'][-1]['query_origin'].startswith('finding:')
    else:
        assert len(state['observations'])==2 and state['observations'][-1]['query_origin'].startswith('goal:')
    assert trace['result']['candidate_id']==state['candidate']['candidate_id']
    assert task['budget']['models']==len(trace['model_calls'])<=12
    assert task['budget']['reserved_tokens']<=60000
    payloads=[json.loads(c['messages'][1]['content']) for c in trace['model_calls'] if c['role']=='agent_review']
    if scenario!='prelookup': assert payloads[-1]['pending_findings'] and payloads[-1]['finding_responses']
    public=json.dumps([public_event(e['kind'],e['payload']) for e in trace['events'] if e['kind']!='done'],ensure_ascii=False)
    assert '取消完成' not in public and '受控误报' not in public and 'finding_responses' not in public


@pytest.mark.parametrize('scenario,point',[('rewrite','candidate_created'),('lookup','observation_applied'),
    ('dispute','candidate_reused'),('rewrite','review_result')])
async def test_repair_durable_resume_without_repeating(monkeypatch,scenario,point):
    fixture=install_scenario(monkeypatch,scenario)
    original=LoopStore.transition;crashed=False
    async def transition(self,old,new,events):
        nonlocal crashed
        if not crashed and old['repair_episodes'] and any(k==point for k,p in events):
            crashed=True
            raise AgentError('fixture_repair_durable_before_apply')
        return await original(self,old,new,events)
    monkeypatch.setattr(LoopStore,'transition',transition)
    s,db,store=await env();sid,tid=await start(s,store,'包装例外条件是什么？' if scenario=='lookup' else 'ORD-006订单状态')
    task,trace=await run(store,s,tid,fixture)
    assert task['status']=='failed',trace
    before=deepcopy(task['budget'])
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    assert (await store.task(tid))['budget']==before
    task,trace=await run(store,s,tid,fixture)
    assert trace['result']['reviewed'],trace
    expected={'rewrite':8,'dispute':7,'lookup':11}[scenario]
    assert task['budget']['models']==expected and task['budget']['tools']==(2 if scenario=='lookup' else 1)
    assert trace['result']['correction_rounds']==1


@pytest.mark.parametrize('fault',['omitted','remains','limit','budget'])
async def test_repair_failure_never_publishes(monkeypatch,fault):
    from backend.agent_loop import mock
    fixture=install_scenario(monkeypatch,'dispute' if fault=='remains' else 'rewrite')
    original=mock.respond
    def respond(role,payload):
        value=original(role,payload)
        if role=='agent_review' and payload.get('pending_findings'):
            if fault=='omitted':value['dispositions']=[]
            if fault in {'remains','limit'}:
                for d in value['dispositions']:d['status']='remains'
        if role=='agent_draft' and payload.get('repair') and fault=='limit':
            value['blocks'][0]['text']='未支持的不同说法 '+str(payload['repair']['episode'])
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env()
    if fault=='budget':
        from backend.agent_contract import AgentConfig
        from backend.agent_store import AgentStore
        from backend.db import Database
        s=s.model_copy(update={'agent_config':AgentConfig(id='repair-low',strategy='dynamic',experiment=True,request_models=6)})
        store=AgentStore(Database(s))
    sid,tid=await start(s,store);task,trace=await run(store,s,tid,fixture)
    assert not (trace['result'] or {}).get('reviewed') and not any(e['kind']=='answer_release' for e in trace['events'])
    if fault=='omitted': assert task['status']=='failed' and trace['error']['code']=='loop_disposition_coverage'
    else:
        assert trace['result']['reason']=={'remains':'no_progress','limit':'repair_limit','budget':'unresolved_budget'}[fault]
    state=await LoopStore(fixture(s,task)).load()
    assert state['findings'] and state['repair_episodes']<=2


async def test_api_repair_and_evaluation_use_shared_runtime(monkeypatch):
    import httpx
    from uuid import uuid4
    from backend.main import create_app
    from backend.agent_contract import AgentRequest
    from scripts.evaluate import evaluation_runtime
    install_scenario(monkeypatch,'rewrite')
    s,db,store=await env(api_access_token='repair-test-token')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(s)),base_url='http://localhost',
        headers={'Authorization':'Bearer repair-test-token'}) as client:
        sid=(await client.post('/api/agent/sessions')).json()['session_id']
        body=AgentRequest(session_id=sid,request_id=uuid4(),question='ORD-006订单状态')
        response=await client.post('/api/agent/chat',json=body.model_dump(mode='json',exclude_unset=True))
        assert response.status_code==200 and 'event: done' in response.text
        assert '取消完成' not in response.text and 'finding_responses' not in response.text
        tid=response.headers['X-Task-ID'];task=await store.task(tid);trace=await db.trace(tid)
        assert trace['result']['reviewed'] and trace['result']['correction_rounds']==1
        before=deepcopy(task['budget'])
        seq=next(e['sequence'] for e in trace['events'] if e['kind']=='candidate_created')
        replay=await client.get(f'/api/traces/{tid}/events?after={seq}')
        assert 'event: done' in replay.text and '取消完成' not in replay.text
        assert (await store.task(tid))['budget']==before
    sid,tid=await start(s,store)
    task=await store.task(tid)
    task,trace=await run(store,s,tid,evaluation_runtime([],task['frozen']))
    assert trace['result']['reviewed'] and trace['result']['correction_rounds']==1
    assert task['budget']['models']==8


@pytest.mark.parametrize('rewrite,retries',[(False,0),(True,0),(True,1)])
async def test_optional_hybrid_rerank_and_rewrite_share_ledger(rewrite,retries):
    import json
    from pathlib import Path
    from backend.retrieval_config import RetrievalConfig,RerankConfig
    from backend.answer_config import AnswerConfig,RewriteConfig
    retrieval=RetrievalConfig(id='repair-hybrid',version='1.0.0',hypothesis='offline frozen enhancements',
        mode='hybrid',fusion='rrf',rerank=RerankConfig(enabled=True,candidates=6))
    answer=AnswerConfig(id='repair-rewrite-enabled' if rewrite else 'repair-rewrite',version='1.0.0',hypothesis='offline rewrite',rewrite=RewriteConfig(enabled=rewrite))
    dictionary=json.loads(Path(retrieval.bm25.dictionary_path).read_text('utf-8-sig'))
    s,db,store=await env(retrieval_config=retrieval,answer_config=answer,bm25_dictionary=dictionary,model_retry_count=retries)
    index=await db.current_index();await db.build_bm25(index['id'])
    sid,tid=await start(s,store,'鞋盒包装；退货条件')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    if retries:
        assert trace['result']['reason']=='unresolved_budget' and task['budget']['tools']==0
        return
    assert trace['result']['reviewed'],trace['result']
    roles=[c['role'] for c in trace['model_calls']]
    assert 'embedding' in roles and 'rerank' in roles and ('query_rewrite' in roles)==rewrite
    assert task['budget']['models']==len(roles)<=12
    checks=[e['payload'] for e in trace['events'] if e['kind']=='budget_admission' and e['payload'].get('auxiliary',{}).get('models')]
    assert checks and all(not c['reservation'] for c in checks)


async def test_repair_limit_stop_survives_notification_crash(monkeypatch):
    from backend.agent_loop import mock
    from backend.agent_loop.runtime import LoopRuntime
    fixture=install_scenario(monkeypatch,'rewrite')
    respond_original=mock.respond;stop_original=LoopRuntime.stop;crashed=False
    def respond(role,payload):
        value=respond_original(role,payload)
        if role=='agent_review' and payload.get('pending_findings'):
            for d in value['dispositions']:d['status']='remains'
        if role=='agent_draft' and payload.get('repair'):
            value['blocks'][0]['text']='待核实的另一说法 '+str(payload['repair']['episode'])
        return value
    async def stop(self,state,reason):
        nonlocal crashed
        if reason=='repair_limit' and not crashed:
            crashed=True
            raise AgentError('fixture_before_terminal_notice')
        return await stop_original(self,state,reason)
    monkeypatch.setattr(mock,'respond',respond);monkeypatch.setattr(LoopRuntime,'stop',stop)
    s,db,store=await env();sid,tid=await start(s,store);task,trace=await run(store,s,tid,fixture)
    assert task['status']=='failed' and crashed
    before=task['budget']['models']
    state=await LoopStore(fixture(s,task)).load()
    assert state['stop_reason']=='repair_limit' and state['repair_episodes']==2
    await submit(store,s,sid,'恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid,fixture)
    assert task['budget']['models']==before and trace['result']['reason']=='repair_limit'
    assert not trace['result']['reviewed']
