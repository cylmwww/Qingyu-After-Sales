"""仅计算可追溯窗口条件；不产生授权、可退资格或退款金额。"""
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath, PureWindowsPath
import json
import re
from backend.knowledge import digest

CALCULATOR_VERSION = 'continuous-window-v1'


def policy_source_path(value):
    """入库使用仓库相对路径；旧 data 相对路径仅作为显式别名。"""
    from backend.config import ROOT
    if not isinstance(value, str) or '\\' in value or ':' in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or PureWindowsPath(value).is_absolute() or '..' in relative.parts:
        return None
    if relative.parts[:2] == ('knowledge', 'policies'):
        relative = PurePosixPath('data') / relative
    if relative.parts[:3] != ('data', 'knowledge', 'policies'):
        return None
    path = (ROOT / relative).resolve()
    allowed = (ROOT / 'data/knowledge/policies').resolve()
    return path if path.is_relative_to(allowed) and path.is_file() else None


def full_policy_parent(item, ref, shop_id):
    """A parent label alone does not prove that a complete anchored rule was retained."""
    path = policy_source_path(item.get('source_path'))
    if path is None or item.get('parent_id','missing') is not None:
        return False
    raw = path.read_text('utf-8-sig')
    match = re.search(r'<!-- policy-metadata\s*(.*?)\s*-->', raw, re.S)
    if not match:
        return False
    policy = json.loads(match.group(1))
    if (policy['shop_id'] != shop_id or policy['version'] != ref.get('version')
        or item.get('document_version') != policy['version']
        or item.get('document_id', policy['id']) != policy['id']):
        return False
    anchor = '<a id="' + str(ref.get('id')) + '"></a>'
    start = raw.find(anchor)
    end = raw.find('<a id=', start+len(anchor))
    end = len(raw) if end < 0 else end
    return (start >= 0 and item.get('source_start') == start and item.get('source_end') == end
        and item.get('text') == raw[start:end])


