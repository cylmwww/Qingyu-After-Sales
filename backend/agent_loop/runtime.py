"""由实际观察驱动的单行动循环；第一切片明确扣留未通过审核的候选。"""
import asyncio
import time
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import StateGraph, START, END
from backend.agent_model import structured, structured_input_tokens
from backend.agent import recoverable_protocol_error, protocol_feedback
from backend.agent_store import AgentError
from backend.knowledge import digest
from backend.evidence_bundle import build_bundle
from backend.agent_loop import context, mock
from backend.agent_loop import ablation
from backend.agent_loop.contracts import evidence_contract, obligation_contract
from backend.agent_loop import obligations
from backend.agent_loop import requirements
from backend.agent_loop.contracts import Decision, Draft, Review, VERSION, decision_schema, bound_contract, recovery_contract, review_schema, conditions_contract, draft_schema, call_bound_contract, scoped_contract
from backend.agent_loop.state import initial, accept, observe, readiness, response_key, candidate_key, rejection_feedback, draft_gate
from backend.agent_loop.store import LoopStore
from backend.agent_loop.recovery import (OutputContractError, validate_output_draft, validate_output_review, normalized_review, review_transition, bind_review_output, review_binding)
from backend.agent_loop.tools import bind, BUSINESS_BLOCKS, auxiliary_cost
from backend.agent_loop.review import candidate_from, candidate_problems, validate_review, eligibility, remaining_findings


class Cursor(TypedDict):
    terminal: bool
    state_version: int


