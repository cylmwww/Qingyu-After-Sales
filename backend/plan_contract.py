"""工具前置条件、意图完成要求与计划校验的单一来源；提示与服务端校验共用。"""
from copy import deepcopy

TOOLS = ('search_knowledge', 'read_policy_context', 'get_order', 'get_after_sales', 'get_logistics', 'calculate_refund')

TOOL_CONTRACTS = {
    'search_knowledge': '查询政策/FAQ；使用原问题或审核给出的定向查询和冻结检索配置，无业务参数猜测。',
    'get_order': '必需已确认order_id；返回授权订单、商品与冻结支付份额。',
    'get_after_sales': '先取得订单；仅查询该订单售后/退款。无记录允许observed完成，但不能推断退款失败。',
    'get_logistics': '先取得订单；仅查询原订单出库物流与签收，不提供退件物流或资金去向。',
    'read_policy_context': '先取得订单；按业务事件选择精确版本的计算与品类完整条款。政策意图的定向补查且无订单时，可用已有证据chunk_ids读取完整条款。',
    'calculate_refund': '必须先取得get_order、get_after_sales、get_logistics、read_policy_context；退款预估仅此工具计算，不执行交易。'
}

INTENT_REQUIRED_TOOLS = {
    'refund_amount': {'get_order', 'get_after_sales', 'get_logistics', 'read_policy_context', 'calculate_refund'},
    'refund_progress': {'get_order', 'get_after_sales'},
    'logistics': {'get_order', 'get_logistics'},
    'order': {'get_order'},
    'policy': {'search_knowledge'},
    'unsupported': set(),
}

TASK_COMPLETION_REQUIREMENTS = {
    'refund_amount': '必须calculate_refund成功，缺输入则澄清；不是用模型算术',
    'refund_progress': '必须get_after_sales取得记录或明确无记录；settled字段才是到账',
    'logistics': '必须get_logistics；退件物流未知，不能冒用出库物流',
}

ORDER_DEPENDENT_TOOLS = {'get_after_sales', 'get_logistics', 'read_policy_context', 'calculate_refund'}
CALCULATION_INPUTS = {'get_after_sales', 'get_logistics', 'read_policy_context'}


def planned_query(issue):
    """审核 lookup 问题的知识查询：优先明确 query，其次结论，最后目标说明。"""
    for field in ('query', 'conclusion', 'detail'):
        value = issue.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ''


def planner_rules():
    """模型可见规则由契约常量生成，避免与服务端校验分叉。"""
    return ('\nPlanner按执行顺序使用唯一的step1至stepN，步骤id必须从step1开始连续编号，不能跳号或乱序。'
            'depends_on是字符串数组，只能填写该步骤之前已经出现的步骤id；第一步为[]。禁止填写本步骤id，禁止填写尚未出现的步骤id。\n'
            '例如计算退款是第5步时写["step1","step2","step3","step4"]，不能写["step1","step2","step3","step5"]，也不能写成自依赖。\n'
            'get_after_sales/get_logistics/read_policy_context须先有get_order成功；calculate_refund须先有'
            'get_order/get_after_sales/get_logistics/read_policy_context结果。\n'
            '例外：政策意图的定向补查且没有订单时，read_policy_context可直接读取已有证据的完整条款，不需要先查订单。\n'
            'results目录可能包含失败、无结果或无权状态；只有status=success的结果才能满足任务完成要求，目录中的未知业务事实仍然未知。\n'
            '重规划不得删除任务完成所需工具来缩短计划；工具与参数由服务端按计划绑定执行。')


def validate_plan(plan, intent, path, prior_success_tools=(), correction_lookup=False):
    """服务端计划校验；抛出 ValueError(code)，由调用方转换为 AgentError。"""
    steps = plan['steps']
    expected = ['step' + str(i) for i in range(1, len(steps) + 1)]
    if [step['id'] for step in steps] != expected:
        raise ValueError('plan_step_sequence_invalid')
    if path == 'fast':
        return
    required = INTENT_REQUIRED_TOOLS.get(intent, {'search_knowledge'})
    before = set(prior_success_tools)
    if not required <= ({step['tool'] for step in steps} | before):
        raise ValueError('plan_missing_task_completion_tools')
    # 依赖按实际已满足的工具判断，而不是按“曾经调用过工具名”。
    for step in steps:
        tool = step['tool']
        policy_lookup = tool == 'read_policy_context' and correction_lookup and intent == 'policy'
        if tool in ORDER_DEPENDENT_TOOLS and 'get_order' not in before and not policy_lookup:
            raise ValueError('plan_business_dependency_missing')
        if tool == 'calculate_refund' and not CALCULATION_INPUTS <= before:
            raise ValueError('plan_calculation_dependency_missing')
        before.add(tool)


def payload_contract(intent, prior_catalog_note=None):
    """Planner载荷中的契约字段，与validate_plan使用同一份常量。"""
    payload = {'tool_contracts': deepcopy(TOOL_CONTRACTS),
               'required_tools': sorted(INTENT_REQUIRED_TOOLS.get(intent, {'search_knowledge'})),
               'result_contract': 'results只含工具、状态、错误和证据条数，不含政策正文或证据ID；'
                                  '失败、无结果和无权状态都会出现，只有success才满足任务完成要求；'
                                  '完整结果由服务端保留用于参数、回答和审核，不得从目录猜测缺失业务事实。',
               'task_completion_requirements': TASK_COMPLETION_REQUIREMENTS.get(intent),
               'step_rules': planner_rules()}
    if prior_catalog_note:
        payload['prior_results_contract'] = prior_catalog_note
    return payload
