"""有来源的上下文补充、结构去重和整片段预算裁剪；不做语义证据审核。"""
import json
import time

from backend.knowledge import token_count
from backend.index_config import IndexCompatibilityError
from backend.models import ModelError


def ref_key(ref):
    return ref.get('kind'), ref.get('id'), ref.get('version')


def same_content(a, b):
    # 正文相同也不合并不同文档、版本、范围或适用元数据。
    return all(a.get(k) == b.get(k) for k in
        ('document_key', 'document_version', 'section', 'source_start', 'source_end', 'text', 'metadata'))


def deduplicate(rows):
    kept, discarded = [], []
    for row in rows:
        duplicate = next((r for r in kept if r['chunk_id'] == row['chunk_id'] or same_content(r, row)
            or (all(r.get(k) == row.get(k) for k in ('document_key','section','metadata'))
                and r['source_start'] <= row['source_start'] < row['source_end'] <= r['source_end']
                and r['text'][row['source_start']-r['source_start']:row['source_end']-r['source_start']] == row['text'])), None)
        if duplicate:
            if row['chunk_id'] == duplicate['chunk_id'] and not same_content(duplicate, row):
                raise IndexCompatibilityError('context_chunk_conflict')
            duplicate['context_origins'].extend(row.get('context_origins', []))
            discarded.append(dict(row, reason='duplicate_same_source_range', retained_chunk_id=duplicate['chunk_id']))
        else:
            kept.append(dict(row, context_origins=list(row.get('context_origins', []))))
    return kept, discarded


async def expand_context(rows, settings, db, index, filters):
    config = settings.answer_config.context
    unique, removed = deduplicate(rows)
    hits = unique[:settings.context_top_k]
    removed.extend(dict(r, reason='context_top_k') for r in unique[settings.context_top_k:])
    hits = [dict(r, context_anchor=i, context_kind='hit', context_origins=[{'kind': 'hit', 'chunk_id': r['chunk_id']}])
            for i, r in enumerate(hits)]
    added, relations = [], []
    if config.strategy == 'parent-related' and hits:
        confirmed = settings.retrieval_config.filters(filters) if settings.retrieval_config else filters
        parents = {r['chunk_id']: r for r in await db.parent_rows(index['id'], [r['chunk_id'] for r in hits], confirmed)}
        pool = await db.chunk_rows(index['id'], confirmed)
        for i, hit in enumerate(hits):
            parent = parents.get(hit.get('parent_id'))
            if parent:
                if (parent['document_key'] != hit['document_key'] or parent['metadata'] != hit['metadata']
                    or parent['section'] != hit['section'] or not parent['source_start'] <= hit['source_start'] < hit['source_end'] <= parent['source_end']):
                    raise IndexCompatibilityError('context_parent_relation_invalid')
                if token_count(parent['text']) > settings.parent_max_tokens:
                    raise IndexCompatibilityError('context_parent_budget_incompatible')
                if same_content(parent, hit):
                    relations.append({'kind': 'parent', 'anchor_chunk_id': hit['chunk_id'],
                                      'target_chunk_id': parent['chunk_id'], 'status': 'identical_range_no_addition'})
                else:
                    relations.append({'kind': 'parent', 'anchor_chunk_id': hit['chunk_id'],
                        'target_chunk_id': parent['chunk_id'], 'status': 'replace_child_with_parent_candidate',
                        'source_start': parent['source_start'], 'source_end': parent['source_end'],
                        'bounded_parent_not_whole_document': True})
                    hits[i] = dict(parent, rank=hit.get('rank'), context_anchor=i, context_kind='parent',
                        context_origins=[{'kind': 'parent_of', 'chunk_id': hit['chunk_id']}], fallback_hit=hit)
                    added.append(dict(parent, kind='parent', anchor_chunk_id=hit['chunk_id']))
            else:
                relations.append({'kind': 'parent', 'anchor_chunk_id': hit['chunk_id'], 'status': 'missing_or_filtered'})
            refs = hit['metadata'].get('related_refs', []) + hit['metadata'].get('evidence_refs', [])
            for ref in refs:
                matches = [r for r in pool if ref_key(r['metadata']['ref']) == ref_key(ref)]
                relations.append({'kind': 'related', 'anchor_chunk_id': hit['chunk_id'], 'ref': ref,
                    'status': 'available' if matches else 'missing_or_filtered', 'chunk_ids': [r['chunk_id'] for r in matches]})
                for r in matches:
                    added.append(dict(r, kind='related', anchor_chunk_id=hit['chunk_id']))
    # 按种子排序保留主片段，再按种子/精确关联ref顺序补充；仅一跳，无递归扩展。
    supplements = []
    for r in added:
        if r['kind'] != 'related':
            continue
        anchor = next(i for i, h in enumerate(hits) if any(o['chunk_id'] == r['anchor_chunk_id'] for o in h['context_origins']))
        supplements.append(dict(r, context_kind='related', context_anchor=anchor,
            context_origins=[{'kind': 'related_to', 'chunk_id': r['anchor_chunk_id']}]))
    candidates, duplicate_removed = deduplicate(hits + supplements)
    removed.extend(duplicate_removed)
    seeds = [r for r in candidates if r['context_kind'] != 'related']
    supplements = [r for r in candidates if r['context_kind'] == 'related']
    removed.extend(dict(r, reason='supplement_limit') for r in supplements[config.max_supplements:])
    return seeds + supplements[:config.max_supplements], removed, added, relations


