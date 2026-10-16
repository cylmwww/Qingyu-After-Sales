"""任务锁内 CAS、观察应用和最终发布；检查点不覆盖权威状态。"""
from copy import deepcopy
from psycopg.types.json import Jsonb

from backend.agent_store import AgentError
from backend.agent_loop.contracts import VERSION


class LoopStore:
    def __init__(self, host):
        self.host = host

    async def load(self):
        async with self.host.db.connection() as c:
            row = await (await c.execute('SELECT * FROM agent_loop_states WHERE task_id=%s', (self.host.tid,))).fetchone()
        return row['state'] if row else None

    async def initialize(self, state):
        h = self.host
        async with h.db.connection() as c:
            task = await h.store.locked(c, h.tid, h.attempt)
            row = await (await c.execute('SELECT * FROM agent_loop_states WHERE task_id=%s FOR UPDATE', (h.tid,))).fetchone()
            if row:
                if row['revision'] != task['revision']:
                    raise AgentError('loop_revision_change_not_supported_in_slice1')
                return row['state']
            await c.execute('INSERT INTO agent_loop_states VALUES(%s,%s,%s,%s,%s,now())',
                (h.tid, h.revision, 0, VERSION, Jsonb(state)))
            return state

    async def transition(self, old, new, events):
        h = self.host
        value = deepcopy(new)
        value['state_version'] = old['state_version'] + 1
        async with h.db.connection() as c:
            task = await h.store.locked(c, h.tid, h.attempt)
            if task['revision'] != old['revision']:
                raise AgentError('loop_stale_revision')
            row = await (await c.execute('''UPDATE agent_loop_states SET state_version=%s,state=%s,updated_at=now()
                WHERE task_id=%s AND revision=%s AND state_version=%s RETURNING task_id''',
                (value['state_version'], Jsonb(value), h.tid, h.revision, old['state_version']))).fetchone()
            if not row:
                raise AgentError('loop_stale_state')
            for kind, payload in events:
                await c.execute('INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,%s,%s)',
                    (h.tid, kind, Jsonb(dict(payload, runtime_version=VERSION, revision=h.revision, state_version=value['state_version']))))
        return value

    async def publish(self, expected):
        h = self.host
        async with h.db.connection() as c:
            task = await h.store.locked(c, h.tid, h.attempt)
            row = await (await c.execute('SELECT * FROM agent_loop_states WHERE task_id=%s FOR UPDATE', (h.tid,))).fetchone()
            state = row['state']
            index = await (await c.execute('SELECT status FROM knowledge_indexes WHERE id=%s AND shop_id=%s FOR SHARE',
                (h.index['id'], h.settings.shop_id))).fetchone()
            if not index or index['status'] != 'published':
                raise AgentError('knowledge_index_unavailable')
            if state['state_version'] != expected['state_version'] or task['revision'] != state['revision']:
                raise AgentError('loop_stale_release')
            candidate, review, grant = state['candidate'], state['review'], state['eligibility']
            from backend.agent_loop.review import eligibility, validate_candidate, validate_review
            if not grant or state['phase'] != 'publish':
                raise AgentError('loop_release_forbidden')
            if state.get('state_schema_version') == 'loop-state-conditions-v4':
                from backend.agent_loop.conditions import condition_view
                expected_view = condition_view(state, task['facts'], h.business.validator.policies, h.settings.shop_id)
                from backend.agent_loop.contracts import scoped_contract
                if scoped_contract(h) and not expected_view['required_conditions']:
                    expected_view = dict(expected_view, derived_conditions=[], applicability='not_applicable')
                if state['condition_view'] != expected_view:
                    raise AgentError('loop_condition_binding_mismatch')
            validate_candidate(candidate, state)
            from backend.agent_loop import ablation
            if ablation.has_review(h):
                validate_review(review, candidate, state)
            if grant != ablation.release_eligibility(h, state, h.tid, h.attempt):
                raise AgentError('loop_release_binding_mismatch')
            b, limits = task['budget'], h.config
            if (max(b['reserved_tokens'], b['request_tokens']) > limits.token_budget or
                b['models'] > limits.task_models or b['request_models'] > limits.request_models or
                b['tools'] > limits.task_tools or b['request_tools'] > limits.request_tools or
                b['active_ms'] >= limits.task_seconds * 1000 or b['request_ms'] >= h.settings.request_timeout_seconds * 1000):
                raise AgentError('loop_release_budget_exceeded')
            result = dict(task_id=h.tid, trace_id=h.tid, answer=candidate['answer'],
                evidence=state['bundle']['items'], reviewed=ablation.has_review(h), prechecked=False, outcome='full_answer',
                coverage='full', runtime_version=VERSION, mode=h.settings.rag_mode, path='dynamic',
                reason=None, budget=b, candidate_id=candidate['candidate_id'], bundle_id=candidate['bundle_id'],
                review_id=review['review_id'] if review else None, correction_rounds=state['repair_episodes'])
            if ablation.mode(h):
                result['ablation'] = ablation.mode(h)
            final = dict(state, phase='terminal', state_version=state['state_version']+1)
            await c.execute('UPDATE agent_loop_states SET state=%s,state_version=%s,updated_at=now() WHERE task_id=%s',
                (Jsonb(final), final['state_version'], h.tid))
            await c.execute("UPDATE agent_tasks SET status='completed',state=%s,updated_at=now() WHERE id=%s",
                (Jsonb({'runtime_version':VERSION}), h.tid))
            await c.execute("UPDATE request_traces SET status='completed',result=%s,error=NULL,finished_at=now(),elapsed_ms=%s WHERE id=%s",
                (Jsonb(result), b['active_ms'], h.tid))
            for kind, payload in [('answer_release', grant), ('done', result)]:
                await c.execute('INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,%s,%s)', (h.tid, kind, Jsonb(payload)))
        return result
