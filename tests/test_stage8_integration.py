"""管理页面API实际PostgreSQL联调，模型为明确mock。"""
import asyncio
import os
import httpx
import pytest
from backend.agent_contract import AgentRequest
from backend.main import create_app
from backend.business_tools import BusinessService
from test_stage6_integration import environment, submit, run

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='需要隔离PostgreSQL')]


async def test_management_auth_conflict_freezing_and_index_no_deletion():
    s, db, store = await environment(api_access_token='stage8-fixture-token')
    # 清理只限本测试库的新管理记录；从不删除现有业务/知识/追踪。
    async with db.connection() as c:
        await c.execute('DELETE FROM runtime_profiles WHERE shop_id=%s', (s.shop_id,))
    index = await db.current_index()
    sid = await store.create_session('CUST-001')
    tid, _ = await submit(store, s, sid, '退款金额是多少？')
    old_task, _ = await run(store, s, tid)
    assert old_task['status'] == 'waiting'
    app = create_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        assert (await client.get('/api/configuration')).status_code == 401
        client.headers['Authorization'] = 'Bearer stage8-fixture-token'
        data = (await client.get('/api/configuration')).json()
        assert data['revision'] == 0 and 'thinking_model_api_key' not in data['effective']
        assert (await client.put('/api/configuration', json={'revision': 0, 'agent': '../../.env'})).status_code == 409
        assert (await client.post(f"/api/indexes/{index['id']}/bm25")).status_code == 200
        chosen = {'revision': 0, 'retrieval': 'r3-hybrid-rrf.v1.0.0.json', 'agent': 'fixed.v1.1.0.json', 'review': 'off.v1.0.0.json'}
        assert (await client.put('/api/configuration', json=chosen)).status_code == 200
        assert (await client.put('/api/configuration', json=chosen)).status_code == 409
        knowledge = (await client.get('/api/knowledge')).json()
        assert knowledge['documents'] and knowledge['indexes'] and knowledge['jobs']
        assert str((await db.current_index())['id']) == str(index['id'])
        frozen = (await store.task(tid))['frozen']
        assert frozen == old_task['frozen']
        traces = (await client.get('/api/traces')).json()['items']
        assert any(str(row['id']) == tid for row in traces)
        assert (await client.get('/api/agent/sessions')).json()['items']
        # 新请求使用刚应用配置，旧澄清任务保持原配置。
        new_sid = (await client.post('/api/agent/sessions')).json()['session_id']
        body = AgentRequest(session_id=new_sid, request_id=__import__('uuid').uuid4(), question='鞋靴退货规则是什么？')
        response = await client.post('/api/agent/chat', json=body.model_dump(mode='json'))
        assert response.status_code == 200 and 'event: done' in response.text
        new = await store.task(response.headers['X-Task-ID'])
        assert new['frozen']['agent_config']['strategy'] == 'fixed'
        assert new['frozen']['retrieval_config']['mode'] == 'hybrid'
    async with db.connection() as c:
        await c.execute('DELETE FROM runtime_profiles WHERE shop_id=%s', (s.shop_id,))


async def test_index_selection_rejects_hybrid_config_without_bm25_artifact():
    s, db, store = await environment(api_access_token='stage8-fixture-token')
    # 清理只限本测试库的新管理记录；从不删除现有业务/知识/追踪。
    async with db.connection() as c:
        await c.execute('DELETE FROM runtime_profiles WHERE shop_id=%s', (s.shop_id,))
    index = await db.current_index()
    app = create_app(s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        client.headers['Authorization'] = 'Bearer stage8-fixture-token'
        assert (await client.post(f"/api/indexes/{index['id']}/bm25")).status_code == 200
        assert (await client.put('/api/configuration', json={'revision': 0, 'retrieval': 'r3-hybrid-rrf.v1.0.0.json'})).status_code == 200
        # 导入构建新索引但不切换发布；后台任务完成后出现第二个已发布索引。
        prior_jobs = {j['id'] for j in (await client.get('/api/knowledge')).json()['jobs']}
        assert (await client.post('/api/knowledge/import')).status_code == 202
        job = None
        for _ in range(60):
            await asyncio.sleep(0.5)
            jobs = (await client.get('/api/knowledge')).json()['jobs']
            new_jobs = [j for j in jobs if j['id'] not in prior_jobs]
            if new_jobs and new_jobs[0]['status'] in {'completed', 'failed'}:
                job = new_jobs[0]; break
        assert job and job['status'] == 'completed', job
        imported = str(job['index_id'])
        assert imported != str(index['id'])
        # 负向路径：双路配置下选择未构建BM25派生产物的新索引必须拒绝，发布指针不变。
        rejected = await client.post(f'/api/indexes/{imported}/select')
        assert rejected.status_code == 409 and rejected.json()['detail'] == 'bm25_index_missing'
        assert str((await db.current_index())['id']) == str(index['id'])
        assert (await client.post(f'/api/indexes/{imported}/bm25')).status_code == 200
        assert (await client.post(f'/api/indexes/{imported}/select')).status_code == 200
        assert str((await db.current_index())['id']) == imported
        # 恢复原发布指针，避免影响同库其他用例。
        assert (await client.post(f"/api/indexes/{index['id']}/select")).status_code == 200
        assert str((await db.current_index())['id']) == str(index['id'])
    async with db.connection() as c:
        await c.execute('DELETE FROM runtime_profiles WHERE shop_id=%s', (s.shop_id,))
