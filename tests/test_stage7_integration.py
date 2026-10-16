"""实际PostgreSQL/LangGraph验证，模型为明确标识的模拟调用。"""
import asyncio
import json
import os
from uuid import uuid4
import pytest
from backend.agent import AgentRuntime
from backend.agent_contract import AgentConfig
from backend.agent_store import AgentError, AgentStore
from backend.config import Settings
from backend.db import Database
from backend.evidence_review import issue
from backend.models import ModelAdapter, ModelError
from backend.review_contract import ReviewConfig
from backend.review_contract import model_report
from test_stage6_integration import environment, submit, run

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要隔离PostgreSQL')]


async def env(**values):
    review=ReviewConfig(id='test-review-'+str(uuid4()),**values)
    return await environment(review_config=review)


def reviewer_fixture(monkeypatch, reports=None, fault=None):
    """审核桩按输入内容决定通过的语义由具体用例提供；这里只做载荷契约检查。

    候选与其审核必须绑定同一bundle：bundle_id与citation_mapping是契约的一部分，
    用于把引用解析到本轮证据，不能退回只检查旧字段集合。
    """
    import backend.agent as module
    original=module.structured
    reports=[dict(complete=True, **report) for report in (reports or [])]
    async def invoke(adapter,role,payload,schema,maximum,record,mock,system_prompt=None):
        if role in {'pre_evidence_check','post_answer_review'}:
            assert set(payload)=={'question','necessary_facts','candidate_answer','evidence',
                                  'citation_mapping','bundle_id','task_scope'}
            assert payload['bundle_id'] and payload['citation_mapping']
            if fault:
                await adapter.before_call(role,100)
                await fault()
            if reports:
                mock=model_report(reports.pop(0))
        return await original(adapter,role,payload,schema,maximum,record,mock,system_prompt)
    monkeypatch.setattr(module,'structured',invoke)


class WrongNumber(ModelAdapter):
    async def generate(self,messages,evidence,record):
        await self.before_call('generation',1200)
        e=next(e for e in evidence if 'total_estimated_cents' in e['text'])
        value=f"预估退款为999.00元 [{e['citation_id']}]。"
        yield value
        await record({'call_id':str(uuid4()),'role':'generation','mode':'mock','usage':None,
            'usage_source':'unknown','status':'success','elapsed_ms':0,'actual_model':'mock-wrong-number-fixture'})


class BadAnswerRuntime(AgentRuntime):
    def __init__(self,s,task):
        super().__init__(s,task,WrongNumber(s))


@pytest.mark.parametrize('pre,post',[(False,False),(True,False),(False,True),(True,True)])
async def test_independent_switches_and_no_hidden_audit(pre,post):
    s,db,store=await env(precheck=pre,postcheck=post)
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-006退款金额为什么只能退这么多？')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['reviewed']==post
    roles=[c['role'] for c in trace['model_calls']]
    assert roles.count('pre_evidence_check')==int(pre)
    assert roles.count('post_answer_review')==int(post)
    if not pre and not post:
        assert not any(e['kind']=='review_result' for e in trace['events'])
    for c in trace['model_calls']:
        if c['role'] in {'pre_evidence_check','post_answer_review'}:
            payload=json.loads(c['messages'][1]['content'])
            assert set(payload)=={'question','necessary_facts','candidate_answer','evidence',
                                  'citation_mapping','bundle_id','task_scope'}
            assert all(e.get('evidence_id') for e in payload['evidence'])


@pytest.mark.parametrize('strategy,outcome,rounds',[('none','partial_answer',0),('answer_only','repaired',1)])
async def test_numeric_error_withheld_or_repaired(strategy,outcome,rounds):
    s,db,store=await env(postcheck=True,correction=strategy)
    # 固定路径减少角色消耗，在同样12次请求额度内验证修复；动态额度不足另测失败关闭。
    s=s.model_copy(update={'agent_config':AgentConfig(id='test-review-fixed',strategy='fixed',experiment=True)})
    store=AgentStore(Database(s))
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-006退款金额？')
    task,trace=await run(store,s,tid,BadAnswerRuntime)
    assert task['status']=='completed',trace['error']
    r=trace['result']
    assert r['outcome']==outcome and r['correction_rounds']==rounds
    assert '999' not in r['answer'] and '158.19' in r['answer']
    assert r['reviewed']==(strategy=='answer_only')
    if rounds:
        repairs=[e['payload'] for e in trace['events'] if e['kind']=='answer_repair']
        assert '999' in repairs[0]['before'] and '158.19' in repairs[0]['after']


