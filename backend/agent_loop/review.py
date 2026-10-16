"""候选全文检查、严格审核身份与程序生成的发布凭据。"""
import re
from uuid import uuid4

from backend.agent_store import AgentError
from backend.evidence_review import program_checks
from backend.knowledge import digest
from backend.agent_loop.contracts import Draft, Review, VERSION


def candidate_from(draft, state):
    import json
    from backend.agent_loop.contracts import ConditionsDraft
    schema = ConditionsDraft if state.get('state_schema_version') == 'loop-state-conditions-v4' else Draft
    draft = schema.model_validate_json(json.dumps(draft, ensure_ascii=False)).model_dump(mode='json')
    mapping = {e['evidence_id']: e['citation_id'] for e in state['bundle']['items']}
    goals = {g['goal_id'] for g in state['goals']}
    parts = []
    for block in draft['blocks']:
        if not set(block['support_refs']) <= mapping.keys() or not set(block['goal_ids']) <= goals:
            raise AgentError('loop_candidate_invalid_reference')
        # S 编号只能由本 bundle 生成，模型不能自行插入旧编号。
        if re.search(r'\[S\d+\]', block['text']):
            raise AgentError('loop_candidate_embedded_citation')
        parts.append(block['text'] + ''.join(' [' + mapping[r] + ']' for r in block['support_refs']))
    answer = '\n\n'.join(parts)
    return dict(draft, candidate_id=str(uuid4()), revision=state['revision'], goal_version=state['goal_version'],
        bundle_id=state['bundle']['bundle_id'], answer=answer, content_hash=digest(answer),
        blocks_hash=digest(draft['blocks']))


def candidate_problems(candidate, state):
    problems = program_checks(state['route']['question'], state['route']['facts'], candidate['answer'],
                          state['bundle']['items'], 'post')
    if state.get('state_schema_version') == 'loop-state-conditions-v4':
        from backend.agent_loop.conditions import coverage_problems
        problems += coverage_problems(candidate['condition_coverage'], state['condition_view'])
    return problems


def validate_candidate(candidate, state, *, check_quality=True):
    raw = {'blocks':candidate['blocks']}
    if state.get('state_schema_version') == 'loop-state-conditions-v4':
        raw['condition_coverage'] = candidate['condition_coverage']
    rebuilt = candidate_from(raw, state)
    for key in ['revision', 'goal_version', 'bundle_id', 'answer', 'content_hash', 'blocks_hash']:
        if candidate[key] != rebuilt[key]:
            raise AgentError('loop_candidate_binding_mismatch')
    if check_quality and candidate_problems(candidate, state):
        raise AgentError('loop_program_check_failed')


def validate_review(review, candidate, state):
    import json
    if state.get('review_binding_version')=='call-bound-v5':
        from backend.agent_loop.recovery import review_binding
        if review.get('call_binding') != review_binding(state):
            raise AgentError('loop_review_call_binding_stale')
    if state.get('state_schema_version') in {'loop-state-recovery-v3', 'loop-state-conditions-v4'}:
        from backend.agent_loop.contracts import RecoveryReview
        from backend.agent_loop.recovery import normalized_review
        raw = {k:review[k] for k in RecoveryReview.model_fields}
        if review['complete'] != normalized_review(raw)['complete']:
            raise AgentError('loop_review_execution_mismatch')
    value = Review.model_validate_json(json.dumps({k:review[k] for k in Review.model_fields if k in review})).model_dump()
    if value['candidate_id'] != candidate['candidate_id'] or value['bundle_id'] != candidate['bundle_id']:
        raise AgentError('loop_review_binding_mismatch')
    if review.get('candidate_hash') != candidate['content_hash'] or review.get('blocks_hash') != candidate['blocks_hash']:
        raise AgentError('loop_review_content_mismatch')
    for f in value['findings']:
        if (f['block_id'] is not None and f['block_id'] not in {b['block_id'] for b in candidate['blocks']} or
            f['goal_id'] is not None and f['goal_id'] not in {g['goal_id'] for g in state['goals']} or
            not set(f['evidence_refs']) <= {e['evidence_id'] for e in state['bundle']['items']}):
            raise AgentError('loop_finding_invalid_reference')
    pending = {f['finding_id'] for f in review.get('input_findings', state.get('findings', []))}
    dispositions = value['dispositions']
    ids = [d['finding_id'] for d in dispositions]
    if len(ids) != len(set(ids)) or not set(ids) <= pending or (value['complete'] and set(ids) != pending):
        raise AgentError('loop_disposition_coverage')
    for d in dispositions:
        if (d['block_id'] is not None and d['block_id'] not in {b['block_id'] for b in candidate['blocks']} or
            not set(d['evidence_refs']) <= {e['evidence_id'] for e in state['bundle']['items']}):
            raise AgentError('loop_disposition_invalid_reference')


def remaining_findings(review, state):
    """只由完整审核侧判定关闭，原 Review 保留不可变，旧 finding 身份不重新编号。"""
    closed = {d['finding_id'] for d in review.get('dispositions', []) if d['status'] != 'remains'} if review['complete'] else set()
    pending = [f for f in state.get('findings', []) if f['finding_id'] not in closed]
    added = [dict(f, finding_id='f-'+str(uuid4()), review_id=review['review_id'],
        candidate_id=review['candidate_id'], bundle_id=review['bundle_id']) for f in review['findings']]
    return pending+added


def eligibility(state, tid, attempt):
    c, r = state['candidate'], state['review']
    if (not r['complete'] or r['findings'] or r.get('registered_findings') or
        any(d['status']=='remains' for d in r.get('dispositions', [])) or state.get('findings') or state.get('program_issues')):
        raise AgentError('loop_unreviewed_release_forbidden')
    value = dict(task_id=tid, attempt=attempt, revision=state['revision'], state_version=state['state_version'],
        candidate_id=c['candidate_id'], candidate_hash=c['content_hash'], blocks_hash=c['blocks_hash'],
        bundle_id=c['bundle_id'], bundle_hash=digest(state['bundle']), goal_version=state['goal_version'],
        review_id=r['review_id'], review_hash=digest(r), coverage='full', gate_version='loop-release-v3' if state.get('state_schema_version')=='loop-state-recovery-v3' else 'loop-release-v1', runtime_version=VERSION)
    if state.get('state_schema_version') == 'loop-state-conditions-v4':
        value.update(gate_version='loop-release-v4', condition_view_hash=digest(state['condition_view']),
            condition_coverage_hash=digest(c['condition_coverage']))
    if state.get('obligation_contract_version') == 'obligations-v10':
        value.update(gate_version='loop-release-v10', obligation_hash=digest({
            k:state.get(k) for k in ('evidence_bindings','evidence_requirements','repair_obligations')}))
    return dict(value, eligibility_id=digest(value))
