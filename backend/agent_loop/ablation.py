"""显式 v7 消融：固定取证、动态取证、单次审核、有限纠错。"""
from backend.agent_store import AgentError
from backend.knowledge import digest
from backend.plan_contract import INTENT_REQUIRED_TOOLS


def mode(host):
    return host.settings.loop_config.ablation


def has_review(host):
    return mode(host) not in {'E2', 'E3'}


def closure_calls(host, calls):
    from backend.agent_loop.contracts import advisory_contract
    return [c for c in calls if c[0] != 'agent_review' or (has_review(host) and not advisory_contract(host))]


def fixed_decision(state):
    """只由意图选择固定序列；不凭观察或模型扩展取证路径。"""
    required = INTENT_REQUIRED_TOOLS[state['route']['intent']['intent']]
    sequence = [t for t in ('get_order', 'get_after_sales', 'get_logistics',
        'read_policy_context', 'calculate_refund', 'search_knowledge') if t in required]
    attempted = {o['tool'] for o in state['observations']}
    tool = next((t for t in sequence if t not in attempted), None)
    if tool is None:
        return None
    return dict(based_on_state_version=state['state_version'], goal_proposals=[], assessment_updates=[],
        action=dict(kind='tool', tool=tool,
            arguments={'mode':'original'} if tool=='search_knowledge' else {'order_id_ref':'fact:order_id'},
            targets=['g-request'], expected_information='固定流程必要取证', evidence_basis=[]))


def validate_fixed_action(decision):
    if decision['action']['kind'] == 'tool':
        raise AgentError('ablation_fixed_extra_tool_forbidden')


def unreviewed_eligibility(host, state, tid, attempt):
    """不生成假审核；未审核实验发布仍绑定实际候选、证据和程序门禁。"""
    from backend.agent_loop.review import validate_candidate
    from backend.agent_loop.state import readiness
    if mode(host) not in {'E2', 'E3'} or not host.config.experiment:
        raise AgentError('ablation_unreviewed_release_forbidden')
    if state.get('review') or state.get('findings') or state.get('review_history') or readiness(state, state['bundle']):
        raise AgentError('ablation_unreviewed_state_invalid')
    validate_candidate(state['candidate'], state)
    c = state['candidate']
    value = dict(task_id=tid, attempt=attempt, revision=state['revision'], state_version=state['state_version'],
        candidate_id=c['candidate_id'], candidate_hash=c['content_hash'], blocks_hash=c['blocks_hash'],
        bundle_id=c['bundle_id'], bundle_hash=digest(state['bundle']), goal_version=state['goal_version'],
        reviewed=False, review_id=None, ablation=mode(host), gate_version='loop-ablation-unreviewed-v1')
    if 'condition_view' in state:
        value.update(condition_view_hash=digest(state['condition_view']),
            condition_coverage_hash=digest(c['condition_coverage']))
    return dict(value, eligibility_id=digest(value))


def release_eligibility(host, state, tid, attempt):
    from backend.agent_loop.contracts import advisory_contract
    if advisory_contract(host):
        from backend.agent_loop.advisory import eligibility
        return eligibility(state, tid, attempt)
    from backend.agent_loop.review import eligibility
    return eligibility(state, tid, attempt) if has_review(host) else unreviewed_eligibility(host, state, tid, attempt)
