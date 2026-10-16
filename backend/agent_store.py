"""会话串行、请求幂等、租约、尝试隔离与可靠操作结果。"""
from datetime import datetime, timezone
import uuid
import re
from psycopg.types.json import Jsonb

from backend.agent_contract import frozen_settings
from backend.knowledge import digest


class AgentError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def empty_budget():
    return dict(tools=0, models=0, request_tools=0, request_models=0,
                reserved_tokens=0, request_tokens=0, active_ms=0, request_ms=0, reported_tokens=0)


def merge_facts(old, new, order_id=None):
    values = {k: v['value'] for k, v in old.items()}
    target = new.get('order_id', order_id or values.get('order_id'))
    switched = target != values.get('order_id')
    result = {} if switched else {k:v for k,v in old.items() if v['source'] in {'user', 'user_text'}}
    for k, value in new.items():
        if value is None:
            result.pop(k, None)
            continue
        result[k] = {'value': value, 'source': 'user', 'order_id': target,
                     'confirmed': True, 'updated_at': datetime.now(timezone.utc).isoformat()}
    return result, switched


def explicit_text_facts(question):
    """有限字面抽取，不用模型推测身份、金额或缺失参数。"""
    facts = {}
    phrases = {'activated': [('没激活', False), ('未激活', False), ('已经激活', True), ('激活过了', True)],
        'packaging_intact': [('包装完整', True), ('鞋盒扔了', False), ('包装不完整', False)],
        'tags_intact': [('吊牌还在', True), ('吊牌剪了', False)],
        'return_reason': [('无理由', 'no_reason'), ('质量问题', 'quality')]}
    for field, candidates in phrases.items():
        values = {value for phrase, value in candidates if phrase in question}
        if len(values) == 1:
            facts[field] = values.pop()
    return facts