def aware(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError('timezone_missing')
    return stamp


def condition_view(state, facts, policies, shop_id):
    values = state['route']['facts']
    provided = {f'fact:{k}': dict(value=v, source=facts.get(k, {}).get('source', 'user'),
        scope='user_condition', revision=state['revision']) for k, v in values.items() if v is not None}
    source_conflict = any(k in facts and (facts[k].get('value') != v or facts[k].get('source') not in {'user', 'user_text'})
        for k, v in values.items() if v is not None)
    required = set()
    if values.get('return_reason') in {'no_reason', 'quality'}:
        required.update(('category_id', 'return_reason', 'purchased_at', 'signed_at', 'after_sales_applied_at'))
        for policy in policies:
            if policy['category_ids'] == [values.get('category_id')]:
                for rule in policy['rules']:
                    reason_conditions = [c for c in rule['conditions'] if c['field'] == 'return_reason']
                    if any(c['op'] == 'eq' and c['value'] == values['return_reason'] for c in reason_conditions):
                        required.update(c['field'] for c in rule['conditions'])
    rules = []
    issues = []
    for item in state['bundle'].get('items', []):
        meta = item.get('metadata') or {}
        ref = meta.get('ref') or {}
        matches = [(p, r) for p in policies for r in p['rules'] if ref == dict(kind='policy_rule', id=r['id'], version=p['version'])]
        if not matches:
            continue
        p, rule = matches[0]
        if item.get('parent_id', 'missing') is not None or item.get('document_version') != p['version']:
            continue
        if meta.get('shop_id') != shop_id or p['shop_id'] != shop_id:
            continue
        if not full_policy_parent(item, ref, shop_id):
            continue
        if values.get('category_id') not in p['category_ids']:
            continue
        try:
            if p['selection_event'] != 'purchased_at':
                continue
            purchased = aware(values['purchased_at'])
            if purchased < aware(p['effective_from']) or p.get('effective_until') and purchased >= aware(p['effective_until']):
                continue
        except (KeyError, ValueError, TypeError):
            issues.append('policy_selection_input_missing')
            continue
        required.update(c['field'] for c in rule['conditions'])
        applicable = all(c['op'] == 'eq' and values.get(c['field']) == c['value'] for c in rule['conditions'])
        if applicable:
            rules.append((item, p, rule))
    windows = [(i, p, r, k) for i, p, r in rules for k in ('no_reason_window_days', 'quality_window_days') if k in r['effects']]
    result = dict(calculator_version=CALCULATOR_VERSION, scope='conditional_on_user_facts', status='unavailable',
        reason='rule_not_applicable_or_missing', fact_refs=[], policy_refs=[], overall_eligibility='not_computed')
    if windows:
        required.update(('signed_at', 'after_sales_applied_at', 'purchased_at', 'category_id', 'return_reason'))
        result['fact_refs'] = ['fact:signed_at', 'fact:after_sales_applied_at']
        result['policy_refs'] = [i['evidence_id'] for i, _, _, _ in windows]
        result['policy_bindings'] = [dict(evidence_id=i['evidence_id'], ref=i['metadata']['ref'],
            source_path=i.get('source_path'), source_start=i.get('source_start'), source_end=i.get('source_end')) for i, _, _, _ in windows]
        sizes = {r['effects'][k] for _, _, r, k in windows}
        if len(sizes) != 1 or len({p['version'] for _, p, _, _ in windows}) != 1:
            result['reason'] = 'policy_conflict'
        else:
            try:
                if source_conflict:
                    raise ValueError('fact_source_conflict')
                signed = aware(values['signed_at'])
                applied = aware(values['after_sales_applied_at'])
                authorized = []
                import json
                for item in state['bundle'].get('items', []):
                    if item.get('kind') == 'business':
                        data = json.loads(item['text']).get('data') or {}
                        order = data.get('order') or {}
                        if order.get('signed_at'):
                            authorized.append(aware(order['signed_at']))
                if any(t != signed for t in authorized):
                    raise ValueError('fact_source_conflict')
                elapsed = (applied.astimezone(timezone.utc) - signed.astimezone(timezone.utc)).total_seconds()
                if elapsed < 0:
                    raise ValueError('application_before_signed')
                days = next(iter(sizes))
                if type(days) is not int or days <= 0:
                    raise ValueError('rule_unsupported')
                deadline = signed.astimezone(timezone.utc) + timedelta(seconds=days * 86400)
                result.update(status='computed', reason=None, elapsed_seconds=elapsed, allowed_seconds=days * 86400,
                    deadline=deadline.isoformat(), signed_timezone=str(signed.tzinfo), application_timezone=str(applied.tzinfo),
                    boundary='inclusive', within_window=elapsed <= days * 86400)
            except KeyError:
                result['reason'] = 'required_time_missing'
            except (ValueError, TypeError) as error:
                result['reason'] = str(error) if str(error) in {'timezone_missing', 'fact_source_conflict', 'application_before_signed', 'rule_unsupported'} else 'invalid_time'
    result['input_hash'] = digest(dict(values=values, sources=provided, policies=result.get('policy_bindings', []), revision=state['revision']))
    result['condition_id'] = 'condition-' + digest(result)
    unresolved = sorted('fact:' + k for k in required if 'fact:' + k not in provided)
    return dict(version='condition-view-v1', required_conditions=sorted('fact:' + k for k in required),
        provided_facts=provided, derived_conditions=[result], unresolved_requirements=unresolved,
        policy_issues=sorted(set(issues)), interpretation='时间满足不等于整体可退；用户给定条件不等于授权记录核实')


def coverage_problems(coverage, view):
    provided = set(view['provided_facts'])
    needed = set(view['required_conditions']) & provided
    derived = {r['condition_id'] for r in view['derived_conditions']}
    if not needed <= set(coverage['used_fact_refs']):
        return ['condition_fact_coverage_missing']
    if set(coverage['unknown_fact_refs']) - set(view['unresolved_requirements']):
        return ['condition_false_unknown']
    if set(coverage['used_fact_refs']) - provided or set(coverage['derived_condition_refs']) != derived:
        return ['condition_reference_invalid']
    if coverage['fact_scope'] != 'user_conditions' or coverage['overall_eligibility'] != 'not_computed':
        return ['condition_source_or_eligibility_invalid']
    return []
