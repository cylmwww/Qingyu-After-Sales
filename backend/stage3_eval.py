"""阶段三的离线标注校验与证据覆盖评分，不向模型提供标准。"""
import re

from backend.knowledge import digest


def first_turn(case):
    return next(t for t in case['turns'] if t['role'] == 'user')


def knowledge_groups(case):
    turn_id = first_turn(case)['id']
    return [g for g in case['required_evidence'] if g['visibility'] == 'assistant'
            and g['available_from_turn_id'] == turn_id
            and any(r['kind'] in {'policy_rule', 'faq'} for r in g['any_of'])]


def ref_key(ref):
    return ref['kind'], ref['id'], ref['version']


def source_ranges(chunks):
    result = {}
    for c in chunks:
        if c.role == 'parent':
            result.setdefault(ref_key(c.metadata['ref']), []).append((c.source_start, c.source_end))
    return result


def covered(intervals, ranges):
    """合并同一证据的实际子片段，要求所有标注正文范围被覆盖。"""
    for start, end in ranges:
        cursor = start
        for a, b in sorted(intervals):
            if a <= cursor:
                cursor = max(cursor, b)
        if cursor < end:
            return False
    return bool(ranges)


def score(groups, rows, ranges):
    hits, complete = [], []
    for group in groups:
        alternatives = [ref_key(r) for r in group['any_of'] if r['kind'] in {'policy_rule', 'faq'}]
        hits.append(any(ref_key(row['metadata']['ref']) in alternatives for row in rows))
        complete.append(any(covered([(row['source_start'], row['source_end']) for row in rows
            if ref_key(row['metadata']['ref']) == ref], ranges.get(ref, [])) for ref in alternatives))
    return {'groups': len(groups), 'hit_groups': sum(hits), 'complete_text_groups': sum(complete),
            'recall': sum(hits) / len(groups) if groups else None,
            'condition_text_completeness_proxy': sum(complete) / len(groups) if groups else None,
            'all_groups_hit': all(hits) if groups else None,
            'all_group_text_complete': all(complete) if groups else None}


def validate_pilot(pilot, development, ranges):
    if pilot.get('split') != 'validation' or pilot.get('scope') != 'retrieval_only_pilot':
        raise ValueError('只允许阶段三检索验证子集，禁止独立测试集')
    if pilot.get('review_status') != 'pending_human':
        raise ValueError('本版新增验证标准尚未取得负责人审核，不可自行升级状态')
    families = {c['family_id'] for c in development['cases']}
    normalized = lambda s: re.sub(r'\s+', '', s)
    questions = {normalized(first_turn(c)['text']) for c in development['cases']}
    combinations = {digest(first_turn(c)['facts']) for c in development['cases'] if first_turn(c)['facts']}
    seen_ids, seen_queries, seen_combinations = set(), set(), set()
    for case in pilot['cases']:
        turn = first_turn(case)
        if case['family_id'] in families or normalized(turn['text']) in questions:
            raise ValueError('验证问题族或问题与开发集重叠')
        if case['id'] in seen_ids or normalized(turn['text']) in seen_queries or case['condition_combination_id'] in seen_combinations:
            raise ValueError('验证案例或条件组合重复')
        if not case['split_rationale'] or case['review_status'] != 'pending_human':
            raise ValueError('缺少划分理由或审核状态不真实')
        if turn['facts'] and digest(turn['facts']) in combinations:
            raise ValueError('验证显式条件组合与开发首轮完全重复')
        for key, value in turn['facts'].items():
            if key.endswith('_at') and value not in turn['text']:
                raise ValueError('验证时间过滤值未在用户文本中提供')
        for group in knowledge_groups(case):
            if not all(ref_key(r) in ranges for r in group['any_of']):
                raise ValueError('验证证据引用不存在')
        if not knowledge_groups(case):
            raise ValueError('验证案例必须具有首轮知识证据')
        seen_ids.add(case['id']); seen_queries.add(normalized(turn['text']))
        seen_combinations.add(case['condition_combination_id'])
    return {'status': 'pass', 'cases': len(seen_ids), 'family_overlap': [],
            'human_review': 'pending_human', 'semantic_split_review': 'agent_draft_not_human_signoff'}
