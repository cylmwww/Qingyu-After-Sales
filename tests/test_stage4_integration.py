import json
import os
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from backend.config import ROOT, Settings
from backend.db import Database
from backend.ingest import ingest
from backend.index_config import IndexCompatibilityError
from backend.knowledge import digest
from backend.models import ModelAdapter, mock_vector
from backend.rag import RAGService, make_context
from backend.retrieval import retrieve
from backend.retrieval_config import BM25Config

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='显式独立阶段四测试库')]


def settings(name=None, **extra):
    return Settings(**dict({'database_url':os.getenv('TEST_DATABASE_URL','postgresql://rag:rag-local-only@127.0.0.1:5433/rag_stage4_test'),
        'rag_mode':'mock','index_config_path':None,'retrieval_config_path':f'configs/retrieval/{name}.v1.0.0.json' if name else None},**extra))


async def test_pipeline_baseline_switches_dedup_permissions_versions_and_trace():
    s=settings(); db=Database(s); adapter=ModelAdapter(s)
    try:
        await db.migrate(); await db.migrate()
        built=await ingest(s,db,adapter,publish=False); index=await db.get_index(built['index_id'])
        question='已激活数码配件质量问题能退吗'
        filters={'category_id':'digital_accessories','after_sales_applied_at':'2026-10-09T10:00:00+08:00'}
        legacy=await db.search(index['id'],mock_vector(question),filters)
        before=[r['chunk_id'] for r in legacy]
        bm25_id=None
        for name in ['r1-vector','r2-bm25','r3-hybrid-rrf','r4-hybrid-rrf-rerank']:
            cs=settings(name); cb=Database(cs); ca=ModelAdapter(cs)
            try:
                if name!='r1-vector':
                    b=await cb.build_bm25(built['index_id']); bm25_id=b['bm25_index_id']
                    assert (await cb.build_bm25(built['index_id']))['bm25_index_id']==bm25_id
                tid,frozen=await cb.new_trace(question,filters,built['index_id'])
                await RAGService(cs,cb,ca).run(tid,frozen,question,filters)
                trace=await cb.trace(tid)
                assert trace['status']=='completed'
                evidence=trace['result']['evidence']
                assert len(evidence)<=6 and len({r['chunk_id'] for r in evidence})==len(evidence)
                assert all('digital_accessories' in r['metadata']['category_ids'] for r in evidence)
                kinds={e['kind'] for e in trace['events']}
                assert kinds >= {'metadata_filter','fusion','retrieval_route','rerank','evidence','context','done'}
                routes=[e['payload'] for e in trace['events'] if e['kind']=='retrieval_route']
                assert {r['route'] for r in routes}==({'vector'} if name=='r1-vector' else {'bm25'} if name=='r2-bm25' else {'vector','bm25'})
                assert [c['role'] for c in trace['model_calls']]==(['generation'] if name=='r2-bm25' else ['embedding','rerank','generation'] if name=='r4-hybrid-rrf-rerank' else ['embedding','generation'])
                if name=='r1-vector':
                    assert [r['chunk_id'] for r in routes[0]['results']]==before
                    assert make_context(legacy,s)[1]==next(e['payload']['actual_context'] for e in trace['events'] if e['kind']=='context')
                assert await Database(settings(shop_id='FOREIGN')).trace(tid) is None
                assert await Database(settings(shop_id='FOREIGN')).evidence(built['index_id'],evidence[0]['chunk_id']) is None
                with pytest.raises(IndexCompatibilityError): await Database(settings(shop_id='FOREIGN')).search(built['index_id'],mock_vector(question),{})
                with pytest.raises(IndexCompatibilityError): await Database(settings('r2-bm25',shop_id='FOREIGN')).build_bm25(built['index_id'])
            finally: await ca.close()
        # 未确认时间：不推断最新版本；已确认时间：向量与BM25共享SQL过滤。
        cb=Database(settings('r2-bm25'))
        pool=await cb.chunk_rows(built['index_id'],{})
        assert {r['document_version'] for r in pool if r['document_id']=='POL-SHIPPING'}=={'1.0.0','1.1.0'}
        for date,version in [('2026-06-30T23:59:59+08:00','1.0.0'),('2026-07-01T00:00:00+08:00','1.1.0')]:
            filtered=await cb.chunk_rows(built['index_id'],{'purchased_at':date})
            assert {r['document_version'] for r in filtered if r['document_id']=='POL-SHIPPING'}=={version}
        with pytest.raises(ValueError): await cb.chunk_rows(built['index_id'],{'category_id':'guess'})
        # 同版本词典/参数变化拒绝，不覆盖已构建派生索引。
        wrong=settings('r2-bm25').model_copy(update={'bm25_dictionary':{'version':'1.0.0','terms':['不同术语']}})
        with pytest.raises(IndexCompatibilityError,match='bm25_index_incompatible'):
            await Database(wrong).bm25_artifact(built['index_id'],pool)
        with pytest.raises(IndexCompatibilityError,match='bm25_index_version_conflict'):
            await Database(wrong).build_bm25(built['index_id'])
        changed=settings('r2-bm25')
        profile=changed.retrieval_config.model_copy(update={'hypothesis':'同名同版本漂移'})
        conflict=changed.model_copy(update={'retrieval_config':profile})
        with pytest.raises(IndexCompatibilityError,match='retrieval_config_version_conflict'):
            await Database(conflict).new_trace('问题',{},built['index_id'])
        # 派生产物篡改检测；只操作本测试新建的产物，再恢复。
        async with cb.connection() as c:
            saved=await(await c.execute('SELECT artifact FROM bm25_indexes WHERE id=%s',(bm25_id,))).fetchone()
            artifact=saved['artifact']; bad=dict(artifact,knowledge_version='wrong')
            await c.execute('UPDATE bm25_indexes SET artifact=%s WHERE id=%s',(Jsonb(bad),bm25_id))
        try:
            with pytest.raises(IndexCompatibilityError): await cb.bm25_artifact(built['index_id'],pool)
        finally:
            async with cb.connection() as c: await c.execute('UPDATE bm25_indexes SET artifact=%s WHERE id=%s',(Jsonb(artifact),bm25_id))
        async with cb.connection() as c: await c.execute("UPDATE knowledge_indexes SET status='withdrawn' WHERE id=%s",(built['index_id'],))
        with pytest.raises(IndexCompatibilityError): await cb.chunk_rows(built['index_id'],{})
    finally: await adapter.close()


async def test_hybrid_api_auth_and_unknown_inferred_filters_rejected():
    from backend.main import create_app
    s=settings('r3-hybrid-rrf',api_access_token='local-test-token')
    db=Database(s); adapter=ModelAdapter(s)
    await db.migrate(); b=await ingest(s,db,adapter,publish=False); await db.build_bm25(b['index_id'])
    app=create_app(s,db,adapter)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.post('/api/chat',json={'question':'政策'})).status_code==401
            headers={'Authorization':'Bearer local-test-token'}
            assert (await client.post('/api/chat',json={'question':'政策','shop_id':'FOREIGN'},headers=headers)).status_code==422
            assert (await client.post('/api/chat',json={'question':'政策','guessed_category_id':'customized'},headers=headers)).status_code==422
            res=await client.post('/api/chat',json={'question':'退货运费如何承担','index_id':b['index_id']},headers=headers)
            assert res.status_code==200 and 'event: fusion' in res.text and 'event: done' in res.text
            assert (await client.get('/api/traces/'+res.headers['X-Trace-ID'])).status_code==401