class AgentStore:
    def __init__(self, db):
        self.db = db

    async def session(self, id, customer):
        async with self.db.connection() as c:
            row = await (await c.execute('SELECT * FROM agent_sessions WHERE id=%s AND shop_id=%s AND customer_id=%s',
                (id, self.db.settings.shop_id, customer))).fetchone()
            if not row:
                raise AgentError('session_not_found')
            return row

    async def create_session(self, customer):
        id = str(uuid.uuid4())
        async with self.db.connection() as c:
            await c.execute('INSERT INTO agent_sessions(id,shop_id,customer_id) VALUES(%s,%s,%s)',
                            (id, self.db.settings.shop_id, customer))
        return id

    async def task(self, id, customer=None):
        async with self.db.connection() as c:
            row = await (await c.execute('''SELECT t.*,s.customer_id,r.index_id FROM agent_tasks t
                JOIN agent_sessions s ON s.id=t.session_id JOIN request_traces r ON r.id=t.id
                WHERE t.id=%s AND s.shop_id=%s''', (id, self.db.settings.shop_id))).fetchone()
        if not row or customer is not None and row['customer_id'] != customer:
            raise AgentError('task_not_found')
        return row

    async def submit(self, body, customer, business):
        """会话行锁内同时建立trace、任务和幂等键，避免孤立请求。"""
        s = self.db.settings
        fingerprint = digest(body.model_dump(mode='json'))
        sid, rid = str(body.session_id), str(body.request_id)
        async with self.db.connection() as c:
            session = await (await c.execute('SELECT * FROM agent_sessions WHERE id=%s AND shop_id=%s AND customer_id=%s FOR UPDATE',
                (sid, s.shop_id, customer))).fetchone()
            if not session:
                raise AgentError('session_not_found')
            previous = await (await c.execute('SELECT * FROM agent_submissions WHERE session_id=%s AND request_id=%s', (sid, rid))).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise AgentError('idempotency_conflict')
                return str(previous['task_id']), False
            active = await (await c.execute("SELECT * FROM agent_tasks WHERE session_id=%s AND status IN ('running','waiting','failed','interrupted') FOR UPDATE", (sid,))).fetchone()
            if body.action == 'new':
                if active:
                    raise AgentError('session_busy_cancel_or_resume')
                supplied = {**explicit_text_facts(body.question), **body.facts.values()}
                ids = set(re.findall(r'(?<![A-Za-z0-9-])ORD-\d{3}(?![A-Za-z0-9-])', body.question))
                if len(ids) == 1 and 'order_id' not in supplied:
                    supplied['order_id'] = ids.pop()
                facts, _ = merge_facts(session['facts'], supplied)
                index = await (await c.execute('''SELECT i.*,s.snapshot AS build_config FROM knowledge_indexes i
                    JOIN config_snapshots s ON s.id=i.config_id WHERE i.id=COALESCE(%s::uuid,
                    (SELECT index_id FROM knowledge_publications WHERE shop_id=%s)) AND i.shop_id=%s FOR SHARE OF i''',
                    (str(body.index_id) if body.index_id else None, s.shop_id, s.shop_id))).fetchone()
                self.db.check_index(index)
                config_id = await self.db.save_config(c, s)
                profile = s.agent_config.model_dump(mode='json')
                await c.execute('INSERT INTO agent_config_versions VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                    (profile['id'], profile['version'], digest(profile), Jsonb(profile)))
                saved = await (await c.execute('SELECT fingerprint FROM agent_config_versions WHERE id=%s AND version=%s',
                    (profile['id'], profile['version']))).fetchone()
                if saved['fingerprint'] != digest(profile):
                    raise AgentError('agent_config_version_conflict')
                tid, attempt = str(uuid.uuid4()), str(uuid.uuid4())
                await c.execute("INSERT INTO request_traces(id,owner_id,config_id,index_id,question,filters,status) VALUES(%s,%s,%s,%s,%s,%s,'running')",
                    (tid, self.db.owner_id, config_id, index['id'], body.question, Jsonb({'agent': True})))
                await c.execute('''INSERT INTO agent_tasks(id,session_id,status,attempt,question,facts,frozen,business_hash,snapshot_id,budget,lease_until)
                    VALUES(%s,%s,'running',%s,%s,%s,%s,%s,%s,%s,now()+interval '20 seconds')''',
                    (tid, sid, attempt, body.question, Jsonb(facts), Jsonb(frozen_settings(s)), business.fingerprint,
                     business.snapshot_id, Jsonb(empty_budget())))
            else:
                if not active or str(active['id']) != str(body.task_id) or active['status'] == 'running':
                    raise AgentError('task_not_resumable')
                if body.action == 'clarify' and active['status'] != 'waiting':
                    raise AgentError('clarification_not_pending')
                if body.action == 'resume' and active['status'] not in {'interrupted', 'failed'}:
                    raise AgentError('explicit_resume_not_needed')
                if body.index_id and str(body.index_id) != str((await (await c.execute('SELECT index_id FROM request_traces WHERE id=%s', (active['id'],))).fetchone())['index_id']):
                    raise AgentError('frozen_index_change_forbidden')
                from backend.agent_loop.failures import is_v4
                if body.action == 'resume' and is_v4(active['frozen']) and active['status'] == 'failed':
                    trace = await (await c.execute('SELECT error FROM request_traces WHERE id=%s', (active['id'],))).fetchone()
                    if not (trace['error'] or {}).get('resumable', False):
                        raise AgentError('task_not_resumable')
                tid = str(active['id'])
                supplied = {**explicit_text_facts(body.question), **body.facts.values()}
                ids = set(re.findall(r'(?<![A-Za-z0-9-])ORD-\d{3}(?![A-Za-z0-9-])', body.question))
                if len(ids) == 1 and 'order_id' not in supplied:
                    supplied['order_id'] = ids.pop()
                facts, switched = merge_facts(active['facts'], supplied)
                changed = any(k not in active['facts'] or active['facts'][k]['value'] != v for k, v in supplied.items())
                if active['frozen'].get('runtime_version') == 'agent-loop-v1':
                    loop = await (await c.execute('SELECT task_id FROM agent_loop_states WHERE task_id=%s', (tid,))).fetchone()
                    if loop and (changed or body.action == 'revise'):
                        raise AgentError('loop_revision_change_not_supported_in_slice1')
                    if loop and body.question.strip() not in {active['question'].strip(), '继续', '恢复', '重试'}:
                        raise AgentError('loop_scope_change_requires_new_task')
                revision = active['revision'] + int(changed or body.action == 'revise')
                state = dict(active['state'], resume_action=body.action, question_update=body.question,
                             facts_changed=changed or body.action == 'revise', order_switched=switched)
                # 同请求故障恢复沿用请求预算；澄清/修正是新的用户请求，但仍共享任务预算。
                budget = dict(active['budget']) if body.action == 'resume' else dict(
                    active['budget'], request_tools=0, request_models=0, request_tokens=0, request_ms=0)
                await c.execute("UPDATE agent_tasks SET status='running',attempt=%s,revision=%s,facts=%s,state=%s,budget=%s,lease_until=now()+interval '20 seconds',updated_at=now() WHERE id=%s",
                    (str(uuid.uuid4()), revision, Jsonb(facts), Jsonb(state), Jsonb(budget), tid))
                await c.execute("UPDATE request_traces SET status='running',finished_at=NULL,error=NULL WHERE id=%s", (tid,))
            await c.execute('UPDATE agent_sessions SET facts=%s WHERE id=%s', (Jsonb(facts), sid))
            await c.execute('INSERT INTO agent_submissions VALUES(%s,%s,%s,%s)', (sid, rid, fingerprint, tid))
            await c.execute('INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,\'submission\',%s)',
                            (tid, Jsonb({'request_id': rid, 'action': body.action, 'facts': facts})))
        return tid, True

    async def locked(self, c, tid, attempt):
        # 与submit/cancel保持会话→任务的锁顺序，避免并发提交和落盘死锁。
        await c.execute('SELECT s.id FROM agent_sessions s JOIN agent_tasks t ON t.session_id=s.id WHERE t.id=%s FOR UPDATE OF s', (tid,))
        row = await (await c.execute('SELECT * FROM agent_tasks WHERE id=%s FOR UPDATE', (tid,))).fetchone()
        if not row or str(row['attempt']) != str(attempt) or row['status'] != 'running':
            raise AgentError('stale_attempt')
        if row['frozen'].get('runtime_version') == 'agent-loop-v1' and row['lease_until'] <= datetime.now(timezone.utc):
            raise AgentError('stale_attempt')
        return row

    async def event(self, tid, attempt, kind, payload):
        async with self.db.connection() as c:
            await self.locked(c, tid, attempt)
            await c.execute('INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,%s,%s)', (tid, kind, Jsonb(payload)))

    async def reserve(self, tid, attempt, config, kind, tokens=0):
        async with self.db.connection() as c:
            row = await self.locked(c, tid, attempt)
            b = dict(row['budget'])
            b[kind] += 1
            b['request_' + kind] += 1
            b['reserved_tokens'] += tokens
            b['request_tokens'] += tokens
            if b[kind] > getattr(config, 'task_' + kind) or b['request_' + kind] > getattr(config, 'request_' + kind):
                raise AgentError(kind + '_budget_exceeded')
            if b['request_tokens'] > config.token_budget or b['reserved_tokens'] > config.token_budget:
                raise AgentError('token_budget_exceeded')
            await c.execute('UPDATE agent_tasks SET budget=%s WHERE id=%s', (Jsonb(b), tid))

    async def heartbeat(self, tid, attempt, elapsed_ms, settings):
        exceeded = False
        async with self.db.connection() as c:
            row = await self.locked(c, tid, attempt)
            b = dict(row['budget'])
            b['active_ms'] += elapsed_ms
            b['request_ms'] += elapsed_ms
            await c.execute("UPDATE agent_tasks SET budget=%s,lease_until=now()+interval '20 seconds',updated_at=now() WHERE id=%s", (Jsonb(b), tid))
            exceeded = b['active_ms'] >= settings.agent_config.task_seconds * 1000 or b['request_ms'] >= settings.request_timeout_seconds * 1000
        # 超限仍提交实际消耗，不能通过恢复反复返还最后一个计时窗口。
        if exceeded:
            raise AgentError('active_time_budget_exceeded')

    async def operation(self, tid, attempt, revision, key, kind, arguments):
        async with self.db.connection() as c:
            await self.locked(c, tid, attempt)
            row = await (await c.execute('SELECT * FROM agent_operations WHERE task_id=%s AND revision=%s AND key=%s', (tid, revision, key))).fetchone()
            if row and row['arguments'] != arguments:
                raise AgentError('operation_argument_conflict')
            task = await (await c.execute('SELECT frozen FROM agent_tasks WHERE id=%s', (tid,))).fetchone()
            from backend.agent_loop.failures import is_v4, tool_policy
            technical_failure = row and row['result'] and kind=='tool' and (
                tool_policy(row['result'])['reexecute'] if is_v4(task['frozen']) else
                row['result'].get('status')=='transient_failure' or (row['result'].get('error') or {}).get('code')=='tool_fault')
            if row and row['status'] == 'completed' and not technical_failure:
                await c.execute("INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,'operation_reused',%s)", (tid, Jsonb({'key': key, 'revision': revision})))
                return row['result']
            starts = row['starts'] + 1 if row else 1
            await c.execute('''INSERT INTO agent_operations(task_id,revision,key,kind,attempt,arguments,status,starts)
                VALUES(%s,%s,%s,%s,%s,%s,'started',%s) ON CONFLICT(task_id,revision,key)
                DO UPDATE SET attempt=excluded.attempt,status='started',starts=excluded.starts,started_at=now()''',
                (tid, revision, key, kind, attempt, Jsonb(arguments), starts))
            if row:
                await c.execute("INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,%s,%s)",
                    (tid, 'operation_retry_after_failure' if technical_failure else 'possible_reexecution',
                     Jsonb({'key': key, 'revision': revision, 'previous_status': row['status'], 'starts': starts,
                            'reason': 'explicit_resume_after_technical_failure' if technical_failure else 'result_not_durable_in_crash_window'})))
        return None

    async def result(self, tid, attempt, revision, key, result):
        async with self.db.connection() as c:
            row = await self.locked(c, tid, attempt)
            await c.execute("UPDATE agent_operations SET result=%s,status='completed',finished_at=now() WHERE task_id=%s AND revision=%s AND key=%s AND attempt=%s",
                            (Jsonb(result), tid, revision, key, attempt))
            if key.startswith('tool:') and result.get('status') in {'success', 'no_result'}:
                op = await (await c.execute('SELECT arguments FROM agent_operations WHERE task_id=%s AND revision=%s AND key=%s', (tid,revision,key))).fetchone()
                tool = op['arguments']['tool']
                fact = {'value': {'operation_key': key, 'call_id': result['call_id'], 'status': result['status']},
                        'source': 'tool', 'order_id': row['facts'].get('order_id',{}).get('value'),
                        'confirmed': result['status']=='success', 'updated_at': result['observed_at']}
                await c.execute("UPDATE agent_sessions SET facts=jsonb_set(facts,ARRAY[%s],%s) WHERE id=%s",
                                ('tool.'+tool, Jsonb(fact), row['session_id']))

    async def operation_results(self, tid, revision):
        async with self.db.connection() as c:
            return await (await c.execute("SELECT key,kind,arguments,result FROM agent_operations WHERE task_id=%s AND revision=%s AND status='completed' ORDER BY started_at", (tid, revision))).fetchall()

    async def finish(self, tid, attempt, status, state, result=None, code=None):
        async with self.db.connection() as c:
            row = await self.locked(c, tid, attempt)
            await c.execute('UPDATE agent_tasks SET status=%s,state=%s,updated_at=now() WHERE id=%s', (status, Jsonb(state), tid))
            error = {'code': code} if code else None
            await c.execute('UPDATE request_traces SET status=%s,result=%s,error=%s,finished_at=now(),elapsed_ms=%s WHERE id=%s',
                (status, Jsonb(result), Jsonb(error), row['budget']['active_ms'], tid))
            kind = 'clarification' if status == 'waiting' else 'done' if status == 'completed' else 'error'
            from backend.agent_loop.failures import is_v4, resumable_error
            can_resume = status in {'interrupted', 'failed'}
            if is_v4(row['frozen']):
                can_resume = can_resume and (status == 'interrupted' or resumable_error(code))
                error = dict(error or {}, resumable=can_resume)
                await c.execute('UPDATE request_traces SET error=%s WHERE id=%s', (Jsonb(error), tid))
            payload = result if result else {'code': code, 'task_id': tid, 'resumable': can_resume}
            await c.execute('INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,%s,%s)', (tid, kind, Jsonb(payload)))

    async def cancel(self, tid, customer):
        task = await self.task(tid, customer)
        async with self.db.connection() as c:
            await c.execute('SELECT id FROM agent_sessions WHERE id=%s FOR UPDATE', (task['session_id'],))
            row = await (await c.execute("UPDATE agent_tasks SET status='cancelled',attempt=%s,updated_at=now() WHERE id=%s AND status IN ('running','waiting','failed','interrupted') RETURNING id", (str(uuid.uuid4()), tid))).fetchone()
            if row:
                await c.execute("UPDATE request_traces SET status='cancelled',error=%s,finished_at=now() WHERE id=%s", (Jsonb({'code': 'user_cancelled'}), tid))
                await c.execute("INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,'error',%s)", (tid, Jsonb({'code': 'user_cancelled'})))

    async def recover_expired(self):
        async with self.db.connection() as c:
            rows = await (await c.execute("SELECT * FROM agent_tasks WHERE status='running' AND lease_until<now() FOR UPDATE SKIP LOCKED")).fetchall()
            for row in rows:
                # 最后心跳至租约结束窗口保守计入主动时间，恢复不返还调用预留。
                b = dict(row['budget'])
                b['active_ms'] += 20000
                b['request_ms'] += 20000
                await c.execute("UPDATE agent_tasks SET status='interrupted',attempt=%s,budget=%s WHERE id=%s", (str(uuid.uuid4()), Jsonb(b), row['id']))
                await c.execute("UPDATE request_traces SET status='interrupted',error=%s,finished_at=now() WHERE id=%s", (Jsonb({'code': 'lease_expired_explicit_resume'}), row['id']))
                await c.execute("INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,'error',%s)", (row['id'], Jsonb({'code': 'lease_expired_explicit_resume', 'resumable': True})))