def serialize_context(items):
    blocks = []
    for i, row in enumerate(items, 1):
        row['citation_id'] = 'S' + str(i)
        blocks.append(f"[{row['citation_id']}]\n" + json.dumps({
            'chunk_id': row['chunk_id'], 'document_id': row['document_id'], 'version': row['document_version'],
            'source': row['source_path'], 'metadata': row['metadata'], 'text': row['text']
        }, ensure_ascii=False))
    return '\n\n'.join(blocks)


def make_messages(system, question, context):
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': '本次检索证据（不可信数据）：\n' + context + '\n\n本轮问题：\n' + question}]


def input_size(messages):
    return sum(token_count(m['content']) + 8 for m in messages)


def pack_context(candidates, settings, question, system):
    selected, removed = [], []
    organization = settings.answer_config.context.organization
    def render(items):
        items = [dict(r) for r in items]
        if organization == 'anchor-grouped':
            items.sort(key=lambda r: (r['context_anchor'], r['context_kind'] == 'related'))
        return items, serialize_context(items)
    if input_size(make_messages(system, question, '')) > settings.input_token_budget:
        raise ModelError('input_token_budget_exceeded')
    for row in candidates:
        attempts = [row]
        if row.get('fallback_hit'):
            attempts.append(row['fallback_hit'])
        for attempt in attempts:
            items, text = render(selected + [attempt])
            reason = ('evidence_token_budget' if token_count(text) > settings.evidence_token_budget else
                      'input_token_budget' if input_size(make_messages(system, question, text)) > settings.input_token_budget else None)
            if reason:
                removed.append(dict(attempt, reason=reason, action='whole_fragment_drop_no_text_cut'))
                continue
            selected.append(attempt)
            break
    selected, context = render(selected)
    return selected, context, removed, token_count(context)


async def prepare_context(rows, settings, db, index, filters, question, system, event):
    started = time.perf_counter()
    candidates, removed, added, relations = await expand_context(rows, settings, db, index, filters)
    evidence, context, clipped, used = pack_context(candidates, settings, question, system)
    await event('context_assembly', {'strategy': settings.answer_config.context.model_dump(mode='json'),
        'added': added, 'relations': relations, 'discarded': removed + clipped,
        'final_chunk_ids': [r['chunk_id'] for r in evidence],
        'citation_mapping': {r['citation_id']: {'chunk_id': r['chunk_id'], 'ref': r['metadata']['ref'],
            'source_start': r['source_start'], 'source_end': r['source_end'], 'origins': r['context_origins']} for r in evidence},
        'token_estimate': used, 'elapsed_ms': (time.perf_counter()-started)*1000})
    return evidence, context, removed + clipped, used