async def test_targeted_lookup_compiles_gap_and_keeps_shared_budget(monkeypatch):
    gap=issue('missing_exception','coverage','商品退货条件',[],'需要完整包装例外','lookup',
        tool='search_knowledge',query='鞋盒缺失包装例外')
    reviewer_fixture(monkeypatch,[{'passed':False,'issues':[gap]},{'passed':True,'issues':[]},{'passed':True,'issues':[]}])
    s,db,store=await env(precheck=True,postcheck=True,correction='targeted_lookup')
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'鞋盒扔了还能退吗？')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['outcome']=='repaired'
    lookups=[e['payload'] for e in trace['events'] if e['kind']=='tool_start' and e['payload']['purpose']=='correction_lookup']
    assert lookups and lookups[0]['arguments']['query']=='鞋盒缺失包装例外'
    # 参数来源指向审核给出的逐缺口查询，而不是程序猜测的订单参数。
    assert lookups[0]['argument_sources']['query']=='review_issue_query'
    assert lookups[0]['issue_ids']==['missing_exception']
    # 逐缺口补查由程序编译：初始规划只用一次Planner，缺口轮不再进入全局重规划。
    assert sum(c['role']=='planner' for c in trace['model_calls'])==1
    plans=[e['payload'] for e in trace['events'] if e['kind']=='repair_plan']
    assert plans and plans[0]['global_replan'] is False and plans[0]['issue_count']==1
    assert task['budget']['request_models']==task['budget']['models']
    # 新证据确实进入复审：补查证据事件与新一轮证据组装都记录了变化。
    corrections=[e['payload'] for e in trace['events'] if e['kind']=='correction_evidence']
    assert corrections and corrections[0]['new_evidence']
    bundles=[e['payload'] for e in trace['events'] if e['kind']=='evidence_bundle']
    assert len(bundles)>=2 and bundles[-1]['bundle_id']!=bundles[0]['bundle_id']
    assert bundles[-1]['citation_mapping']
    # 审核与候选绑定同一bundle，引用可解析到稳定evidence_id。
    reviews=[e['payload'] for e in trace['events'] if e['kind']=='review_result']
    assert reviews and reviews[-1]['bundle_id']==bundles[-1]['bundle_id']
    assert trace['result']['evidence'] and all(e['evidence_id'] for e in trace['result']['evidence'])
    assert trace['result']['bundle']['incomplete'] is False
    admissions=[e['payload'] for e in trace['events'] if e['kind']=='budget_admission']
    assert admissions and admissions[0]['admitted'] is True and admissions[0]['minimal_path']


@pytest.mark.parametrize('context_items,expected_rounds', [(2,2), (20,None)])
async def test_max_two_rounds_and_handoff(monkeypatch,context_items,expected_rounds):
    p=issue('unsupported','fact','候选结论',[],'证据无法支持')
    reviewer_fixture(monkeypatch,[{'passed':False,'issues':[p]}]*3)
    s,db,store=await env(postcheck=True,correction='answer_only')
    # 此用例隔离轮次上限；默认大上下文的预算不足另测，不能要求超额完成两轮。
    s=s.model_copy(update={'agent_config':s.agent_config.model_copy(update={
        'id':'round-limit-'+str(uuid4()),'context_items':context_items})})
    store=AgentStore(Database(s))
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'鞋盒完整需要什么条件？')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['outcome']=='handoff'
    rounds=trace['result']['correction_rounds']
    if expected_rounds is not None:
        assert rounds==expected_rounds
    else:
        assert 1<=rounds<=2
        assert trace['result']['reason'] in {'review_unresolved','unresolved_budget'}
    assert task['budget']['request_tokens']<=s.agent_config.token_budget
    assert trace['result']['reviewed'] is False


async def test_review_clarification_and_resume_keep_rounds(monkeypatch):
    p=issue('missing_category','coverage','商品适用政策',[],'品类未知','clarify')
    p['missing_fields']=['category_id']
    reviewer_fixture(monkeypatch,[{'passed':False,'issues':[p]},{'passed':True,'issues':[]}])
    s,db,store=await env(precheck=True)
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'鞋盒扔了还能退吗？')
    task,trace=await run(store,s,tid)
    assert task['status']=='waiting' and trace['result']['missing']==['category_id']
    before=task['budget']['models']
    await submit(store,s,sid,'服饰鞋靴',{'category_id':'apparel_footwear'},task_id=tid,action='clarify')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed' and task['budget']['models']>before


