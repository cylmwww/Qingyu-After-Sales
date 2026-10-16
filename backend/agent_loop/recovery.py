"""v3 输出协议与审核执行恢复；不改变旧版本的处理策略。"""
from backend.agent_store import AgentError
from backend.agent_loop.contracts import RecoveryReview


class OutputContractError(AgentError):
    category = 'semantic_contract'

    def __init__(self, code, feedback=None):
        super().__init__(code)
        self.validation_feedback = feedback or [{'type': code, 'loc': ['output_contract']}]


def reference_catalog(state):
    extra = {}
    if state.get('state_schema_version') == 'loop-state-conditions-v4':
        view = state.get('condition_view', {})
        extra = dict(condition_facts=sorted(view.get('provided_facts', {})),
            derived_conditions=[r['condition_id'] for r in view.get('derived_conditions', [])],
            policy_context_evidence=[e['evidence_id'] for e in state['bundle']['items'] if e.get('kind') == 'knowledge'])
    return dict(**extra,
        fact_arguments=['fact:order_id'] if state['route']['facts'].get('order_id') else [],
        evidence=[e['evidence_id'] for e in state['bundle']['items']],
        goals=[g['goal_id'] for g in state['goals']],
        open_goals=[g['goal_id'] for g in state['goals'] if state['assessments'].get(g['goal_id'], {}).get('status', 'open') in {'open', 'conflicted'}],
        pending_findings=[f['finding_id'] for f in state.get('findings', [])],
        unavailable_observations=[o['observation_id'] for o in state['observations'] if o['status'] in {'no_result', 'missing_input', 'permanent_failure'}])


def normalized_review(output):
    import json
    value = RecoveryReview.model_validate_json(json.dumps(output)).model_dump(mode='json')
    return dict(value, complete=value['execution']['status']=='completed')


def validate_output_review(output, candidate, state):
    from backend.agent_loop.review import validate_review
    value = normalized_review(output)
    issues = [{'type':'identity_mismatch', 'loc':[field], 'received':value[field], 'expected':candidate[field]}
              for field in ('candidate_id','bundle_id') if value[field]!=candidate[field]]
    visible = {e['evidence_id'] for e in state['bundle']['items']}
    for i, finding in enumerate(value['findings']):
        invalid = sorted(set(finding['evidence_refs'])-visible)
        if invalid:
            issues.append(dict(type='invalid_evidence_reference',loc=['findings',i,'evidence_refs'],
                received=invalid,allowed=sorted(visible)))
    if issues:
        raise OutputContractError('loop_review_binding_mismatch' if any(i['type']=='identity_mismatch' for i in issues) else 'loop_finding_invalid_reference', issues[:8])
    execution = value['execution']
    pools = dict(unchecked_blocks={b['block_id'] for b in candidate['blocks']},
        unchecked_goals={g['goal_id'] for g in state['goals']},
        unchecked_findings={f['finding_id'] for f in state.get('findings', [])})
    for field, allowed in pools.items():
        refs = execution[field]
        if len(refs)!=len(set(refs)) or not set(refs)<=allowed:
            raise AgentError('loop_review_unchecked_reference')
    validate_review(dict(value, candidate_hash=candidate['content_hash'], blocks_hash=candidate['blocks_hash'],
        **({'call_binding':review_binding(state)} if state.get('review_binding_version')=='call-bound-v5' else {})), candidate, state)


def validate_output_draft(output, state):
    from backend.agent_loop.review import candidate_from
    candidate = candidate_from(output, state)
    blocks = {b['block_id'] for b in candidate['blocks']}
    for response in state['pending_action'].get('finding_responses', []):
        if response.get('block_id') and response['block_id'] not in blocks:
            raise AgentError('loop_response_invalid_block')


def review_transition(state, new, passed, max_rounds):
    """审核恢复占原修复额度；停止时不虚增阶段。"""
    if passed:
        return None
    review = new['review']
    execution = review['execution']
    if len(new['findings'])>6:
        return 'finding_capacity_exceeded'
    incomplete = execution['status']=='incomplete'
    if incomplete and execution['reason']!='not_finished':
        return 'review_incomplete_'+execution['reason']
    if state['repair_episodes']>=max_rounds:
        return 'repair_limit'
    if incomplete and not new['findings'] and not state['program_issues']:
        if state.get('review_execution_recovery_used', 0):
            return 'review_execution_recovery_exhausted'
        new.update(phase='review_pending', review_execution_recovery_used=1,
            review_sequence=state.get('review_sequence', 0)+1)
    else:
        new.update(phase='decide', pending_action=None, decision_input=None, phase_tool_actions=0)
    new['repair_episodes'] = state['repair_episodes']+1
    return None


def review_binding(state):
    from backend.knowledge import digest
    c=state['candidate']
    return dict(version='call-bound-v5', revision=state['revision'], goal_version=state['goal_version'],
        candidate_id=c['candidate_id'], bundle_id=c['bundle_id'], candidate_hash=c['content_hash'],
        blocks_hash=c['blocks_hash'], bundle_hash=digest(state['bundle']),
        pending_findings_hash=digest(state.get('findings', [])))


def bind_review_output(output, binding, state):
    import json
    from backend.agent_loop.contracts import CallBoundReview
    if binding != review_binding(state):
        raise AgentError('loop_review_call_binding_stale')
    value=CallBoundReview.model_validate_json(json.dumps(output)).model_dump(mode='json')
    return dict(value, candidate_id=binding['candidate_id'], bundle_id=binding['bundle_id'])
