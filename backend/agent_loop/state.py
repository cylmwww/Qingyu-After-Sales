"""纯状态转换：模型不能修改事实、观察、审核或发布状态。"""
from copy import deepcopy
from uuid import uuid4

from backend.agent_store import AgentError
from backend.knowledge import digest
from backend.plan_contract import INTENT_REQUIRED_TOOLS
from backend.review_contract import CLARIFIABLE_FIELDS


def initial(route, revision, question):
    return dict(revision=revision, state_version=0, phase='decide', route=route,
        goals=[dict(goal_id='g-request', text=question, origin='user_request', required=True,
                    request_anchor=[0, len(question)])],
        assessments={}, observations=[], applied_operations=[], pending_action=None,
        goal_version=1, no_progress=0, tool_actions=0, repair_episodes=0,
        phase_tool_actions=0, findings=[], review_history=[], candidate_history=[],
        reviewed_responses=[], priority_origins=[],
        candidate=None, review=None, eligibility=None, bundle={})


def content_key(tool, args, result, scope):
    # 仅去除调用信封的易变字段；业务时间、版本和状态全部保留。
    return digest(dict(tool=tool, arguments=args, scope=scope,
        result={k:v for k,v in result.items() if k not in {'call_id', 'observed_at'}}))


def completed_tools(state):
    return {o['tool'] for o in state['observations'] if o['status'] == 'success' or
        (o['tool'] == 'get_after_sales' and o['status'] == 'no_result'
         and o.get('error_code') == 'no_after_sale_record')}


def rejection_feedback(error, decision, state):
    """有界字段诊断，不回显任意正文；整项拒绝，不清洗模型输出。"""
    visible = {e['evidence_id'] for e in state['bundle'].get('items', [])}
    unavailable = {o['observation_id'] for o in state['observations']
                   if o['status'] in {'no_result','missing_input','permanent_failure'}}
    issues = []
    def check(path, refs, allowed, kinds):
        invalid = sorted(set(refs)-allowed)
        if invalid:
            issues.append(dict(field=path, invalid_refs=[r[:100] for r in invalid[:8]], allowed_types=kinds))
    for i,update in enumerate(decision.get('assessment_updates', [])):
        allowed = visible | unavailable if update['status']=='unavailable' else visible
        for field in ('support_refs','conflict_refs'):
            check(f'assessment_updates[{i}].{field}',update[field],allowed,
                  ['visible_evidence_id','unavailable_observation_id'] if update['status']=='unavailable' else ['visible_evidence_id'])
    action = decision.get('action', {})
    for field in ('evidence_basis','support_refs'):
        check('action.'+field,action.get(field, []),visible,['visible_evidence_id'])
    for i,response in enumerate(action.get('finding_responses', [])):
        check(f'action.finding_responses[{i}].support_refs',response['support_refs'],visible,['visible_evidence_id'])
    if not issues and 'search_requires_' in error.code:
        issues = [dict(field='action.arguments.mode', invalid_refs=[str(action.get('arguments', {}).get('mode', 'missing'))[:100]],
                       allowed_types=['original'] if not any(o['tool']=='search_knowledge' for o in state['observations']) else ['targeted'])]
    if not issues and error.code=='loop_targeted_query_requires_registered_gap':
        issues = [dict(field='action.arguments.gap_ref', invalid_refs=[str(action.get('arguments', {}).get('gap_ref', 'missing'))[:100]],
                       allowed_types=['targeted_open_goal_id','pending_finding_id'])]
    if not issues and error.code=='loop_invalid_goal_reference':
        issues = [dict(field='goal_proposals/assessment_updates/action.targets', invalid_refs=[],
                       allowed_types=['visible_goal_id','valid_local_goal_id'])]
    if not issues:
        issues = [dict(field='action.arguments' if action.get('kind')=='tool' else 'decision',
                       invalid_refs=[], allowed_types=['current_schema_and_visible_references'])]
    return dict(reason=error.code, atomic_rejection=True, issues=issues[:8],
        reference_rule='fact:order_id only in arguments.order_id_ref; evidence_basis may be []',
        search_rule='first: mode=original, no query/gap_ref; later: mode=targeted with query and registered open gap_ref')


