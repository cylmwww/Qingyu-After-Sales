"""在线与评测共用分派；route 的业务规则保持唯一实现。"""
from uuid import uuid4
from psycopg.types.json import Jsonb
from langgraph.graph import StateGraph, START, END

from backend.agent import AgentRuntime, GraphState
from backend.agent_store import AgentError


def runtime_class(frozen):
    version = frozen.get('runtime_version', 'legacy')
    if version == 'legacy':
        return AgentRuntime
    if version == 'agent-loop-v1':
        return DispatchRuntime
    raise AgentError('unsupported_runtime_version')


def create_runtime(current, task, adapter=None):
    return runtime_class(task['frozen'])(current, task, adapter)


class DispatchRuntime(AgentRuntime):
    async def route(self, state):
        async def route_once():
            return await super(DispatchRuntime, self).route(state)
        routed = await self.operation('loop:route', 'route', {'revision':self.revision}, route_once)
        actual = 'agent-loop-v1' if routed['path']=='dynamic' and routed['status']=='plan' else 'legacy'
        await self.event('runtime_dispatch', {'runtime_version':actual, 'configured_runtime':'agent-loop-v1',
            'reason':'dynamic_handoff' if actual=='agent-loop-v1' else 'existing_route_retained'})
        return dict(routed, status='loop' if actual=='agent-loop-v1' else routed['status'])

    async def before_call(self, role, tokens):
        if not getattr(self, 'loop_operation_key', None):
            return await super().before_call(role, tokens)
        await self.store.reserve(self.tid, self.attempt, self.config, 'models', tokens)
        self.loop_reservation = dict(reservation_id=str(uuid4()), operation_key=self.loop_operation_key,
                                     role=role, reserved_tokens=tokens)
        await self.event('model_reservation', self.loop_reservation)

    async def record(self, payload):
        if not getattr(self, 'loop_operation_key', None):
            return await super().record(payload)
        reservation = self.loop_reservation
        if reservation is None or reservation['role'] != payload['role']:
            raise AgentError('loop_model_reservation_mismatch')
        value = dict(payload, **{k:reservation[k] for k in ['reservation_id','operation_key']},
                     runtime_version='agent-loop-v1', prompt_version=self.settings.loop_config.prompt_version)
        async with self.db.connection() as c:
            row = await self.store.locked(c, self.tid, self.attempt)
            await c.execute('INSERT INTO model_calls(id,trace_id,payload) VALUES(%s,%s,%s)', (value['call_id'],self.tid,Jsonb(value)))
            budget = dict(row['budget'])
            roles = dict(budget.get('roles') or {})
            roles[payload['role']] = roles.get(payload['role'],0)+1
            budget['roles'] = roles
            usage = payload.get('usage')
            if isinstance(usage,dict) and type(usage.get('total_tokens')) is int and usage['total_tokens']>=0:
                delta = usage['total_tokens']-reservation['reserved_tokens']
                budget['reported_tokens'] += usage['total_tokens']
                budget['request_tokens'] += delta
                budget['reserved_tokens'] += delta
            await c.execute('UPDATE agent_tasks SET budget=%s WHERE id=%s', (Jsonb(budget),self.tid))
        self.loop_reservation = None

    def graph(self, saver):
        from backend.agent_loop.runtime import LoopRuntime
        graph = StateGraph(GraphState)
        for name in ['route','plan','execute','clarify','prepare','precheck','answer','postcheck','resolve','publish']:
            graph.add_node(name, getattr(self,name))
        async def loop(state):
            return await LoopRuntime(self).run(state, saver)
        graph.add_node('loop', loop)
        graph.add_edge(START,'route')
        graph.add_conditional_edges('route',lambda s:s['status'],
            {'loop':'loop','plan':'plan','execute':'execute','clarify':'clarify','answer':'prepare'})
        # fast 的原升级仍留在 legacy，不新增条件也不二次调用 intent。
        for name in ['plan','execute']:
            graph.add_conditional_edges(name,lambda s:s['status'],
                {'plan':'plan','execute':'execute','clarify':'clarify','answer':'prepare'})
        graph.add_edge('clarify','route')
        for name in ['prepare','precheck','answer','postcheck','resolve']:
            graph.add_conditional_edges(name,lambda s:s['status'],
                {n:n for n in ['answer','precheck','postcheck','resolve','plan','clarify','publish']}|{'completed':END})
        graph.add_edge('publish',END)
        graph.add_edge('loop',END)
        return graph.compile(checkpointer=saver)
