"""Agent 证据的一次性组装：稳定引用、去重、单次预算分配与淘汰原因。

工具原始结果继续保留在受控操作日志与追踪中；这里只产出模型可见投影。
同一候选答案与其审核共用同一 bundle；证据变化后生成新 bundle_id。
"""
from copy import deepcopy
import hashlib
import json

from backend.knowledge import token_count

# 缺口及补查优先，随后业务观察和政策条款共享总预算；不足显式记录。
BUNDLE_VERSION = 2

# 业务观察的选取顺序：结论依据（计算/售后/物流）先于订单明细。
BUSINESS_ORDER = {'calculate_refund': 0, 'get_after_sales': 1, 'get_logistics': 2, 'get_order': 3}


def stable_id(kind, value):
    raw = json.dumps({'kind': kind, 'value': value}, ensure_ascii=False, sort_keys=True, default=str)
    return kind + '-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


# 业务观察的必要字段：判断与金额解释所需，去掉重复的关联ID与内部引用。
PROJECTION = {
    'get_order': {'order': ('id', 'status', 'currency', 'purchased_at', 'signed_at', 'line_ids',
                            'merchandise_paid_cents', 'total_paid_cents', 'outbound_shipping_paid_cents',
                            'payment_id', 'version', 'snapshot_id', 'captured_at'),
                  'payment': ('id', 'status', 'amount_cents', 'currency', 'paid_at', 'version'),
                  'lines': ('id', 'product_id', 'quantity', 'unit_price_cents', 'paid_cents',
                            'unit_paid_amounts_cents', 'gift_for_line_id', 'conditions', 'version'),
                  'products': ('id', 'name', 'category_id', 'is_gift', 'version')},
    'get_after_sales': {'after_sales': ('id', 'status', 'return_reason', 'selections', 'applied_at',
                                        'approved_at', 'quality_status', 'requested_cents', 'approved_cents',
                                        'approved_merchandise_cents', 'approved_outbound_shipping_cents',
                                        'gift_returned_line_ids', 'refund_ids', 'version'),
                        'refunds': ('id', 'status', 'initiated_at', 'settled_at', 'settled_cents', 'version')},
    'get_logistics': {'logistics': ('id', 'status', 'shipped_at', 'signed_at', 'version')},
    'calculate_refund': ('merchandise_refund_cents', 'outbound_shipping_refund_cents',
                         'total_estimated_cents', 'currency', 'amount_semantics',
                         'approval_or_payment_performed', 'selections', 'checks'),
}


def _pick(record, fields):
    return {key: record[key] for key in (fields or ()) if key in record}


def _project_business(tool, data):
    """按工具保留必要字段；未知工具结构原样保留，避免误删判断依据。"""
    shape = PROJECTION.get(tool)
    if shape is None or not isinstance(data, dict):
        return data
    if isinstance(shape, tuple):
        return _pick(data, shape)
    projected = {}
    for key, value in data.items():
        if key in shape and isinstance(value, list):
            projected[key] = [_pick(item, shape[key]) if isinstance(item, dict) else item for item in value]
        elif key in shape and isinstance(value, dict):
            projected[key] = _pick(value, shape[key])
        else:
            projected[key] = value
    return projected


def _business_projection(result, tool):
    """只保留判断所需字段；去掉原始 envelope 中重复的追踪细节。"""
    return {'status': result.get('status'), 'data': _project_business(tool, deepcopy(result.get('data'))),
            'evidence_refs': [{k:r[k] for k in ('kind','id','version') if k in r}
                              for r in result.get('evidence_refs') or []],
            'observed_at': result.get('observed_at'),
            'error': deepcopy(result.get('error')) if result.get('error') else None}


def _business_row(key, result, tool, args):
    projection = _business_projection(result, tool)
    identity = {'tool': tool, 'arguments': args, 'observed_at': projection['observed_at'],
                'status': projection['status'], 'data': projection['data'],
                'error': (projection['error'] or {}).get('code')}
    evidence_id = stable_id('observation', identity)
    snapshot = projection['data'].get('snapshot_id') if isinstance(projection['data'], dict) else None
    return {'kind': 'business', 'evidence_id': evidence_id, 'chunk_id': 'observation-' + evidence_id[-12:],
            'document_id': 'MOCK-BUSINESS', 'document_version': snapshot,
            'source_path': 'data/business/seed.json',
            'metadata': {'title': '只读模拟业务结果', 'tool': tool,
                         'arguments': json.loads(json.dumps(args or {}, ensure_ascii=False, default=str))},
            'text': json.dumps(projection, ensure_ascii=False), 'origin_key': key,
            'observed_at': projection['observed_at'], 'tool': tool}


