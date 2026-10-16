import asyncio
import json
import os
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from backend.config import Settings
from backend.db import Database
from backend.ingest import ingest
from backend.models import ModelAdapter, ModelError, mock_vector
from backend.rag import RAGService

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv('RUN_DB_TESTS') != '1', reason='显式设置 RUN_DB_TESTS=1，使用隔离测试数据库')]


async def test_database_ingestion_retrieval_trace_and_failure_atomicity():
    url = os.getenv('TEST_DATABASE_URL', 'postgresql://rag:rag-local-only@127.0.0.1:5433/rag_stage2_test')
    settings = Settings(database_url=url, rag_mode='mock')
    db, adapter = Database(settings), ModelAdapter(settings)
    try:
        await db.migrate()
        await db.migrate()
        imported = await ingest(settings, db, adapter)
        index = await db.current_index()
        assert imported['children'] == 58 and imported['documents'] == 36
        query = '免运费门槛是多少'
        old = await db.search(index['id'], mock_vector(query), {'purchased_at': '2026-06-30T23:59:59+08:00'})
        new = await db.search(index['id'], mock_vector(query), {'purchased_at': '2026-07-01T00:00:00+08:00'})
        assert all(row['document_version'] == '1.0.0' for row in old if row['document_id'] == 'POL-SHIPPING')
        assert all(row['document_version'] == '1.1.0' for row in new if row['document_id'] == 'POL-SHIPPING')
        rows = await db.search(index['id'], mock_vector(query), {'category_id': 'customized'})
        assert all('customized' in row['metadata']['category_ids'] for row in rows)
        assert [row['rank'] for row in rows] == list(range(1, len(rows) + 1))
        trace_id, frozen = await db.new_trace(query, {})
        await RAGService(settings, db, adapter).run(trace_id, frozen, query, {})
        trace = await db.trace(trace_id)
        assert trace['status'] == 'completed'
        assert trace['result']['mode'] == 'mock'
        assert trace['result']['citation_check']['invalid_ids'] == []
        assert {event['kind'] for event in trace['events']} >= {'retrieval', 'context', 'delta', 'done', 'evidence'}
        for item in trace['result']['evidence']:
            evidence = await db.evidence(index['id'], item['chunk_id'])
            assert evidence['source_text'][evidence['source_start']:evidence['source_end']] == evidence['text']
        assert len(trace['model_calls']) == 2
        assert all(call['usage'] is None for call in trace['model_calls'])

        class FailedAdapter:
            async def embed(self, texts, record): raise ModelError('deliberate_embedding_failure')
        with pytest.raises(ModelError): await ingest(settings, db, FailedAdapter())
        assert (await db.current_index())['id'] == index['id']
        async with db.connection() as connection:
            job = await (await connection.execute('SELECT status,error FROM ingestion_jobs ORDER BY started_at DESC LIMIT 1')).fetchone()
            assert job['status'] == 'failed'
            assert job['error']['code'] == 'deliberate_embedding_failure'
        missing_id, _ = await db.new_trace('问题', {})
        await RAGService(settings, db, adapter).run(missing_id, None, '问题', {})
        assert (await db.trace(missing_id))['error']['code'] == 'knowledge_index_missing'
        mismatch_id, _ = await db.new_trace('问题', {})
        changed = settings.model_copy(update={'embedding_model_id': 'different-model'})
        await RAGService(changed, db, adapter).run(mismatch_id, frozen, '问题', {})
        assert (await db.trace(mismatch_id))['error']['code'] == 'embedding_index_incompatible'
    finally:
        await adapter.close()


async def test_api_sse_auth_validation_and_replay():
    import httpx
    from backend.main import create_app
    settings = Settings(database_url=os.getenv('TEST_DATABASE_URL', 'postgresql://rag:rag-local-only@127.0.0.1:5433/rag_stage2_test'), rag_mode='mock', api_access_token='test-local-token')
    db, adapter = Database(settings), ModelAdapter(settings)
    app = create_app(settings, db, adapter)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/api/chat', json={'question': '你好'})).status_code == 401
            headers = {'Authorization': 'Bearer test-local-token'}
            assert (await client.post('/api/chat', json={'question': '你好', 'shop_id': 'other'}, headers=headers)).status_code == 422
            response = await client.post('/api/chat', json={'question': '鞋盒不在了能退吗'}, headers=headers)
            assert response.status_code == 200 and 'event: delta' in response.text and 'event: done' in response.text
            trace_id = response.headers['X-Trace-ID']
            trace = (await client.get('/api/traces/' + trace_id, headers=headers)).json()
            assert trace['status'] == 'completed'
            replay = await client.get('/api/traces/' + trace_id + '/events', headers=headers)
            assert 'event: done' in replay.text
            item = trace['result']['evidence'][0]
            evidence = await client.get('/api/evidence/' + trace['index_id'] + '/' + item['chunk_id'], headers=headers)
            assert evidence.status_code == 200
            assert (await client.get('/api/traces/' + trace_id)).status_code == 401
