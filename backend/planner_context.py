"""Planner仅接收有界操作目录与纠错缺口；完整结果继续用于工具参数、证据与回答。"""
import json
from copy import deepcopy

# 单条审核问题最长约4000 token，16条原样放入会单独超过12000输入预算。
# 先保留行动字段，再按固定上限纳入靠前的问题；省略的只记条数，完整报告仍在review_result。
GAP_FIELDS = ('code', 'dimension', 'action', 'tool', 'query', 'missing_fields', 'evidence_ids')
GAP_TEXT_LIMIT = 80
GAP_QUERY_LIMIT = 120
GAP_REF_LIMIT = 4
GAP_TOKEN_LIMIT = 1800
CATALOG_LIMIT = 8


def result_catalog(keys, results, tool_names):
    selected = [key for key in dict.fromkeys(keys) if key in results and key in tool_names]
    omitted = selected[:-CATALOG_LIMIT]
    catalog = {}
    if omitted:
        catalog['omitted_prior_results'] = {'count': len(omitted),
            'tools': sorted({tool_names[key] for key in omitted}),
            'full_results_retained': True}
    for key in selected[-CATALOG_LIMIT:]:
        value = results[key]
        data = value.get('data') or {}
        # 工具引用有版本，不把政策正文或业务JSON重复塞进重规划。
        refs = value.get('evidence_refs') or []
        error = value.get('error') or {}
        missing = [item for item in (error.get('missing') or []) if isinstance(item, str)]
        entry = {'tool': tool_names[key], 'status': value['status'],
                 'error': {'code': str(error.get('code') or '')[:80], 'missing': missing[:8]} if error else None,
                 'evidence_ref_count': len(refs),
                 'result_kind': 'knowledge_evidence' if isinstance(data.get('evidence'), list) else 'business_record',
                 'full_result_available': True}
        if isinstance(data.get('after_sales'), list):
            entry['after_sales_count'] = len(data['after_sales'])
        if isinstance(data.get('refunds'), list):
            entry['refund_count'] = len(data['refunds'])
        if type(data.get('total_estimated_cents')) is int:
            entry['refund_estimate'] = {k: data[k] for k in ('total_estimated_cents', 'currency', 'amount_semantics',
                'approval_or_payment_performed') if k in data}
        catalog[key] = entry
    return catalog


def _bounded(value):
    if not isinstance(value, str) or len(value) <= GAP_TEXT_LIMIT:
        return value
    return value[:GAP_TEXT_LIMIT]


def _one_gap(issue):
    gap = {field: deepcopy(issue.get(field)) for field in GAP_FIELDS if field in issue}
    refs = gap.get('evidence_ids') or []
    if len(refs) > GAP_REF_LIMIT:
        gap['evidence_ids'] = refs[:GAP_REF_LIMIT]
        gap['evidence_ids_omitted'] = len(refs) - GAP_REF_LIMIT
    fields = gap.get('missing_fields') or []
    if len(fields) > GAP_REF_LIMIT:
        gap['missing_fields'] = fields[:GAP_REF_LIMIT]
        gap['missing_fields_omitted'] = len(fields) - GAP_REF_LIMIT
    # 知识补查的查询是行动参数，优先于说明文字保留。
    if isinstance(gap.get('query'), str) and len(gap['query']) > GAP_QUERY_LIMIT:
        gap['query_truncated'] = True
        gap['query'] = gap['query'][:GAP_QUERY_LIMIT]
    for field in ('conclusion', 'detail', 'suggestion'):
        text = _bounded(issue.get(field))
        if text:
            gap[field] = text
            if isinstance(issue.get(field), str) and len(issue[field]) > GAP_TEXT_LIMIT:
                gap[field + '_truncated'] = True
    return gap


def correction_gaps(issues):
    """只传补查所需的问题标识，不把审核长文再次装入重规划。"""
    from backend.knowledge import token_count
    gaps, used = [], 0
    for issue in issues or []:
        gap = _one_gap(issue)
        # 说明文字可再缩短，行动字段不能为了塞下后面的问题而被改写。
        while token_count(json.dumps(gap, ensure_ascii=False)) > 280:
            shrunk = False
            for field in ('suggestion', 'detail', 'conclusion'):
                if isinstance(gap.get(field), str) and len(gap[field]) > 40:
                    gap[field] = gap[field][:len(gap[field]) // 2]
                    gap[field + '_truncated'] = True
                    shrunk = True
                    break
            if not shrunk:
                break
        size = token_count(json.dumps(gap, ensure_ascii=False))
        if gaps and used + size > GAP_TOKEN_LIMIT:
            return {'issues': gaps, 'omitted_issue_count': len(issues) - len(gaps),
                    'omission_reason': 'planner_gap_token_limit', 'full_report_retained': True}
        gaps.append(gap)
        used += size
    if not gaps:
        return {'issues': [], 'omitted_issue_count': 0, 'full_report_retained': True}
    return {'issues': gaps, 'omitted_issue_count': len(issues) - len(gaps), 'full_report_retained': True}
