"""只用开发集验证基础 RAG 链路，不读取验证集、独立测试集或业务标准作为模型输入。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.config import Settings
from backend.db import Database
from backend.models import ModelAdapter
from backend.rag import RAGService
from scripts.validate_stage1 import read_json


async def verify(ids, output):
    settings = Settings()
    db, adapter = Database(settings), ModelAdapter(settings)
    results = []
    try:
        development = read_json(ROOT / 'data/evaluation/development.json')
        if development['split'] != 'development':
            raise ValueError('此入口只允许开发集')
        cases = {case['id']: case for case in development['cases']}
        for id in ids:
            case = cases[id]
            turn = next(turn for turn in case['turns'] if turn['role'] == 'user')
            filters = {key: turn['facts'][key] for key in ['category_id', 'purchased_at', 'after_sales_applied_at'] if key in turn['facts']}
            trace_id, index = await db.new_trace(turn['text'], filters)
            await RAGService(settings, db, adapter).run(trace_id, index, turn['text'], filters)
            trace = await db.trace(trace_id)
            result = trace.get('result') or {}
            evidence = result.get('evidence', [])
            refs = [item['metadata']['ref'] for item in evidence]
            knowledge_groups = [group for group in case['required_evidence'] if group['visibility'] == 'assistant' and
                                any(ref['kind'] in {'policy_rule', 'faq'} for ref in group['any_of'])]
            coverage = [{'group_id': group['id'], 'hit': any(ref in refs for ref in group['any_of'])} for group in knowledge_groups]
            links = []
            for item in evidence:
                source = await db.evidence(index['id'], item['chunk_id'])
                links.append({'chunk_id': item['chunk_id'], 'source_range_matches': bool(source and
                    source['source_text'][source['source_start']:source['source_end']] == item['text'])})
            results.append({'case_id': id, 'first_user_turn_only': True, 'trace_id': trace_id, 'status': trace['status'],
                'mode': settings.rag_mode, 'question': turn['text'], 'filters': filters, 'result': result,
                'error': trace['error'], 'knowledge_group_coverage_at_context_top_k': coverage, 'source_chain': links,
                'semantic_task_success': None, 'semantic_review_status': 'not_scored', 'trace': trace})
        payload = {'scope': 'stage2_development_chain_validation', 'mode': settings.rag_mode,
                   'dataset': 'development', 'cases': results,
                   'notes': ['证据ID命中和引用可回查不等于语义支持或任务正确。', '未向模型输入标准答案、业务种子、后台证据或案例后续轮次。']}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print(json.dumps({'report': str(output), 'mode': settings.rag_mode, 'completed': sum(r['status'] == 'completed' for r in results), 'total': len(results)}, ensure_ascii=False))
    finally:
        await adapter.close()


def main():
    parser = argparse.ArgumentParser(description='基础 RAG 开发案例链路验证')
    parser.add_argument('--cases', default='DEV-001,DEV-002,DEV-003,DEV-004,DEV-005')
    parser.add_argument('--output', type=Path, default=ROOT / 'local/artifacts/stage2/development-real.json')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'local'):
        parser.error('实际模型输出与追踪必须保存在 local')
    asyncio.run(verify(args.cases.split(','), output))


if __name__ == '__main__':
    main()