def accept(state, decision, visible_refs):
    if decision['based_on_state_version'] != state['state_version']:
        raise AgentError('loop_stale_state')
    new = deepcopy(state)
    goals = {g['goal_id'] for g in new['goals']}
    ids = {}
    for proposal in decision['goal_proposals']:
        if proposal['local_id'] in ids or proposal['parent_id'] not in goals:
            raise AgentError('loop_invalid_goal_reference')
        gid = 'g-' + str(uuid4())
        ids[proposal['local_id']] = gid
        goals.add(gid)
        new['goals'].append(dict(proposal, goal_id=gid, required=True))
    if len(goals) > 8:
        raise AgentError('scope_capacity_exceeded')
    seen = set()
    for update in decision['assessment_updates']:
        value = deepcopy(update)
        gid = ids.get(value['goal_id'], value['goal_id'])
        if gid not in goals or gid in seen:
            raise AgentError('loop_invalid_goal_reference')
        seen.add(gid)
        refs = set(value['support_refs'] + value['conflict_refs'])
        unavailable = {o['observation_id'] for o in new['observations'] if o['status'] in {'no_result', 'missing_input', 'permanent_failure'}}
        allowed = visible_refs | unavailable if value['status'] == 'unavailable' else visible_refs
        if not refs <= allowed:
            raise AgentError('loop_invalid_evidence_reference')
        value.update(goal_id=gid, based_on_state=state['state_version'])
        new['assessments'][gid] = value
    action = deepcopy(decision['action'])
    target_field = {'tool':'targets', 'draft':'addressed_goals', 'clarify':'goal_ids', 'finish':'unresolved_goal_ids'}[action['kind']]
    action[target_field] = [ids.get(g, g) for g in action[target_field]]
    if not set(action[target_field]) <= goals:
        raise AgentError('loop_invalid_goal_reference')
    if not set(action.get('support_refs', []) + action.get('evidence_basis', [])) <= visible_refs:
        raise AgentError('loop_invalid_evidence_reference')
    if action['kind'] == 'clarify' and not set(action['fields']) <= set(CLARIFIABLE_FIELDS):
        raise AgentError('loop_invalid_clarification_field')
    if action['kind'] == 'draft':
        responses = action.get('finding_responses', [])
        pending = {f['finding_id'] for f in state.get('findings', [])}
        if {r['finding_id'] for r in responses} != pending or len(responses) != len(pending):
            raise AgentError('loop_finding_response_coverage')
        if any(not set(r['support_refs']) <= visible_refs for r in responses):
            raise AgentError('loop_invalid_evidence_reference')
        reuse = action.get('reuse_candidate_id')
        candidate = state.get('candidate')
        if reuse:
            if (not pending or not candidate or candidate['candidate_id'] != reuse or
                candidate['bundle_id'] != state['bundle']['bundle_id'] or
                candidate['goal_version'] != state['goal_version'] or
                decision['assessment_updates'] or decision['goal_proposals']):
                raise AgentError('loop_reuse_candidate_binding')
            if any(not r.get('block_id') or r['block_id'] not in {b['block_id'] for b in candidate['blocks']} for r in responses):
                raise AgentError('loop_response_location_required')
            if response_key(action, candidate) in state.get('reviewed_responses', []):
                raise AgentError('loop_repeated_finding_response')
    action.update(action_id=str(uuid4()), decision_id=str(uuid4()))
    new.update(pending_action=action, phase=action['kind'] + '_pending')
    if decision['goal_proposals'] or decision['assessment_updates']:
        new['goal_version'] += 1
    return new