def _knowledge_rows(key, evidence):
    rows = []
    for row in evidence:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text']:
            continue
        identity = {field: row.get(field) for field in
                    ('chunk_id', 'document_id', 'document_version', 'source_path', 'text', 'source_start', 'source_end')}
        rows.append({**{k:deepcopy(row[k]) for k in ('source_start','source_end','document_key','parent_id',
                     'rank','score_type','cosine_similarity','cosine_distance','rrf_score','query_rrf_score',
                     'rerank_score','query_scores') if k in row},
                     'kind': 'knowledge', 'evidence_id': stable_id('chunk', identity),
                     'chunk_id': row.get('chunk_id'), 'document_id': row.get('document_id'),
                     'document_version': row.get('document_version'), 'source_path': row.get('source_path'),
                     'metadata': deepcopy(row.get('metadata') or {}), 'text': row['text'],
                     'origin_key': key, 'tool': 'knowledge'})
    return rows


def build_bundle(results, result_keys, tool_names, tool_args, settings, context_items,
                 priority_origin_keys=(), gap_evidence_ids=(), previous=None, evidence_requirements=None):
    """冻结当前证据为一次组装结果；返回 bundle 字典，不修改任何输入。

    priority_origin_keys 指定本轮新增的工具结果，其证据显式优先，
    不会因排在旧证据之后被静默淘汰。
    """
    priority_origins = {str(value) for value in priority_origin_keys}
    gap_refs = {str(value) for value in gap_evidence_ids}
    candidates, seen_chunks = [], {}
    required_rules = {}
    required_spans = {}
    policy_scope = []
    business_positions = {}
    for key in dict.fromkeys(result_keys):
        result = results.get(key)
        if not isinstance(result, dict) or 'status' not in result:
            continue
        data = result.get('data')
        if isinstance(data, dict):
            requirements=data.get('policy_requirements') or {}
            scope={k:requirements[k] for k in ('order_id','after_sale_id','selected_line_ids') if k in requirements}
            if scope and scope not in policy_scope:policy_scope.append(scope)
            for ref in requirements.get('required_rules', []):
                required_rules[(ref['kind'],ref['id'],ref['version'])] = ref
        evidence = data.get('evidence') if isinstance(data, dict) else None
        if isinstance(evidence, list):
            for row in _knowledge_rows(key, evidence):
                ref = row['metadata'].get('ref') or {}
                rule_key = (ref.get('kind'),ref.get('id'),ref.get('version'))
                if tool_names.get(key) == 'read_policy_context' and 'parent_id' in row and row['parent_id'] is None:
                    if type(row.get('source_start')) is int and type(row.get('source_end')) is int:
                        required_spans.setdefault(rule_key, []).append(
                            (row['source_path'],row['source_start'],row['source_end']))
                if row['evidence_id'] in seen_chunks:
                    seen_chunks[row['evidence_id']]['origin_keys'].append(key)
                    continue
                row['origin_keys'] = [key]
                seen_chunks[row['evidence_id']] = row
                candidates.append(row)
            continue
        row = _business_row(key, result, tool_names.get(key), tool_args.get(key))
        # 同一工具与同一规范化参数只保留最新一次观察；历史结果仍留在操作日志中。
        identity = (row['tool'], json.dumps(tool_args.get(key) or {}, ensure_ascii=False, sort_keys=True))
        previous_position = business_positions.get(identity)
        if previous_position is not None:
            superseded = candidates[previous_position]
            row['supersedes_evidence_id'] = superseded['evidence_id']
            candidates[previous_position] = row
        else:
            business_positions[identity] = len(candidates)
            candidates.append(row)
    # 必要规则的完整父块覆盖已有子块时，只占一份预算；仅限同文档/版本/来源和明确父子关系。
    parents = {row['chunk_id']:row for row in candidates if row['kind']=='knowledge'
               and 'parent_id' in row and row['parent_id'] is None}
    collapsed, retained = [], []
    for row in candidates:
        parent = parents.get(row.get('parent_id'))
        ref = row['metadata'].get('ref') or {}
        rule_key = (ref.get('kind'),ref.get('id'),ref.get('version'))
        same_scope = parent and all(parent.get(k)==row.get(k) for k in
                                     ('document_id','document_version','source_path')) and parent['metadata'].get('ref')==ref
        spans = [row.get('source_start'),row.get('source_end'),
                 (parent or {}).get('source_start'),(parent or {}).get('source_end')]
        from backend.agent_loop.requirements import same_parent, matches
        collapse = same_parent(row, parent) if evidence_requirements is not None and parent else False
        if collapse or (evidence_requirements is None and rule_key in required_rules and same_scope and all(type(v) is int for v in spans) \
                and spans[2]<=spans[0]<=spans[1]<=spans[3]):
            parent['origin_keys'] = list(dict.fromkeys(parent['origin_keys']+row['origin_keys']))
            parent.setdefault('covered_evidence_ids', []).extend([row['evidence_id'],row['chunk_id']])
            collapsed.append({'evidence_id':row['evidence_id'],'kind':'knowledge','document_id':row['document_id'],
                'reason':'covered_by_required_parent','reasons':['covered_by_required_parent'],
                'replacement_evidence_id':parent['evidence_id'],'required_gap_evidence':False,'origin_key':row['origin_key']})
        else:
            retained.append(row)
    candidates = retained
    for row in candidates:
        ref = row['metadata'].get('ref') or {}
        row['required_policy'] = (ref.get('kind'),ref.get('id'),ref.get('version')) in required_rules
        row['required_business'] = bool(required_rules) and row['kind'] == 'business' and row.get('tool') in BUSINESS_ORDER
        row_required = any(matches(row, r) for r in evidence_requirements or [])
        if row_required or row['evidence_id'] in gap_refs or row.get('chunk_id') in gap_refs or gap_refs.intersection(row.get('covered_evidence_ids', [])):
            row['priority'] = 0
        elif row['required_policy'] or row['required_business']:
            row['priority'] = 0
        elif priority_origins.intersection(row.get('origin_keys', [row['origin_key']])):
            # 本轮补查或修复轮新增的结果优先于历史证据，新旧证据地位平等。
            row['priority'] = 1
        elif row['kind'] == 'business':
            # 业务观察是结论与金额的依据，先于政策条款占位。
            row['priority'] = 2
        else:
            row['priority'] = 3
    # 本轮新增证据优先；其后业务观察（按结论依据相关性）先占位，条款使用剩余预算。
    candidates.sort(key=lambda row: (row['priority'],
        (0 if row['kind'] == 'business' else 1) if row['priority'] == 0 else 0,
        BUSINESS_ORDER.get(row.get('tool'), 9) if row['kind'] == 'business' else 0))
    selected, discarded, used = [], collapsed, 0
    for row in candidates:
        citation_id = 'S' + str(len(selected) + 1)
        size = token_count(_block(row, citation_id))
        reasons = []
        if len(selected) >= context_items:
            reasons.append('context_items')
        # 只做一次总量预算分配；业务观察先占位，条款在其后按剩余额度选择。
        if used + size > settings.evidence_token_budget:
            reasons.append('evidence_token_budget')
        if reasons:
            discarded.append({'evidence_id': row['evidence_id'], 'kind': row['kind'],
                              'document_id': row['document_id'], 'size': size,
                              'reason': reasons[0], 'reasons': reasons,
                              # 只有本轮缺口证据被淘汰才意味着"缺口未覆盖"，必须显式标记；
                              # 本轮新增的非缺口分片被裁剪属于正常预算竞争，不据此判定不完整。
                              'required_gap_evidence': row['priority'] == 0,
                               'required_policy': row['required_policy'],
                               'required_business': row['required_business'],
                              'origin_key': row['origin_key']})
            continue
        selected.append(dict(row, citation_id=citation_id, token_estimate=size))
        used += size
    uncovered = [item['evidence_id'] for item in discarded if item['required_gap_evidence']]
    def rule_covered(key):
        expected = required_spans.get(key, [])
        if not expected:
            return False
        for source,start,end in expected:
            intervals = sorted((r.get('source_start'),r.get('source_end')) for r in selected
                if r['required_policy'] and (r['metadata']['ref']['kind'],r['metadata']['ref']['id'],r['metadata']['ref']['version'])==key
                and r['source_path']==source and type(r.get('source_start')) is int and type(r.get('source_end')) is int)
            cursor=start
            for a,b in intervals:
                if a<=cursor:cursor=max(cursor,b)
            if cursor<end:return False
        return True
    missing_rules = [required_rules[key] for key in required_rules if not rule_covered(key)]
    # 整次补查结果完全未进入上下文时，也必须视为不完整而非静默丢失。
    kept_origins = {key for row in selected for key in row.get('origin_keys', [row['origin_key']])}
    missing_origins = sorted(priority_origins - kept_origins)
    missing_priority = bool(missing_origins)
    return {'version': BUNDLE_VERSION,
            'bundle_id': stable_id('bundle', [dict(model_item(item), metadata=item['metadata'],
                source_start=item.get('source_start'), source_end=item.get('source_end')) for item in selected]),
            'previous_bundle_id': (previous or {}).get('bundle_id'),
            'items': selected,
            'citation_mapping': {item['citation_id']: {'evidence_id': item['evidence_id'],
                'chunk_id': item['chunk_id'], 'document_id': item['document_id'],
                'version': item['document_version'], 'kind': item['kind']} for item in selected},
            'discarded': discarded, 'token_estimate': used,
            'incomplete': bool(uncovered) or missing_priority or bool(missing_rules),
            'required_policy_refs': list(required_rules.values()),
            'policy_scope':policy_scope,
            'missing_required_policy_refs': missing_rules,
            'uncovered_evidence_ids': uncovered,
            'missing_priority_origin': missing_priority,
            'missing_priority_origins': missing_origins,
            'superseded_evidence_ids': [row['supersedes_evidence_id'] for row in candidates
                                        if row.get('supersedes_evidence_id')]}


