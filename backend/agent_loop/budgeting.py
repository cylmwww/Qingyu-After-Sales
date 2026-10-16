"""预算预览与执行准入共用计数；预览不预留、不授予执行许可。"""
from backend.agent_model import structured_input_tokens
from backend.agent_loop import ablation


def estimate(host, budget, calls, auxiliary=None, counter=structured_input_tokens):
    calls = ablation.closure_calls(host, calls)
    sizes = [counter(role,payload,schema,prompt) for role,payload,schema,limit,prompt in calls]
    required = sum(size+call[3] for size,call in zip(sizes,calls))
    if any(c[0]=='agent_draft' for c in calls):
        required += 2*host.settings.max_output_tokens
    auxiliary = auxiliary or dict(models=0,tokens=0,roles={})
    required += auxiliary['tokens']
    count = len(calls)+auxiliary['models']
    from backend.agent_loop.contracts import evidence_contract
    admitted = (budget['models']+count<=host.config.task_models
        and budget['request_models']+count<=host.config.request_models
        and max(budget['reserved_tokens'],budget['request_tokens'])+required<=host.config.token_budget
        and (not evidence_contract(host) or all(size<=host.settings.input_token_budget for size in sizes)))
    return dict(admitted=admitted,required=dict(models=count,tokens=required),auxiliary=auxiliary,
                inputs=[dict(role=c[0],tokens=size) for c,size in zip(calls,sizes)])


def preview(host, state, budget, payload):
    from backend.agent_loop import context
    from backend.agent_loop.contracts import decision_schema,draft_schema,review_schema
    from backend.knowledge import token_count
    # 固定上界计入当前与未来决策；实际附加视图超过该界即拒绝而非少计。
    allowance = 512
    draft_state = dict(state,pending_action=dict(limitations=[]))
    draft = context.draft_input(host,draft_state)
    candidate = dict(candidate_id='reserved-candidate',blocks=[],answer='x'*host.settings.max_output_tokens,
                     bundle_id=state['bundle'].get('bundle_id'))
    review = context.review_input(host,draft_state,candidate)
    d = ('agent_decision',payload,decision_schema(host),host.settings.loop_config.decision_tokens,context.prompt(host))
    closing = [('agent_draft',draft,draft_schema(host),min(host.settings.max_output_tokens,host.review.repair_tokens)
                if state.get('repair_episodes') else host.settings.max_output_tokens,context.prompt(host)),
               ('agent_review',review,review_schema(host),host.review.review_tokens,context.review_prompt(host))]
    direct = estimate(host,budget,[d,*closing],dict(models=0,tokens=allowance,roles={}))
    parent = estimate(host,budget,[d,d,*closing],dict(models=0,tokens=2*allowance,roles={}))
    # 未知查询按原400字符上限估计；增强检索配置须在执行时取得实际辅助成本。
    auxiliary_known = host.settings.answer_config is None and host.settings.retrieval_config is None
    search = estimate(host,budget,[d,d,*closing],dict(models=1,tokens=2*allowance+1600+
        token_count(host.settings.encode_query('')),roles={})) if auxiliary_known else None
    used=max(budget['reserved_tokens'],budget['request_tokens'])
    def brief(value, tool=False):
        if value is None:
            return dict(fits_now=None)
        input_fits = all(i['tokens'] + (allowance if i['role']=='agent_decision' else 0)
                         <= host.settings.input_token_budget for i in value['inputs'])
        tool_fits = not tool or (budget['tools'] < host.config.task_tools
                                and budget['request_tools'] < host.config.request_tools)
        return dict(estimated_tokens=value['required']['tokens'],fits_now=value['admitted'] and input_fits and tool_fits)
    value=dict(token_limit=host.config.token_budget,remaining_tokens=max(0,host.config.token_budget-used),
        remaining_models=min(host.config.task_models-budget['models'],host.config.request_models-budget['request_models']),
        remaining_tools=min(host.config.task_tools-budget['tools'],host.config.request_tools-budget['request_tools']),
        remaining_repairs=max(0,host.review.max_rounds-state.get('repair_episodes',0)),
        includes_current_decision=True,provisional=True,grants_execution=False,
        draft=brief(direct),read_then_draft=brief(parent,tool=True),search_then_draft=brief(search,tool=True))
    import json
    if token_count(json.dumps(value,ensure_ascii=False,separators=(',',':')))+16>allowance:
        from backend.agent_store import AgentError
        raise AgentError('budget_preview_capacity_exceeded')
    return value
