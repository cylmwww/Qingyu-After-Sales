"""v9 bounded evidence obligations and monotonic coverage, shared by view and runtime."""
from backend.knowledge import digest
from backend.agent_loop.conditions import aware, full_policy_parent

MAX_REQUIREMENTS = 32


def same_parent(child, parent):
    """Only an actual child ID and exact source containment establish replacement."""
    fields = ('document_id', 'document_version', 'source_path')
    cm, pm = child.get('metadata') or {}, parent.get('metadata') or {}
    a, b, x, y = (child.get('source_start'), child.get('source_end'), parent.get('source_start'), parent.get('source_end'))
    return bool(child.get('parent_id') and child['parent_id'] == parent.get('chunk_id')
        and parent.get('parent_id', 'missing') is None and all(child.get(k) is not None and child[k] == parent.get(k) for k in fields)
        and cm.get('shop_id') and cm.get('shop_id') == pm.get('shop_id') and cm.get('ref') == pm.get('ref')
        and all(type(v) is int for v in (a,b,x,y)) and x <= a < b <= y
        and child.get('text') == parent.get('text', '')[a-x:b-x])


def matches(row, requirement):
    meta = row.get('metadata') or {}
    scope = requirement['scope']
    return bool(row.get('kind') == 'knowledge' and meta.get('shop_id') == scope['shop_id']
        and meta.get('ref') == requirement['rule']
        and row.get('document_version') == requirement['rule']['version']
        and (not requirement.get('source_path') or row.get('source_path') == requirement['source_path'])
        and (not requirement.get('document_id') or row.get('document_id') == requirement['document_id']))


def collect(state, policies, scope, rows):
    """Only active deterministic window dependencies and explicit cited/context needs.

    Related refs and search result lists are not recursively made mandatory.
    """
    facts = state['route']['facts']
    requirements = {}
    def add(source, rule, row=None):
        value = dict(source=source, scope=scope, rule=rule, completeness='parent')
        if row:
            value.update(source_path=row['source_path'], document_id=row['document_id'])
        key = digest(value)
        requirements[key] = dict(value, requirement_id=key)
    for policy in policies:
        try:
            valid = (policy['shop_id'] == scope['shop_id'] and facts.get('category_id') in policy['category_ids']
                and policy['selection_event'] == 'purchased_at' and aware(facts['purchased_at']) >= aware(policy['effective_from'])
                and (not policy.get('effective_until') or aware(facts['purchased_at']) < aware(policy['effective_until'])))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            continue
        for rule in policy['rules']:
            if (any(k in rule['effects'] for k in ('no_reason_window_days','quality_window_days'))
                and all(c['op']=='eq' and facts.get(c['field']) == c['value'] for c in rule['conditions'])):
                add('condition:window', dict(kind='policy_rule', id=rule['id'], version=policy['version']))
    findings = {f['finding_id']: f for f in state.get('findings', [])}
    goals = {g['goal_id'] for g in state['goals']}
    for source, refs in [(f'finding:{fid}', f.get('evidence_refs', [])) for fid, f in findings.items()]:
        for row in rows:
            if row['evidence_id'] in refs and (row['metadata'].get('ref') or {}).get('kind') == 'policy_rule':
                add(source, row['metadata']['ref'], row)
    # Persist only actual selected context requirements while their goal/finding is active.
    for old in state.get('evidence_requirements', []):
        kind, _, ref = old['source'].partition(':')
        if old['scope'] == scope and (kind == 'finding' and ref in findings or kind == 'goal' and ref in goals
                and state['assessments'].get(ref, {}).get('status','open') in {'open','conflicted'}):
            requirements[old['requirement_id']] = {k:v for k,v in old.items() if k not in ('coverage','visible_refs')}
    action = state.get('pending_action') or {}
    if action.get('tool') == 'read_policy_context':
        selected = set(action.get('arguments', {}).get('chunk_refs', []))
        for row in rows:
            if row['evidence_id'] not in selected or (row['metadata'].get('ref') or {}).get('kind') != 'policy_rule':
                continue
            linked = [f'finding:{fid}' for fid,f in findings.items() if f.get('goal_id') in action.get('targets', [])
                or selected.intersection(f.get('evidence_refs', []))]
            for source in linked or ['goal:'+g for g in action.get('targets', []) if g in goals]:
                add(source, row['metadata']['ref'], row)
    if len(requirements) > MAX_REQUIREMENTS:
        from backend.agent_store import AgentError
        raise AgentError('scope_capacity_exceeded')
    return list(requirements.values())


def covered(requirements, bundle):
    result = []
    for r in requirements:
        rows = [e for e in bundle['items'] if matches(e, r)]
        level = 2 if any(full_policy_parent(e, r['rule'], r['scope']['shop_id']) for e in rows) else 1 if rows else 0
        result.append(dict(r, coverage=level, visible_refs=[e['evidence_id'] for e in rows]))
    return result


def apply_progress(before, after, observation):
    """Applied once with observation/state transaction; high-water prevents eviction oscillation."""
    if observation is None:
        return
    epoch = digest([before['revision'], before.get('repair_episodes', 0)])
    old = before.get('coverage_progress', {})
    levels = dict(old.get('levels', {})) if old.get('epoch') == epoch else {}
    for r in before.get('evidence_requirements', []):
        levels[r['requirement_id']] = max(levels.get(r['requirement_id'],0), r.get('coverage',0))
    improvements = []
    for r in after['evidence_requirements']:
        key = r['requirement_id']
        if r['coverage'] > levels.get(key,0):
            improvements.append(key)
        levels[key] = max(levels.get(key,0),r['coverage'])
    # Non-policy/business routes keep structural progress; policy needs cannot be reset by irrelevant rows.
    progress = bool(improvements) if after['evidence_requirements'] else not observation['reused']
    if observation['tool'] not in {'search_knowledge','read_policy_context'}:
        progress = not observation['reused']
    after['coverage_progress'] = dict(epoch=epoch, levels=levels)
    after['no_progress'] = 0 if progress else before['no_progress']+1
    observation.update(coverage_improved=improvements, structural_progress=progress)


def gaps(state):
    from backend.agent_loop.recovery import reference_catalog
    catalog = reference_catalog(state)
    result=[]
    for r in state.get('evidence_requirements', []):
        if r['coverage'] >= 2:
            continue
        kind, _, ref = r['source'].partition(':')
        legal = [ref] if kind=='finding' and ref in catalog['pending_findings'] else catalog['open_goals']
        refs = [v for v in r['visible_refs'] if v in catalog['policy_context_evidence']]
        result.append(dict(required_rule=r['rule'], source=r['source'], coverage=r['coverage'],
            legal_gap_refs=legal, existing_policy_refs=refs,
            context_arguments={'chunk_refs':refs} if refs else None,
            available_tool='read_policy_context' if refs else 'search_knowledge'))
    return result
