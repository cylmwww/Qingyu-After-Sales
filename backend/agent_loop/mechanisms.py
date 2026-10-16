"""从持久候选、审核和操作派生机制证据，不作为发布许可或语义评分。"""
from backend.knowledge import digest


KINDS = {'adaptive_evidence', 'review_rewrite', 'review_lookup'}


def qualified_lookup(state, operations, old, new, pending):
    """v2 diagnosis: completed operation + persisted review order + exact parent containment."""
    from backend.evidence_bundle import _knowledge_rows
    from backend.agent_loop.requirements import same_parent
    audit = new.get('evidence_audit') or {}
    scope = {k:audit.get(k) for k in ('task_id','revision','index_id','shop_id')}
    ops = {o.get('key'):o for o in operations if o.get('status','completed')=='completed'}
    review_op = ops.get(audit.get('input_operation'), {})
    payload = review_op.get('arguments', {}).get('payload', {})
    old_inputs = [o.get('arguments', {}).get('payload', {}) for o in ops.values() if o.get('kind')=='model'
        and (o.get('arguments', {}).get('payload', {}).get('candidate') or {}).get('candidate_id')==old['candidate_id']]
    if (any(v is None for v in scope.values()) or not old_inputs or not payload
        or digest(payload) != audit.get('input_hash') or review_op.get('kind')!='model'
        or (payload.get('candidate') or {}).get('candidate_id') != new['candidate_id']):
        return set(), [], [], ['missing_bound_review_input']
    observed = {o['operation_key']:o for o in state.get('observations', [])}
    def valid(key, tool):
        op, ob = ops.get(key, {}), observed.get(key, {})
        args = op.get('arguments') or {}
        return bool(op.get('kind')=='tool' and args.get('tool')==tool
            and (op.get('result') or {}).get('status')=='success'
            and ob.get('after_review_id')==old['review_id']
            and type(ob.get('applied_state_version')) is int
            and ob['applied_state_version'] < audit.get('state_version',0)
            and all(args.get('scope',{}).get(k)==v and ob.get('scope',{}).get(k)==v for k,v in scope.items()))
    refs, queries, edges = set(), [], []
    for key, ob in observed.items():
        if ob.get('query_origin') not in {'finding:'+f for f in pending} or not valid(key,'search_knowledge'):
            continue
        children = _knowledge_rows(key, (ops[key]['result'].get('data') or {}).get('evidence', []))
        children = [r for r in children if r['metadata'].get('shop_id')==scope['shop_id']]
        queries.append(key)
        refs.update(r['evidence_id'] for r in children)
        for parent_key, parent_ob in observed.items():
            if not valid(parent_key,'read_policy_context') or parent_ob['applied_state_version'] <= ob['applied_state_version']:
                continue
            op = ops[parent_key]
            requested = op['arguments'].get('args', {}).get('chunk_ids', [])
            parents = _knowledge_rows(parent_key, (op['result'].get('data') or {}).get('evidence', []))
            for child in children:
                for parent in parents:
                    if child['chunk_id'] in requested and same_parent(child,parent):
                        refs.add(parent['evidence_id'])
                        edges.append(dict(query_operation=key, context_operation=parent_key,
                            child_ref=child['evidence_id'], parent_ref=parent['evidence_id'], scope=scope))
    old_refs = {e['evidence_id'] for p in old_inputs for e in p.get('evidence', [])}
    reviewed = {e['evidence_id'] for e in payload.get('evidence', [])}
    return (refs-old_refs) & reviewed, queries, edges, ([] if queries else ['missing_post_review_scoped_lookup'])


