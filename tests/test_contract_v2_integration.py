"""隔离PG/真实LangGraph；受控模型与工具夹具均明确标识且按输入内容判断。"""
import os
from uuid import uuid4

import pytest

from backend.agent import AgentRuntime
from backend.agent_store import AgentStore
from backend.business_tools import response
from backend.models import ModelAdapter
from backend.review_contract import ReviewConfig
from test_stage6_integration import environment, submit, run

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='需要隔离PostgreSQL')]


async def configured():
    return await environment(review_config=ReviewConfig(id='v2-'+str(uuid4()),
        precheck=True,postcheck=True,correction='targeted_lookup'))


async def test_mixed_order_required_rules_reach_pre_and_post_review(monkeypatch):
    import backend.agent as module
    original=module.structured
    payloads=[]
    async def review(adapter,role,payload,*args):
        if role in {'pre_evidence_check','post_answer_review'}:
            refs={e['metadata'].get('ref',{}).get('id') for e in payload['evidence']}
            assert {'RULE-APP-001','RULE-GEN-001','RULE-PRO-003','RULE-REF-001'}<=refs
            payloads.append(payload)
        return await original(adapter,role,payload,*args)
    monkeypatch.setattr(module,'structured',review)
    s,db,store=await configured();sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'ORD-006只退外套返还赠品，为什么不是退199元？')
    task,trace=await run(store,s,tid)
    assert task['status']=='completed',trace['error']
    assert trace['result']['reviewed'] and len(payloads)==2
    assert payloads[0]['bundle_id']==payloads[1]['bundle_id']
    coverage=[e['payload'] for e in trace['events'] if e['kind']=='policy_coverage'][-1]
    assert not coverage['missing'] and coverage['required']
    assert all(e.get('source_start') is not None for e in trace['result']['evidence'] if e['kind']=='knowledge')


async def test_dynamic_wrong_candidate_uses_repair_then_rechecks():
    class Wrong(ModelAdapter):
        async def generate(self,messages,evidence,record):
            await self.before_call('generation',1200)
            calc=next(e for e in evidence if e.get('tool')=='calculate_refund')
            yield f"预估退款为999元 [{calc['citation_id']}]。"
            await record({'call_id':str(uuid4()),'role':'generation','status':'success',
                          'mode':'mock','usage':None,'actual_model':'controlled-wrong-answer-v2'})
    class Runtime(AgentRuntime):
        def __init__(self,s,task):super().__init__(s,task,Wrong(s))
    s,db,store=await configured();sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'ORD-006退款金额为什么不是199元？')
    task,trace=await run(store,s,tid,Runtime)
    assert task['status']=='completed',trace['error']
    result=trace['result']
    assert result['path']=='dynamic' and result['reviewed'] and result['outcome']=='repaired'
    assert '999' not in result['answer'] and '158.19' in result['answer']
    assert len([e for e in trace['events'] if e['kind']=='answer_repair'])==1
    assert sum(c['role']=='answer_repair' for c in trace['model_calls'])==1


async def test_policy_no_order_survives_model_needs_business_flag():
    class Runtime(AgentRuntime):
        async def decision(self,role,*args):
            if role=='intent':return {'intent':'policy','needs_business':True}
            return await super().decision(role,*args)
    s,db,store=await configured();sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'服饰鞋靴吊牌鞋盒完好，未在室外使用，签收六天可否无理由退货？',
                       {'category_id':'apparel_footwear','return_reason':'no_reason'})
    task,trace=await run(store,s,tid,Runtime)
    assert task['status']=='completed',trace['error']
    assert not any(e['kind']=='tool_start' and e['payload']['tool']=='get_order' for e in trace['events'])


