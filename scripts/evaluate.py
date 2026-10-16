"""第八阶段可重复评测：冻结、串行执行、离线复核和分组汇总。标准不进入助手输入。"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.agent import AgentRuntime, setup_agent
from backend.agent_dispatch import runtime_class
from backend.agent_loop.contracts import LoopConfig
from backend.agent_contract import AgentConfig, AgentRequest, Facts
from backend.agent_store import AgentStore
from backend.business_tools import BusinessService, response
from backend.config import Settings
from backend.db import Database
from backend.knowledge import digest, load_knowledge
from backend.models import ModelAdapter
from backend.rag import RAGService, citation_result
from backend.request_contract import ChatRequest
from backend.retrieval import retrieve
from backend.review_contract import ReviewConfig
from backend.stage3_eval import first_turn, knowledge_groups, score, source_ranges
from scripts.compare_stage4 import index_hash
from scripts.compare_stage5 import save
from scripts.compare_stage7 import input_facts, PRESETS

SCORING = {'version': 'offline-v1', 'semantic_source': 'independent_human_review_only',
           'online_reviewer_is_ground_truth': False, 'failure_in_denominator': True,
           'unknown_usage_cost_is_null': True, 'aggregation': 'all_case_repeats_no_best_selection',
           'auto_resolution_denominator': 'human_confirmed_auto_eligible',
           'evidence_proxy': 'required_versioned_reference_and_full_text_coverage_not_semantic_support'}
PROFILES = ['R1', 'R2', 'R3', 'R4', 'E0', 'E1', 'E2', 'E3', 'E4', 'E5', 'E5-no-pre', 'E5-no-post', 'E3-context', 'E3-rewrite', 'LOOP-V1', 'LOOP-V2', 'LOOP-V3', 'LOOP-V4', 'LOOP-V5', 'LOOP-V6', 'LOOP-V7', 'LOOP-V8', 'LOOP-V9', 'LOOP-V10']
RPATHS = {f'R{i}': f'configs/retrieval/{name}.v1.0.0.json' for i, name in enumerate(
    ['r1-vector', 'r2-bm25', 'r3-hybrid-rrf', 'r4-hybrid-rrf-rerank'], 1)}
PROFILES += ['LOOP-E2', 'LOOP-E3', 'LOOP-E4', 'LOOP-E5']


def read(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def local_path(path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(ROOT / 'local'):
        raise ValueError('评测产物只能保存在local')
    return resolved


def source_hashes():
    # 提示词位于源码；冻结全部运行代码、依赖、迁移、知识/业务/配置/夹具。
    paths = []
    for folder in ['backend', 'scripts', 'configs', 'migrations', 'data']:
        paths.extend(p for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    paths.extend(ROOT / p for p in ['requirements.lock', 'requirements.txt'])
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def split_audit(dataset, references):
    families = {c.get('family_id') for d in references for c in d['cases']}
    questions = {first_turn(c)['text'].strip() for d in references for c in d['cases']}
    combinations = {digest(first_turn(c).get('facts')) for d in references for c in d['cases'] if first_turn(c).get('facts')}
    errors = []
    ids = set()
    for c in dataset['cases']:
        if c['id'] in ids or not c.get('family_id'):
            errors.append({'id': c['id'], 'reason': 'missing_family_or_duplicate_id'})
        ids.add(c['id'])
        if c.get('family_id') in families or first_turn(c)['text'].strip() in questions:
            errors.append({'id': c['id'], 'reason': 'family_or_text_overlap'})
        if first_turn(c).get('facts') and digest(first_turn(c)['facts']) in combinations:
            errors.append({'id': c['id'], 'reason': 'explicit_condition_overlap'})
    return {'errors': errors, 'semantic_independence': 'requires_human_review', 'case_count': len(ids)}


def formal_gate(dataset, validation, selection, preflight):
    if dataset.get('split') != 'test' or dataset.get('review_status') != 'approved':
        raise ValueError('正式评测需要人工确认的独立test数据集')
    if validation.get('split') != 'validation' or validation.get('review_status') != 'approved':
        raise ValueError('先完成人工确认的validation，不得用test选择配置')
    if not selection or selection.get('selected_using_splits') != ['development', 'validation'] or selection.get('review_status') != 'approved':
        raise ValueError('缺少开发/验证选型记录或负责人确认')
    if not preflight or preflight.get('phase') != 'preflight' or preflight.get('status') != 'completed' or len(preflight.get('cases', [])) != 5:
        raise ValueError('缺少五个开发案例预检')
    if any(c.get('status') == 'failed' for c in preflight['cases']):
        raise ValueError('五案例预检存在接口/执行失败，先解决开发集问题')


def base_settings(mode, protocol):
    url = os.getenv('STAGE8_DATABASE_URL')
    if not url or urlsplit(url).path.rstrip('/').split('/')[-1] in {'rag', 'postgres', 'template0', 'template1', ''}:
        raise ValueError('显式STAGE8_DATABASE_URL必须是隔离实验库，不能是正式库')
    return Settings(_env_file=None if mode == 'mock' else ROOT / '.env', database_url=url, rag_mode=mode,
                    index_config_path='configs/indexes/stage2-baseline.v1.0.0.json',
                    retrieval_config_path=None, answer_config_path=None, agent_config_path=None, review_config_path=None,
                    structured_response_protocol=protocol, agent_config=AgentConfig(), review_config=ReviewConfig())


def profile_settings(base, name, retrieval=None, answer=None):
    data = base.model_dump()
    data.update(retrieval_config_path=RPATHS[name] if name in RPATHS else retrieval,
                answer_config_path=('configs/answers/q2-parent-related.v1.0.0.json' if name == 'E3-context' else
                                    'configs/answers/q1-rewrite.v1.0.0.json' if name == 'E3-rewrite' else answer),
                agent_config=AgentConfig(id='stage8-' + name, strategy='fixed' if name == 'E2' else 'dynamic', experiment=True),
                review_config=ReviewConfig.read(ROOT / PRESETS[name]) if name in PRESETS else ReviewConfig())
    data.update(runtime_version='agent-loop-v1' if name in {'LOOP-V1','LOOP-V2','LOOP-V3'} else 'legacy', loop_config_path=None,
                loop_config=(LoopConfig.read(ROOT/'configs/loops/loop-v2-bound.v1.2.0.json') if name=='LOOP-V2'
                             else LoopConfig() if name=='LOOP-V1' else None))
    if name=='LOOP-V10':
        data.update(runtime_version='agent-loop-v1',loop_config=LoopConfig.read(ROOT/'configs/loops/loop-v10-obligations.v1.10.0.json'))
    if name=='LOOP-V9':
        data.update(runtime_version='agent-loop-v1',loop_config=LoopConfig.read(ROOT/'configs/loops/loop-v9-evidence.v1.9.0.json'))
    if name=='LOOP-V8':
        data.update(runtime_version='agent-loop-v1',loop_config=LoopConfig.read(ROOT/'configs/loops/loop-v8-gap-view.v1.8.0.json'))
    if name=='LOOP-V7':
        data.update(runtime_version='agent-loop-v1',loop_config=LoopConfig.read(ROOT/'configs/loops/loop-v7-gap-full.v1.7.0.json'))
    if name=='LOOP-V6':
        data.update(runtime_version='agent-loop-v1',loop_config=LoopConfig.read(ROOT/'configs/loops/loop-v6-scoped-view.v1.6.0.json'))
    if name=='LOOP-V5':
        data.update(runtime_version='agent-loop-v1',loop_config=LoopConfig.read(ROOT/'configs/loops/loop-v5-call-bound.v1.5.0.json'))
    if name=='LOOP-V4':
        data.update(runtime_version='agent-loop-v1',
            loop_config=LoopConfig.read(ROOT/'configs/loops/loop-v4-conditions.v1.4.0.json'))
    if name=='LOOP-V3':
        data['loop_config'] = LoopConfig.read(ROOT/'configs/loops/loop-v3-recovery.v1.3.0.json')
    if name in {'LOOP-V1','LOOP-V2','LOOP-V3','LOOP-V4','LOOP-V5','LOOP-V6','LOOP-V7','LOOP-V8','LOOP-V9','LOOP-V10'}:
        data['review_config'] = ReviewConfig.read(ROOT/'configs/reviews/loop-post-required.v1.0.0.json')
    if name in {'LOOP-E2','LOOP-E3','LOOP-E4','LOOP-E5'}:
        data.update(runtime_version='agent-loop-v1',
            model_retry_count=0,
            loop_config=LoopConfig.read(ROOT/f'configs/loops/loop-{name[-2:].lower()}-ablation.v1.7.0.json'),
            review_config=ReviewConfig.read(ROOT/'configs/reviews/loop-post-required.v1.0.0.json'))
    return Settings(_env_file=None, **data)


async def freeze(args):
    output = local_path(args.output)
    if output.exists():
        raise ValueError('冻结清单不可覆盖')
    dataset = read(args.dataset)
    if dataset.get('split') not in {'development', 'validation', 'test'} or not dataset.get('cases'):
        raise ValueError('非法数据集')
    if args.phase != 'formal' and dataset.get('split') == 'test':
        raise ValueError('test只能走正式门禁，不能循环进行pilot')
    if args.phase == 'preflight' and (dataset.get('split') != 'development' or len(dataset['cases']) != 5):
        raise ValueError('预检必须恰好五个开发案例')
    validation = read(args.validation) if args.validation else None
    selection = read(args.selection) if args.selection else None
    preflight = read(args.preflight) if args.preflight else None
    if args.phase == 'formal':
        formal_gate(dataset, validation or {}, selection, preflight)
        if args.repeats != 3 and not args.reduction_reason:
            raise ValueError('正式默认三次，缩减必须说明原因')
    references = [read(ROOT / p) for p in ['data/evaluation/development.json', 'data/stage3-evaluation/validation.v1.0.0.json', 'data/stage6-evaluation/validation.v1.0.0.json']]
    audit = split_audit(dataset, references) if dataset['split'] == 'test' else None
    if audit and audit['errors']:
        raise ValueError('独立test划分检查失败')
    base = base_settings(args.mode, args.protocol)
    db = Database(base)
    await db.migrate(); await setup_agent(db)
    index = await db.get_index(args.index_id); db.check_index(index)
    profiles = {name: profile_settings(base, name, args.retrieval_config, args.answer_config).snapshot() for name in args.profiles}
    manifest = {'phase': args.phase, 'mode': args.mode, 'dataset': dataset, 'dataset_hash': digest(dataset),
                'source_hashes': source_hashes(), 'index_id': args.index_id, 'index_hash': await index_hash(db, args.index_id),
                'index_contract': index['embedding_contract'], 'knowledge_version': index['knowledge_version'],
                'business_fingerprint': BusinessService().fingerprint, 'base': base.snapshot(), 'profiles': profiles,
                'repeats': args.repeats, 'concurrency': 1, 'scoring': SCORING, 'selection': selection,
                'validation_hash': digest(validation) if validation else None,
                'preflight_fingerprint': preflight.get('fingerprint') if preflight else None,
                'retrieval_config': args.retrieval_config, 'answer_config': args.answer_config,
                'reduction_reason': args.reduction_reason, 'split_audit': audit,
                'dependency_versions': __import__('backend.agent_contract', fromlist=['runtime_fingerprint']).runtime_fingerprint()}
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, {'fingerprint': digest(manifest), 'manifest': manifest, 'frozen_at': datetime.now(timezone.utc).isoformat()})
    print(json.dumps({'status': 'frozen', 'output': str(output), 'fingerprint': digest(manifest)}, ensure_ascii=False))


async def faq_baseline(s, db, case, index):
    """E0是确定性精确FAQ问句/已有同义问匹配，条件校验，无LLM/业务查询。"""
    from scripts.validate_stage1 import condition_matches, timestamp
    turn = first_turn(case); facts = turn.get('facts', {})
    question = turn['text'].strip()
    faq = read(ROOT / 'data/knowledge/faq.json')
    matches = [f for f in faq['items'] if question in [f['question'], *f['paraphrases']]]
    filters = {k: v for k, v in facts.items() if k in {'category_id', 'purchased_at', 'after_sales_applied_at'}}
    tid, _ = await db.new_trace(question, filters, index['id'])
    result = {'answer': 'FAQ未精确命中，需政策检索或人工核查。', 'action': 'handoff', 'evidence': [], 'mode': 'deterministic_faq', 'scope': 'exact_question_or_curated_paraphrase'}
    if len(matches) == 1:
        f = matches[0]; scope = f['applicability']; missing = []
        event = scope['selection_event']; at = facts.get(event)
        conditions = scope['conditions']
        missing = [c['field'] for c in conditions if c['field'] not in facts]
        if not at: missing.append(event)
        if not facts.get('category_id'): missing.append('category_id')
        if missing:
            result.update(answer='请补充FAQ适用条件。', action='clarification', missing=missing)
        elif (facts['category_id'] in scope['category_ids'] and all(condition_matches(c, facts[c['field']]) for c in conditions)
              and timestamp(at) >= timestamp(scope['effective_from'])
              and (not scope['effective_until'] or timestamp(at) < timestamp(scope['effective_until']))):
            rows = [r for r in await db.chunk_rows(index['id'], filters) if r['document_id'] == f['id'] and r['document_version'] == f['version']]
            evidence = [dict(r, citation_id='S' + str(i + 1)) for i, r in enumerate(rows)]
            result.update(answer=f['answer'] + (' [S1]' if evidence else ''), action='answer', evidence=evidence)
    await db.event(tid, 'faq_match', {'match_ids': [f['id'] for f in matches], 'result': result})
    await db.finish_trace(tid, 'completed', result=result, elapsed_ms=0)
    await db.event(tid, 'done', result)
    return [await db.trace(tid)]


def evaluation_runtime(fixtures, frozen=None):
    class Runtime(runtime_class(frozen or {})):
        async def invoke_tool(self, tool, arguments, state):
            for f in fixtures:
                op = f['operation']
                if f['fixture_kind'] == 'tool_timeout' and tool == op['tool'] and arguments.get('order_id') == op['order_id']:
                    await asyncio.sleep(self.config.business_timeout + .05)
                if f['fixture_kind'] == 'knowledge_conflict' and tool == 'search_knowledge':
                    return response('permanent_failure', code='fixture_policy_service_refusal')
            value = await super().invoke_tool(tool, arguments, state)
            for f in fixtures:
                if f['fixture_kind'] == 'prompt_injection' and tool == 'search_knowledge' and value['status'] == 'success':
                    first = value['data']['evidence'][0]
                    first['text'] += '\n【不可信测试夹具】' + f['operation']['untrusted_text']
                    first['metadata'] = dict(first['metadata'], injected_fixture=f['id'])
            return value
    return Runtime


async def execute_case(s, name, case, index, entry, checkpoint):
    db = Database(s)
    if name == 'E0':
        traces = await faq_baseline(s, db, case, index)
        entry['trace_ids'] = [str(t['id']) for t in traces]; checkpoint()
        return traces
    if name in RPATHS or name == 'E1':
        turn = first_turn(case)
        filters = {k: v for k, v in turn.get('facts', {}).items() if k in {'category_id', 'purchased_at', 'after_sales_applied_at'}}
        tid, _ = await db.new_trace(turn['text'], filters, index['id'])
        entry['trace_ids'] = [tid]; checkpoint()
        adapter = ModelAdapter(s)
        try:
            if name == 'E1':
                await RAGService(s, db, adapter).run(tid, index, turn['text'], filters)
            else:
                async def event(kind, payload): await db.event(tid, kind, payload)
                async def record(payload): await db.model_call(payload, trace_id=tid)
                start = time.perf_counter()
                rows = await retrieve(s, db, adapter, index, turn['text'], filters, event, record)
                from backend.rag import make_context
                evidence, _, discarded, _ = make_context(rows, s)
                result = {'evidence': evidence, 'retrieval_only': True, 'discarded': discarded}
                await db.finish_trace(tid, 'completed', result=result, elapsed_ms=(time.perf_counter()-start)*1000)
            return [await db.trace(tid)]
        except Exception as e:
            await db.finish_trace(tid, 'failed', error={'code': getattr(e, 'code', type(e).__name__)})
            return [await db.trace(tid)]
        finally:
            await adapter.close()
    fixture_ids = {r['id'] for r in case.get('fixture_refs', [])}
    fixtures = [read(p) for p in (ROOT / 'data/fixtures').glob('*.json') if read(p)['id'] in fixture_ids]
    store = AgentStore(db); sid = await store.create_session(s.agent_customer_id)
    entry['session_id'] = sid; checkpoint()
    traces = {}; current = None
    for turn in [t for t in case['turns'] if t['role'] == 'user']:
        snap = turn.get('business_snapshot_ref', case.get('business_snapshot_ref', {})).get('id', s.business_snapshot_id)
        current_s = s.model_copy(update={'business_snapshot_id': snap})
        store = AgentStore(Database(current_s))
        if current and current['status'] not in {'waiting', 'completed'}: break
        action = 'clarify' if current and current['status'] == 'waiting' else 'new'
        body = AgentRequest(session_id=sid, request_id=uuid4(), question=turn['text'], facts=input_facts(turn),
                            index_id=index['id'], action=action, task_id=current['id'] if action == 'clarify' else None)
        tid, launch = await store.submit(body, s.agent_customer_id, BusinessService(snapshot_id=snap))
        entry.setdefault('trace_ids', []).append(tid); checkpoint()
        if launch:
            saved = await store.task(tid)
            await evaluation_runtime(fixtures, saved['frozen'])(current_s, saved).run()
        current = await store.task(tid)
        traces[tid] = await store.db.trace(tid)
        if current['frozen'].get('runtime_version')=='agent-loop-v1':
            from backend.agent_loop.mechanisms import mechanism_evidence
            async with store.db.connection() as connection:
                row = await (await connection.execute('SELECT state FROM agent_loop_states WHERE task_id=%s', (tid,))).fetchone()
                operations = await (await connection.execute('SELECT key,kind,status,arguments,result FROM agent_operations WHERE task_id=%s ORDER BY started_at', (tid,))).fetchall()
            if row:
                entry.setdefault('mechanisms', {})[str(tid)] = mechanism_evidence(row['state'], operations, traces[tid].get('result'))
    return list(traces.values())


def diagnose(case, entry, ranges):
    traces = entry.get('traces', [])
    final = (traces[-1].get('result') or {}) if traces else {}
    evidence = final.get('evidence', [])
    answer = final.get('answer', '')
    action = final.get('action') or ('clarification' if entry['status'] == 'waiting' else
              'technical_failure' if entry['status'] in {'failed', 'interrupted'} else
              final.get('outcome') if final.get('outcome') in {'handoff', 'partial_answer'} else
              'refusal' if final.get('reason') in {'order_access_denied', 'unsupported_operation'} else
              'retrieval_only' if final.get('retrieval_only') else 'answer')
    calls = {c['call_id']: c for t in traces for c in t.get('model_calls', [])}
    token_values = [c.get('usage', {}).get('total_tokens') if isinstance(c.get('usage'), dict) else None for c in calls.values()]
    tokens = sum(v for v in token_values if isinstance(v, int))
    costs = [c.get('cost') for c in calls.values()]
    policy_evidence = [e for e in evidence if e.get('metadata', {}).get('ref', {}).get('kind') in {'faq', 'policy_rule'}]
    missing_spans = [e.get('evidence_id') or e.get('chunk_id') for e in policy_evidence if
        type(e.get('source_start')) is not int or type(e.get('source_end')) is not int
        or e['source_start'] < 0 or e['source_end'] < e['source_start']]
    return {'observed_action': action, 'citation_resolution': citation_result(answer, evidence),
            'evidence_coverage_proxy': score(knowledge_groups(case), policy_evidence, ranges)
                if case.get('required_evidence') and not missing_spans else None,
            'evidence_coverage_error': {'code': 'missing_or_invalid_source_spans', 'evidence_ids': missing_spans}
                if missing_spans else None,
            'model_calls': len(calls), 'reported_tokens': tokens if any(type(v) is int for v in token_values) else None,
            'unknown_usage_calls': sum(v is None for v in token_values),
            'cost': sum(costs) if costs and all(type(v) in {int, float} for v in costs) else None,
            'failure_codes': [t['error'].get('code') for t in traces if t.get('error')],
            'semantic_task_success': False if entry['status'] in {'failed', 'interrupted'} else None,
            'semantic_support': None, 'human_review_status': 'pending_human',
            'mechanisms': entry.get('mechanisms', {})}


def progress_snapshot(report):
    # 断点续跑所需的最小进度；完整报告仅在各案例完成时整体落盘，避免逐案例重写全部追踪。
    keys = ('case_id', 'family_id', 'group', 'profile', 'repeat', 'status', 'trace_ids', 'session_id', 'error', 'scope', 'fixtures')
    return {'fingerprint': report['fingerprint'],
            'cases': [{k: e[k] for k in keys if k in e} for e in report['cases']]}


async def merge_interrupted(report, progress, fetch_trace):
    """崩溃恢复：running条目保留为interrupted并回填侧车进度；侧车独有条目补录，不静默重执行成本调用。"""
    if not progress or progress.get('fingerprint') != report.get('fingerprint'):
        return
    saved = {(c['case_id'], c['profile'], c['repeat']): c for c in progress.get('cases', [])}
    for e in report['cases']:
        c = saved.get((e['case_id'], e['profile'], e['repeat']))
        if e['status'] == 'running' and c:
            e['trace_ids'] = list(dict.fromkeys(c.get('trace_ids', e.get('trace_ids', []))))
            if c.get('session_id'):
                e.setdefault('session_id', c['session_id'])
    known = {(e['case_id'], e['profile'], e['repeat']) for e in report['cases']}
    for (case_id, profile, repeat), c in saved.items():
        if (case_id, profile, repeat) in known or c.get('status') != 'running':
            continue
        report['cases'].append({k: c[k] for k in
            ('case_id', 'family_id', 'group', 'profile', 'repeat', 'status', 'trace_ids', 'session_id', 'scope', 'fixtures') if k in c})
    for e in report['cases']:
        if e['status'] == 'running':
            e.update(status='interrupted', error='evaluation_process_interrupted')
            e['traces'] = [t for tid in dict.fromkeys(e.get('trace_ids', [])) if (t := await fetch_trace(tid))]


def review_template(entry, case):
    return {'case_id': entry['case_id'], 'profile': entry['profile'], 'repeat': entry['repeat'], 'trace_ids': entry.get('trace_ids', []),
            'expected_conclusions': case.get('expected_conclusions', []), 'forbidden_errors': case.get('forbidden_errors', []),
            'allowed_actions': case.get('allowed_actions', []), 'required_evidence': case.get('required_evidence', []),
            'review_status': 'pending_human', 'reviewer': None, 'reviewed_at': None,
            'task_success': None, 'auto_eligible': None, 'correct_auto_resolved': None,
            'expected_action': None, 'observed_action': entry.get('diagnostics', {}).get('observed_action'),
            'action_correct': None, 'answer_correct': None, 'evidence_supported': None,
            'unsupported_assertions': None, 'notes': '', 'review_source': 'human_required',
            'program_diagnostics': entry.get('diagnostics')}


async def run(args):
    lock = read(args.lock); m = lock['manifest']
    if digest(m) != lock['fingerprint'] or source_hashes() != m['source_hashes']:
        raise ValueError('冻结后源码/数据/提示/依赖发生变化，必须保留旧结果并建立新清单')
    base = base_settings(m['mode'], m['base']['structured_response_protocol'])
    if base.snapshot() != m['base']:
        raise ValueError('模型/运行配置与冻结清单不一致')
    db = Database(base); index = await db.get_index(m['index_id']); db.check_index(index)
    if await index_hash(db, m['index_id']) != m['index_hash']:
        raise ValueError('索引与冻结哈希不一致')
    output = local_path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    progress_path = output.with_name(output.stem + '.progress.json')
    if output.exists():
        if not args.resume: raise ValueError('保留原始结果，续跑需--resume；已失败案例不重复挑选')
        report = read(output)
        if report['fingerprint'] != lock['fingerprint']: raise ValueError('续跑冻结条件不兼容')
        # 进程中断未可靠完成的案例保留为中断；不静默重执行成本调用。
        await merge_interrupted(report, read(progress_path) if progress_path.exists() else None, db.trace)
    else:
        report = {'status': 'running', 'fingerprint': lock['fingerprint'], 'phase': m['phase'], 'mode': m['mode'],
                  'started_at': datetime.now(timezone.utc).isoformat(), 'manifest': m, 'cases': [], 'concurrency': 1,
                  'evidence_class': 'real_model' if m['mode'] == 'real' else 'simulated', 'semantic_results': 'pending_human'}
        if args.resume:
            await merge_interrupted(report, read(progress_path) if progress_path.exists() else None, db.trace)
    checkpoint = lambda: save(progress_path, progress_snapshot(report))
    def publish():
        save(output, report); checkpoint()
    checkpoint()
    _, _, chunks, _ = load_knowledge(base); ranges = source_ranges(chunks)
    for name in m['profiles']:
        s = profile_settings(base, name, m['retrieval_config'], m['answer_config'])
        if s.snapshot() != m['profiles'][name]: raise ValueError('预设与冻结不一致')
        for case in m['dataset']['cases']:
            for repeat in range(m['repeats']):
                if any(e['case_id'] == case['id'] and e['profile'] == name and e['repeat'] == repeat for e in report['cases']): continue
                e = {'case_id': case['id'], 'family_id': case.get('family_id'), 'group': case.get('group_id', 'ungrouped'),
                     'profile': name, 'repeat': repeat, 'status': 'running', 'trace_ids': [],
                     'scope': 'first_turn_knowledge_retrieval' if name in RPATHS else 'first_turn_no_business' if name in {'E0', 'E1'} else 'full_conversation_readonly_business',
                     'fixtures': case.get('fixture_refs', [])}
                report['cases'].append(e); checkpoint(); start = time.perf_counter()
                try:
                    traces = await execute_case(s, name, case, index, e, checkpoint)
                    e.update(traces=traces, status=traces[-1]['status'] if traces else 'failed')
                except Exception as error:
                    e.update(status='failed', error=getattr(error, 'code', type(error).__name__),
                             traces=[t for tid in dict.fromkeys(e['trace_ids']) if (t := await db.trace(tid))])
                e['wall_elapsed_ms'] = (time.perf_counter()-start)*1000
                e['trace_ids'] = list(dict.fromkeys(e['trace_ids']))
                e['diagnostics'] = diagnose(case, e, ranges)
                publish(); print(name, case['id'], repeat + 1, e['status'], flush=True)
    if await index_hash(db, m['index_id']) != m['index_hash']: raise ValueError('评测中索引改变，结果不完整')
    report.update(status='completed', finished_at=datetime.now(timezone.utc).isoformat())
    publish()
    review_path = output.with_name(output.stem + '.review.json')
    if not review_path.exists():
        by_id = {c['id']: c for c in m['dataset']['cases']}
        save(review_path, {'fingerprint': lock['fingerprint'], 'review_status': 'pending_human', 'records': [review_template(e, by_id[e['case_id']]) for e in report['cases']]})
    print(json.dumps(aggregate(report, None), ensure_ascii=False, indent=2))


def aggregate(report, reviews):
    reviewed = {(r['case_id'], r['profile'], r['repeat']): r for r in (reviews or {}).get('records', []) if r.get('review_status') == 'approved' and r.get('review_source') == 'human' and r.get('reviewer') and r.get('reviewed_at')}
    summaries = []
    for name in report['manifest']['profiles']:
        groups = sorted({e['group'] for e in report['cases'] if e['profile'] == name})
        for group in ['all', *groups]:
            entries = [e for e in report['cases'] if e['profile'] == name and (group == 'all' or e['group'] == group)]
            n = len(entries); rows = [reviewed.get((e['case_id'], name, e['repeat'])) for e in entries]
            complete = bool(n) and all(r and type(r.get('task_success')) is bool for r in rows)
            auto = [r for r in rows if r and r.get('auto_eligible') is True]
            times = sorted(e.get('wall_elapsed_ms', 0) for e in entries)
            failures = {}
            for e in entries:
                for code in e.get('diagnostics', {}).get('failure_codes', []) or ([e['error']] if e.get('error') else []): failures[code] = failures.get(code, 0) + 1
            d = [e.get('diagnostics', {}) for e in entries]
            summaries.append({'profile': name, 'group': group, 'case_repeats': n, 'unique_cases': len({e['case_id'] for e in entries}),
                'completed': sum(e['status'] == 'completed' for e in entries), 'waiting': sum(e['status'] == 'waiting' for e in entries),
                'failed_or_interrupted': sum(e['status'] in {'failed', 'interrupted'} for e in entries),
                'human_reviewed': sum(r is not None for r in rows), 'task_success_rate': sum(bool(r['task_success']) for r in rows)/n if complete else None,
                'correct_auto_resolution_rate': sum(r.get('correct_auto_resolved') is True for r in auto)/len(auto) if complete and auto else None,
                'auto_eligible_denominator': len(auto) if complete else None,
                'action_correct_rate': sum(r.get('action_correct') is True for r in rows)/n if complete and all(type(r.get('action_correct')) is bool for r in rows) else None,
                'evidence_supported_rate': sum(r.get('evidence_supported') is True for r in rows)/n if complete and all(type(r.get('evidence_supported')) is bool for r in rows) else None,
                'evidence_coverage_proxy': statistics.mean(x['evidence_coverage_proxy']['recall'] for x in d if x.get('evidence_coverage_proxy') and x['evidence_coverage_proxy']['recall'] is not None) if any(x.get('evidence_coverage_proxy') and x['evidence_coverage_proxy']['recall'] is not None for x in d) else None,
                'p50_wall_ms': statistics.median(times) if times else None, 'p95_wall_ms': times[min(n-1, int(n*.95))] if n else None,
                'reported_tokens_sum': sum(x.get('reported_tokens') or 0 for x in d) if any(x.get('reported_tokens') is not None for x in d) else None,
                'unknown_usage_calls': sum(x.get('unknown_usage_calls', 0) for x in d),
                'provider_cost': sum(x['cost'] for x in d) if d and all(x.get('cost') is not None for x in d) else None,
                'failure_types': failures,
                'action_confusion': [{'expected': r.get('expected_action'), 'observed': r.get('observed_action')} for r in rows if r]})
    return {'fingerprint': report['fingerprint'], 'evidence_class': report.get('evidence_class'), 'phase': report['phase'],
            'denominator_rule': 'all_runs_failures_retained_no_best_selection', 'groups': summaries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    f = sub.add_parser('freeze')
    f.add_argument('--dataset', required=True, type=Path); f.add_argument('--index-id', required=True)
    f.add_argument('--output', required=True, type=Path); f.add_argument('--mode', choices=['mock', 'real'], default='mock')
    f.add_argument('--phase', choices=['preflight', 'pilot', 'formal'], default='pilot')
    f.add_argument('--protocol', choices=['strict-json-v1', 'json-or-sse-v1', 'sse-json-v1'], default='strict-json-v1')
    f.add_argument('--profiles', choices=PROFILES, nargs='+', default=['E3'])
    f.add_argument('--repeats', type=int, choices=[1, 2, 3], default=3)
    f.add_argument('--validation', type=Path); f.add_argument('--selection', type=Path); f.add_argument('--preflight', type=Path)
    f.add_argument('--retrieval-config'); f.add_argument('--answer-config'); f.add_argument('--reduction-reason')
    r = sub.add_parser('run'); r.add_argument('--lock', required=True, type=Path); r.add_argument('--output', required=True, type=Path); r.add_argument('--resume', action='store_true')
    a = sub.add_parser('report'); a.add_argument('--results', required=True, type=Path); a.add_argument('--review', type=Path); a.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.command == 'report':
        results = read(args.results); reviews = read(args.review) if args.review else None
        if reviews and reviews['fingerprint'] != results['fingerprint']: raise ValueError('复核材料不属于本次冻结条件')
        destination = local_path(args.output)
        if destination.exists(): raise ValueError('汇总不可覆盖，请使用新文件名')
        save(destination, aggregate(results, reviews))
    else:
        asyncio.run(freeze(args) if args.command == 'freeze' else run(args))


if __name__ == '__main__':
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
