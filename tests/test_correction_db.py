"""在显式隔离数据库中验证操作持久化和模型预算结算。"""
import os
from uuid import uuid4

import pytest

from backend.agent import AgentRuntime
from test_stage6_integration import environment, submit, run

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    os.getenv('RUN_DB_TESTS') != '1', reason='需要隔离PostgreSQL')]


async def test_deterministic_operation_is_durable():
    s, db, store = await environment()
    sid = await store.create_session('CUST-001')
    tid, _ = await submit(store, s, sid, '帮我取消订单')
    task, trace = await run(store, s, tid)
    assert task['status'] == 'completed', trace['error']
    assert '只读' in trace['result']['answer']
    rows = await store.operation_results(tid, task['revision'])
    assert any(r['kind'] == 'deterministic' and '只读' in r['result']['answer'] for r in rows)


async def test_known_usage_releases_reservation_unknown_usage_keeps_it():
    s, db, store = await environment()
    sid = await store.create_session('CUST-001')
    tid, _ = await submit(store, s, sid, '退货规则')
    runtime = AgentRuntime(s, await store.task(tid))
    try:
        await runtime.before_call('post_answer_review', 1000)
        await runtime.record({'call_id': str(uuid4()), 'role': 'post_answer_review',
                              'usage': {'total_tokens': 100}, 'status': 'success'})
        budget = (await store.task(tid))['budget']
        assert budget['reserved_tokens'] == budget['request_tokens'] == 100
        assert budget['reported_tokens'] == 100
        await runtime.before_call('generation', 500)
        await runtime.record({'call_id': str(uuid4()), 'role': 'generation',
                              'usage': None, 'status': 'failed'})
        budget = (await store.task(tid))['budget']
        assert budget['reserved_tokens'] == budget['request_tokens'] == 600
        assert budget['reported_tokens'] == 100
        assert budget['roles'] == {'post_answer_review': 1, 'generation': 1}
    finally:
        await runtime.adapter.close()