@pytest.mark.parametrize('fault_kind',['timeout','exception','budget'])
async def test_audit_failure_never_releases_candidate(monkeypatch,fault_kind):
    async def fault():
        if fault_kind=='timeout': await asyncio.sleep(.1)
        else: raise ModelError('fixture_review_exception')
    s,db,store=await env(postcheck=True,timeout_seconds=.02 if fault_kind=='timeout' else 30)
    if fault_kind=='budget':
        s=s.model_copy(update={'agent_config':AgentConfig(id='review-budget-'+str(uuid4()),strategy='dynamic',experiment=True,request_models=4)})
        store=AgentStore(Database(s))
    else: reviewer_fixture(monkeypatch,fault=fault)
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'鞋盒缺了还能退吗？')
    task,trace=await run(store,s,tid)
    assert task['status']=='failed',trace['result']
    assert trace['result'] is None and not any(e['kind']=='done' for e in trace['events'])
    assert any(e['kind']=='review_failure' for e in trace['events'])
    budget=task['budget']
    await submit(store,s,sid,'显式恢复',task_id=tid,action='resume')
    assert (await store.task(tid))['budget']==budget
    await store.cancel(tid,'CUST-001')


async def test_candidate_durable_before_review_crash(monkeypatch):
    s,db,store=await env(postcheck=True)
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'鞋盒缺了还能退吗？')
    class Crash(AgentRuntime):
        async def postcheck(self,state): raise RuntimeError('审核前崩溃')
    task,trace=await run(store,s,tid,Crash)
    assert task['status']=='failed' and trace['result'] is None
    old_generation=sum(c['role']=='generation' for c in trace['model_calls'])
    await submit(store,s,sid,'显式恢复',task_id=tid,action='resume')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed' and trace['result']['reviewed']
    assert sum(c['role']=='generation' for c in trace['model_calls'])==old_generation


async def test_program_vetoes_model_pass(monkeypatch):
    reviewer_fixture(monkeypatch,[{'passed':True,'issues':[]}])
    s,db,store=await env(postcheck=True)
    s=s.model_copy(update={'agent_config':AgentConfig(id='test-review-fixed',strategy='fixed',experiment=True)})
    store=AgentStore(Database(s))
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-006退款金额？')
    task,trace=await run(store,s,tid,BadAnswerRuntime)
    assert task['status']=='completed' and trace['result']['reviewed'] is False
    assert '999' not in trace['result']['answer']
    issues=next(e['payload']['report']['issues'] for e in trace['events'] if e['kind']=='review_result')
    assert any(i['code']=='amount_not_in_cited_record' for i in issues)


async def test_correction_checkpoint_preserves_round_and_operation(monkeypatch):
    s,db,store=await env(postcheck=True,correction='answer_only')
    s=s.model_copy(update={'agent_config':AgentConfig(id='test-review-fixed',strategy='fixed',experiment=True)})
    store=AgentStore(Database(s))
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'ORD-006退款金额？')
    class CrashBeforeRepair(BadAnswerRuntime):
        async def answer(self,state):
            if state.get('corrections'): raise RuntimeError('修复前故障')
            return await super().answer(state)
    task,trace=await run(store,s,tid,CrashBeforeRepair)
    assert task['status']=='failed' and trace['result'] is None
    before=task['budget'].copy()
    await submit(store,s,sid,'显式恢复',task_id=tid,action='resume')
    assert (await store.task(tid))['budget']==before
    task,trace=await run(store,s,tid,BadAnswerRuntime)
    assert task['status']=='completed',trace['error']
    assert trace['result']['correction_rounds']==1 and trace['result']['outcome']=='repaired'
    assert sum(e['kind']=='correction_start' for e in trace['events'])==1


@pytest.mark.parametrize('post',[False,True])
async def test_unsupported_request_exits_without_correction_plan(post):
    s,db,store=await env(precheck=True,postcheck=post,correction='targeted_lookup')
    sid=await store.create_session('CUST-001');tid,_=await submit(store,s,sid,'帮我取消 ORD-012 并执行退款。')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['outcome']=='handoff' and trace['result']['reason']=='read_only_scope'
    assert trace['result']['correction_rounds']==0 and not trace['result']['reviewed']
    assert not any(c['role']=='planner' for c in trace['model_calls'])
    assert task['budget']['tools']==0
