import asyncio
import uuid
from dataclasses import asdict

from psycopg.types.json import Jsonb

from backend.config import Settings
from backend.db import Database, vector_literal
from backend.knowledge import load_knowledge
from backend.models import ModelAdapter, ModelError
from backend.index_config import IndexCompatibilityError


async def ingest(settings: Settings, db: Database, adapter: ModelAdapter, publish=True):
    job_id, index_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with db.connection() as lock:
        await lock.execute('SELECT pg_advisory_lock(8721002)')
        await lock.commit()
        async with db.connection() as connection:
            await connection.execute("UPDATE ingestion_jobs SET status='interrupted',error='{\"code\":\"process_interrupted\"}',finished_at=now() WHERE status='running'")
            await connection.execute("UPDATE knowledge_indexes SET status='failed' WHERE status='building'")
            config_id = await db.save_config(connection, settings)
            await connection.execute("INSERT INTO ingestion_jobs(id,owner_id,status,config_id) VALUES(%s,%s,'running',%s)", (job_id, db.owner_id, config_id))
        try:
            manifest, documents, chunks, fingerprint = await asyncio.to_thread(load_knowledge, settings)
            if manifest['shop_id'] != settings.shop_id:
                raise ValueError('知识包店铺与服务配置不一致')
            async with db.connection() as connection:
                await connection.execute('''INSERT INTO knowledge_indexes(id,shop_id,knowledge_version,config_id,source_fingerprint,
                    embedding_contract,status,job_id) VALUES(%s,%s,%s,%s,%s,%s,'building',%s)''',
                    (index_id, settings.shop_id, manifest['knowledge_version'], config_id, fingerprint, Jsonb(settings.embedding_contract()), job_id))
                await connection.execute('UPDATE ingestion_jobs SET index_id=%s WHERE id=%s', (index_id, job_id))
                for doc in documents:
                    existing = await (await connection.execute('SELECT source_hash,metadata FROM knowledge_documents WHERE document_key=%s', (doc.key,))).fetchone()
                    if existing and (existing['source_hash'] != doc.source_hash or existing['metadata'] != doc.metadata):
                        raise ValueError('同一文档版本内容不可覆盖，请追加内容版本')
                    await connection.execute('''INSERT INTO knowledge_documents(document_key,shop_id,kind,stable_id,version,
                        source_path,source_text,source_hash,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                        (doc.key, settings.shop_id, doc.kind, doc.stable_id, doc.version, doc.source_path, doc.source_text, doc.source_hash, Jsonb(doc.metadata)))
            children = [c for c in chunks if c.role == 'child']
            vectors = {}

            async def record(payload):
                await db.model_call(payload, job_id=job_id)

            for start in range(0, len(children), settings.embedding_batch_size):
                batch = children[start:start + settings.embedding_batch_size]
                values = await adapter.embed([c.embedding_text for c in batch], record)
                if len(values) != len(batch):
                    raise ModelError('embedding_index_mismatch')
                vectors.update(zip([c.id for c in batch], values))
            async with db.connection() as connection:
                for chunk in chunks:
                    await connection.execute('''INSERT INTO knowledge_chunks(index_id,id,document_key,parent_id,role,ordinal,
                        section,text,embedding_text,token_count,source_start,source_end,metadata,embedding)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)''',
                        (index_id, chunk.id, chunk.document_key, chunk.parent_id, chunk.role, chunk.ordinal, chunk.section,
                         chunk.text, chunk.embedding_text, chunk.tokens, chunk.source_start, chunk.source_end, Jsonb(chunk.metadata),
                         vector_literal(vectors[chunk.id]) if chunk.role == 'child' else None))
                count = await (await connection.execute("SELECT count(*) AS n FROM knowledge_chunks WHERE index_id=%s AND role='child'", (index_id,))).fetchone()
                if count['n'] != len(children) or not children:
                    raise ValueError('索引完整性检查失败')
                await connection.execute("UPDATE knowledge_indexes SET status='published',published_at=now() WHERE id=%s", (index_id,))
                if publish:
                    await connection.execute('''INSERT INTO knowledge_publications(shop_id,index_id) VALUES(%s,%s)
                        ON CONFLICT(shop_id) DO UPDATE SET index_id=excluded.index_id,updated_at=now()''', (settings.shop_id, index_id))
                await connection.execute("UPDATE ingestion_jobs SET status='completed',finished_at=now() WHERE id=%s", (job_id,))
            return {'job_id': job_id, 'index_id': index_id, 'mode': settings.rag_mode, 'documents': len(documents),
                    'children': len(children), 'parents': len(chunks) - len(children), 'selected': publish,
                    'knowledge_version': manifest['knowledge_version']}
        except BaseException as error:
            code = error.code if isinstance(error, (ModelError, IndexCompatibilityError)) else ('ingestion_interrupted' if isinstance(error, asyncio.CancelledError) else 'ingestion_failed')
            async with db.connection() as connection:
                await connection.execute("UPDATE knowledge_indexes SET status='failed' WHERE id=%s", (index_id,))
                await connection.execute("UPDATE ingestion_jobs SET status=%s,error=%s,finished_at=now() WHERE id=%s",
                    ('interrupted' if isinstance(error, asyncio.CancelledError) else 'failed', Jsonb({'code': code}), job_id))
            raise
        finally:
            await lock.execute('SELECT pg_advisory_unlock(8721002)')
            await lock.commit()
