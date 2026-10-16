"""固定阶段四检索配置比较查询与上下文；固定查询评分与生成分别报告。"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.config import Settings
from backend.db import Database
from backend.knowledge import digest, load_knowledge
from backend.models import ModelAdapter
from backend.query import retrieve_queries
from backend.rag import system_prompt, citation_result
from backend.answer_context import prepare_context, make_messages, input_size
from backend.stage3_eval import first_turn, knowledge_groups, score, source_ranges, validate_pilot
from scripts.compare_stage4 import index_hash


def save(path, data):
    temporary=path.with_suffix(path.suffix+'.tmp')
    # 不减少追踪内容；紧凑序列化避免每例反复写入大量缩进空白。
    temporary.write_text(json.dumps(data,ensure_ascii=False,default=str,separators=(',',':')),encoding='utf-8')
    for attempt in range(6):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt==5: raise
            time.sleep(.1)


def summarize(cases, key):
    scored=[c for c in cases if c['groups']]
    fixed=key=='fixed_query_context_score'
    return {'queries':len(cases),'scored_queries':len(scored),
        'technical_failures':sum((not c.get('fixed_query_completed',c['status']=='completed')) if fixed else c['status']!='completed' for c in cases),
        'context_recall_macro':statistics.mean(c[key]['recall'] if (c.get('fixed_query_completed',c['status']=='completed') if fixed else c['status']=='completed') else 0 for c in scored) if scored else None,
        'full_text_context_proxy':statistics.mean(c[key]['condition_text_completeness_proxy'] if (c.get('fixed_query_completed',c['status']=='completed') if fixed else c['status']=='completed') else 0 for c in scored) if scored else None,
        'mean_context_tokens':statistics.mean((c.get('fixed_context_tokens',next((e['payload']['token_estimate'] for e in c.get('events',[]) if e['kind']=='context_assembly'),0))
            if key=='fixed_query_context_score' else c.get('context_tokens',0)) for c in cases) if cases else None}


async def compare(args):
    output=args.output.resolve()
    if not output.is_relative_to(ROOT/'local') or output.exists():
        raise ValueError('输出必须为local内新文件，不覆盖历史')
    if not os.getenv('STAGE5_DATABASE_URL'):
        raise ValueError('必须提供STAGE5_DATABASE_URL，复用已选索引')
    common=dict(database_url=os.environ['STAGE5_DATABASE_URL'],rag_mode=args.mode,
        index_config_path='configs/indexes/stage2-baseline.v1.0.0.json',
        retrieval_config_path=args.retrieval_config,answer_config_path=None)
    base=Settings(**common); db=Database(base); adapter=ModelAdapter(base)
    _,_,chunks,knowledge_fp=await asyncio.to_thread(load_knowledge,base)
    ranges=source_ranges(chunks)
    dev=json.loads((ROOT/'data/evaluation/development.json').read_text(encoding='utf-8'))
    val=json.loads((ROOT/'data/stage3-evaluation/validation.v1.0.0.json').read_text(encoding='utf-8'))
    datasets={'development':dev,'validation':val}
    generation_ids=set(args.generate_cases)
    all_ids={c['id'] for ds in datasets.values() for c in ds['cases']}
    if not generation_ids<=all_ids: raise ValueError('生成案例必须来自开发或验证草稿')
    report={'status':'running','started_at':datetime.now(timezone.utc).isoformat(),'mode':args.mode,
        'index_id':args.index_id,'knowledge_fingerprint':knowledge_fp,
        'dataset_hashes':{k:digest(v) for k,v in datasets.items()},'dataset_checks':validate_pilot(val,dev,ranges),
        'fixed_configuration':base.snapshot(),'fixed_queries':[],'profiles':[],
        'generation_case_ids':sorted(generation_ids),
        'comparisons':{'q1-rewrite':'q0-baseline','q2-parent-related':'q0-baseline',
                       'q3-grouped':'q2-parent-related','q4-prompt':'q0-baseline'},
        'notes':['固定原查询检索只运行一次，所有上下文对照复用相同排名，不把新增关联正文当检索召回收益。',
                 '改写另运行端到端检索；跨查询RRF后以原问题重排一次。',
                 '仅首轮知识问题，验证为11例pending_human草稿，非设计60例完整验证集。',
                 '证据ID覆盖和完整正文代理不是答案成功率；无模型自评或独立审核调用。',
                 'mock仅验证机制；真实生成另列逐例输出，语义评分需人工复核。',
                 '一次观测，无统计显著性；失败保留，不读取独立测试集。']}
    previous=None
    resume=getattr(args,'resume_from',None)
    if resume:
        resume=Path(resume).resolve()
        if not resume.is_relative_to(ROOT/'local') or resume==output:
            raise ValueError('恢复来源必须为local内不同历史文件')
        previous=json.loads(resume.read_text(encoding='utf-8'))
        for key in ['mode','index_id','knowledge_fingerprint','dataset_hashes','fixed_configuration']:
            if previous[key]!=report[key]: raise ValueError('恢复实验指纹或配置不一致')
        previous_ids=previous.get('generation_case_ids')
        if previous_ids is None:
            completed_baseline=next((p for p in previous['profiles'] if p['configuration']['id']=='q0-baseline' and p['status']=='completed'),None)
            if not completed_baseline: raise ValueError('历史生成样本集合未知，拒绝恢复')
            previous_ids=[g['case_id'] for g in completed_baseline['generation']]
        if set(previous_ids)!=generation_ids: raise ValueError('恢复时生成样本集合变化')
        report['resume_source']=str(resume)
        report['reused_trace_ids']=[]
    save(output,report)
    fixed={}
    try:
        await db.migrate()
        index=await db.get_index(args.index_id); db.check_index(index)
        if index['source_fingerprint']!=knowledge_fp: raise ValueError('固定知识指纹漂移')
        report['index_before']=await index_hash(db,args.index_id)
        pointer=await db.current_index(); report['pointer_before']=str(pointer['id']) if pointer else None
        if previous and (previous['index_before']!=report['index_before'] or previous['pointer_before']!=report['pointer_before']):
            raise ValueError('恢复时索引/指针变化')
        prior_fixed={c['case_id']:c for c in previous['fixed_queries']} if previous else {}
        # BM25派生产物必须已存在；脚本不重建知识/向量、不切发布指针。
        for split,ds in datasets.items():
            for case in ds['cases']:
                prior=prior_fixed.get(case['id'])
                if prior and prior['status']=='completed':
                    fixed[case['id']]=prior;report['fixed_queries'].append(prior)
                    report['reused_trace_ids'].append(prior['trace_id'])
                    continue
                turn=first_turn(case); question=turn['text']
                filters={k:v for k,v in turn['facts'].items() if k in {'category_id','purchased_at','after_sales_applied_at'}}
                tid,frozen=await db.new_trace(question,filters,args.index_id)
                events=[]; calls=[]; start=time.perf_counter()
                async def event(kind,payload):
                    events.append({'kind':kind,'payload':payload}); await db.event(tid,kind,payload)
                async def record(payload):
                    calls.append(payload); await db.model_call(payload,trace_id=tid)
                try:
                    async with asyncio.timeout(base.request_timeout_seconds):
                        rows=await retrieve_queries(base,db,adapter,frozen,question,filters,event,record)
                    status='completed'; error=None
                except Exception as e:
                    rows=[];status='failed';error={'code':getattr(e,'code','fixed_retrieval_failed'),'type':type(e).__name__}
                item={'split':split,'case_id':case['id'],'question':question,'filters':filters,
                      'trace_id':tid,'status':status,'error':error,'rows':rows,'events':events,
                      'model_calls':calls,'retrieval_ms':(time.perf_counter()-start)*1000}
                fixed[case['id']]=item; report['fixed_queries'].append(item)
                await db.finish_trace(tid,status,result={'retrieval_only':True},error=error,elapsed_ms=item['retrieval_ms'])
                save(output,report)
        for path in sorted((ROOT/'configs/answers').glob('*.json')):
            s=Settings(**dict(common,answer_config_path=str(path))); cb=Database(s); ca=ModelAdapter(s)
            p={'configuration':s.answer_config.model_dump(mode='json'),'snapshot':s.snapshot(),
               'system_prompt_hash':digest(system_prompt(s)),'cases':[],'generation':[],'status':'running'}
            report['profiles'].append(p);save(output,report)
            old_profile=next((v for v in previous['profiles'] if v['configuration']==p['configuration'] and v['snapshot']==p['snapshot']
                and v['system_prompt_hash']==p['system_prompt_hash']) ,None) if previous else None
            prior_cases={c['case_id']:c for c in old_profile['cases']} if old_profile else {}
            try:
                for split,ds in datasets.items():
                    for case in ds['cases']:
                        prior=prior_cases.get(case['id'])
                        prior_generation=next((g for g in old_profile['generation'] if g['case_id']==case['id']),None) if old_profile else None
                        if prior and prior['status']=='completed' and (case['id'] not in generation_ids or prior_generation and prior_generation['status']=='completed'):
                            p['cases'].append(prior)
                            if prior_generation and case['id'] in generation_ids: p['generation'].append(prior_generation)
                            report['reused_trace_ids'].append(prior['trace_id'])
                            continue
                        original=fixed[case['id']];question=original['question'];filters=original['filters']
                        tid,frozen=await cb.new_trace(question,filters,args.index_id)
                        events=[]; calls=[]; start=time.perf_counter(); groups=knowledge_groups(case)
                        item={'case_id':case['id'],'split':split,'groups':len(groups),'trace_id':tid,
                              'question':question,'status':'running','events':events,'model_calls':calls}
                        p['cases'].append(item)
                        async def event(kind,payload):
                            events.append({'kind':kind,'payload':payload}); await cb.event(tid,kind,payload)
                        async def record(payload):
                            calls.append(payload); await cb.model_call(payload,trace_id=tid)
                        try:
                            async with asyncio.timeout(s.request_timeout_seconds):
                                if original['status']!='completed': raise ValueError('固定查询失败，不静默将空排名当成功')
                                await event('retrieval_reuse',{'source_trace_id':original['trace_id'],'fixed_query':question,
                                    'rows':original['rows'],'fresh_model_call':False})
                                # 固定原查询评测：Q1关闭改写，这个分数不混入改写查询的效果。
                                fixed_evidence,_,_,fixed_tokens=await prepare_context(original['rows'],s,cb,frozen,filters,question,system_prompt(s),event)
                                item['fixed_query_context_score']=score(groups,fixed_evidence,ranges)
                                item['fixed_context_tokens']=fixed_tokens
                                item['fixed_query_completed']=True
                                rows=(await retrieve_queries(s,cb,ca,frozen,question,filters,event,record)
                                      if s.answer_config.rewrite.enabled else original['rows'])
                                evidence,context,discarded,used=await prepare_context(rows,s,cb,frozen,filters,question,system_prompt(s),event)
                                item.update(context_score=score(groups,evidence,ranges),context_tokens=used,
                                    evidence=evidence,discarded=discarded,actual_context=context)
                                messages=make_messages(system_prompt(s),question,context)
                                await event('context',{'messages':messages,'actual_context':context,
                                    'input_token_estimate':input_size(messages),'prompt_version':s.prompt_version,
                                    'generation_invoked':case['id'] in generation_ids,'max_output_tokens':s.max_output_tokens})
                                if case['id'] in generation_ids:
                                    answer='';gen_start=time.perf_counter()
                                    try:
                                        if not await cb.index_available(args.index_id): raise ValueError('索引不可用')
                                        if evidence:
                                            async for text in ca.generate(messages,evidence,record): answer+=text
                                        else: answer='资料不足，无法依据本店规则回答。'
                                        check=citation_result(answer,evidence)
                                        if check['invalid_ids']: raise ValueError('未知引用')
                                        if not await cb.index_available(args.index_id): raise ValueError('索引不可用')
                                        p['generation'].append({'case_id':case['id'],'split':split,'trace_id':tid,
                                            'status':'completed','answer':answer,'citation_check':check,
                                            'generation_ms':(time.perf_counter()-gen_start)*1000,
                                            'semantic_success':None,'review':'pending_human'})
                                    except Exception as e:
                                        p['generation'].append({'case_id':case['id'],'split':split,'trace_id':tid,
                                            'status':'failed','partial_answer':answer,
                                            'error':{'code':getattr(e,'code','generation_failed'),'type':type(e).__name__},
                                            'semantic_success':None})
                            item['status']='completed'
                        except Exception as e:
                            item.update(status='failed',error={'code':getattr(e,'code','experiment_case_failed'),'type':type(e).__name__},
                                        context_score=score(groups,[],ranges),context_tokens=0)
                            if 'fixed_query_context_score' not in item:
                                item['fixed_query_context_score']=score(groups,[],ranges)
                                item['fixed_query_completed']=False
                            if case['id'] in generation_ids and not any(g['case_id']==case['id'] for g in p['generation']):
                                p['generation'].append({'case_id':case['id'],'split':split,'trace_id':tid,'status':'failed',
                                    'generation_invoked':False,'error':item['error'],'semantic_success':None})
                        item['elapsed_ms']=(time.perf_counter()-start)*1000
                        generated=next((g for g in p['generation'] if g['trace_id']==tid),None)
                        trace_status='failed' if generated and generated['status']=='failed' else item['status']
                        trace_result={k:v for k,v in item.items() if k not in {'events','model_calls'}}
                        trace_result.update(experiment_scope='context_comparison',generation_result=generated)
                        await cb.finish_trace(tid,trace_status,result=trace_result,
                                              error=generated.get('error') if generated and generated['status']=='failed' else item.get('error'),elapsed_ms=item['elapsed_ms'])
                        save(output,report)
                p['summaries']={split:{'fixed_query_context':summarize([c for c in p['cases'] if c['split']==split],'fixed_query_context_score'),
                    'query_pipeline_context':summarize([c for c in p['cases'] if c['split']==split],'context_score')} for split in datasets}
                p['status']='completed'
            finally: await ca.close();save(output,report)
        report['index_after']=await index_hash(db,args.index_id)
        pointer=await db.current_index();report['pointer_after']=str(pointer['id']) if pointer else None
        if report['index_before']!=report['index_after'] or report['pointer_before']!=report['pointer_after']:
            raise ValueError('索引或指针改变')
        report['status']='completed_with_failures' if any(c['status']=='failed' for p in report['profiles'] for c in p['cases']+p['generation']) else 'completed'
    except BaseException as e:
        report.update(status='failed',error={'code':getattr(e,'code','experiment_failed'),'type':type(e).__name__})
        raise
    finally:
        await adapter.close();report['finished_at']=datetime.now(timezone.utc).isoformat();save(output,report)


def main():
    parser=argparse.ArgumentParser(description='阶段五固定查询与端到端单因素对照')
    parser.add_argument('--mode',choices=['mock','real'],required=True)
    parser.add_argument('--index-id',required=True)
    parser.add_argument('--retrieval-config',default='configs/retrieval/r4-hybrid-rrf-rerank.v1.0.0.json')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--generate-cases',nargs='*',default=[])
    parser.add_argument('--resume-from',type=Path,help='只复用固定指纹相同且技术完成的实验逐例结果；旧文件与失败不覆盖')
    asyncio.run(compare(parser.parse_args()))


if __name__=='__main__': main()