async def test_lookup_success_is_driven_by_new_source_in_review_input(monkeypatch):
    import backend.agent as module
    original=module.structured
    seen=[]
    async def review(adapter,role,payload,schema,maximum,record,mock,prompt=None):
        if role in {'pre_evidence_check','post_answer_review'}:
            refs={e['metadata'].get('ref',{}).get('id') for e in payload['evidence']}
            seen.append((role,refs))
            complete='RULE-APP-003' in refs
            mock={'passed':complete,'complete':True,'issues':[] if complete else [
                {'code':'missing_packaging_exception','dimension':'policy','conclusion':'包装缺失例外',
                 'detail':'缺少包装缺失的适用条款','evidence_ids':[], 'action':'lookup',
                 'tool':'search_knowledge','query':'鞋盒缺失包装例外'}]}
        return await original(adapter,role,payload,schema,maximum,record,mock,prompt)
    monkeypatch.setattr(module,'structured',review)
    class Runtime(AgentRuntime):
        async def invoke_tool(self,tool,args,state):
            if tool!='search_knowledge':return await super().invoke_tool(tool,args,state)
            target='RULE-APP-003' if args['query']=='鞋盒缺失包装例外' else 'RULE-APP-001'
            children=await self.db.chunk_rows(self.index['id'],await self.filters(state))
            rows=[r for r in children if r['metadata']['ref']['id']==target]
            parents=await self.db.parent_rows(self.index['id'],[r['chunk_id'] for r in rows],await self.filters(state))
            assert parents
            return response('success',{'evidence':parents})
    s,db,store=await configured();sid=await store.create_session('CUST-001')
    tid,_=await submit(store,s,sid,'吊牌在但鞋盒丢了，服饰鞋靴还能退吗？',{'category_id':'apparel_footwear'})
    task,trace=await run(store,s,tid,Runtime)
    assert task['status']=='completed',trace['error']
    assert trace['result']['reviewed'] and trace['result']['outcome']=='repaired'
    assert 'RULE-APP-003' not in seen[0][1] and all('RULE-APP-003' in refs for _,refs in seen[1:])
    assert not any(c['role']=='executor' for c in trace['model_calls'])
    admissions = [e['payload'] for e in trace['events'] if e['kind'] == 'budget_admission']
    assert admissions[0]['phase'] == 'lookup_fixed_overhead'
    assert admissions[1]['phase'] == 'materialized_closure' and admissions[1]['admitted']
    assert admissions[1]['required']['tools'] == 0
    assert admissions[1]['bundle_id'] == trace['result']['bundle']['bundle_id']


async def test_materialized_lookup_denial_withholds_candidate_in_real_ledger(monkeypatch):
    import backend.agent as module
    original = module.structured
    async def review(adapter, role, payload, schema, maximum, record, mock, prompt=None):
        if role == 'pre_evidence_check':
            mock = {'passed': False, 'complete': True, 'issues': [
                {'code': 'controlled_missing_rule', 'dimension': 'coverage', 'conclusion': '包装例外',
                 'detail': '受控缺证据', 'evidence_ids': [], 'action': 'lookup',
                 'tool': 'search_knowledge', 'query': '鞋盒缺失包装例外'}]}
        return await original(adapter, role, payload, schema, maximum, record, mock, prompt)
    monkeypatch.setattr(module, 'structured', review)
    class ExhaustAfterLookup(AgentRuntime):
        async def budget_admission(self, state, materialized=False):
            if materialized:
                b = (await self.store.task(self.tid))['budget']
                remaining = self.config.token_budget - max(b['request_tokens'], b['reserved_tokens'])
                await self.store.reserve(self.tid, self.attempt, self.config, 'models', remaining - 500)
                await self.event('controlled_fixture', {'kind': 'offline_unknown_usage_reservation'})
            return await super().budget_admission(state, materialized)
    s, db, store = await configured()
    sid = await store.create_session('CUST-001')
    tid, _ = await submit(store, s, sid, '鞋盒缺失能否退货？', {'category_id': 'apparel_footwear'})
    task, trace = await run(store, s, tid, ExhaustAfterLookup)
    assert task['status'] == 'completed', trace['error']
    assert trace['result']['reason'] == 'unresolved_budget' and not trace['result']['reviewed']
    assert task['budget']['reserved_tokens'] == s.agent_config.token_budget - 500
    assert not any(c['role'] == 'generation' for c in trace['model_calls'])
    admissions = [e['payload'] for e in trace['events'] if e['kind'] == 'budget_admission']
    assert admissions[0]['admitted'] and not admissions[1]['admitted']
