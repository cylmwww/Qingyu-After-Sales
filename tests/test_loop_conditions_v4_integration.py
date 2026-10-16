"""独立PG的v4恢复和发布绑定；不调用真实模型。"""
import os
import pytest
from backend.agent_loop.contracts import LoopConfig
from backend.review_contract import ReviewConfig
from backend.agent_loop.store import LoopStore
from backend.agent_dispatch import DispatchRuntime
from backend.agent_store import AgentError
from test_stage6_integration import environment, submit
from test_agent_loop_integration import start, run

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要显式隔离PG')]

async def env(**extra):
    return await environment(runtime_version='agent-loop-v1',loop_config=LoopConfig.read('configs/loops/loop-v4-conditions.v1.4.0.json'),
        review_config=ReviewConfig(id='conditions-test-post',postcheck=True),**extra)

async def test_v4_publish():
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace['error']
    state=await LoopStore(DispatchRuntime(s,task)).load()
    assert state['eligibility']['gate_version']=='loop-release-v4'
    assert state['candidate']['condition_coverage']['overall_eligibility']=='not_computed'

@pytest.mark.parametrize('role,event',[('agent_decision','agent_decision'),('agent_draft','candidate_created'),('agent_review','review_result')])
async def test_v4_cr01(monkeypatch,role,event):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_protocol_feedback_result_resume_combination(monkeypatch,role,event)

@pytest.mark.parametrize('rewrite,retries',[(False,0),(True,0),(True,1)])
async def test_v4_cr02(monkeypatch,rewrite,retries):
    import test_loop_repair_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_optional_hybrid_rerank_and_rewrite_share_ledger(rewrite,retries)

async def test_gate_redecision(monkeypatch):
    from backend.agent_loop import mock
    original=mock.respond
    def respond(role,payload):
        if role=='agent_decision' and not payload['observations'] and not payload.get('action_rejection'):
            return dict(based_on_state_version=payload['state_version'],goal_proposals=[],assessment_updates=[],
                action=dict(kind='draft',coverage='full',addressed_goals=['g-request'],support_refs=[payload['evidence'][0]['evidence_id']],limitations=[])) if payload['evidence'] else original(role,payload)
        value=original(role,payload)
        if role=='agent_decision' and payload['observations'] and not payload.get('action_rejection') and not payload['assessments']:
            value['assessment_updates']=[]
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert trace['result']['reviewed'],trace['error']
    assert any(e['kind']=='draft_gate_rejected' for e in trace['events'])

@pytest.mark.parametrize('point',['candidate_created','review_result'])
async def test_v4_durable(monkeypatch,point):
    import test_loop_recovery_v3_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_recovery_crash_reuses_durable_outputs(monkeypatch,point)

async def test_v4_cancel(monkeypatch):
    import test_agent_loop_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_cancel_after_review_before_publish(monkeypatch)

@pytest.mark.parametrize('status,resumable',[('transient_failure',True),('permanent_failure',False)])
async def test_tool_failure_resume(monkeypatch,status,resumable):
    from backend.agent import AgentRuntime
    original=AgentRuntime.invoke_tool
    count=0
    async def invoke(self,tool,args,state):
        nonlocal count
        count+=1
        if count==1:
            from datetime import datetime,timezone
            return dict(status=status,error=dict(code='unknown_technical'),data=None,evidence_refs=[],
                call_id='failure',observed_at=datetime.now(timezone.utc).isoformat())
        return await original(self,tool,args,state)
    monkeypatch.setattr(AgentRuntime,'invoke_tool',invoke)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert task['status']=='failed' and trace['error']['resumable'] is resumable,trace
    if resumable:
        await submit(store,s,sid,'恢复',task_id=tid,action='resume')
        task,trace=await run(store,s,tid)
        assert (trace.get('result') or {}).get('reviewed') and count==2, dict(error=trace['error'], events=[e for e in trace['events'] if e['kind']=='runtime_failure'])
    else:
        with pytest.raises(AgentError,match='not_resumable'):
            await submit(store,s,sid,'恢复',task_id=tid,action='resume')
        assert count==1

@pytest.mark.parametrize('fault',['fact_evidence','first_targeted'])
async def test_v4_atomic_rejection(monkeypatch,fault):
    import test_loop_bound_integration as prior
    monkeypatch.setattr(prior,'env',env)
    await prior.test_action_feedback_recovery_and_atomic_updates(monkeypatch,fault)

