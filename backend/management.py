"""本机管理接口：预设版本、知识状态与追踪，不接受任意文件路径或密钥。"""
import asyncio
import json
from uuid import UUID

from fastapi import Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from backend.config import ROOT, Settings
from backend.db import Database
from backend.ingest import ingest
from backend.knowledge import digest
from backend.models import ModelAdapter

CATEGORIES = {'retrieval': 'retrieval', 'answer': 'answers', 'agent': 'agents', 'review': 'reviews', 'loop': 'loops'}


def catalog():
    return {kind: [{'key': p.name, 'configuration': json.loads(p.read_text('utf-8-sig'))}
                   for p in sorted((ROOT / 'configs' / folder).glob('*.json'))]
            for kind, folder in CATEGORIES.items()}


class ProfileSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    retrieval: str | None = None
    answer: str | None = None
    agent: str | None = None
    review: str | None = None
    loop: str | None = None


def selected_settings(base, selections):
    data = base.model_dump()
    # BaseSettings 的排除字段不进入model_dump，保留初始化的默认配置。
    for field in ['index_config', 'retrieval_config', 'answer_config', 'agent_config', 'review_config', 'loop_config', 'bm25_dictionary']:
        data[field] = getattr(base, field)
    available = catalog()
    for kind, folder in CATEGORIES.items():
        key = selections.get(kind)
        if key and key not in {item['key'] for item in available[kind]}:
            raise ValueError('unknown_configuration_preset')
        data[kind + '_config_path'] = str(ROOT / 'configs' / folder / key) if key else None
        if not key:
            data[kind + '_config'] = None
    if data['agent_config'] is None:
        from backend.agent_contract import AgentConfig
        data['agent_config'] = AgentConfig()
    if data['review_config'] is None:
        from backend.review_contract import ReviewConfig
        data['review_config'] = ReviewConfig()
    data['runtime_version'] = 'agent-loop-v1' if selections.get('loop') else 'legacy'
    return Settings(_env_file=None, **data)


def register_management(app, base, db, authorize, tasks):
    async def effective():
        async with db.connection() as c:
            row = await (await c.execute('SELECT * FROM runtime_profiles WHERE shop_id=%s', (base.shop_id,))).fetchone()
        return selected_settings(base, row['selections']) if row else base

    @app.get('/api/configuration', dependencies=[Depends(authorize)])
    async def configuration():
        async with db.connection() as c:
            row = await (await c.execute('SELECT * FROM runtime_profiles WHERE shop_id=%s', (base.shop_id,))).fetchone()
        settings = await effective()
        snapshot = settings.snapshot()
        return jsonable_encoder({'revision': row['revision'] if row else 0, 'selections': row['selections'] if row else {},
                                'effective': snapshot, 'fingerprint': digest(snapshot), 'presets': catalog()})

    @app.put('/api/configuration', dependencies=[Depends(authorize)])
    async def update_configuration(body: ProfileSelection):
        selections = body.model_dump(exclude={'revision'})
        try:
            settings = selected_settings(base, selections)
            index = await db.current_index()
            current_db = Database(settings)
            current_db.check_index(index)
            if settings.retrieval_config and settings.retrieval_config.mode != 'vector':
                # 必须先构建，配置应用不能暗中换索引或忽略派生产物缺失。
                await current_db.bm25_artifact(str(index['id']), await current_db.chunk_rows(index['id'], {}))
            async with db.connection() as c:
                await c.execute('SELECT pg_advisory_xact_lock(8721008)')
                row = await (await c.execute('SELECT revision FROM runtime_profiles WHERE shop_id=%s', (base.shop_id,))).fetchone()
                revision = row['revision'] if row else 0
                if revision != body.revision:
                    raise HTTPException(409, 'configuration_revision_conflict')
                await current_db.save_config(c, settings)
                await c.execute('''INSERT INTO runtime_profiles(shop_id,revision,selections) VALUES(%s,%s,%s)
                    ON CONFLICT(shop_id) DO UPDATE SET revision=excluded.revision,selections=excluded.selections,updated_at=now()''',
                    (base.shop_id, revision + 1, Jsonb(selections)))
            return {'revision': revision + 1, 'fingerprint': digest(settings.snapshot()), 'applies_to': 'new_requests'}
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(409, getattr(error, 'code', 'configuration_invalid')) from None

    @app.get('/api/traces', dependencies=[Depends(authorize)])
    async def traces(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
        async with db.connection() as c:
            rows = await (await c.execute('''SELECT t.id,t.question,t.status,t.started_at,t.elapsed_ms,t.error,t.config_id,t.index_id
                FROM request_traces t JOIN config_snapshots cfg ON cfg.id=t.config_id
                LEFT JOIN agent_tasks a ON a.id=t.id LEFT JOIN agent_sessions s ON s.id=a.session_id
                WHERE cfg.snapshot->>'shop_id'=%s AND (a.id IS NULL OR s.customer_id=%s)
                ORDER BY t.started_at DESC,t.id LIMIT %s OFFSET %s''',
                (base.shop_id, base.agent_customer_id, limit, offset))).fetchall()
        return jsonable_encoder({'items': rows, 'limit': limit, 'offset': offset})

    @app.get('/api/knowledge', dependencies=[Depends(authorize)])
    async def knowledge():
        async with db.connection() as c:
            documents = await (await c.execute('''SELECT document_key,kind,stable_id,version,source_path,source_hash,metadata
                FROM knowledge_documents WHERE shop_id=%s ORDER BY stable_id,version''', (base.shop_id,))).fetchall()
            jobs = await (await c.execute('''SELECT j.id,j.index_id,j.status,j.error,j.started_at,j.finished_at
                FROM ingestion_jobs j JOIN config_snapshots s ON s.id=j.config_id
                WHERE s.snapshot->>'shop_id'=%s ORDER BY j.started_at DESC LIMIT 30''', (base.shop_id,))).fetchall()
        return jsonable_encoder({'documents': documents, 'jobs': jobs, 'indexes': await db.list_indexes(),
                                'import_scope': 'approved_bundled_knowledge_only'})

    import_lock = asyncio.Lock()

    @app.post('/api/knowledge/import', dependencies=[Depends(authorize)], status_code=202)
    async def import_knowledge():
        if import_lock.locked():
            raise HTTPException(409, 'knowledge_import_running')
        await import_lock.acquire()
        settings = await effective()
        async def build():
            adapter = ModelAdapter(settings)
            try:
                await ingest(settings, Database(settings), adapter, publish=False)
            except Exception:
                pass  # ingest 已持久记录失败；用户从jobs读取，不冒充成功。
            finally:
                await adapter.close()
                import_lock.release()
        task = asyncio.create_task(build())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {'status': 'accepted', 'publication_changed': False}

    @app.post('/api/indexes/{index_id}/select', dependencies=[Depends(authorize)])
    async def select_index(index_id: UUID):
        try:
            return await Database(await effective()).select_index(str(index_id))
        except Exception as error:
            raise HTTPException(409, getattr(error, 'code', 'index_selection_failed')) from None

    @app.post('/api/indexes/{index_id}/bm25', dependencies=[Depends(authorize)])
    async def build_bm25(index_id: UUID):
        # 使用固定已有R3词典契约构建，之后可应用R2/R3/R4。
        data = selected_settings(base, {'retrieval': 'r3-hybrid-rrf.v1.0.0.json'})
        try:
            return await Database(data).build_bm25(str(index_id))
        except Exception as error:
            raise HTTPException(409, getattr(error, 'code', 'bm25_build_failed')) from None

    return effective
