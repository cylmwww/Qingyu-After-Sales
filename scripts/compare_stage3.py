"""固定查询和生成边界，比较少量索引配置；报告只能追加到 local。"""
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
from backend.ingest import ingest
from backend.knowledge import digest, load_knowledge
from backend.models import ModelAdapter
from backend.rag import SYSTEM_PROMPT, RAGService, make_context
from backend.stage3_eval import first_turn, knowledge_groups, score, source_ranges, validate_pilot


def save(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def summarize(cases):
    scored = [c for c in cases if c['context_score']['groups']]
    def mean(field, metric):
        return statistics.mean(c[field][metric] for c in scored) if scored else None
    timings = [c['query_ms'] for c in cases]
    return {'queries': len(cases), 'scored_queries': len(scored),
        'technical_failures': sum(c['status'] != 'completed' for c in cases),
        'recall_at_20_macro': mean('candidate_score', 'recall'),
        'recall_at_6_context_macro': mean('context_score', 'recall'),
        'condition_text_completeness_at_20_proxy': mean('candidate_score', 'condition_text_completeness_proxy'),
        'condition_text_completeness_at_6_proxy': mean('context_score', 'condition_text_completeness_proxy'),
        'all_groups_at_context_rate': mean('context_score', 'all_groups_hit'),
        'query_ms_p50': statistics.median(timings) if timings else None,
        'query_ms_p95': sorted(timings)[min(len(timings)-1, int(len(timings)*.95))] if timings else None,
        'search_ms_p50': statistics.median(c['search_ms'] for c in cases if c['search_ms'] is not None) if any(c['search_ms'] is not None for c in cases) else None}


async def compare(args):
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'local') or output.exists():
        raise ValueError('实验输出必须为 local 内尚不存在的文件，禁止覆盖历史')
    output.parent.mkdir(parents=True, exist_ok=True)
    if not os.getenv('STAGE3_DATABASE_URL'):
        raise ValueError('请显式配置 STAGE3_DATABASE_URL；建议独立实验库')
    common = dict(database_url=os.environ['STAGE3_DATABASE_URL'], rag_mode=args.mode, index_config_path=None,
                  retrieval_config_path=None, answer_config_path=None)  # 冻结原查询与上下文，不继承后续增强。
    baseline = Settings(**common)
    _, documents, baseline_chunks, fingerprint = await asyncio.to_thread(load_knowledge, baseline)
    ranges = source_ranges(baseline_chunks)
    dev = json.loads((ROOT / 'data/evaluation/development.json').read_text(encoding='utf-8'))
    pilot = json.loads((ROOT / 'data/stage3-evaluation/validation.v1.0.0.json').read_text(encoding='utf-8'))
    check = validate_pilot(pilot, dev, ranges)
    datasets = {'development': dev, 'validation': pilot}
    fixed = {key: baseline.snapshot()[key] for key in ['retrieval_top_k', 'context_top_k', 'evidence_token_budget',
        'input_token_budget', 'max_output_tokens', 'prompt_version', 'thinking_model_id', 'thinking_model_provider',
        'thinking_model_base_url', 'generation_enable_thinking', 'model_timeout_seconds',
        'embedding_timeout_seconds', 'request_timeout_seconds', 'model_retry_count']}
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'mode': args.mode,
        'status': 'running', 'dataset_checks': check, 'dataset_hashes': {k: digest(v) for k,v in datasets.items()},
        'knowledge_fingerprint': fingerprint, 'fixed_query': 'first_user_turn_original_no_rewrite',
        'fixed_generation': fixed, 'system_prompt_hash': digest(SYSTEM_PROMPT), 'repeat_count': args.repeats,
        'generation_case_ids': args.generate_cases, 'candidates': [],
        'notes': ['未使用独立测试集。验证子集为待人工复核草稿，不能据此认定最优配置。',
                  '条件完整性是条款/FAQ原文完全覆盖的保守代理，不是语义正确性或答案成功率。',
                  '仅嵌入和精确检索参与排名比较；生成冒烟独立报告，不参与选参。',
                  'query_ms包含查询Embedding、其model_call记录写入、兼容检查、SQL/连接；不含生成、上下文组装及检索/上下文事件写入。',
                  'row_bytes/vector_bytes为指定版本的行和向量字节估计，非数据库磁盘含TOAST/WAL/空闲空间。']}
    save(output, report)
    db = Database(baseline)
    try:
        await db.migrate()
        pointer = await db.current_index()
        report['default_index_before'] = str(pointer['id']) if pointer else None
        for config_path in args.configs:
            settings = Settings(**dict(common, index_config_path=str(config_path)))
            current_fixed = {k: settings.snapshot()[k] for k in fixed}
            if current_fixed != fixed or settings.embedding_contract() != baseline.embedding_contract():
                raise ValueError('本组实验只比较分块，不允许改变查询/模型/预算/向量空间')
            candidate_db, adapter = Database(settings), ModelAdapter(settings)
            candidate = {'configuration': settings.index_config.model_dump(mode='json'), 'config_hash': digest(settings.snapshot()),
                'status': 'building', 'cases': [], 'generation_smoke': []}
            report['candidates'].append(candidate); save(output, report)
            try:
                start = time.perf_counter()
                candidate['build'] = await ingest(settings, candidate_db, adapter, publish=False)
                candidate['build_ms'] = (time.perf_counter()-start)*1000
                candidate['size'] = await candidate_db.index_stats(candidate['build']['index_id'])
                index = await candidate_db.get_index(candidate['build']['index_id'])
                for split, dataset in datasets.items():
                    for repetition in range(args.repeats):
                        for case in dataset['cases']:
                            turn = first_turn(case)
                            filters = {k:v for k,v in turn['facts'].items() if k in {'category_id','purchased_at','after_sales_applied_at'}}
                            trace_id, _ = await candidate_db.new_trace(turn['text'], filters, str(index['id']))
                            async def record(payload):
                                await candidate_db.model_call(payload, trace_id=trace_id)
                            start = time.perf_counter(); rows = []; search_ms = None; error = None
                            try:
                                vector = (await adapter.embed([settings.encode_query(turn['text'])], record))[0]
                                search_start = time.perf_counter()
                                rows = await candidate_db.search(index['id'], vector, filters)
                                search_ms = (time.perf_counter()-search_start)*1000
                            except Exception as exc:
                                error = {'code': getattr(exc, 'code', 'experiment_query_failed')}
                            query_ms = (time.perf_counter()-start)*1000
                            context, context_text, discarded, tokens = make_context(rows, settings)
                            # SQL同时返回字符位置，仅用于离线评分，不加入提示词证据块。
                            actual = {r['chunk_id']:r for r in rows}
                            context_rows = [actual[r['chunk_id']] for r in context]
                            groups = knowledge_groups(case)
                            item = {'split':split, 'case_id':case['id'], 'repetition':repetition+1, 'trace_id':trace_id,
                                'question':turn['text'], 'filters':filters, 'status':'failed' if error else 'completed',
                                'error':error, 'query_ms':query_ms, 'search_ms':search_ms,
                                'candidate_score':score(groups, rows, ranges), 'context_score':score(groups, context_rows, ranges),
                                'candidates':rows, 'context_chunk_ids':[r['chunk_id'] for r in context],
                                'discarded':discarded, 'context_token_estimate':tokens}
                            candidate['cases'].append(item)
                            await candidate_db.event(trace_id, 'stage3_retrieval', item)
                            await candidate_db.event(trace_id, 'context', {'actual_context':context_text, 'generation_invoked':False})
                            await candidate_db.finish_trace(trace_id, item['status'], result=item, error=error, elapsed_ms=query_ms)
                for case_id in args.generate_cases:
                    case = next(c for c in dev['cases'] if c['id'] == case_id)
                    turn = first_turn(case)
                    filters = {k:v for k,v in turn['facts'].items() if k in {'category_id','purchased_at','after_sales_applied_at'}}
                    trace_id, frozen = await candidate_db.new_trace(turn['text'], filters, str(index['id']))
                    await RAGService(settings, candidate_db, adapter).run(trace_id, frozen, turn['text'], filters)
                    candidate['generation_smoke'].append({'case_id':case_id, 'semantic_task_success':None,
                        'semantic_review':'not_scored', 'trace':await candidate_db.trace(trace_id)})
                candidate['summaries'] = {split:summarize([c for c in candidate['cases'] if c['split']==split]) for split in datasets}
                candidate['status'] = 'completed'
            finally:
                await adapter.close(); save(output, report)
        after = await db.current_index()
        report['default_index_after'] = str(after['id']) if after else None
        if report['default_index_after'] != report['default_index_before']:
            raise ValueError('实验期间默认索引发生改变，请检查并发发布')
        report['status'] = 'completed'
    except BaseException as error:
        report['status'] = 'failed'
        report['error'] = {'code':getattr(error,'code','experiment_failed'), 'type':type(error).__name__}
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat(); save(output, report)


def main():
    parser = argparse.ArgumentParser(description='阶段三分块候选比较，不读取独立测试集、不切换发布指针')
    parser.add_argument('--mode', choices=['mock','real'], required=True)
    parser.add_argument('--configs', type=Path, nargs='+', default=sorted((ROOT/'configs/indexes').glob('*.json')))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, choices=[1,2,3], default=1)
    parser.add_argument('--generate-cases', nargs='*', default=[])
    args = parser.parse_args()
    asyncio.run(compare(args))


if __name__ == '__main__':
    main()