@pytest.mark.parametrize('case',['budget','lease','publication'])
async def test_v4_protection(monkeypatch,case):
    import test_agent_loop_integration as prior
    monkeypatch.setattr(prior,'env',env)
    if case=='budget':await prior.test_publish_rechecks_budget_after_review(monkeypatch)
    elif case=='lease':await prior.test_expired_lease_cannot_publish(monkeypatch)
    else:await prior.test_stale_publication_binding_rejected(monkeypatch,'candidate')


async def test_conditional_packaging_inputs_reach_all_roles(monkeypatch):
    from backend.agent_loop import mock
    from backend.agent import AgentRuntime
    from backend.config import Settings
    from backend.knowledge import load_knowledge
    from copy import deepcopy
    _,documents,chunks,_=load_knowledge(Settings(_env_file=None,rag_mode='mock'))
    document=next(d for d in documents if d.stable_id=='POL-GENERAL' and d.version=='1.0.1')
    parent=next(c for c in chunks if c.document_key==document.key and c.role=='parent'
        and c.metadata['ref']['id']=='RULE-GEN-001')
    row=dict(chunk_id=parent.id,document_id=document.stable_id,document_version=document.version,
        parent_id=parent.parent_id,text=parent.text,metadata=parent.metadata,source_path=document.source_path,
        source_start=parent.source_start,source_end=parent.source_end)
    facts=dict(category_id='apparel_footwear',return_reason='no_reason',
        purchased_at='2026-10-01T10:00:00+08:00',signed_at='2026-10-03T10:00:00+08:00',
        after_sales_applied_at='2026-10-09T10:00:00+08:00',packaging_intact=False,
        tags_intact=True,product_intact=True,used_outdoors=False)
    original=mock.respond;seen={}
    def respond(role,payload):
        if role.startswith('agent_'):seen[role]=deepcopy(payload.get('condition_view'))
        return original(role,payload)
    monkeypatch.setattr(mock,'respond',respond)
    async def tool(self,name,args,state):
        return dict(status='success',data=dict(evidence=[row]),evidence_refs=[],call_id='policy',observed_at='2026-10-13T00:00:00+08:00')
    monkeypatch.setattr(AgentRuntime,'invoke_tool',tool)
    s,db,store=await env();sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'按这些条件咨询包装缺失的退货政策',facts=facts)
    task,trace=await run(store,s,tid)
    assert (trace.get('result') or {}).get('reviewed'),trace['error']
    for role in ('agent_decision','agent_draft','agent_review'):
        assert seen[role]['derived_conditions'][0]['elapsed_seconds']==518400
        assert seen[role]['provided_facts']['fact:packaging_intact']['value'] is False
    assert '整体资格未计算' in trace['result']['answer']


@pytest.mark.parametrize('repeat',[False,True])
async def test_missing_tool_gate_bounded(monkeypatch,repeat):
    from backend.agent_loop import mock
    original=mock.respond
    def respond(role,payload):
        value=original(role,payload)
        if role=='agent_decision' and any(o['tool']=='get_order' for o in payload['observations']) and not any(o['tool']=='get_logistics' for o in payload['observations']):
            if repeat or not payload.get('action_rejection'):
                value['action']=dict(kind='draft',coverage='full',addressed_goals=['g-request'],support_refs=[payload['evidence'][0]['evidence_id']],limitations=[])
                value['assessment_updates']=[]
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env();sid,tid=await start(s,store,'ORD-006物流在哪里')
    task,trace=await run(store,s,tid)
    gates=[e for e in trace['events'] if e['kind']=='draft_gate_rejected']
    assert gates and 'get_logistics' in gates[0]['payload']['missing_requirements']
    if repeat:
        assert trace['result']['reason']=='no_progress' and not trace['result']['reviewed']
        assert not any(e['kind']=='answer_release' for e in trace['events'])
    else:assert trace['result']['reviewed']

async def test_coverage_issue_cannot_be_overridden_by_empty_review(monkeypatch):
    from backend.agent_loop import mock
    original=mock.respond
    def respond(role,payload):
        value=original(role,payload)
        if role=='agent_draft':value['condition_coverage']['unknown_fact_refs']=['fact:order_id']
        if role=='agent_review':value['findings']=[]
        return value
    monkeypatch.setattr(mock,'respond',respond)
    s,db,store=await env();sid,tid=await start(s,store)
    task,trace=await run(store,s,tid)
    assert not (trace.get('result') or {}).get('reviewed')
    assert not any(e['kind']=='answer_release' for e in trace['events'])