def model_item(item):
    """模型可见投影：保留正文、适用条件和出处；完整追踪/排名仍保存在bundle与原始工具日志。"""
    value = {k:item.get(k) for k in ('kind','evidence_id','citation_id','document_id',
                                    'document_version','text')}
    metadata = item.get('metadata') or {}
    keys = ('ref','shop_id','category_ids','conditions','exceptions','effective_from',
            'effective_until','selection_event','tool','arguments')
    value['metadata'] = {k:deepcopy(metadata[k]) for k in keys if k in metadata}
    if item.get('kind') == 'business':
        observation = json.loads(item['text'])
        # 原始引用仍保存在canonical item和操作日志；模型已获得独立规则正文，避免重复列举规则ID。
        observation.pop('evidence_refs', None)
        value['text'] = json.dumps(observation, ensure_ascii=False, separators=(',', ':'))
    return value


def model_items(bundle):
    return [model_item(item) for item in (bundle or {}).get('items', [])]


def _block(item, citation_id):
    return '[' + citation_id + ']\n' + json.dumps(dict(model_item(item), citation_id=citation_id), ensure_ascii=False, separators=(',', ':'))


def model_context(bundle):
    return '\n\n'.join(_block(item, item['citation_id']) for item in (bundle or {}).get('items') or [])


def evidence_ids(bundle):
    return {item['evidence_id'] for item in (bundle or {}).get('items') or []}


def new_evidence(bundle, previous):
    before = evidence_ids(previous)
    return [item for item in (bundle or {}).get('items') or [] if item['evidence_id'] not in before]


def resolve_reference(value, bundle):
    """显示编号(S1)与稳定ID都可解析；只在本 bundle 内解析，跨轮不串义。"""
    mapping = (bundle or {}).get('citation_mapping') or {}
    if value in mapping:
        return mapping[value]['evidence_id']
    allowed = evidence_ids(bundle)
    return value if value in allowed else None


def citation_remap(bundle):
    """修复输入携带的旧引用映射：旧S#到稳定ID，避免新S1被误读。"""
    return {'bundle_id': (bundle or {}).get('bundle_id'), 'version': (bundle or {}).get('version'),
            'citations': deepcopy((bundle or {}).get('citation_mapping') or {})}
