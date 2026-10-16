"""原查询单路/双路检索、可追踪融合与可选重排。"""
import time

from backend.bm25 import search_artifact
from backend.rerank import rerank
from backend.index_config import IndexCompatibilityError


def fuse(routes, config):
    merged, duplicates = {}, []
    order = ['vector', 'bm25'] if config.merge_order == 'vector-first' else ['bm25', 'vector']
    for route in order:
        # 同一路也防御性去重，只有最早排名参与贡献。
        seen = set()
        for row in routes.get(route, []):
            key = row['chunk_id']
            if key in seen:
                duplicates.append({'chunk_id': key, 'route': route, 'reason': 'duplicate_within_route'})
                continue
            seen.add(key)
            if key in merged:
                if any(merged[key][field] != row[field] for field in ('document_key','document_version','section','text','metadata','source_start','source_end')):
                    raise IndexCompatibilityError('fusion_chunk_conflict')
                duplicates.append({'chunk_id': key, 'route': route, 'reason': 'duplicate_across_routes',
                                   'action': 'merge_route_scores'})
            else:
                merged[key] = dict(row, route_scores={}, fusion_score=0., merge_position=len(merged))
            target = merged[key]
            score_key = 'cosine_similarity' if route == 'vector' else 'bm25_score'
            target['route_scores'][route] = {'rank': row['rank'], 'score': row[score_key], 'score_type': row['score_type']}
            if config.fusion == 'rrf':
                weight = config.vector_weight if route == 'vector' else config.bm25_weight
                target['fusion_score'] += weight / (config.rrf_k + row['rank'])
    rows = list(merged.values())
    if config.fusion == 'rrf':
        # 分数相同：最优单路名次优先，再按稳定 chunk ID；不混加余弦/BM25原始分数。
        rows.sort(key=lambda r: (-r['fusion_score'], min(x['rank'] for x in r['route_scores'].values()), r['chunk_id']))
    else:
        rows.sort(key=lambda r: r['merge_position'])
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
        if config.fusion == 'rrf':
            row['score_type'] = 'rrf'
    return rows, duplicates


async def retrieve(settings, db, adapter, index, question, filters, event, record, apply_rerank=True):
    profile = settings.retrieval_config
    if profile is None:
        # 原纯向量调用路径与数量保持兼容。
        vector = (await adapter.embed([settings.encode_query(question)], record))[0]
        start = time.perf_counter()
        rows = await db.search(index['id'], vector, filters)
        await event('retrieval', {'query': question, 'filters': filters, 'index_id': str(index['id']),
            'knowledge_version': index['knowledge_version'], 'results': rows,
            'elapsed_ms': (time.perf_counter()-start)*1000, 'method': 'exact_cosine_vector'})
        return rows
    start = time.perf_counter()
    confirmed = profile.filters(filters)
    all_rows = await db.chunk_rows(index['id'], {})
    allowed = await db.chunk_rows(index['id'], confirmed)
    allowed_ids = {r['chunk_id'] for r in allowed}
    await event('metadata_filter', {'index_id': str(index['id']), 'query': question, 'filters': confirmed,
        'filter_source': 'explicit_structured_fields_no_inference', 'shop_id': settings.shop_id,
        'business_filter_fields': list(profile.business_filter_fields), 'elapsed_ms': (time.perf_counter()-start)*1000,
        'discarded': [dict(r, reason='confirmed_metadata_filter') for r in all_rows if r['chunk_id'] not in allowed_ids]})
    routes = {}
    bm25_id = None
    for route in ['vector', 'bm25']:
        if profile.mode not in {route, 'hybrid'}:
            continue
        route_start = time.perf_counter()
        calls = []
        async def route_record(payload):
            calls.append(payload)
            await record(payload)
        if route == 'vector':
            vector = (await adapter.embed([settings.encode_query(question)], route_record))[0]
            # 完整授权池排名用于记录 TopK 淘汰；仍为原 pgvector 精确检索。
            complete = await db.search(index['id'], vector, confirmed, top_k=max(1, len(allowed)))
            rows = complete[:profile.vector_top_k]
            discarded = [dict(r, reason='vector_top_k') for r in complete[profile.vector_top_k:]]
        else:
            artifact, bm25_id = await db.bm25_artifact(index['id'], all_rows)
            rows, reasons = search_artifact(artifact, allowed, question, profile.bm25_top_k, profile.bm25)
            discarded = [dict(r, reason=reasons[r['chunk_id']]) for r in allowed if r['chunk_id'] in reasons]
        routes[route] = rows
        await event('retrieval_route', {'route': route, 'query': question, 'filters': confirmed,
            'ranking_source': 'historical_real_vector_replay' if route == 'vector' and hasattr(db,'replay_cases') else 'fresh',
            'historical_trace_id': db.replay_cases[db.current_case]['trace_id'] if route == 'vector' and hasattr(db,'replay_cases') else None,
            'complete_pool_ranking_available': not (route == 'vector' and hasattr(db,'replay_cases')),
            'index_id': str(index['id']), 'knowledge_version': index['knowledge_version'], 'bm25_index_id': bm25_id,
            'results': rows, 'discarded': discarded, 'elapsed_ms': (time.perf_counter()-route_start)*1000,
            'model_call_ids': [c['call_id'] for c in calls],
            'usage': [dict(call_id=c['call_id'], usage=c['usage'], usage_source=c['usage_source']) for c in calls] or None,
            'cost': 0 if route == 'bm25' else None,
            'cost_source': 'local_no_provider_call' if route == 'bm25' else 'see_model_calls'})
    fusion_start = time.perf_counter()
    rows, duplicates = fuse(routes, profile)
    await event('fusion', {'method': profile.fusion, 'rrf_k': profile.rrf_k,
        'weights': {'vector':profile.vector_weight, 'bm25':profile.bm25_weight},
        'results': rows, 'discarded': duplicates, 'elapsed_ms': (time.perf_counter()-fusion_start)*1000,
        'usage': None, 'cost': 0, 'cost_source':'local_no_provider_call'})
    if profile.rerank.enabled and apply_rerank:
        rerank_start = time.perf_counter()
        candidates = rows[:profile.rerank.candidates]
        await event('rerank_input', {'items': candidates, 'discarded': [dict(r, reason='rerank_candidate_limit')
            for r in rows[profile.rerank.candidates:]]})
        calls = []
        async def rerank_record(payload):
            calls.append(payload)
            await record(payload)
        rows = await rerank(adapter, question, candidates, profile.rerank, rerank_record) if candidates else []
        await event('rerank', {'results': rows, 'mode': settings.rag_mode,
            'model_call_ids':[c['call_id'] for c in calls],
            'elapsed_ms': (time.perf_counter()-rerank_start)*1000,
            'usage':[dict(call_id=c['call_id'],usage=c['usage'],usage_source=c['usage_source']) for c in calls] or None,
            'cost': None, 'cost_source':'see_model_calls'})
    else:
        await event('rerank', {'enabled': False, 'deferred_to_query_merge': profile.rerank.enabled and not apply_rerank,
                              'results': rows, 'elapsed_ms': 0, 'usage': None,
                              'cost': 0, 'cost_source':'disabled_no_provider_call'})
    await event('retrieval', {'query': question, 'filters': confirmed, 'index_id': str(index['id']),
        'knowledge_version': index['knowledge_version'], 'bm25_index_id': bm25_id, 'results': rows,
        'elapsed_ms': (time.perf_counter()-start)*1000, 'method':profile.mode})
    return rows