def observe(state, key, tool, args, result, scope):
    if key in state['applied_operations']:
        return state, None
    new = deepcopy(state)
    identity = content_key(tool, args, result, scope)
    knowledge = tool in {'search_knowledge', 'read_policy_context'}
    keys = knowledge_keys(result) if knowledge else []
    known = {k for o in new['observations'] for k in o.get('knowledge_keys', [])}
    reused = (not (set(keys)-known)) if knowledge else any(o['content_key'] == identity for o in new['observations'])
    observation = dict(observation_id='o-' + str(uuid4()), operation_key=key, tool=tool,
        status=result['status'], content_key=identity, scope=scope, reused=reused,
        action_id=state['pending_action']['action_id'],
        error_code=(result.get('error') or {}).get('code'))
    observation['knowledge_keys'] = keys
    if knowledge and state['pending_action'].get('query_origin'):
        observation.update(query_origin=state['pending_action']['query_origin'],
                           query_purpose=state['pending_action']['expected_information'])
    if knowledge and not reused and state['observations']:
        new['priority_origins'] = [key]
    new['observations'].append(observation)
    new['applied_operations'].append(key)
    new['tool_actions'] += 1
    new['phase_tool_actions'] = state.get('phase_tool_actions', 0)+1
    new['no_progress'] = state['no_progress'] + 1 if reused else 0
    new.update(phase='decide', pending_action=None)
    return new, observation


def knowledge_keys(result):
    # query、排名、调用时间及观察编号变化不是新增知识；保留真实正文、版本和跨度。
    return sorted({digest({k:r.get(k) for k in ('chunk_id','document_id','document_version',
        'source_path','source_start','source_end','text','metadata')})
        for r in (result.get('data') or {}).get('evidence', [])})


def response_key(action, candidate):
    # 只换措辞、列表顺序或 finding 身份不能创造新的定位依据。
    return digest(dict(candidate_hash=candidate['content_hash'], bundle_id=candidate['bundle_id'],
        locations=sorted({(r.get('block_id') or '', tuple(sorted(r['support_refs'])))
                          for r in action.get('finding_responses', [])})))


def candidate_key(candidate):
    return digest([dict(text=' '.join(b['text'].split()), kind=b['kind'],
        goals=sorted(b['goal_ids']), refs=sorted(b['support_refs'])) for b in candidate['blocks']])


def draft_gate(state, bundle):
    reason = readiness(state, bundle)
    required = INTENT_REQUIRED_TOOLS[state['route']['intent']['intent']]
    missing = sorted(required - completed_tools(state))
    goals = [g['goal_id'] for g in state['goals'] if
        g['goal_id'] not in state['pending_action']['addressed_goals'] or
        state['assessments'].get(g['goal_id'], {}).get('status') != 'supported' or
        not set(state['assessments'].get(g['goal_id'], {}).get('support_refs', [])) <=
        {e['evidence_id'] for e in bundle['items']}]
    hard = next((o['status'] for o in state['observations'] if o['status'] in {'no_permission', 'missing_input'}), None)
    if hard:
        reason = 'permission_denied' if hard == 'no_permission' else 'clarification_not_supported_in_slice1'
    recoverable = reason in {'completion_requirements_missing', 'goal_not_supported'} or (
        reason == 'context_capacity_exceeded' and not bundle['incomplete'] and not bundle['items'])
    if hard or bundle['incomplete'] or state.get('stop_reason'):
        recoverable = False
    return dict(reason=reason, admitted=reason is None, recovery='decide' if recoverable else 'stop',
        missing_requirements=missing, unsupported_goals=goals, atomic_rejection=True,
        based_on_state_version=state['state_version'])


def readiness(state, bundle):
    action = state['pending_action']
    if action['coverage'] != 'full':
        return 'partial_answer_not_supported_in_slice1'
    required = INTENT_REQUIRED_TOOLS[state['route']['intent']['intent']]
    if not required <= completed_tools(state):
        return 'completion_requirements_missing'
    visible = {e['evidence_id'] for e in bundle['items']}
    if bundle['incomplete'] or not visible:
        return 'context_capacity_exceeded'
    for goal in state['goals']:
        a = state['assessments'].get(goal['goal_id'])
        if goal['goal_id'] not in action['addressed_goals'] or not a or a['status'] != 'supported' or not set(a['support_refs']) <= visible:
            return 'goal_not_supported'
    return None
