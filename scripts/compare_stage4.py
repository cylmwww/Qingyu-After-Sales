"""在固定已有索引上对照 R1～R4；不入库、不切换指针、不读取独立测试集。"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.config import Settings
from backend.db import Database
from backend.knowledge import digest, load_knowledge
from backend.models import ModelAdapter
from backend.rag import SYSTEM_PROMPT, RAGService, make_context
from backend.retrieval import retrieve
from backend.stage3_eval import first_turn, knowledge_groups, score, source_ranges, validate_pilot, ref_key


class ReplayAdapter(ModelAdapter):
    """离线复算禁止网络调用；向量仅为满足原函数签名的占位。"""
    def __init__(self, settings):
        def reject(request):
            raise ValueError('历史复算禁止外部模型调用')
        super().__init__(settings, httpx.AsyncClient(transport=httpx.MockTransport(reject)))

    async def embed(self, texts, record):
        return [[1.]+[0.]*(self.settings.embedding_model_dimensions-1)]

    async def generate(self, messages, evidence, record):
        raise ValueError('历史复算禁止生成')
        yield ''


class ReplayDatabase(Database):
    def __init__(self, settings, replay_cases):
        super().__init__(settings)
        self.replay_cases=replay_cases
        self.current_case=None

    async def search(self, index_id, query_vector, filters, top_k=None):
        item=self.replay_cases[self.current_case]
        if filters!=item['filters']:
            raise ValueError('历史复算过滤条件改变')
        self.check_index(await self.get_index(index_id))
        pool={r['chunk_id']:r for r in await self.chunk_rows(index_id,filters)}
        for r in item['candidates']:
            if r['chunk_id'] not in pool or pool[r['chunk_id']]['text']!=r['text']:
                raise ValueError('历史排名与固定chunk不一致')
        return item['candidates'][:top_k or self.settings.retrieval_top_k]


def save(path, data):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


async def index_hash(db, index_id):
    async with db.connection() as c:
        row=await(await c.execute('''SELECT md5(string_agg(id||text||coalesce(embedding::text,''),'' ORDER BY id)) AS hash,
            count(*) FILTER (WHERE role='child') children FROM knowledge_chunks WHERE index_id=%s''',(index_id,))).fetchone()
    return row


def stage_diagnosis(groups, pool, routes, fusion, ranked, context):
    result=[]
    for g in groups:
        keys={ref_key(r) for r in g['any_of'] if r['kind'] in {'policy_rule','faq'}}
        hit=lambda rows:any(ref_key(r['metadata']['ref']) in keys for r in rows)
        union=[r for rows in routes.values() for r in rows]
        if not hit(pool): reason='confirmed_metadata_filter_or_not_in_fixed_index'
        elif not hit(union): reason='recall_omission'
        elif not hit(ranked): reason='rerank_candidate_limit'
        elif not hit(context): reason='final_rank_or_context_budget'
        else: reason='retained'
        result.append({'group_id':g.get('id'), 'allowed_refs':g['any_of'], 'reason':reason,
            'route_ranks':{name:[{'chunk_id':r['chunk_id'],'rank':r['rank']} for r in rows if ref_key(r['metadata']['ref']) in keys]
                           for name,rows in routes.items()},
            'fusion_ranks':[{'chunk_id':r['chunk_id'],'rank':r['rank']} for r in fusion if ref_key(r['metadata']['ref']) in keys],
            'final_ranks':[{'chunk_id':r['chunk_id'],'rank':r['rank']} for r in ranked if ref_key(r['metadata']['ref']) in keys]})
    return result


def summarize(cases):
    # 技术失败仍在分母；无证据组案例不计召回分母，保留耗时和失败统计。
    scored=[c for c in cases if c['groups']]
    mean=lambda key: statistics.mean(c[key]['recall'] if c['status']=='completed' else 0 for c in scored) if scored else None
    timings=[c['retrieval_ms'] for c in cases]
    return {'queries':len(cases),'scored_queries':len(scored),'technical_failures':sum(c['status']!='completed' for c in cases),
        'union_recall_macro':mean('union_score'),'fusion_recall_macro':mean('fusion_score'),
        'ranked_recall_macro':mean('ranked_score'),'context_recall_macro':mean('context_score'),
        'full_text_context_proxy':statistics.mean(c['context_score']['condition_text_completeness_proxy'] if c['status']=='completed' else 0 for c in scored) if scored else None,
        'retrieval_ms_p50':statistics.median(timings) if timings else None,
        'retrieval_ms_p95':sorted(timings)[min(len(timings)-1,int(len(timings)*.95))] if timings else None,
        'context_tokens_mean':statistics.mean(c['context_tokens'] for c in cases) if cases else None,
        'loss_reasons':{reason:sum(d['reason']==reason for c in cases for d in c['diagnosis']) for reason in
            ['confirmed_metadata_filter_or_not_in_fixed_index','recall_omission','rerank_candidate_limit','final_rank_or_context_budget','retained']}}


async def compare(args):
    output=args.output.resolve()
    if not output.is_relative_to(ROOT/'local') or output.exists():
        raise ValueError('输出必须为 local 内新文件，不覆盖历史')
    if not os.getenv('STAGE4_DATABASE_URL'):
        raise ValueError('必须显式提供 STAGE4_DATABASE_URL；需包含第三阶段已选定索引')
    output.parent.mkdir(parents=True,exist_ok=True)
    common=dict(database_url=os.environ['STAGE4_DATABASE_URL'],rag_mode=args.mode,
                index_config_path='configs/indexes/stage2-baseline.v1.0.0.json',retrieval_config_path=None,answer_config_path=None)
    base=Settings(**common); db=Database(base)
    _,_,chunks,knowledge_fp=await asyncio.to_thread(load_knowledge,base)
    ranges=source_ranges(chunks)
    dev=json.loads((ROOT/'data/evaluation/development.json').read_text(encoding='utf-8'))
    val=json.loads((ROOT/'data/stage3-evaluation/validation.v1.0.0.json').read_text(encoding='utf-8'))
    datasets={'development':dev,'validation':val}
    replay_cases=None
    replay_path=getattr(args,'vector_replay',None)
    if replay_path:
        if args.mode!='real' or args.generate_cases:
            raise ValueError('历史真实向量复算仅允许real、不调用生成')
        prior=json.loads(Path(replay_path).read_text(encoding='utf-8'))
        if prior['mode']!='real' or prior['dataset_hashes']!={k:digest(v) for k,v in datasets.items()}:
            raise ValueError('历史模式或数据哈希不一致')
        baseline=next(p for p in prior['candidates'] if p['configuration']['id']=='stage2-baseline')
        if baseline['build']['index_id']!=args.index_id or prior['knowledge_fingerprint']!=knowledge_fp:
            raise ValueError('必须复用相同知识及向量索引')
        if prior['system_prompt_hash']!=digest(SYSTEM_PROMPT) or any(prior['fixed_generation'][k]!=base.snapshot()[k]
            for k in ['thinking_model_id','thinking_model_provider','thinking_model_base_url','generation_enable_thinking',
                      'evidence_token_budget','input_token_budget','max_output_tokens','prompt_version']):
            raise ValueError('历史生成模型/提示词/预算基线漂移')
        replay_cases={(c['split'],c['case_id']):c for c in baseline['cases'] if c['repetition']==1}
    report={'status':'running','started_at':datetime.now(timezone.utc).isoformat(),'mode':args.mode,
        'index_id':args.index_id,'dataset_checks':validate_pilot(val,dev,ranges),
        'dataset_hashes':{k:digest(v) for k,v in datasets.items()},'knowledge_fingerprint':knowledge_fp,
        'system_prompt_hash':digest(SYSTEM_PROMPT),'fixed_generation':{k:base.snapshot()[k] for k in
            ['thinking_model_id','thinking_model_provider','generation_enable_thinking','context_top_k',
             'evidence_token_budget','input_token_budget','max_output_tokens','prompt_version']},
        'query_policy':'原始首轮，每配置单独执行；不改写；无自动父补充', 'repeat_count':args.repeats,
        'evidence_mode':'historical_real_vector_replay_current_bm25' if replay_cases else 'fresh',
        'vector_replay_source':str(replay_path) if replay_cases else None,
        'profiles':[],'notes':['验证沿用阶段三11例检索草稿，pending_human；开发30例其中29例有首轮知识标注。',
            'R4模拟仅验证机制，不能用于认定重排效果。接口未配置时R4标不可用，不静默回退。',
            '计时含检索调用、DB连接、追踪写入；不含上下文构建、生成及索引构建。顺序固定未随机化。',
            '准确证据ID覆盖及完整正文代理不等同业务任务成功率；失败不删除。']}
    if replay_cases:
        report['notes'].append('向量Top20取自第三阶段已存真实请求第一重复；无新Embedding/生成调用、无真实费用/接口时延测量。第20位以外原始向量排名未知，淘汰追踪不补造。')
    save(output,report)
    try:
        await db.migrate()
        index=await db.get_index(args.index_id); db.check_index(index)
        if index['source_fingerprint']!=knowledge_fp:
            raise ValueError('知识源与固定索引不一致')
        report['index_before']=await index_hash(db,args.index_id)
        pointer=await db.current_index(); report['pointer_before']=str(pointer['id']) if pointer else None
        for path in sorted((ROOT/'configs/retrieval').glob('*.json')):
            s=Settings(**dict(common,retrieval_config_path=str(path)))
            candidate_db=ReplayDatabase(s,replay_cases) if replay_cases else Database(s)
            adapter=ReplayAdapter(s) if replay_cases else ModelAdapter(s)
            p={'configuration':s.retrieval_config.model_dump(mode='json'),'status':'running','cases':[],
               'generation':[],'model_calls':[],'config_hash':digest(s.snapshot())}
            report['profiles'].append(p); save(output,report)
            try:
                if s.retrieval_config.rerank.enabled and replay_cases:
                    p.update(status='unavailable',reason='historical_source_has_no_rerank_scores',summaries=None)
                    continue
                if s.retrieval_config.rerank.enabled and args.mode=='real' and not (s.rerank_model_url and s.rerank_model_api_key.get_secret_value()):
                    p.update(status='unavailable',reason='rerank_configuration_missing',summaries=None)
                    continue
                if s.retrieval_config.mode!='vector':
                    t=time.perf_counter(); p['bm25_build']=await candidate_db.build_bm25(args.index_id)
                    p['bm25_build_ms']=(time.perf_counter()-t)*1000
                for split,dataset in datasets.items():
                    for repeat in range(args.repeats):
                        for case in dataset['cases']:
                            turn=first_turn(case); filters={k:v for k,v in turn['facts'].items() if k in {'category_id','purchased_at','after_sales_applied_at'}}
                            if replay_cases:
                                if turn['text']!=replay_cases[(split,case['id'])]['question']:
                                    raise ValueError('历史查询改变')
                                candidate_db.current_case=(split,case['id'])
                            trace_id,frozen=await candidate_db.new_trace(turn['text'],filters,args.index_id)
                            events=[]
                            async def event(kind,payload):
                                events.append({'kind':kind,'payload':payload}); await candidate_db.event(trace_id,kind,payload)
                            async def record(payload): await candidate_db.model_call(payload,trace_id=trace_id)
                            start=time.perf_counter(); rows=[]; error=None
                            try:
                                rows=await retrieve(s,candidate_db,adapter,frozen,turn['text'],filters,event,record)
                            except Exception as exc:
                                error={'code':getattr(exc,'code','retrieval_failed'),'type':type(exc).__name__}
                            elapsed=(time.perf_counter()-start)*1000
                            context,text,discarded,tokens=make_context(rows,s)
                            await event('evidence',{'items':context,'discarded':discarded,'token_estimate':tokens})
                            await event('context',{'actual_context':text,'generation_invoked':False,'prompt_version':s.prompt_version})
                            routes={e['payload']['route']:e['payload']['results'] for e in events if e['kind']=='retrieval_route'}
                            fusion=next((e['payload']['results'] for e in events if e['kind']=='fusion'),[])
                            union={r['chunk_id']:r for rs in routes.values() for r in rs}
                            groups=knowledge_groups(case)
                            pool=await candidate_db.chunk_rows(args.index_id,s.retrieval_config.filters(filters))
                            item={'case_id':case['id'],'split':split,'repetition':repeat+1,'trace_id':trace_id,
                                'question':turn['text'],'filters':filters,'groups':len(groups),'status':'failed' if error else 'completed',
                                'error':error,'retrieval_ms':elapsed,'union_score':score(groups,list(union.values()),ranges),
                                'fusion_score':score(groups,fusion,ranges),'ranked_score':score(groups,rows,ranges),
                                'context_score':score(groups,context,ranges),'context_tokens':tokens,
                                'context_chunk_ids':[r['chunk_id'] for r in context], 'discarded':discarded,
                                'diagnosis':stage_diagnosis(groups,pool,routes,fusion,rows,context),'events':events}
                            await candidate_db.finish_trace(trace_id,item['status'],result={k:v for k,v in item.items() if k!='events'},error=error,elapsed_ms=elapsed)
                            trace=await candidate_db.trace(trace_id); p['model_calls'].extend(trace['model_calls']); p['cases'].append(item)
                            save(output,report)
                for case_id in args.generate_cases:
                    case=next(c for c in dev['cases'] if c['id']==case_id); turn=first_turn(case)
                    filters={k:v for k,v in turn['facts'].items() if k in {'category_id','purchased_at','after_sales_applied_at'}}
                    tid,frozen=await candidate_db.new_trace(turn['text'],filters,args.index_id)
                    await RAGService(s,candidate_db,adapter).run(tid,frozen,turn['text'],filters)
                    trace=await candidate_db.trace(tid)
                    p['generation'].append({'case_id':case_id,'trace':trace,'semantic_success':None,'review':'not_formally_scored'})
                p['summaries']={split:summarize([c for c in p['cases'] if c['split']==split]) for split in datasets}
                p['status']='completed'
            finally:
                await adapter.close(); save(output,report)
        report['index_after']=await index_hash(db,args.index_id)
        pointer=await db.current_index(); report['pointer_after']=str(pointer['id']) if pointer else None
        if report['index_before']!=report['index_after'] or report['pointer_before']!=report['pointer_after']:
            raise ValueError('固定索引或指针发生变化')
        report['status']='completed_with_unavailable_r4' if any(p['status']=='unavailable' for p in report['profiles']) else 'completed'
    except BaseException as e:
        report.update(status='failed',error={'code':getattr(e,'code','experiment_failed'),'type':type(e).__name__})
        raise
    finally:
        report['finished_at']=datetime.now(timezone.utc).isoformat(); save(output,report)


def main():
    parser=argparse.ArgumentParser(description='固定第三阶段原索引比较召回排序')
    parser.add_argument('--mode',choices=['mock','real'],required=True)
    parser.add_argument('--index-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repeats',type=int,choices=[1,2,3],default=1)
    parser.add_argument('--generate-cases',nargs='*',default=[])
    parser.add_argument('--vector-replay',type=Path,help='明确离线复算既有第三阶段真实向量Top20；禁止生成，不调用外部模型')
    args=parser.parse_args()
    if sys.platform=='win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(compare(args))


if __name__=='__main__': main()