class LoopRuntime:
    def __init__(self, host):
        self.h = host
        self.store = LoopStore(host)

    async def run(self, route, saver):
        h = self.h
        value = initial(dict(route, question=h.task['question']), h.revision, h.task['question'])
        if recovery_contract(h):
            value.update(state_schema_version='loop-state-recovery-v3', review_sequence=0, review_execution_recovery_used=0)
        if conditions_contract(h):
            value['state_schema_version'] = 'loop-state-conditions-v4'
        if evidence_contract(h):
            value['evidence_contract_version'] = 'evidence-v9'
        if obligation_contract(h):
            value['obligation_contract_version'] = 'obligations-v10'
        state = await self.store.initialize(value)
        if call_bound_contract(h):
            value['review_binding_version']='call-bound-v5'
        graph = StateGraph(Cursor)
        graph.add_node('advance', self.advance)
        graph.add_edge(START, 'advance')
        graph.add_conditional_edges('advance', lambda s: END if s['terminal'] else 'advance')
        compiled = graph.compile(checkpointer=saver)
        # 独立检查点线程；输入仅为调度游标，每一步重读数据库。
        await compiled.ainvoke({'terminal':False, 'state_version':state['state_version']},
            {'configurable':{'thread_id':h.tid+':'+VERSION}, 'recursion_limit':100}, durability='sync')
        return dict(route, loop_terminal=True)

    def bundle(self, state):
        h = self.h
        keys = [o['operation_key'] for o in state['observations']]
        required = None
        if evidence_contract(h):
            from backend.evidence_bundle import _knowledge_rows
            scope = dict(task_id=h.tid, revision=h.revision, index_id=str(h.index['id']), shop_id=h.settings.shop_id)
            keys = [o['operation_key'] for o in state['observations'] if
                o.get('scope', {}).get('revision') == h.revision and
                str(o.get('scope', {}).get('index_id')) == str(h.index['id'])]
            if obligation_contract(h):
                keys = [key for key in keys if any(o['operation_key']==key and
                    o.get('scope',{}).get('task_id',h.tid)==h.tid and
                    o.get('scope',{}).get('shop_id',h.settings.shop_id)==h.settings.shop_id for o in state['observations'])]
                state['obligation_contract_version'] = 'obligations-v10'
            rows = [r for key in keys for r in _knowledge_rows(key, (h.results.get(key, {}).get('data') or {}).get('evidence', []))]
            collector = obligations.collect if obligation_contract(h) else requirements.collect
            required = collector(state, h.business.validator.policies, scope, rows)
            state['evidence_contract_version'] = 'evidence-v9'
        bundle = build_bundle(h.results, keys, h.tool_names, h.tool_args, h.settings, h.config.context_items,
            priority_origin_keys=state.get('priority_origins', []), evidence_requirements=required)
        if required is not None:
            state['evidence_requirements'] = requirements.covered(required, bundle)
            bundle['missing_evidence_requirements'] = [r['requirement_id'] for r in state['evidence_requirements'] if r['coverage'] < 2]
            bundle['incomplete'] |= bool(bundle['missing_evidence_requirements']) or len(required) > requirements.MAX_REQUIREMENTS
            if obligation_contract(h):
                obligations.refresh(state, scope)
        return bundle

    async def call(self, role, payload, schema, limit, key, prompt, validator=None):
        if recovery_contract(self.h):
            return await self.recovery_call(role, payload, schema, limit, key, prompt, validator)
        h = self.h
        original = dict(payload=payload, schema=schema.model_json_schema(), prompt=prompt, maximum=limit)
        async def invoke_stage(stage, current):
            args = dict(original, payload=current)
            operation_key = key + f':protocol-{stage}:' + digest(args)
            async def invoke():
                # 显式关联行动/操作；串行调用中 reservation 按具体调用记录结算。
                h.loop_operation_key = operation_key
                return await structured(h.adapter, role, current, schema, limit, h.record,
                    mock.respond(role, current) if h.settings.rag_mode == 'mock' else None, prompt)
            result = await h.operation(operation_key, 'model', args, invoke)
            return dict(selected_operation=operation_key, result=result)

        async def first():
            try:
                return await invoke_stage(0, payload)
            except Exception as error:
                if not recoverable_protocol_error(error):
                    raise
                return dict(feedback_input=dict(payload, protocol_feedback=protocol_feedback(error)))

        async def complete():
            # 持久化反馈阶段及其精确输入，反馈结果已存而状态未应用时不再请求首次调用。
            phase = await h.operation(key+':protocol-phase:'+digest(original), 'loop_protocol', original, first)
            if 'result' in phase:
                return phase
            await h.event('loop_protocol_feedback', {'operation_key':key, 'role':role})
            return await invoke_stage(1, phase['feedback_input'])

        selected = await h.operation(key+':protocol-selected:'+digest(original), 'loop_protocol', original, complete)
        return selected['result']

    async def recovery_call(self, role, payload, schema, limit, key, prompt, validator):
        h = self.h
        original = dict(payload=payload, schema=schema.model_json_schema(), prompt=prompt, maximum=limit,
                        validation_version='loop-output-v3')

        async def stage(number, current):
            args = dict(original, payload=current)
            operation_key = key+f':protocol-{number}:'+digest(args)
            async def invoke():
                h.loop_operation_key = operation_key
                try:
                    value = await structured(h.adapter, role, current, schema, limit, h.record,
                        mock.respond(role, current) if h.settings.rag_mode=='mock' else None, prompt)
                except Exception as error:
                    if not recoverable_protocol_error(error):
                        raise
                    return dict(protocol_error=protocol_feedback(error))
                return dict(output=value)
            raw = await h.operation(operation_key, 'model', args, invoke)
            if 'protocol_error' in raw:
                return dict(error=raw['protocol_error'])
            try:
                if validator:
                    validator(raw['output'])
            except AgentError as error:
                return dict(error=protocol_feedback(OutputContractError(error.code, getattr(error, 'validation_feedback', None))))
            return dict(result=raw['output'], selected_operation=operation_key)

        async def first():
            result = await stage(0, payload)
            return result if 'result' in result else dict(feedback_input=dict(payload, protocol_feedback=result['error']))

        async def complete():
            phase = await h.operation(key+':protocol-phase:'+digest(original), 'loop_protocol', original, first)
            if 'result' in phase:
                return phase
            await h.event('loop_protocol_feedback', dict(role=role, operation_key=key, runtime_version=VERSION))
            result = await stage(1, phase['feedback_input'])
            return result if 'result' in result else dict(exhausted=True, error=result['error'])

        selected = await h.operation(key+':protocol-selected:'+digest(original), 'loop_protocol', original, complete)
        if selected.get('exhausted'):
            raise AgentError('loop_protocol_exhausted')
        return selected['result']

    async def admission(self, state, calls, provisional=False, auxiliary=None):
        h = self.h
        from backend.agent_loop.budgeting import estimate
        result = estimate(h, (await h.store.task(h.tid))['budget'], calls, auxiliary, counter=structured_input_tokens)
        await h.event('budget_admission', dict(runtime_version=VERSION,
            action_id=(state.get('pending_action') or {}).get('action_id'), **result,
            provisional=provisional, evidence_input_pending=provisional, reservation=False))
        return result['admitted']

    async def stop(self, state, reason):
        h = self.h
        now = time.perf_counter()
        try:
            await h.store.heartbeat(h.tid, h.attempt, (now-h.last_heartbeat)*1000, h.settings)
        except AgentError as error:
            if error.code != 'active_time_budget_exceeded':
                raise
        h.last_heartbeat = now
        new = dict(state, phase='terminal', reason=reason, eligibility=None)
        await self.store.transition(state, new, [('loop_stopped', {'reason':reason, 'candidate_withheld':True})])
        await h.store.finish(h.tid, h.attempt, 'completed', {'runtime_version':VERSION},
            dict(task_id=h.tid, trace_id=h.tid, answer='本次任务未能完成可发布的结果，请补充条件或转人工核查。未执行交易操作。',
                 evidence=[], reviewed=False, outcome='handoff', reason=reason, runtime_version=VERSION,
                 coverage='none', mode=h.settings.rag_mode, path='dynamic', correction_rounds=state['repair_episodes']))

    async def advance(self, cursor):
        try:
            return await self._advance(cursor)
        except AgentError as error:
            if 'budget_exceeded' not in error.code:
                raise
            state = await self.store.load()
            await self.stop(state, 'unresolved_budget')
            return {'terminal':True, 'state_version':state['state_version']}

    async def _advance(self, cursor):
        h = self.h
        state = await self.store.load()
        if state['phase'] == 'terminal':
            # 终止状态与终止事件之间崩溃时只补固定通知，不重跑模型。
            task = await h.store.task(h.tid)
            if task['status'] == 'running':
                await self.stop(state, state.get('reason', 'interrupted_terminal'))
            return {'terminal':True, 'state_version':state['state_version']}
        if state['no_progress'] >= h.config.no_progress_limit:
            await self.stop(state, 'no_progress')
            return {'terminal':True, 'state_version':state['state_version']}
        phase = state['phase']
        if state.get('stop_reason'):
            await self.stop(state, state['stop_reason'])
            return {'terminal':True, 'state_version':state['state_version']}
        if phase == 'decide':
            bundle = self.bundle(state)
            view = dict(state, bundle=bundle)
            if conditions_contract(h):
                from backend.agent_loop.conditions import condition_view
                view['condition_view'] = condition_view(view, h.task['facts'], h.business.validator.policies, h.settings.shop_id)
                if scoped_contract(h) and not view['condition_view']['required_conditions']:
                    view['condition_view'] = dict(view['condition_view'], derived_conditions=[], applicability='not_applicable')
            budget = (await h.store.task(h.tid))['budget']
            payload = context.decision_input(h, view, budget)
            # 恢复时预算已包含上次决策；决策操作身份不含易变账本，载荷从持久 pending 中恢复。
            if not state.get('decision_input'):
                payload = dict(payload, state_version=state['state_version']+1)
                state = await self.store.transition(state, dict(view, decision_input=payload), [])
            payload = dict(state['decision_input'], state_version=state['state_version'])
            key = f'loop:{h.revision}:decision:{state["state_version"]}'
            fixed = ablation.fixed_decision(state) if ablation.mode(h)=='E2' else None
            decision = fixed or await self.call('agent_decision', payload, decision_schema(h), h.settings.loop_config.decision_tokens, key, context.prompt(h))
            try:
                if ablation.mode(h)=='E2' and fixed is None:
                    ablation.validate_fixed_action(decision)
                new = accept(state, decision, {e['evidence_id'] for e in state['bundle']['items']})
                if obligation_contract(h):
                    obligations.apply(new, decision, dict(task_id=h.tid, revision=h.revision, index_id=str(h.index['id']), shop_id=h.settings.shop_id))
                    new['bundle'] = self.bundle(new)
                if bound_contract(h) and new['pending_action']['kind']=='tool':
                    bind(h, new)  # 行动参数和状态提议同准入，拒绝不能部分更新 Assessment。
            except AgentError as error:
                feedback = rejection_feedback(error, decision, state) if bound_contract(h) else {'reason':error.code}
                if conditions_contract(h):
                    from backend.agent_loop.recovery import reference_catalog
                    feedback['reference_catalog'] = reference_catalog(state)
                    feedback['missing_requirements'] = error.code.split(':', 1)[1].split(',') if error.code.startswith('missing_dependency:') else []
                new = dict(state, no_progress=state['no_progress']+1, decision_input=None, last_rejection=feedback)
                await self.store.transition(state, new, [('action_rejected', feedback)])
            else:
                new['decision_input'] = None
                new['last_rejection'] = None
                await self.store.transition(state, new, [('agent_decision', dict(decision, **new['pending_action'], decision_source='fixed_workflow' if fixed else 'model')),
                    ('goal_update', {'goals':new['goals'], 'assessments':new['assessments']})])
        elif phase == 'tool_pending':
            await self.tool(state)
        elif phase == 'draft_pending':
            await self.draft(state)
        elif phase == 'review_pending':
            await self.review(state)
        elif phase == 'publish':
            if not await h.db.index_available(h.index['id']):
                raise AgentError('knowledge_index_unavailable')
            now = time.perf_counter()
            await h.store.heartbeat(h.tid, h.attempt, (now-h.last_heartbeat)*1000, h.settings)
            h.last_heartbeat = now
            if state['eligibility']['attempt'] != h.attempt:
                new = dict(state, state_version=state['state_version']+1)
                new['eligibility'] = ablation.release_eligibility(h, new, h.tid, h.attempt)
                state = await self.store.transition(state, new, [('release_rebound', {'reason':'explicit_resume_same_revision'})])
            await self.store.publish(state)
            return {'terminal':True, 'state_version':state['state_version']}
        elif phase == 'review_withheld':
            await self.stop(state, 'review_correction_not_supported_in_slice1')
            return {'terminal':True, 'state_version':state['state_version']}
        elif phase in {'clarify_pending', 'finish_pending'}:
            await self.stop(state, 'clarification_not_supported_in_slice1' if phase == 'clarify_pending' else state['pending_action']['reason'])
            return {'terminal':True, 'state_version':state['state_version']}
        else:
            raise AgentError('loop_unknown_phase')
        return {'terminal':False, 'state_version':state['state_version']}

    async def tool(self, state):
        h, action = self.h, state['pending_action']
        if state.get('phase_tool_actions', state['tool_actions']) >= h.settings.loop_config.max_tool_actions:
            return await self.stop(state, 'tool_phase_limit')
        try:
            args = bind(h, state)
        except AgentError as error:
            feedback = rejection_feedback(error, {'action':action}, state) if bound_contract(h) else {'reason':error.code}
            await self.store.transition(state, dict(state, phase='decide', pending_action=None,
                no_progress=state['no_progress']+1, last_rejection=feedback),
                [('action_rejected', dict(feedback, action_id=action['action_id']))])
            return
        scope = dict(revision=h.revision, snapshot_id=h.task['snapshot_id'], index_id=str(h.index['id']),
                     customer_id=h.task['customer_id'], facts=state['route']['facts'])
        if evidence_contract(h):
            scope.update(task_id=h.tid, shop_id=h.settings.shop_id)
        arguments = dict(tool=action['tool'], args=args, scope=scope)
        key = f'loop:{h.revision}:{action["action_id"]}:tool:' + digest(arguments)
        h.tool_names[key], h.tool_args[key] = action['tool'], args
        async def execute():
            # 已持久同范围结果可复用，但仍产生 reused 观察且不算知识进展。
            prior = next((r for r in await h.store.operation_results(h.tid, h.revision)
                if r['kind']=='tool' and r['arguments']==arguments and r['result']['status'] in {'success','no_result'}), None)
            if prior:
                await h.event('operation_reused', {'key':key, 'source_key':prior['key'], 'action_id':action['action_id']})
                return prior['result']
            payload = context.decision_input(h, state, (await h.store.task(h.tid))['budget'])
            draft_payload = context.draft_input(h, dict(state, pending_action={'limitations':[]}))
            placeholder = dict(candidate_id='reserved-candidate', blocks=[], answer='x'*h.settings.max_output_tokens,
                               bundle_id=state['bundle'].get('bundle_id'))
            if not await self.admission(state, [
                ('agent_decision',payload,decision_schema(h),h.settings.loop_config.decision_tokens,context.prompt(h)),
                ('agent_draft',draft_payload,draft_schema(h),self.draft_limit(state),context.prompt(h)),
                ('agent_review',context.review_input(h,state,placeholder),review_schema(h),h.review.review_tokens,context.review_prompt(h))], True,
                await auxiliary_cost(h, action['tool'], args)):
                raise AgentError('loop_closure_budget_exceeded')
            await h.store.reserve(h.tid, h.attempt, h.config, 'tools')
            await h.event('tool_start', dict(runtime_version=VERSION, action_id=action['action_id'], operation_key=key, tool=action['tool']))
            h.loop_operation_key = key
            try:
                async with asyncio.timeout(h.config.business_timeout if action['tool'] in {'get_order','get_after_sales','get_logistics','calculate_refund'} else h.settings.request_timeout_seconds):
                    result = await h.invoke_tool(action['tool'], args, state['route'])
            except TimeoutError:
                raise AgentError('loop_tool_timeout') from None
            return result
        result = await h.operation(key, 'tool', arguments, execute)
        if evidence_contract(h):
            # Register explicitly selected context needs before clearing the pending action.
            self.bundle(state)
            # The before-view is the persisted, actually visible bundle, not that speculative rebuild.
            state['evidence_requirements'] = requirements.covered(state['evidence_requirements'], state['bundle'])
        new, observation = observe(state, key, action['tool'], args, result, scope)
        if evidence_contract(h) and observation is not None:
            new['bundle'] = self.bundle(new)
            observation['after_review_id'] = (state.get('review') or {}).get('review_id')
            observation['applied_state_version'] = state['state_version']+1
            requirements.apply_progress(state, new, observation)
        business_block = result['status']=='permanent_failure' and (result.get('error') or {}).get('code') in BUSINESS_BLOCKS
        from backend.agent_loop.failures import tool_policy
        policy = tool_policy(result)
        technical = policy['category'].startswith('technical_') if conditions_contract(h) else (
            result['status']=='transient_failure' or result['status']=='permanent_failure' and not business_block)
        if technical:
            # 技术失败不得恢复后越过失败行动；状态保留 pending，仅诊断写事件。
            await h.event('tool_result', dict(runtime_version=VERSION, tool=action['tool'], result=result,
                action_id=action['action_id'], operation_key=key))
            if conditions_contract(h):
                if not policy['resumable']:
                    raise AgentError('loop_tool_permanent_failure')
            raise AgentError('loop_tool_technical_failure')
        if result['status'] in {'no_permission','missing_input'}:
            new['stop_reason'] = 'permission_denied' if result['status']=='no_permission' else 'clarification_not_supported_in_slice1'
        if business_block:
            new['stop_reason'] = result['error']['code']
        await self.store.transition(state, new, [('tool_result', dict(runtime_version=VERSION, tool=action['tool'], result=result,
            action_id=action['action_id'], operation_key=key)), ('observation_applied', observation),
            ('progress_checked', {'action_id':action['action_id'], 'structural_change':observation.get('structural_progress', not observation['reused']),
                                 'no_progress_count':new['no_progress']})])
        if result['status'] in {'no_permission','missing_input'}:
            await self.stop(await self.store.load(), 'permission_denied' if result['status']=='no_permission' else 'clarification_not_supported_in_slice1')

    async def draft(self, state):
        h = self.h
        if obligation_contract(h) and not state['route']['intent']['needs_business'] and obligations.unbound_missing(state):
            return await self.stop(state, 'unbound_repair_obligation')
        if conditions_contract(h):
            gate = draft_gate(state, state['bundle'])
            if gate['reason'] and gate['recovery'] == 'decide':
                from backend.agent_loop.recovery import reference_catalog
                feedback = dict(gate, reference_catalog=reference_catalog(state))
                return await self.store.transition(state, dict(state, phase='decide', pending_action=None,
                    decision_input=None, eligibility=None, last_rejection=feedback, no_progress=state['no_progress']+1),
                    [('draft_gate_rejected', feedback)])
        reason = readiness(state, state['bundle'])
        if conditions_contract(h):
            reason = gate['reason']
        await h.event('draft_readiness', dict(runtime_version=VERSION, admitted=reason is None, reason=reason,
            bundle_id=state['bundle']['bundle_id'], goal_version=state['goal_version']))
        if reason:
            return await self.stop(state, reason)
        action = state['pending_action']
        if action.get('reuse_candidate_id'):
            candidate = state['candidate']
            # accept 与执行均检查版本；只允许未审过的新定位响应，不调用 draft。
            if (candidate['candidate_id'] != action['reuse_candidate_id'] or
                candidate['bundle_id'] != state['bundle']['bundle_id'] or candidate['goal_version'] != state['goal_version']):
                raise AgentError('loop_reuse_candidate_binding')
            response = response_key(action, candidate)
            if response in state['reviewed_responses']:
                raise AgentError('loop_repeated_finding_response')
            new = dict(state, phase='review_pending', eligibility=None,
                program_issues=candidate_problems(candidate, state),
                reviewed_responses=state['reviewed_responses']+[response])
            return await self.store.transition(state, new, [('candidate_reused', dict(candidate_id=candidate['candidate_id'],
                bundle_id=candidate['bundle_id'], response_key=response, repair_episode=state['repair_episodes']))])
        payload = context.draft_input(h, state)
        placeholder = dict(candidate_id='reserved-candidate', blocks=[], answer='x'*h.settings.max_output_tokens,
                           bundle_id=state['bundle']['bundle_id'])
        prefix = f'loop:{h.revision}:{state["pending_action"]["action_id"]}:draft'
        saved = self.saved_output(prefix)
        if not saved and not await self.admission(state, [('agent_draft',payload,draft_schema(h),self.draft_limit(state),context.prompt(h)),
                ('agent_review',context.review_input(h,state,placeholder),review_schema(h),h.review.review_tokens,context.review_prompt(h))]):
            return await self.stop(state, 'unresolved_budget')
        output = await self.call('agent_draft', payload, draft_schema(h), self.draft_limit(state),
            prefix, context.prompt(h),
            (lambda output: validate_output_draft(output, state)) if recovery_contract(h) else None)
        candidate = candidate_from(output, state)
        problems = candidate_problems(candidate, state)
        for response in action.get('finding_responses', []):
            if response.get('block_id') and response['block_id'] not in {b['block_id'] for b in candidate['blocks']}:
                raise AgentError('loop_response_invalid_block')
        unchanged = any(candidate_key(c)==candidate_key(candidate) for c in state['candidate_history'])
        new = dict(state, candidate=candidate, program_issues=problems, phase='review_pending', eligibility=None,
            candidate_history=state['candidate_history']+[candidate],
            no_progress=state['no_progress']+1 if unchanged else 0)
        if not ablation.has_review(h):
            if problems:
                new['stop_reason'] = 'ablation_program_check_failed'
            else:
                new.update(phase='publish', state_version=state['state_version']+1)
                new['eligibility'] = ablation.release_eligibility(h, new, h.tid, h.attempt)
        await self.store.transition(state, new, [('candidate_created', dict(candidate, repair_episode=state['repair_episodes'])),
            ('progress_checked', dict(structural_change=not unchanged, no_progress_count=new['no_progress']))])

    def draft_limit(self, state):
        return min(self.h.settings.max_output_tokens, self.h.review.repair_tokens) if state['repair_episodes'] else self.h.settings.max_output_tokens

    def saved_output(self, prefix):
        # 反馈输入落盘并不等于候选/审核成功，不能因此跳过闭环准入。
        if recovery_contract(self.h):
            return any(key.startswith(prefix+':protocol-selected:') and 'result' in value for key,value in self.h.results.items())
        return any(key.startswith((prefix+':protocol-0:', prefix+':protocol-1:', prefix+':protocol-selected:'))
                   for key in self.h.results)

    async def review(self, state):
        h, candidate = self.h, state['candidate']
        payload = context.review_input(h, state, candidate)
        binding = review_binding(state) if call_bound_contract(h) else None
        if binding:
            payload['call_binding'] = binding
        # 实际候选落地后再核；未知输出导致预估偏小也必须安全退出。
        prefix = f'loop:{h.revision}:{state["pending_action"]["action_id"]}:review'
        if recovery_contract(h):
            prefix += ':execution-'+str(state.get('review_sequence', 0))
        saved = self.saved_output(prefix)
        if not saved and not await self.admission(state, [('agent_review',payload,review_schema(h),h.review.review_tokens,context.review_prompt(h))]):
            return await self.stop(state, 'unresolved_budget')
        async with asyncio.timeout(h.review.timeout_seconds):
            output = await self.call('agent_review', payload, review_schema(h), h.review.review_tokens,
                prefix, context.review_prompt(h),
                (lambda output: validate_output_review(bind_review_output(output, binding, state) if binding else output, candidate, state)) if recovery_contract(h) else None)
        if binding:
            output = bind_review_output(output, binding, state)
        if recovery_contract(h):
            output = normalized_review(output)
        review = dict(output, review_id=str(uuid4()), candidate_hash=candidate['content_hash'], blocks_hash=candidate['blocks_hash'],
                      input_findings=state.get('findings', []))
        if binding:
            review['call_binding'] = binding
        if evidence_contract(h):
            selected = next((v for k,v in h.results.items() if k.startswith(prefix+':protocol-selected:')), {})
            review['evidence_audit'] = dict(task_id=h.tid, revision=h.revision, index_id=str(h.index['id']),
                shop_id=h.settings.shop_id, state_version=state['state_version']+1,
                input_operation=selected.get('selected_operation'), input_hash=digest(payload))
        validate_review(review, candidate, state)
        pending = remaining_findings(review, state)
        review['registered_findings'] = pending
        new = dict(state, review=review, phase='publish', state_version=state['state_version']+1,
            findings=pending, review_history=state['review_history']+[review], eligibility=None)
        passed = review['complete'] and not pending and not state['program_issues']
        reason = None if passed else ('review_incomplete' if not review['complete'] else
            'finding_capacity_exceeded' if len(pending)>6 else
            'repair_limit' if state['repair_episodes'] >= h.review.max_rounds else None)
        if recovery_contract(h):
            reason = review_transition(state, new, passed, h.review.max_rounds)
        if ablation.mode(h)=='E4' and not passed:
            reason = 'ablation_review_withheld'
            new.update(phase='review_withheld', repair_episodes=state['repair_episodes'])
        if passed:
            new['eligibility'] = eligibility(new, h.tid, h.attempt)
        else:
            if not recovery_contract(h):
                new.update(phase='decide', pending_action=None, decision_input=None,
                    phase_tool_actions=0, repair_episodes=min(state['repair_episodes']+1, h.review.max_rounds))
            if reason:
                # 与审核结果原子持久。终止通知前崩溃，恢复不能再开启一个修复阶段。
                new['stop_reason'] = reason
        new = await self.store.transition(state, new, [('review_result', dict(review, runtime_version=VERSION,
            unresolved_count=len(pending), program_issue_count=len(state['program_issues']),
            repair_episode=state['repair_episodes'],
            execution='completed' if review['complete'] else 'incomplete', verdict='pass' if passed else 'not_evaluated' if recovery_contract(h) and not review['complete'] else 'issues_found'))])
        if reason:
            await self.stop(new, reason)
