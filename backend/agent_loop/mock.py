"""输入敏感的模拟模型；仅验证机制，不能用作自然语义效果证据。"""
import json


def business(payload, tool):
    return [(e, json.loads(e['text'])) for e in payload['evidence'] if e['metadata'].get('tool') == tool]


def draft(payload):
    blocks = []
    intent = payload['intent']['intent']
    tools = {'order':['get_order'], 'logistics':['get_logistics'], 'refund_progress':['get_after_sales'],
             'refund_amount':['calculate_refund'], 'policy':[]}[intent]
    for tool in tools:
        for e, result in business(payload, tool):
            data = result.get('data') or {}
            if tool == 'calculate_refund' and 'total_estimated_cents' in data:
                cents = data['total_estimated_cents']
                text = f'预计退款 {cents//100}.{cents%100:02d} 元，仅为只读预估，不代表审批或到账'
            elif tool == 'get_order' and data.get('order'):
                text = '冻结快照中的订单状态为：' + data['order']['status']
            elif tool == 'get_logistics':
                text = '冻结快照中的原订单出库物流：' + json.dumps(data.get('logistics', []), ensure_ascii=False)
            else:
                text = '冻结快照中的售后与退款记录：' + json.dumps(data, ensure_ascii=False)
            blocks.append(dict(block_id='b'+str(len(blocks)+1), text=text,
                goal_ids=[g['goal_id'] for g in payload['goals']], support_refs=[e['evidence_id']], kind='claim'))
    if not tools:
        for e in payload['evidence'][:1]:
            blocks.append(dict(block_id='b1', text=e['text'][:1000], goal_ids=['g-request'],
                support_refs=[e['evidence_id']], kind='claim'))
    if not blocks:
        blocks = [dict(block_id='b1', text='缺少支持材料，无法核实结论', goal_ids=['g-request'], support_refs=[], kind='limitation')]
    value = {'blocks':blocks}
    if 'condition_view' in payload:
        view = payload['condition_view']
        value['condition_coverage'] = dict(used_fact_refs=sorted(set(view['required_conditions']) & set(view['provided_facts'])),
            unknown_fact_refs=view['unresolved_requirements'], derived_condition_refs=[r['condition_id'] for r in view['derived_conditions']],
            fact_scope='user_conditions', overall_eligibility='not_computed')
        summary = dict(provided={k:v['value'] for k,v in view['provided_facts'].items() if k in view['required_conditions']},
            time=[{k:r.get(k) for k in ('status','reason','elapsed_seconds','deadline','within_window')} for r in view['derived_conditions']],
            unknown=view['unresolved_requirements'])
        blocks[0]['text'] = blocks[0]['text'][:700] + '\n基于用户给定条件（不是授权记录核实）：' + json.dumps(summary, ensure_ascii=False) + '。时间满足不等于整体可退，整体资格未计算。'
    return value


def respond(role, payload):
    if role == 'agent_draft':
        return draft(payload)
    if role == 'agent_review':
        candidate = payload['candidate']
        expected = draft(payload)['blocks']
        findings = []
        pending = payload.get('pending_findings', [])
        if candidate['blocks'] != expected and not pending:
            findings.append(dict(kind='unsupported_claim', block_id=candidate['blocks'][0]['block_id'],
                goal_id=None, evidence_refs=[], detail='模拟审核：正文不符合当前实际观察的保守模板'))
        responses = {r['finding_id']:r for r in payload.get('finding_responses', [])}
        dispositions = []
        for f in pending:
            response = responses.get(f['finding_id'])
            supported = response and candidate['blocks']==expected and set(response['support_refs']) <= {e['evidence_id'] for e in payload['evidence']}
            dispositions.append(dict(finding_id=f['finding_id'],
                status=('not_applicable' if f['candidate_id']==candidate['candidate_id'] else 'resolved') if supported else 'remains',
                block_id=candidate['blocks'][0]['block_id'] if supported else None,
                evidence_refs=response['support_refs'] if supported else [], detail='依据当前正文、响应和实际观察核对的模拟判定'))
        value = dict(candidate_id=candidate['candidate_id'], bundle_id=payload['bundle_id'], findings=findings, dispositions=dispositions)
        if payload.get('review_contract') in {'recovery-v3','call-bound-v5'}:
            if payload['review_contract']=='call-bound-v5':
                value.pop('candidate_id');value.pop('bundle_id')
            from backend.agent_loop.contracts import REVIEW_DOMAINS
            return dict(value, execution=dict(status='completed', reason=None, checked_domains=list(REVIEW_DOMAINS),
                unchecked_blocks=[], unchecked_goals=[], unchecked_findings=[]))
        return dict(value, complete=True)
    observations = payload['observations']
    known = {o['tool'] for o in observations if o['status'] == 'success' or
             (o['tool'] == 'get_after_sales' and o['error_code'] == 'no_after_sale_record')}
    refs = [e['evidence_id'] for e in payload['evidence']]
    missing = set(payload['required_tools']) - known
    action = dict(kind='finish', reason='insufficient_evidence', unresolved_goal_ids=['g-request'])
    updates = []
    if missing:
        if 'get_order' in missing:
            tool = 'get_order'
        else:
            # 根据真实订单内容改变独立查询顺序，而不是按轮次输出下一条脚本。
            orders = business(payload, 'get_order')
            status = ((orders[0][1].get('data') or {}).get('order') or {}).get('status') if orders else None
            preferred = 'get_logistics' if status in {'shipped', 'delivered', 'completed'} else 'get_after_sales'
            candidates = missing - ({'calculate_refund'} if len(missing)>1 else set())
            tool = preferred if preferred in candidates else sorted(candidates)[0]
        if not any(o['tool'] == tool for o in observations):
            search_args = {'mode':'original'} if payload.get('search_mode')=='original' else {'query':payload['question']}
            action = dict(kind='tool', tool=tool, arguments=search_args if tool == 'search_knowledge' else {'order_id_ref':'fact:order_id'},
                targets=['g-request'], expected_information='取得当前尚缺的必要依据', evidence_basis=refs[:2])
    elif refs:
        updates = [dict(goal_id='g-request', status='supported', statement='模拟判断：必要观察可用',
                        support_refs=refs[:8], conflict_refs=[], unknowns=[])]
        action = dict(kind='draft', coverage='full', addressed_goals=['g-request'], support_refs=refs[:20], limitations=[])
        repair = payload.get('repair')
        if repair:
            action['finding_responses'] = [dict(finding_id=f['finding_id'], explanation='按现有证据定位并提交审核',
                support_refs=refs[:8], block_id='b1') for f in repair['pending_findings']]
            candidate = repair['candidate']
            if candidate and candidate['blocks']==draft(payload)['blocks'] and candidate['bundle_id']==payload['bundle_id']:
                action['reuse_candidate_id'] = candidate['candidate_id']
                updates = []
    return dict(based_on_state_version=payload['state_version'], goal_proposals=[], assessment_updates=updates, action=action)
