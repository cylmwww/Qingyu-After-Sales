import json
import os
from uuid import uuid4

import httpx
import psycopg
from psycopg.types.json import Jsonb
import pytest

from backend.config import ROOT, Settings
from backend.db import Database
from backend.ingest import ingest
from backend.index_config import IndexCompatibilityError
from backend.knowledge import digest
from backend.models import ModelAdapter, ModelError, mock_vector
from backend.rag import RAGService

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='只运行显式独立测试库')]


def settings(**kwargs):
    return Settings(database_url=os.getenv('TEST_DATABASE_URL','postgresql://rag:rag-local-only@127.0.0.1:5433/rag_stage3_test'),
                    rag_mode='mock',index_config_path=None,**kwargs)


async def test_versions_freezing_compatibility_dimensions_and_source_mapping(tmp_path):
    s=settings(); db=Database(s); adapter=ModelAdapter(s)
    try:
        await db.migrate(); await db.migrate()
        a=await ingest(s,db,adapter)
        old=await db.get_index(a['index_id'])
        # 模拟真实第二阶段旧快照形式，确保追加迁移不要求重建旧索引。
        async with db.connection() as c:
            legacy=s.snapshot(); legacy.pop('chunk_contract'); legacy.pop('index_configuration')
            config_id=digest(legacy)
            await c.execute('INSERT INTO config_snapshots(id,snapshot) VALUES(%s,%s) ON CONFLICT DO NOTHING',(config_id,Jsonb(legacy)))
            await c.execute('UPDATE knowledge_indexes SET config_id=%s WHERE id=%s',(config_id,old['id']))
        old=await db.get_index(old['id']); db.check_index(old)
        frozen_id,frozen=await db.new_trace('定制商品无理由限制',{},a['index_id'])
        async with db.connection() as c:
            before=await (await c.execute('SELECT id,text,embedding::text AS vector,parent_id FROM knowledge_chunks WHERE index_id=%s ORDER BY id',(a['index_id'],))).fetchall()
        candidate=settings(chunk_strategy='token-window-v1',chunk_target_tokens=200)
        cb=Database(candidate); ca=ModelAdapter(candidate)
        try:
            b=await ingest(candidate,cb,ca,publish=False)
            assert b['children'] > a['children'] and (await db.current_index())['id']==old['id']
            with pytest.raises(IndexCompatibilityError,match='chunk_index_incompatible'):
                await db.new_trace('问题',{},b['index_id'])
            await cb.select_index(b['index_id'])
            await RAGService(s,db,adapter).run(frozen_id,frozen,'定制商品无理由限制',{})
            trace=await db.trace(frozen_id)
            assert trace['status']=='completed' and trace['index_id']==old['id']
            for item in trace['result']['evidence']:
                source=await db.evidence(a['index_id'],item['chunk_id'])
                assert source['source_text'][source['source_start']:source['source_end']]==source['text']
                parent=await db.evidence(a['index_id'],source['parent_id'])
                assert parent['role']=='parent' and parent['document_key']==source['document_key']
            assert await cb.evidence(b['index_id'],str(uuid4())) is None
            async with db.connection() as c:
                after=await (await c.execute('SELECT id,text,embedding::text AS vector,parent_id FROM knowledge_chunks WHERE index_id=%s ORDER BY id',(a['index_id'],))).fetchall()
            assert before==after
            source=await cb.evidence(b['index_id'],(await cb.search(b['index_id'],mock_vector('政策'),{}))[0]['chunk_id'])
            async with cb.connection() as c:
                with pytest.raises(psycopg.errors.RaiseException,match='parent_relation_incompatible'):
                    await c.execute('UPDATE knowledge_chunks SET parent_id=id WHERE index_id=%s AND id=%s',(b['index_id'],source['id']))
            async with cb.connection() as c:
                with pytest.raises(psycopg.errors.RaiseException,match='source_range_incompatible'):
                    await c.execute('UPDATE knowledge_chunks SET source_start=source_start+1 WHERE index_id=%s AND id=%s',(b['index_id'],source['id']))
            await db.select_index(a['index_id'])
        finally: await ca.close()
        dims=settings(embedding_model_dimensions=768); dd=Database(dims); da=ModelAdapter(dims)
        try:
            d=await ingest(dims,dd,da,publish=False)
            rows=await dd.search(d['index_id'],mock_vector('退款',768),{})
            assert rows
            with pytest.raises(IndexCompatibilityError,match='embedding_index_incompatible'):
                await db.search(d['index_id'],mock_vector('退款'),{})
            with pytest.raises(IndexCompatibilityError,match='dimension'):
                await dd.search(d['index_id'],mock_vector('退款'),{})
            async with dd.connection() as c:
                with pytest.raises(psycopg.errors.RaiseException,match='embedding_dimension_incompatible'):
                    await c.execute("UPDATE knowledge_chunks SET embedding=%s::vector WHERE index_id=%s AND id=%s",('[1,2]',d['index_id'],rows[0]['chunk_id']))
        finally: await da.close()
        # 配置名+版本不可覆盖；改版本后可以另建。
        raw=json.loads((ROOT/'configs/indexes/window-200.v1.0.0.json').read_text(encoding='utf-8'))
        raw['id']='test-'+uuid4().hex[:10]
        path=tmp_path/'profile.json';path.write_text(json.dumps(raw),encoding='utf-8')
        profile=Settings(**dict(s.model_dump(exclude={'index_config'}),index_config_path=str(path)))
        pd=Database(profile);pa=ModelAdapter(profile)
        try:
            await ingest(profile,pd,pa,publish=False)
            raw['chunk']['target_tokens']=250;path.write_text(json.dumps(raw),encoding='utf-8')
            changed=Settings(**dict(s.model_dump(exclude={'index_config'}),index_config_path=str(path)))
            with pytest.raises(IndexCompatibilityError,match='index_config_version_conflict'):
                await ingest(changed,Database(changed),pa,publish=False)
            assert (await db.current_index())['id']==old['id']
        finally: await pa.close()
    finally: await adapter.close()


async def test_selected_index_api_and_withdrawal():
    from backend.main import create_app
    s=settings(api_access_token='stage3-test-token');db=Database(s);adapter=ModelAdapter(s)
    imported=await ingest(s,db,adapter)
    app=create_app(s,db,adapter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        headers={'Authorization':'Bearer stage3-test-token'}
        assert (await client.get('/api/indexes')).status_code==401
        indexes=(await client.get('/api/indexes',headers=headers)).json()
        assert any(row['id']==imported['index_id'] and row['selected'] for row in indexes)
        response=await client.post('/api/chat',json={'question':'商品退款依据','index_id':imported['index_id']},headers=headers)
        assert 'event: done' in response.text
        assert (await client.post('/api/chat',json={'question':'问题','index_id':str(uuid4())},headers=headers)).status_code==409
        assert (await client.post('/api/chat',json={'question':'问题','index_id':'bad'},headers=headers)).status_code==422
        async with db.connection() as c:
            await c.execute("UPDATE knowledge_indexes SET status='withdrawn' WHERE id=%s",(imported['index_id'],))
        assert (await client.post('/api/chat',json={'question':'问题','index_id':imported['index_id']},headers=headers)).status_code==409
    await adapter.close()
