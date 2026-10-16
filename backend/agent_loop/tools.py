"""单行动工具准入；业务计算及检索完全复用现有实现。"""
from backend.agent_store import AgentError
from backend.plan_contract import ORDER_DEPENDENT_TOOLS, CALCULATION_INPUTS
from backend.agent_loop.state import completed_tools
from backend.agent_loop.contracts import bound_contract, evidence_contract, obligation_contract

# 现有业务服务的确定性人工/适用条件边界，区别于技术故障。
from backend.agent_loop.failures import BUSINESS_BLOCKS


async def auxiliary_cost(host, tool, args):
    """冻结配置下的保守准入预测，不写实际账本；未知召回按授权池上界估算。"""
    from backend.knowledge import token_count
    from backend.query import REWRITE_PROMPT
    from backend.rerank import document_text
    costs = {}
    if tool != 'search_knowledge':
        return dict(models=0, tokens=0, roles=costs)
    s, query = host.settings, args['query']
    profile = s.retrieval_config
    rewrite = s.answer_config.rewrite if s.answer_config and s.answer_config.rewrite.enabled else None
    if rewrite:
        prompt = REWRITE_PROMPT + f'\n最多{rewrite.max_terms}项。'
        costs['query_rewrite'] = dict(models=1, tokens=token_count(prompt)+token_count(query)+16+rewrite.max_output_tokens)
    if profile is None or profile.mode in {'vector', 'hybrid'}:
        # 原问题始终查询。抽取词尚未生成，按允许的字符数估计第二路上界。
        second = (token_count(s.encode_query('')) + rewrite.max_terms*201*4) if rewrite else 0
        costs['embedding'] = dict(models=1+int(bool(rewrite)), tokens=token_count(s.encode_query(query))+second)
    if profile and profile.rerank.enabled:
        rows = await host.db.chunk_rows(host.index['id'], {})
        largest = sorted((token_count(document_text(r)) for r in rows), reverse=True)[:profile.rerank.candidates]
        if largest:
            costs['rerank'] = dict(models=1, tokens=token_count(query)+sum(largest))
    attempts = s.model_retry_count+1
    return dict(models=sum(c['models'] for c in costs.values())*attempts,
                tokens=sum(c['tokens'] for c in costs.values())*attempts, roles=costs, attempts=attempts)


def bind(host, state):
    action, route = state['pending_action'], state['route']
    tool, args = action['tool'], action['arguments']
    completed = completed_tools(state)
    if tool == 'search_knowledge':
        if not any(o['tool'] == tool for o in state['observations']):
            if bound_contract(host):
                if args != {'mode':'original'}:
                    raise AgentError('loop_first_search_requires_original_mode')
            elif args['query'] != host.task['question'] or args.get('gap_ref'):
                raise AgentError('first_search_requires_original_question')
            action['query_origin'] = 'original'
            return {'query':host.task['question']}
        if bound_contract(host) and args.get('mode') != 'targeted':
            raise AgentError('loop_later_search_requires_targeted_mode')
        gap = args.get('gap_ref')
        if evidence_contract(host):
            from backend.agent_loop.recovery import reference_catalog
            catalog = reference_catalog(state)
            findings = set(catalog['pending_findings'])
            goals = set(catalog['open_goals']) & set(action['targets'])
            if obligation_contract(host):
                from backend.agent_loop.obligations import search_goals, validate_search
                goals |= set(search_goals(state)) & set(action['targets'])
                validate_search(state, gap)
        else:
            findings = {f['finding_id'] for f in state.get('findings', [])}
            goals = {g['goal_id'] for g in state['goals'] if g['goal_id'] in action['targets'] and
                state['assessments'].get(g['goal_id'], {}).get('status', 'open') in {'open','conflicted'}}
        if gap not in findings | goals or len(args['query']) > 400:
            raise AgentError('loop_targeted_query_requires_registered_gap')
        action['query_origin'] = ('finding:' if gap in findings else 'goal:') + gap
        return {'query':args['query']}
    if not route['intent']['needs_business']:
        if tool != 'read_policy_context' or 'chunk_refs' not in args:
            raise AgentError('conditional_policy_business_access_forbidden')
        mapping = {e['evidence_id']:e['chunk_id'] for e in state['bundle']['items'] if e['document_id'] != 'MOCK-BUSINESS'}
        if not set(args['chunk_refs']) <= mapping.keys():
            raise AgentError('loop_invalid_chunk_reference')
        return {'chunk_ids':[mapping[r] for r in args['chunk_refs']]}
    if args.get('order_id_ref') != 'fact:order_id' or not route['facts'].get('order_id'):
        raise AgentError('loop_confirmed_order_required')
    needed = {'get_order'} if tool in ORDER_DEPENDENT_TOOLS else set()
    if tool == 'calculate_refund':
        needed |= CALCULATION_INPUTS
    if not needed <= completed:
        raise AgentError('missing_dependency:' + ','.join(sorted(needed-completed)))
    return host.tool_arguments(tool, route)
