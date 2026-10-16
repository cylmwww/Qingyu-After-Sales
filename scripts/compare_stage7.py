"""只使用开发及验证草稿，冻结同数据/工具/检索/模型，比较E3/E4/E5及前后置消融。"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.agent import AgentRuntime, setup_agent
from backend.agent_contract import AgentRequest, AgentConfig, Facts
from backend.review_contract import ReviewConfig
from backend.agent_store import AgentStore
from backend.business_tools import BusinessService, response
from backend.config import Settings
from backend.db import Database
from backend.knowledge import digest
from scripts.compare_stage5 import save
from scripts.compare_stage4 import index_hash


def input_facts(turn):
    raw=dict(turn.get('facts',{}))
    if 'return_selections' in raw:
        raw['selections']=raw.pop('return_selections')
    if raw.get('hypothetical_gift_not_returned'):
        raw['gift_returned_line_ids']=[]
    # 确认质量只能来自服务器；保留原问题中的声明，但不提升工具权限。
    if raw.get('quality_status')=='confirmed':
        raw.pop('quality_status')
    return {k:v for k,v in raw.items() if k in Facts.model_fields and k not in {'question','index_id'} and v is not None}


def summary(cases):
    timings=[sum(t.get('elapsed_ms',0) for t in {t['trace_id']:t for t in c.get('turns',[])}.values()) for c in cases]
    return {'planned_cases':len(cases), 'completed':sum(c['status']=='completed' for c in cases),
        'waiting':sum(c['status']=='waiting' for c in cases),'failed':sum(c['status']=='failed' for c in cases),
        'tool_calls':sum(c.get('tool_calls',0) for c in cases),'model_calls':sum(c.get('model_calls',0) for c in cases),
        'reported_tokens':sum(c.get('reported_tokens',0) for c in cases),
        'p50_active_ms':statistics.median(timings) if timings else None,
        'p95_active_ms':sorted(timings)[min(len(timings)-1,int(len(timings)*.95))] if timings else None,
        'outcomes':{o:sum((c.get('turns') or [{}])[-1].get('result',{}).get('outcome')==o for c in cases if (c.get('turns') or [{}])[-1].get('result')) for o in ['passed','repaired','partial_answer','handoff','unreviewed']},
        'semantic_task_success':None, 'unsupported_assertion_rate':None, 'false_refusal_rate':None, 'note':'技术完成/明确等待与语义任务成功分开，模拟摘录不评分业务回答'}


async def compare(args):
    output=args.output.resolve()
    if not output.is_relative_to(ROOT/'local'):
        raise ValueError('结果只保存到local')
    if output.exists() and not args.resume:
        raise ValueError('保留历史文件，续跑必须显式--resume')
    if not os.getenv('STAGE7_DATABASE_URL'):
        raise ValueError('显式指定STAGE7_DATABASE_URL，使用隔离实验库')
    datasets={name:json.loads((ROOT/path).read_text('utf-8')) for name,path in {
        'development':'data/evaluation/development.json',
        'validation_retrieval':'data/stage3-evaluation/validation.v1.0.0.json',
        'validation_agent':'data/stage6-evaluation/validation.v1.0.0.json'}.items()}
    ids={c['id'] for d in datasets.values() for c in d['cases']}
    if args.cases and not set(args.cases)<=ids:
        raise ValueError('只允许开发/验证案例ID')
    base=Settings(_env_file=None if args.mode=='mock' else ROOT/'.env',
        database_url=os.environ['STAGE7_DATABASE_URL'],rag_mode=args.mode,
        index_config_path='configs/indexes/stage2-baseline.v1.0.0.json',
        retrieval_config_path=args.retrieval_config,answer_config_path=args.answer_config,agent_config_path=None, review_config_path=None, review_config=ReviewConfig())
    db=Database(base);await db.migrate();await setup_agent(db)
    index=await db.get_index(args.index_id);db.check_index(index)
    before=await index_hash(db,index['id'])
    manifest={'dataset_hashes':{k:digest(v) for k,v in datasets.items()},'mode':args.mode,
        'index_id':args.index_id,'index_hash':before,'business_hash':BusinessService().fingerprint,
        'configuration':base.snapshot(),'case_ids':args.cases,'repeats':args.repeats,
        'prompt_version':'agent-readonly-v1', 'review_presets':{k:json.loads((ROOT/v).read_text('utf-8')) for k,v in PRESETS.items()},
        'fixture_hashes':{p.name:digest(p.read_text('utf-8')) for p in (ROOT/'data/fixtures').glob('*.json')},
        'source_hashes':{p:digest((ROOT/p).read_text('utf-8-sig')) for p in ['backend/agent.py','backend/agent_model.py',
            'backend/agent_store.py','backend/business_tools.py','backend/agent_contract.py','backend/models.py','backend/query.py','backend/retrieval.py','backend/evidence_review.py','backend/review_contract.py','scripts/compare_stage7.py']},
        'tools':['search_knowledge','read_policy_context','get_order','get_after_sales','get_logistics','calculate_refund']}
    fp=digest(manifest)
    if args.resume:
        report=json.loads(output.read_text('utf-8'))
        if report['fingerprint']!=fp:
            raise ValueError('续跑数据/配置/索引/模型条件不兼容')
    else:
        report={'status':'running','fingerprint':fp,'manifest':manifest,'started_at':datetime.now(timezone.utc).isoformat(),
            'profiles':[],'limitations':['验证均为pending_human草稿；没有使用独立测试集。',
             '模拟角色为规则/计划脚本和证据摘录，不能证明动态Agent真实效果。',
             '不注入标准结论；离线语义正确性及误拒答需单独复核，在线审核通过不参与正确性评分。']}
    output.parent.mkdir(parents=True,exist_ok=True);save(output,report)
    for strategy in ['E3','E4','E5','E5-no-pre','E5-no-post']:
        profile=next((p for p in report['profiles'] if p['strategy']==strategy),None)
        if not profile:
            profile={'strategy':strategy,'cases':[]};report['profiles'].append(profile)
        for split,dataset in datasets.items():
            for case in dataset['cases']:
                if args.cases and case['id'] not in args.cases:continue
                for repeat in range(args.repeats):
                    entry=next((e for e in profile['cases'] if e['id']==case['id'] and e['repeat']==repeat),None)
                    if entry and entry['status'] in {'completed','waiting','failed'}:
                        continue # 可靠落盘的整个案例结果保持，失败留在分母。
                    s=base.model_copy(update={'agent_config':AgentConfig(id='agent-dynamic',strategy='dynamic',experiment=True),
                        'review_config':ReviewConfig.read(ROOT / PRESETS[strategy])})
                    fixtures=[]
                    for fixture_ref in case.get('fixture_refs',[]):
                        for file in (ROOT/'data/fixtures').glob('*.json'):
                            fixture=json.loads(file.read_text('utf-8'))
                            if fixture['id']==fixture_ref['id']:
                                fixtures.append(fixture)
                    class EvaluationRuntime(AgentRuntime):
                        async def invoke_tool(self,tool,arguments,state):
                            for fixture in fixtures:
                                operation=fixture['operation']
                                if fixture['fixture_kind']=='tool_timeout' and tool==operation['tool'] and arguments.get('order_id')==operation['order_id']:
                                    await asyncio.sleep(self.config.business_timeout+.05)
                                if fixture['fixture_kind']=='knowledge_conflict' and tool=='search_knowledge':
                                    # 受控服务拒绝夹具，不宣称自动识别自然语言证据冲突。
                                    return response('permanent_failure',code='fixture_policy_service_refusal')
                            result=await super().invoke_tool(tool,arguments,state)
                            for fixture in fixtures:
                                if fixture['fixture_kind']=='prompt_injection' and tool=='search_knowledge' and result['status']=='success':
                                    first=result['data']['evidence'][0]
                                    first['text']+='\n【测试夹具不可信内容】'+fixture['operation']['untrusted_text']
                                    first['metadata']=dict(first['metadata'],injected_fixture=fixture['id'])
                            return result
                    store=AgentStore(Database(s))
                    if not entry:
                        entry={'id':case['id'],'split':split,'repeat':repeat,'status':'running',
                            'session_id':await store.create_session('CUST-001'),'turns':[]}
                        profile['cases'].append(entry);save(output,report)
                    turns=[t for t in case['turns'] if t['role']=='user']
                    entry['tool_calls']=entry['model_calls']=entry['reported_tokens']=0
                    current=None
                    for turn_no,turn in enumerate(turns):
                        snap=turn.get('business_snapshot_ref',case.get('business_snapshot_ref',{})).get('id','SNAP-DEMO-20261009')
                        s=s.model_copy(update={'business_snapshot_id':snap});store=AgentStore(Database(s))
                        existing=next((t for t in entry['turns'] if t['number']==turn_no),None)
                        if existing:
                            task=await store.task(existing['trace_id'])
                            if task['status']=='running':
                                await store.recover_expired();task=await store.task(existing['trace_id'])
                            if task['status']=='running':raise ValueError('旧执行租约仍有效，请等待后续跑')
                            if task['status']=='interrupted':
                                body=AgentRequest(session_id=entry['session_id'],request_id=uuid4(),question='显式恢复实验中断任务',task_id=task['id'],action='resume')
                                tid,launch=await store.submit(body,'CUST-001',BusinessService(snapshot_id=snap))
                                if launch:await EvaluationRuntime(s,await store.task(tid)).run()
                                task=await store.task(tid)
                        else:
                            action='clarify' if current and current['status']=='waiting' else 'new'
                            if current and current['status'] in {'failed','interrupted'}:
                                break
                            body=AgentRequest(session_id=entry['session_id'],request_id=uuid4(),question=turn['text'],
                                facts=input_facts(turn),index_id=args.index_id,action=action,
                                task_id=current['id'] if action=='clarify' else None)
                            tid,launch=await store.submit(body,'CUST-001',BusinessService(snapshot_id=snap))
                            existing={'number':turn_no,'trace_id':tid,'request_id':str(body.request_id)}
                            entry['turns'].append(existing);save(output,report)
                            if launch:await EvaluationRuntime(s,await store.task(tid)).run()
                            task=await store.task(tid)
                        trace=await store.db.trace(str(task['id']))
                        existing.update(status=task['status'],elapsed_ms=task['budget']['active_ms'],budget=task['budget'],
                            error=trace['error'],result=trace['result'],events=trace['events'],model_calls=trace['model_calls'])
                        current=task
                        save(output,report)
                    unique={t['trace_id']:t for t in entry['turns']}
                    entry['tool_calls']=sum(t.get('budget',{}).get('tools',0) for t in unique.values())
                    entry['model_calls']=sum(t.get('budget',{}).get('models',0) for t in unique.values())
                    entry['reported_tokens']=sum(t.get('budget',{}).get('reported_tokens',0) for t in unique.values())
                    entry['status']=current['status'] if current else 'failed'
                    if entry['status'] not in {'completed','waiting'}:entry['status']='failed'
                    profile['summary']={split:summary([c for c in profile['cases'] if c['split']==split]) for split in datasets}
                    save(output,report)
                    print(strategy,case['id'],repeat,entry['status'],flush=True)
    after=await index_hash(db,index['id'])
    if after!=before:raise ValueError('原索引/发布指针发生变化')
    report.update(status='completed',finished_at=datetime.now(timezone.utc).isoformat(),baseline_unchanged=True)
    save(output,report)


PRESETS={'E3':'configs/reviews/off.v1.0.0.json','E4':'configs/reviews/checks-only.v1.0.0.json',
         'E5':'configs/reviews/limited-correction.v1.0.0.json',
         'E5-no-pre':'configs/reviews/post-only.v1.0.0.json','E5-no-post':'configs/reviews/pre-only.v1.0.0.json'}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['mock','real'],default='mock')
    parser.add_argument('--index-id',required=True)
    parser.add_argument('--retrieval-config',default=None)
    parser.add_argument('--answer-config',default=None)
    parser.add_argument('--cases',nargs='*',default=[])
    parser.add_argument('--repeats',type=int,choices=[1,2,3],default=1)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--resume',action='store_true')
    asyncio.run(compare(parser.parse_args()))
