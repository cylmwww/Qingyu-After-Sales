"""v10 修复义务；语义选择来自模型，引用、范围和完整性由程序验证。"""
from copy import deepcopy
from backend.agent_store import AgentError
from backend.knowledge import digest
from backend.agent_loop import requirements


def source_valid(source, state):
    kind, _, ref = source.partition(':')
    return (kind == 'finding' and any(f['finding_id'] == ref for f in state.get('findings', []))
            or kind == 'goal' and any(g['goal_id'] == ref for g in state['goals']))


def apply(state, decision, scope):
    """仅写 accept 产生的副本；调用者在绑定行动成功后才原子持久化。"""
    selected = {e['evidence_id']: e for e in state['bundle']['items']}
    updates = decision.get('evidence_need_updates', [])
    bindings = {b['binding_id']: b for b in state.get('evidence_bindings', [])
                if b['scope'] == scope and source_valid(b['source'], state)}
    for proposal in updates:
        source = proposal['source']
        if not source_valid(source, state):
            raise AgentError('loop_invalid_obligation_source')
        for ref in proposal['evidence_refs']:
            row = selected.get(ref)
            if (not row or row.get('kind') != 'knowledge'
                    or row.get('metadata', {}).get('shop_id') != scope['shop_id']
                    or row['metadata'].get('ref', {}).get('kind') != 'policy_rule'
                    or row['document_version'] != row['metadata']['ref']['version']):
                raise AgentError('loop_invalid_obligation_evidence')
            value = dict(source=source, scope=scope, rule=row['metadata']['ref'],
                         source_path=row['source_path'], document_id=row['document_id'],
                         purpose=proposal['purpose'])
            key = digest(value)
            bindings[key] = dict(value, binding_id=key, selected_ref=ref,
                                 selected_at=state['state_version'])
    if len(bindings) > requirements.MAX_REQUIREMENTS:
        raise AgentError('scope_capacity_exceeded')
    state['evidence_bindings'] = list(bindings.values())


def collect(state, policies, scope, rows):
    # v9 的条件/旧明确需求保留；去除仅按同目标附着 finding 的路径。
    view = deepcopy(state)
    view['pending_action'] = None
    result = requirements.collect(view, policies, scope, rows)
    by_key = {r['requirement_id']: r for r in result}

    def add(source, row, purpose='direct'):
        if row.get('metadata', {}).get('ref', {}).get('kind') != 'policy_rule':
            return
        if row['metadata'].get('shop_id') != scope['shop_id']:
            return
        value = dict(source=source, scope=scope, rule=row['metadata']['ref'],
                     source_path=row['source_path'], document_id=row['document_id'], completeness='parent')
        key = digest(value)
        if by_key.get(key, {}).get('purpose') == 'direct':
            purpose = 'direct'
        by_key[key] = dict(value, requirement_id=key, purpose=purpose)

    for b in state.get('evidence_bindings', []):
        if b['scope'] == scope and source_valid(b['source'], state):
            for row in rows:
                if requirements.matches(row, b):
                    add(b['source'], row, b['purpose'])
                    break
            else:
                value = {k: b[k] for k in ('source', 'scope', 'rule', 'source_path', 'document_id')}
                value['completeness'] = 'parent'
                key = digest(value)
                by_key[key] = dict(value, requirement_id=key, purpose=b['purpose'])
    # 当前 supported 的实际政策依据必须验证完整，但不冒充 finding 直接关联。
    for gid, assessment in state['assessments'].items():
        if assessment.get('status') == 'supported':
            for row in rows:
                if row['evidence_id'] in assessment.get('support_refs', []):
                    add('goal:'+gid, row)
    action = state.get('pending_action') or {}
    if action.get('tool') == 'read_policy_context':
        for row in rows:
            if row['evidence_id'] in action.get('arguments', {}).get('chunk_refs', []):
                for gid in action['targets']:
                    add('goal:'+gid, row, 'dependency')
    if len(by_key) > requirements.MAX_REQUIREMENTS:
        raise AgentError('scope_capacity_exceeded')
    return list(by_key.values())


def refresh(state, scope):
    result = []
    for finding in state.get('findings', []):
        needs = [r for r in state.get('evidence_requirements', [])
                 if r['source'] == 'finding:'+finding['finding_id'] and r.get('purpose', 'direct') == 'direct']
        result.append(dict(finding_id=finding['finding_id'], goal_id=finding.get('goal_id'), scope=scope,
                           status='unbound' if not needs else 'verified' if all(r['coverage'] == 2 for r in needs) else 'missing_parent',
                           requirement_ids=[r['requirement_id'] for r in needs]))
    if len(result) > 6:
        raise AgentError('finding_capacity_exceeded')
    state['repair_obligations'] = result


def unbound_missing(state):
    missing = {f['finding_id'] for f in state.get('findings', []) if f['kind'] == 'missing_requirement'}
    return [o['finding_id'] for o in state.get('repair_obligations', [])
            if o['finding_id'] in missing and o['status'] == 'unbound']


def evidence_status(row, shop_id):
    from backend.agent_loop.conditions import full_policy_parent
    ref = row.get('metadata', {}).get('ref', {})
    if ref.get('kind') != 'policy_rule':
        return 'not_policy'
    return 'verified_parent' if full_policy_parent(row, ref, shop_id) else 'needs_parent'


def search_goals(state):
    goals = {g['goal_id'] for g in state['goals']}
    return sorted({r['source'].partition(':')[2] for r in state.get('evidence_requirements', [])
                   if r['source'].startswith('goal:') and r['coverage'] < 2} & goals
                  | (goals if any(r['source'] == 'condition:window' and r['coverage'] < 2
                                  for r in state.get('evidence_requirements', [])) else set()))


def view(state, shop_id):
    rows = state['bundle']['items']
    visible = {r['evidence_id']:r for r in rows}
    needs = state.get('evidence_requirements', [])
    summaries = []
    for obligation in state.get('repair_obligations', []):
        fid = obligation['finding_id']
        relevant = [o for o in state['observations'] if o.get('query_origin') == 'finding:'+fid]
        origins = {o['operation_key'] for o in relevant}
        candidates = [r['evidence_id'] for r in rows if r.get('origin_key') in origins
                      and r.get('metadata', {}).get('ref', {}).get('kind') == 'policy_rule']
        summaries.append(dict(finding_id=fid, status=obligation['status'], candidates=candidates,
            searches=[dict(observation_id=o['observation_id'], status=o['status'], purpose=o.get('query_purpose'))
                      for o in relevant[-2:]],
            bound_refs=sorted({ref for r in needs if r['source']=='finding:'+fid for ref in r['visible_refs']})))
    gaps = []
    for r in needs:
        if r['coverage'] >= 2:
            continue
        refs = [ref for ref in r['visible_refs'] if ref in visible]
        source = r['source']
        legal = [source.split(':',1)[1]] if source.startswith(('finding:', 'goal:')) else search_goals(state)
        gaps.append(dict(source=source, rule=r['rule']['id'], coverage=r['coverage'],
                         legal_gap_refs=legal, chunk_refs=refs, next_tool='read_policy_context' if refs else 'search_knowledge'))
    return dict(repair_obligations=summaries, evidence_gaps=gaps)


def validate_search(state, gap):
    needs = [r for r in state.get('evidence_requirements', []) if r['source'] in {'finding:'+gap, 'goal:'+gap}]
    if needs and any(r.get('visible_refs') and r['coverage'] < 2 for r in needs):
        raise AgentError('loop_existing_context_required')
    if needs and all(r['coverage'] == 2 for r in needs):
        raise AgentError('loop_obligation_ready_for_review')