def mechanism_evidence(state, operations, result, fixture=None):
    reviews = state.get('review_history', [])
    candidates = {c['candidate_id']: c for c in state.get('candidate_history', [])}
    outputs = {o.get('key'): o.get('result') for o in operations if o.get('status', 'completed')=='completed'}
    observations = state.get('observations', [])
    current = state.get('candidate') or {}
    grant = state.get('eligibility') or {}
    last = state.get('review') or {}
    bound = bool(result and result.get('reviewed') and result.get('outcome')=='full_answer'
        and result.get('answer')==current.get('answer')
        and result.get('candidate_id')==current.get('candidate_id')==grant.get('candidate_id')==last.get('candidate_id')
        and result.get('bundle_id')==current.get('bundle_id')==grant.get('bundle_id')==last.get('bundle_id')
        and result.get('review_id')==last.get('review_id')==grant.get('review_id')
        and current.get('content_hash')==digest(current.get('answer'))==grant.get('candidate_hash')
        and last.get('complete') and not last.get('registered_findings') and not state.get('findings') and not state.get('program_issues'))
    failed = [r for r in reviews if not r.get('complete') or r.get('registered_findings')]
    chains = []
    for old in failed:
        pending = {f['finding_id'] for f in old.get('registered_findings', [])}
        if not pending:
            continue
        before = candidates.get(old['candidate_id'], {})
        for new in reviews[reviews.index(old)+1:]:
            responses = {d['finding_id']: d for d in new.get('dispositions', [])}
            disposed = bool(new.get('complete') and pending <= responses.keys()
                and all(responses[f]['status'] in {'resolved','not_applicable'} for f in pending))
            after = candidates.get(new['candidate_id'], {})
            rewrite = bool(before and after and before.get('candidate_id')!=after.get('candidate_id')
                and before.get('content_hash')!=after.get('content_hash'))
            lookup_refs = set()
            query_keys = []
            for observation in observations:
                if observation.get('reused') or observation.get('query_origin') not in {'finding:'+f for f in pending}:
                    continue
                raw = outputs.get(observation['operation_key']) or {}
                data = raw.get('data') or {}
                from backend.evidence_bundle import _knowledge_rows
                lookup_refs.update(row['evidence_id'] for row in _knowledge_rows(observation['operation_key'], data.get('evidence', [])))
                query_keys.append(observation['operation_key'])
            # 只有当前受审材料中存在、并被关闭判定引用的新材料才证明补查用于复审。
            inputs = [o.get('arguments', {}).get('payload', {}) for o in operations if o.get('kind')=='model']
            old_inputs = [p for p in inputs if (p.get('candidate') or {}).get('candidate_id')==old['candidate_id']]
            new_inputs = [p for p in inputs if (p.get('candidate') or {}).get('candidate_id')==new['candidate_id']]
            old_refs = {e['evidence_id'] for p in old_inputs for e in p.get('evidence', [])}
            reviewed_refs = {e['evidence_id'] for p in new_inputs for e in p.get('evidence', [])}
            lookup_refs = (lookup_refs - old_refs) & reviewed_refs
            edges, missing = [], []
            if state.get('evidence_contract_version') == 'evidence-v9':
                lookup_refs, query_keys, edges, missing = qualified_lookup(state, operations, old, new, pending)
            closure_refs = {ref for f in pending for ref in responses.get(f, {}).get('evidence_refs', [])}
            candidate_refs = {ref for b in after.get('blocks', []) for ref in b['support_refs']}
            used_new = bool(lookup_refs & closure_refs & candidate_refs)
            chains.append(dict(failed_review_id=old['review_id'], review_id=new['review_id'],
                candidate_changed=rewrite, finding_ids=sorted(pending), old_findings_disposed=disposed,
                post_review_lookup_operations=query_keys, new_evidence_used=used_new,
                provenance_edges=edges, missing_proof=missing,
                released_candidate_binding=bound and new['review_id']==last.get('review_id'),
                rewrite_complete=disposed and rewrite and bound and new['review_id']==last.get('review_id'),
                lookup_complete=disposed and rewrite and used_new and bound and new['review_id']==last.get('review_id')))
    adaptive_refs = set()
    from backend.evidence_bundle import _knowledge_rows
    for observation in observations:
        if not observation.get('reused') and str(observation.get('query_origin','')).startswith('goal:'):
            raw = outputs.get(observation['operation_key']) or {}
            adaptive_refs.update(row['evidence_id'] for row in _knowledge_rows(observation['operation_key'], (raw.get('data') or {}).get('evidence', [])))
    current_review_refs = {e['evidence_id'] for o in operations for e in o.get('arguments', {}).get('payload', {}).get('evidence', [])
        if (o.get('arguments', {}).get('payload', {}).get('candidate') or {}).get('candidate_id')==current.get('candidate_id')}
    current_candidate_refs = {ref for b in current.get('blocks', []) for ref in b['support_refs']}
    adaptive = bool(adaptive_refs & current_review_refs & current_candidate_refs)
    evidence = dict(version='loop-mechanism-v2' if state.get('evidence_contract_version')=='evidence-v9' else 'loop-mechanism-v1', evidence_class='engineering_chain_not_semantic',
        failed_review_seen=bool(failed), candidate_versions=len(candidates), reviews_executed=len(reviews),
        released_candidate_binding=bound, adaptive_evidence=adaptive and bound and not failed,
        review_rewrite=any(c['rewrite_complete'] for c in chains), review_lookup=any(c['lookup_complete'] for c in chains),
        chains=chains, human_semantic_review='pending',
        activity=dict(reviewed_repair_candidate_versions=len({r['candidate_id'] for r in reviews if r.get('input_findings')}),
            finding_lookup_observations=sum(str(o.get('query_origin','')).startswith('finding:') for o in observations),
            review_execution_recovery_used=state.get('review_execution_recovery_used', 0),
            protocol_feedback_operations=sum(o.get('kind')=='loop_protocol' and ':protocol-phase:' in o.get('key','')
                and 'feedback_input' in (o.get('result') or {}) for o in operations)))
    if fixture:
        expected = fixture['mechanism']
        if expected not in KINDS:
            raise ValueError('unknown_mechanism')
        reached = fixture.get('injection_reached', False)
        evidence['controlled_fixture'] = fixture
        evidence['injection_reached'] = reached
        evidence['post_review_action'] = 'lookup' if evidence['review_lookup'] else 'rewrite' if evidence['review_rewrite'] else None
        evidence['new_evidence_in_review'] = any(c['new_evidence_used'] for c in chains)
        evidence['old_findings_disposed'] = any(c['old_findings_disposed'] for c in chains)
        evidence['acceptance'] = ('not_covered' if not reached or expected=='review_lookup' and evidence['adaptive_evidence'] else
            'mechanism_pass' if evidence[expected] else 'mechanism_failed')
    return evidence
