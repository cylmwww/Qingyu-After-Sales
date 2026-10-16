"""只读模拟业务服务。身份、快照、规则与算术完全由服务端控制。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import uuid

from backend.config import ROOT
from backend.knowledge import digest
from scripts.validate_stage1 import Validation, ref_key, condition_matches, timestamp

COLLECTIONS = {'customer': 'customers', 'product': 'products', 'campaign': 'campaigns', 'order': 'orders',
               'order_line': 'order_lines', 'payment': 'payments', 'logistics': 'logistics',
               'after_sale': 'after_sales', 'refund': 'refunds'}


def response(status, data=None, refs=None, code=None, missing=None):
    return {'status': status, 'data': data, 'evidence_refs': refs or [],
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'error': {'code': code, 'missing': missing or []} if code else None, 'call_id': str(uuid.uuid4())}


class BusinessService:
    def __init__(self, shop_id='SHOP-DEMO-001', snapshot_id='SNAP-DEMO-20261009'):
        validator = Validation(ROOT)
        report = validator.run()
        if report['result'] != 'pass' or report['human_review_status'] != 'approved':
            raise ValueError('业务种子及政策门禁未通过')
        self.validator, self.shop_id = validator, shop_id
        seed = validator.business
        snapshot = next((s for s in seed['snapshots'] if s['id'] == snapshot_id and s['shop_id'] == shop_id), None)
        if not snapshot:
            raise ValueError('未知业务快照')
        self.snapshot_id, self.captured_at = snapshot_id, snapshot['captured_at']
        self.records = {ref_key(r): deepcopy(validator.index[ref_key(r)]) for r in snapshot['record_refs']}
        self.by_id = {k[:2]: v for k, v in self.records.items()}
        self.fingerprint = digest({'seed': seed, 'policies': validator.policies})

    def record(self, kind, id):
        return self.by_id.get((kind, id))

    def references(self, pairs):
        return [{'kind': k, 'id': r['id'], 'version': r['version']} for k, r in pairs if r]

    def authorize_order(self, order_id, customer_id):
        if not order_id:
            return response('missing_input', code='order_required', missing=['order_id'])
        order = self.record('order', order_id)
        if not order:
            return response('no_result', code='order_not_found')
        if order['shop_id'] != self.shop_id or order['customer_id'] != customer_id:
            return response('no_permission', code='order_access_denied')
        return None

    def call(self, tool, args, customer_id, conditions=None):
        allowed = {'get_order': {'order_id'}, 'get_after_sales': {'order_id'}, 'get_logistics': {'order_id'},
                   'calculate_refund': {'order_id', 'after_sale_id'}}
        if tool not in allowed or not set(args) <= allowed[tool]:
            return response('permanent_failure', code='tool_arguments_invalid')
        denied = self.authorize_order(args.get('order_id'), customer_id)
        if denied:
            return denied
        order = self.record('order', args['order_id'])
        if tool == 'get_order':
            lines = [self.record('order_line', id) for id in order['line_ids']]
            products = [self.record('product', l['product_id']) for l in lines]
            payment = self.record('payment', order['payment_id'])
            pairs = [('order', order), ('payment', payment)] + [('order_line', l) for l in lines] + [('product', p) for p in products]
            return response('success', {'order': order, 'lines': lines, 'products': products, 'payment': payment,
                'snapshot_id': self.snapshot_id, 'captured_at': self.captured_at}, self.references(pairs))
        if tool == 'get_after_sales':
            sales = [v for (k, _), v in self.by_id.items() if k == 'after_sale' and v['order_id'] == order['id']]
            refunds = [v for (k, _), v in self.by_id.items() if k == 'refund' and v['order_id'] == order['id']]
            return response('success' if sales else 'no_result', {'after_sales': sales, 'refunds': refunds,
                'snapshot_id': self.snapshot_id}, self.references([('after_sale', s) for s in sales] + [('refund', r) for r in refunds]),
                code=None if sales else 'no_after_sale_record')
        if tool == 'get_logistics':
            rows = [v for (k, _), v in self.by_id.items() if k == 'logistics' and v['order_id'] == order['id']]
            return response('success' if rows else 'no_result', {'logistics': rows}, self.references([('logistics', r) for r in rows]))
        return self.calculate(order, args.get('after_sale_id'), conditions or {})

    def rule(self, id, at):
        rule, policy = self.validator.applicable_rule(id, at)
        return rule, {'kind': 'policy_rule', 'id': id, 'version': policy['version']}

    def candidate_rules(self, order):
        """订单可能相关的规则目录；不能冒充本次计算的必要依赖。"""
        category_rules = {'apparel_footwear': 'RULE-APP-', 'digital_accessories': 'RULE-DIG-', 'customized': 'RULE-CUS-'}
        prefixes = {'RULE-GEN-', 'RULE-REF-', 'RULE-PRO-', 'RULE-SHIP-'}
        for lid in order['line_ids']:
            line = self.record('order_line', lid)
            prefixes.add(category_rules[self.record('product', line['product_id'])['category_id']])
        ids = sorted({r['id'] for p in self.validator.policies for r in p['rules'] if any(r['id'].startswith(x) for x in prefixes)})
        return [self.rule(id, order['purchased_at'])[1] for id in ids]

    def policy_requirements(self, order, sale_id=None, facts=None):
        """用同一授权订单的事实确定本次解释依赖；不接收模型指定的规则或金额。"""
        facts = facts or {}
        sale = self.record('after_sale', sale_id) if sale_id else None
        if sale_id and (not sale or sale['order_id'] != order['id']):
            return {'error': 'after_sale_not_in_order', 'required_rules': []}
        if not sale_id:
            sales = [v for (kind, _), v in self.by_id.items() if kind == 'after_sale' and v['order_id'] == order['id']]
            sale = sales[0] if len(sales) == 1 else None
        selections = facts.get('selections', sale.get('selections') if sale else None) or []
        reason = facts.get('return_reason', sale.get('return_reason') if sale else None)
        applied = facts.get('after_sales_applied_at', sale.get('applied_at') if sale else None)
        logs = [v for (kind, _), v in self.by_id.items() if kind == 'logistics' and v['order_id'] == order['id']]
        signed = facts.get('signed_at') or (logs[0]['signed_at'] if len(logs) == 1 else None)
        reasons = {'RULE-REF-001': '退款按已冻结商品单元实付份额计算',
                   'RULE-REF-002': '预估、审批和到账金额须区分'}
        days = None
        if reason in {'no_reason', 'quality'}:
            window_id = 'RULE-GEN-001' if reason == 'no_reason' else 'RULE-GEN-002'
            window, _ = self.rule(window_id, order['purchased_at'])
            days = window['effects']['no_reason_window_days' if reason == 'no_reason' else 'quality_window_days']
            reasons[window_id] = '当前退货原因对应的窗口与核实要求'
            reasons['RULE-SHIP-004' if reason == 'no_reason' else 'RULE-SHIP-005'] = '原出库运费的退款条件'
        delta = (timestamp(applied)-timestamp(signed)).total_seconds() if applied and signed else None
        selected_ids = set()
        for selection in selections:
            line = self.record('order_line', selection.get('line_id'))
            if not line or line['order_id'] != order['id']:
                return {'error': 'line_not_in_order', 'required_rules': []}
            selected_ids.add(line['id'])
            product = self.record('product', line['product_id'])
            conditions = {**line['conditions'], **{k:v for k,v in facts.items() if k in line['conditions']},
                          'return_reason': reason, 'within_quality_window':
                          delta is not None and days is not None and 0 <= delta <= days*86400}
            options = [r for p in self.validator.policies if p['category_ids'] == [product['category_id']]
                       for r in p['rules'] if 'eligibility' in r['effects'] and any(
                           c['field'] == 'return_reason' and c['value'] == reason for c in r['conditions'])]
            matched = [r for r in options if all(condition_matches(c, conditions.get(c['field'])) for c in r['conditions'])]
            for rule in matched or options:
                reasons[rule['id']] = ('所选商品的适用资格规则' if matched else '解释所选商品尚不能确认资格的条件')
        lines = [self.record('order_line', lid) for lid in order['line_ids']]
        if selected_ids and any(l['paid_cents'] != l['unit_price_cents'] * l['quantity']
                                for l in lines if not self.record('product', l['product_id'])['is_gift']):
            reasons['RULE-PRO-001'] = '解释订单优惠分摊后冻结的实付份额'
            if selected_ids != {l['id'] for l in lines if not self.record('product', l['product_id'])['is_gift']}:
                reasons['RULE-PRO-002'] = '部分退货是否追扣订单优惠'
        if any(l['gift_for_line_id'] in selected_ids for l in lines):
            reasons['RULE-PRO-003'] = '所选主商品的关联赠品返还条件'
        required = [dict(self.rule(id, order['purchased_at'])[1], requirement_reason=why)
                    for id, why in reasons.items()]
        required_ids = {(r['id'], r['version']) for r in required}
        return {'version': 'business-policy-dependencies-v2', 'order_id': order['id'],
                'after_sale_id': sale['id'] if sale else None, 'selected_line_ids': sorted(selected_ids),
                'required_rules': required, 'candidate_rules': [r for r in self.candidate_rules(order)
                    if (r['id'],r['version']) not in required_ids]}

    def required_rules(self, order, sale_id=None, facts=None):
        return self.policy_requirements(order, sale_id, facts)['required_rules']

    def calculate(self, order, sale_id, facts):
        sale = self.record('after_sale', sale_id) if sale_id else None
        if sale_id and (not sale or sale['order_id'] != order['id']):
            return response('no_permission', code='after_sale_not_in_order')
        selections = facts.get('selections', sale.get('selections') if sale else None)
        reason = facts.get('return_reason', sale.get('return_reason') if sale else None)
        applied = facts.get('after_sales_applied_at', sale.get('applied_at') if sale else None)
        missing = [k for k, v in {'selections': selections, 'return_reason': reason, 'after_sales_applied_at': applied}.items() if not v]
        if missing:
            return response('missing_input', code='calculation_inputs_required', missing=missing)
        returned = facts.get('gift_returned_line_ids', sale.get('gift_returned_line_ids') if sale else [])
        selected, merchandise = set(), 0
        requirements = self.policy_requirements(order, sale_id, facts)
        if requirements.get('error'):
            return response('no_permission', code=requirements['error'])
        refs = deepcopy(requirements['required_rules'])
        paid_units = set()
        for lid in order['line_ids']:
            line = self.record('order_line', lid)
            if not self.record('product', line['product_id'])['is_gift']:
                paid_units.update((lid, i) for i in range(1, line['quantity'] + 1))
        logs = [v for (k, _), v in self.by_id.items() if k == 'logistics' and v['order_id'] == order['id']]
        signed = facts.get('signed_at') or (logs[0]['signed_at'] if len(logs) == 1 else None)
        if not signed:
            return response('missing_input', code='signed_time_required', missing=['signed_at'])
        delta = (timestamp(applied) - timestamp(signed)).total_seconds()
        if delta < 0:
            return response('permanent_failure', code='application_before_signed')
        window_rule, _ = self.rule('RULE-GEN-001' if reason == 'no_reason' else 'RULE-GEN-002', order['purchased_at'])
        days = window_rule['effects']['no_reason_window_days' if reason == 'no_reason' else 'quality_window_days']
        if delta > days * 86400:
            return response('permanent_failure', code='return_window_manual_review')
        for selection in selections:
            line = self.record('order_line', selection['line_id'])
            if not line or line['order_id'] != order['id']:
                return response('no_permission', code='line_not_in_order')
            product = self.record('product', line['product_id'])
            if product['is_gift']:
                return response('permanent_failure', code='gift_not_refundable')
            conditions = {**line['conditions'], **{k: v for k, v in facts.items() if k in line['conditions']},
                          'return_reason': reason, 'within_quality_window': delta <= days * 86400}
            # 用户报告不能把服务端未核实质量变成已核实。
            if reason == 'quality' and conditions.get('quality_status') != 'confirmed':
                return response('permanent_failure', code='quality_verification_required')
            category = product['category_id']
            policies = [p for p in self.validator.policies if p['category_ids'] == [category]]
            eligibility = [r['effects']['eligibility'] for p in policies for r in p['rules']
                           if 'eligibility' in r['effects'] and all(condition_matches(c, conditions.get(c['field'])) for c in r['conditions'])]
            if not eligibility or any(v != 'eligible' for v in eligibility):
                return response('permanent_failure', code='eligibility_not_confirmed')
            for unit in selection['unit_indices']:
                key = (line['id'], unit)
                if type(unit) is not int or key not in paid_units or key in selected:
                    return response('permanent_failure', code='selection_invalid')
                selected.add(key)
                merchandise += line['unit_paid_amounts_cents'][unit - 1]
        if not selected:
            return response('missing_input', code='selections_required', missing=['selections'])
        gifts = [self.record('order_line', lid) for lid in order['line_ids']
                 if self.record('order_line', lid)['gift_for_line_id'] in {lid for lid, _ in selected}]
        # 可核实的返还记录优先；用户说未退使它失效，用户说已退不替代记录。
        verified_returned = set(sale['gift_returned_line_ids'] if sale else [])
        verified_returned |= {g['id'] for g in gifts if g['conditions'].get('gift_returned') is True}
        if not {g['id'] for g in gifts} <= verified_returned & set(returned):
            return response('permanent_failure', code='gift_manual_review')
        shipping = 0
        if reason == 'quality' and selected == paid_units:
            rule, _ = self.rule('RULE-SHIP-005', order['purchased_at'])
            if rule['effects'].get('outbound_shipping_refundable') is True:
                shipping = order['outbound_shipping_paid_cents']
        refs += self.references([('order', order), ('after_sale', sale)] + [('logistics', l) for l in logs]
                               + [('order_line', self.record('order_line', lid)) for lid, _ in sorted(selected)])
        return response('success', {'merchandise_refund_cents': merchandise, 'outbound_shipping_refund_cents': shipping,
            'total_estimated_cents': merchandise + shipping, 'currency': order['currency'],
            'amount_semantics': 'estimate_only', 'approval_or_payment_performed': False, 'selections': selections,
            'checks': {'scope': 'snapshot_with_explicit_user_condition_overrides',
                'time_window': {'signed_at':signed,'applied_at':applied,'elapsed_seconds':delta,
                                'allowed_seconds':days*86400,'satisfied':True},
                'selected_units_in_authorized_order':True,'selected_category_conditions_satisfied':True,
                'related_gifts_verified_returned':True,'basis':'frozen_unit_paid_amounts_cents',
                'user_override_fields':sorted(k for k in facts if k in {'selections','signed_at',
                    'after_sales_applied_at','return_reason','gift_returned_line_ids','tags_intact',
                    'product_intact','used_outdoors','packaging_intact','activated','accessories_complete','quality_status'})},
            'policy_requirements': requirements}, refs)
