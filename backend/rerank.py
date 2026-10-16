"""独立重排适配。真实接口失败关闭，不回退到模拟或融合排名。"""
import asyncio
from datetime import datetime, timezone
import math
import time
import uuid

from backend.knowledge import digest
from backend.models import ModelError


def document_text(row):
    return row['section'] + '\n' + str(row['metadata'].get('category_ids', [])) + '\n' + row['text']


async def rerank(adapter, question, rows, config, record):
    s = adapter.settings
    documents = [document_text(r) for r in rows]
    for attempt in range(s.model_retry_count + 1):
        if hasattr(adapter, 'before_call'):
            from backend.knowledge import token_count
            await adapter.before_call('rerank', token_count(question) + sum(token_count(t) for t in documents))
        start = time.perf_counter()
        call = {'call_id': str(uuid.uuid4()), 'started_at': datetime.now(timezone.utc).isoformat(),
            'role': 'rerank', 'attempt': attempt+1, 'mode': s.rag_mode, 'model': s.rerank_model_id,
            'provider': s.rerank_model_provider, 'protocol': config.protocol,
            'input_hash': digest({'query': question, 'documents': documents}),
            'input_chunk_ids': [r['chunk_id'] for r in rows], 'input_count': len(rows),
            'usage': None, 'usage_source': 'unknown', 'cost': None, 'actual_model': None}
        try:
            if s.rag_mode == 'mock':
                # 仅为可预测的接口/排序机制演练，不模拟真实语义相关度。
                query = set(question)
                scores = [{'index': i, 'relevance_score': len(query & set(text)) / max(1, len(query))}
                          for i, text in enumerate(documents)]
                call['actual_model'] = 'mock-character-overlap-rerank-v1'
            else:
                if not s.rerank_model_url or not s.rerank_model_api_key.get_secret_value():
                    raise ModelError('rerank_configuration_missing')
                payload = {'model': s.rerank_model_id, 'query': question, 'documents': documents,
                           'top_n': len(rows), 'return_documents': False}
                if config.protocol == 'dashscope-v1':
                    payload = {'model': s.rerank_model_id, 'input': {'query': question, 'documents': documents},
                               'parameters': {'top_n': len(rows), 'return_documents': False}}
                async with asyncio.timeout(s.rerank_timeout_seconds):
                    response = await adapter.client.post(s.rerank_model_url,
                        headers={'Authorization': 'Bearer ' + s.rerank_model_api_key.get_secret_value()},
                        json=payload, timeout=s.rerank_timeout_seconds)
                response.raise_for_status()
                data = response.json()
                scores = (data['output'] if config.protocol == 'dashscope-v1' else data)['results']
                call.update(actual_model=data.get('model'), usage=data.get('usage'),
                    usage_source='provider' if data.get('usage') is not None else 'unknown',
                    provider_request_id=data.get('request_id') or data.get('id') or response.headers.get('x-request-id'))
            if (len(scores) != len(rows) or any(type(r['index']) is not int for r in scores)
                or sorted(r['index'] for r in scores) != list(range(len(rows)))
                or any(type(r['relevance_score']) not in {int, float} or not math.isfinite(r['relevance_score']) for r in scores)):
                raise ModelError('rerank_response_invalid')
            ranked = [dict(rows[r['index']], rerank_score=r['relevance_score'], score_type='rerank') for r in scores]
            ranked.sort(key=lambda r: (-r['rerank_score'], r['rank'], r['chunk_id']))
            ranked = [dict(r, pre_rerank_rank=r['rank'], rank=i) for i, r in enumerate(ranked, 1)]
            call.update(status='success', elapsed_ms=(time.perf_counter()-start)*1000,
                        output=[{k: r[k] for k in ('chunk_id', 'rank', 'rerank_score', 'pre_rerank_rank')} for r in ranked])
            await record(call)
            return ranked
        except asyncio.CancelledError:
            call.update(status='interrupted', error={'code':'model_cancelled'}, elapsed_ms=(time.perf_counter()-start)*1000)
            await record(call)
            raise
        except Exception as error:
            safe = adapter.safe_error(error)
            call.update(status='failed', error={'code': safe.code, 'http_status': safe.status}, elapsed_ms=(time.perf_counter()-start)*1000)
            await record(call)
            if not safe.retryable or attempt == s.model_retry_count:
                raise safe from None
            await asyncio.sleep(1)
