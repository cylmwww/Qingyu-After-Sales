from contextlib import asynccontextmanager
import json
import math
from datetime import datetime, timezone
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.config import Settings, ROOT
from backend.knowledge import digest
from backend.index_config import IndexCompatibilityError


def vector_literal(vector):
    return '[' + ','.join(str(float(x)) for x in vector) + ']'


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.owner_id = str(uuid.uuid4())

    @asynccontextmanager
    async def connection(self):
        async with await psycopg.AsyncConnection.connect(
            self.settings.database_url.get_secret_value(), row_factory=dict_row, connect_timeout=5
        ) as connection:
            yield connection

    async def migrate(self):
        async with self.connection() as connection:
            await connection.execute('SELECT pg_advisory_xact_lock(8721001)')
            await connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())')
            for path in sorted((ROOT / 'migrations').glob('*.sql')):
                sql = path.read_text(encoding='utf-8')
                row = await (await connection.execute('SELECT checksum FROM schema_migrations WHERE version=%s', (path.stem,))).fetchone()
                if row:
                    if row['checksum'] != digest(sql):
                        raise ValueError('已应用迁移内容发生变化，必须追加新迁移')
                    continue
                await connection.execute(sql, prepare=False)
                await connection.execute('INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)', (path.stem, digest(sql)))

    async def save_config(self, connection, settings):
        settings.validate_runtime()
        if settings.loop_config:
            profile = settings.loop_config.model_dump(mode='json')
            await connection.execute('''INSERT INTO agent_loop_config_versions VALUES(%s,%s,%s,%s)
                ON CONFLICT DO NOTHING''', (profile['id'], profile['version'], digest(profile), Jsonb(profile)))
            saved = await (await connection.execute('SELECT fingerprint FROM agent_loop_config_versions WHERE id=%s AND version=%s',
                (profile['id'], profile['version']))).fetchone()
            if saved['fingerprint'] != digest(profile):
                raise IndexCompatibilityError('loop_config_version_conflict')
        profile = settings.review_config.model_dump(mode='json')
        await connection.execute('''INSERT INTO review_config_versions(id,version,fingerprint,configuration)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (profile['id'], profile['version'], digest(profile), Jsonb(profile)))
        existing = await (await connection.execute('SELECT fingerprint FROM review_config_versions WHERE id=%s AND version=%s',
            (profile['id'], profile['version']))).fetchone()
        if existing['fingerprint'] != digest(profile):
            raise IndexCompatibilityError('review_config_version_conflict')
        if settings.answer_config:
            profile = settings.answer_config.model_dump(mode='json')
            await connection.execute('''INSERT INTO answer_config_versions(id,version,fingerprint,configuration)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (profile['id'], profile['version'], digest(profile), Jsonb(profile)))
            existing = await (await connection.execute('SELECT fingerprint FROM answer_config_versions WHERE id=%s AND version=%s',
                (profile['id'], profile['version']))).fetchone()
            if existing['fingerprint'] != digest(profile):
                raise IndexCompatibilityError('answer_config_version_conflict')
        if settings.retrieval_config:
            profile = settings.retrieval_config.model_dump(mode='json')
            await connection.execute('''INSERT INTO retrieval_config_versions(id,version,fingerprint,configuration)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (profile['id'], profile['version'], digest(profile), Jsonb(profile)))
            existing = await (await connection.execute('SELECT fingerprint FROM retrieval_config_versions WHERE id=%s AND version=%s',
                (profile['id'], profile['version']))).fetchone()
            if existing['fingerprint'] != digest(profile):
                raise IndexCompatibilityError('retrieval_config_version_conflict')
        if settings.index_config:
            profile = settings.index_config.model_dump(mode='json')
            await connection.execute('''INSERT INTO index_config_versions(id,version,fingerprint,configuration)
                VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (profile['id'], profile['version'], digest(profile), Jsonb(profile)))
            existing = await (await connection.execute('SELECT fingerprint FROM index_config_versions WHERE id=%s AND version=%s',
                (profile['id'], profile['version']))).fetchone()
            if existing['fingerprint'] != digest(profile):
                raise IndexCompatibilityError('index_config_version_conflict')
        snapshot = settings.snapshot()
        id = digest(snapshot)
        await connection.execute('INSERT INTO config_snapshots(id,snapshot) VALUES(%s,%s) ON CONFLICT DO NOTHING', (id, Jsonb(snapshot)))
        return id

    async def event(self, trace_id, kind, payload):
        async with self.connection() as connection:
            row = await (await connection.execute('INSERT INTO trace_events(trace_id,kind,payload) VALUES(%s,%s,%s) RETURNING sequence',
                (trace_id, kind, Jsonb(payload)))).fetchone()
            return row['sequence']

    async def model_call(self, payload, trace_id=None, job_id=None):
        payload = dict(payload, finished_at=datetime.now(timezone.utc).isoformat(),
                       cost_source='unknown_no_verified_price', provider_declared_version=None)
        async with self.connection() as connection:
            await connection.execute('INSERT INTO model_calls(id,trace_id,job_id,payload) VALUES(%s,%s,%s,%s)',
                (payload['call_id'], trace_id, job_id, Jsonb(payload)))

    async def current_index(self):
        async with self.connection() as connection:
            return await (await connection.execute('''SELECT i.*,s.snapshot AS build_config FROM knowledge_publications p
                JOIN knowledge_indexes i ON i.id=p.index_id JOIN config_snapshots s ON s.id=i.config_id
                WHERE p.shop_id=%s AND i.status='published' ''',
                (self.settings.shop_id,))).fetchone()

    async def get_index(self, index_id):
        async with self.connection() as connection:
            return await (await connection.execute('''SELECT i.*,s.snapshot AS build_config FROM knowledge_indexes i
                JOIN config_snapshots s ON s.id=i.config_id WHERE i.id=%s AND i.shop_id=%s''',
                (index_id, self.settings.shop_id))).fetchone()

    def check_index(self, index):
        if not index or index['status'] != 'published':
            raise IndexCompatibilityError('knowledge_index_unavailable')
        if index['embedding_contract'] != self.settings.embedding_contract():
            raise IndexCompatibilityError('embedding_index_incompatible')
        snapshot = index['build_config']
        chunk = snapshot.get('chunk_contract') or {
            'strategy': 'rule-v1', 'target_tokens': snapshot['chunk_target_tokens'],
            'max_tokens': snapshot['chunk_max_tokens'], 'overlap_tokens': snapshot['chunk_overlap_tokens'],
            'parent_max_tokens': snapshot['parent_max_tokens'], 'parent_relation': 'bounded-rule-v1',
            'tokenizer': snapshot['tokenizer_version']}
        if chunk != self.settings.chunk_contract():
            raise IndexCompatibilityError('chunk_index_incompatible')

    async def list_indexes(self):
        async with self.connection() as connection:
            return await (await connection.execute('''SELECT i.*,s.snapshot AS build_config,
                COALESCE(p.index_id=i.id,false) AS selected FROM knowledge_indexes i
                JOIN config_snapshots s ON s.id=i.config_id LEFT JOIN knowledge_publications p ON p.shop_id=i.shop_id
                WHERE i.shop_id=%s ORDER BY i.created_at DESC''', (self.settings.shop_id,))).fetchall()

    async def select_index(self, index_id):
        async with self.connection() as connection:
            await connection.execute('SELECT pg_advisory_xact_lock(8721002)')
            index = await (await connection.execute('''SELECT i.*,s.snapshot AS build_config FROM knowledge_indexes i
                JOIN config_snapshots s ON s.id=i.config_id WHERE i.id=%s AND i.shop_id=%s FOR SHARE OF i''',
                (index_id, self.settings.shop_id))).fetchone()
            self.check_index(index)
            if self.settings.retrieval_config is not None and self.settings.retrieval_config.mode != 'vector':
                # 当前检索配置依赖BM25派生产物；切换索引与配置应用执行同一缺失检查，不得暗中绕过。
                await self.bm25_artifact(str(index_id), await self.chunk_rows(str(index_id), {}))
            await self.save_config(connection, self.settings)
            await connection.execute('''INSERT INTO knowledge_publications(shop_id,index_id) VALUES(%s,%s)
                ON CONFLICT(shop_id) DO UPDATE SET index_id=excluded.index_id,updated_at=now()''', (self.settings.shop_id, index_id))
        return {'selected_index_id': str(index_id)}

    async def new_trace(self, question, filters, index_id=None):
        trace_id = str(uuid.uuid4())
        async with self.connection() as connection:
            config_id = await self.save_config(connection, self.settings)
            if index_id:
                row = await (await connection.execute('''SELECT i.*,s.snapshot AS build_config FROM knowledge_indexes i
                    JOIN config_snapshots s ON s.id=i.config_id WHERE i.shop_id=%s AND i.id=%s FOR SHARE OF i''',
                    (self.settings.shop_id, index_id))).fetchone()
                self.check_index(row)
            else:
                row = await (await connection.execute('''SELECT i.*,s.snapshot AS build_config FROM knowledge_publications p
                    JOIN knowledge_indexes i ON p.index_id=i.id JOIN config_snapshots s ON s.id=i.config_id
                    WHERE p.shop_id=%s AND i.status='published' FOR SHARE OF i''', (self.settings.shop_id,))).fetchone()
            await connection.execute('''INSERT INTO request_traces(id,owner_id,config_id,index_id,question,filters,status)
                VALUES(%s,%s,%s,%s,%s,%s,'running')''', (trace_id, self.owner_id, config_id, row['id'] if row else None, question, Jsonb(filters)))
        return trace_id, row

    async def finish_trace(self, id, status, result=None, error=None, elapsed_ms=None):
        async with self.connection() as connection:
            await connection.execute('''UPDATE request_traces SET status=%s,result=%s,error=%s,elapsed_ms=%s,finished_at=now()
                WHERE id=%s AND status='running' ''', (status, Jsonb(result), Jsonb(error), elapsed_ms, id))

    def filter_clauses(self, index_id, filters):
        from backend.request_contract import ChatRequest
        # 内部 CLI/实验也复用 API 的类型、时区及品类检查；不能从自然语言推测过滤值。
        validated = ChatRequest(question='过滤校验', **filters)
        filters = validated.model_dump(mode='json', exclude={'question', 'index_id'}, exclude_none=True)
        params = [index_id, self.settings.shop_id, self.settings.shop_id, self.settings.shop_id]
        clauses = ["c.role='child'", 'c.index_id=%s', "c.metadata->>'shop_id'=%s", 'd.shop_id=%s',
                   'i.shop_id=%s', "i.status='published'"]
        if filters.get('category_id'):
            clauses.append("c.metadata->'category_ids' ? %s")
            params.append(filters['category_id'])
        for event in ['purchased_at', 'after_sales_applied_at']:
            if filters.get(event):
                clauses.append("""(c.metadata->>'selection_event'<>%s OR
                    ((c.metadata->>'effective_from')::timestamptz<=%s::timestamptz AND
                    (c.metadata->>'effective_until' IS NULL OR (c.metadata->>'effective_until')::timestamptz>%s::timestamptz)))""")
                params.extend([event, filters[event], filters[event]])
                clauses.append("""NOT EXISTS (SELECT 1 FROM jsonb_array_elements(
                    COALESCE(c.metadata->'evidence_time_bindings','[]'::jsonb)) binding
                    WHERE binding->>'selection_event'=%s AND
                    ((binding->>'effective_from')::timestamptz>%s::timestamptz OR
                    ((binding->>'effective_until') IS NOT NULL AND
                    (binding->>'effective_until')::timestamptz<=%s::timestamptz)))""")
                params.extend([event, filters[event], filters[event]])
        return clauses, params

    async def chunk_rows(self, index_id, filters):
        self.check_index(await self.get_index(index_id))
        clauses, params = self.filter_clauses(index_id, filters)
        async with self.connection() as connection:
            return await (await connection.execute('''SELECT c.id AS chunk_id,c.document_key,c.parent_id,c.section,c.text,
                c.metadata,c.token_count,c.source_start,c.source_end,d.source_path,d.stable_id AS document_id,d.version AS document_version
                FROM knowledge_chunks c JOIN knowledge_documents d USING(document_key)
                JOIN knowledge_indexes i ON i.id=c.index_id WHERE ''' + ' AND '.join(clauses) + ' ORDER BY c.id', params)).fetchall()

    async def parent_rows(self, index_id, child_ids, filters):
        # 先用现有过滤检查子片段；父片段只能通过同索引/文档的已授权子片段获得。
        allowed = {r['chunk_id']: r for r in await self.chunk_rows(index_id, filters)}
        parents = list({allowed[c]['parent_id'] for c in child_ids if c in allowed})
        if not parents:
            return []
        async with self.connection() as connection:
            return await (await connection.execute('''SELECT c.id AS chunk_id,c.document_key,c.parent_id,c.section,c.text,
                c.metadata,c.token_count,c.source_start,c.source_end,d.source_path,d.stable_id AS document_id,d.version AS document_version
                FROM knowledge_chunks c JOIN knowledge_documents d USING(document_key)
                JOIN knowledge_indexes i ON i.id=c.index_id WHERE c.index_id=%s AND c.id=ANY(%s)
                AND c.role='parent' AND c.metadata->>'shop_id'=%s AND d.shop_id=%s AND i.shop_id=%s
                AND i.status='published' ORDER BY c.id''',
                (index_id, parents, self.settings.shop_id, self.settings.shop_id, self.settings.shop_id))).fetchall()

    async def build_bm25(self, index_id):
        from backend.bm25 import build_artifact
        profile = self.settings.retrieval_config
        if profile is None or profile.mode == 'vector':
            raise ValueError('构建 BM25 需指定 BM25/双路配置')
        config = profile.bm25
        dictionary = self.settings.bm25_dictionary
        index = await self.get_index(index_id)
        self.check_index(index)
        rows = await self.chunk_rows(index_id, {})
        artifact = build_artifact(index, rows, config, dictionary)
        contract_hash = digest({'configuration': config.model_dump(mode='json'), 'dictionary': dictionary})
        id = digest({'index_id': str(index_id), 'contract': contract_hash})
        async with self.connection() as connection:
            await connection.execute('''INSERT INTO bm25_indexes(id,index_id,shop_id,index_version,contract_hash,artifact_hash,artifact)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                (id, index_id, self.settings.shop_id, config.index_version, contract_hash, digest(artifact), Jsonb(artifact)))
            existing = await (await connection.execute('''SELECT contract_hash,artifact_hash FROM bm25_indexes
                WHERE index_id=%s AND shop_id=%s AND index_version=%s''', (index_id, self.settings.shop_id, config.index_version))).fetchone()
            if existing['contract_hash'] != contract_hash or existing['artifact_hash'] != digest(artifact):
                raise IndexCompatibilityError('bm25_index_version_conflict')
        return {'bm25_index_id': id, 'index_id': str(index_id), 'children': len(rows), 'artifact_hash': digest(artifact)}

    async def bm25_artifact(self, index_id, rows):
        from backend.bm25 import corpus_fingerprint
        config = self.settings.retrieval_config.bm25
        dictionary = self.settings.bm25_dictionary
        contract_hash = digest({'configuration': config.model_dump(mode='json'), 'dictionary': dictionary})
        async with self.connection() as connection:
            row = await (await connection.execute('''SELECT * FROM bm25_indexes WHERE index_id=%s AND shop_id=%s AND index_version=%s''',
                (index_id, self.settings.shop_id, config.index_version))).fetchone()
        if not row:
            raise IndexCompatibilityError('bm25_index_missing')
        artifact = row['artifact']
        index = await self.get_index(index_id)
        self.check_index(index)
        if (row['contract_hash'] != contract_hash or row['artifact_hash'] != digest(artifact)
            or artifact['corpus_fingerprint'] != corpus_fingerprint(rows)
            or artifact['index_id'] != str(index_id) or artifact['knowledge_version'] != index['knowledge_version']
            or artifact['source_fingerprint'] != index['source_fingerprint'] or artifact['shop_id'] != self.settings.shop_id):
            raise IndexCompatibilityError('bm25_index_incompatible')
        return artifact, row['id']

    async def search(self, index_id, query_vector, filters, top_k=None):
        self.check_index(await self.get_index(index_id))
        if len(query_vector) != self.settings.embedding_model_dimensions:
            raise IndexCompatibilityError('embedding_dimension_incompatible')
        if any(type(x) not in {int,float} or not math.isfinite(x) for x in query_vector) or not any(query_vector):
            raise IndexCompatibilityError('embedding_vector_invalid')
        clauses, params = self.filter_clauses(index_id, filters)
        params = [vector_literal(query_vector), *params, top_k or self.settings.retrieval_top_k]
        async with self.connection() as connection:
            rows = await (await connection.execute('''SELECT c.id AS chunk_id,c.document_key,c.parent_id,c.section,c.text,
                c.metadata,c.token_count,c.source_start,c.source_end,d.source_path,d.stable_id AS document_id,d.version AS document_version,
                (c.embedding <=> %s::vector) AS cosine_distance
                FROM knowledge_chunks c JOIN knowledge_documents d USING(document_key)
                JOIN knowledge_indexes i ON i.id=c.index_id WHERE ''' + ' AND '.join(clauses) +
                ' ORDER BY cosine_distance,c.id LIMIT %s', params)).fetchall()
        for rank, row in enumerate(rows, 1):
            row.update(rank=rank, cosine_similarity=1 - row['cosine_distance'], score_type='cosine_similarity')
        return rows

    async def index_available(self, id):
        async with self.connection() as connection:
            row = await (await connection.execute("SELECT status FROM knowledge_indexes WHERE id=%s AND shop_id=%s", (id, self.settings.shop_id))).fetchone()
            return bool(row and row['status'] == 'published')

    async def trace(self, id):
        async with self.connection() as connection:
            row = await (await connection.execute('''SELECT t.*,s.snapshot FROM request_traces t
                JOIN config_snapshots s ON s.id=t.config_id WHERE t.id=%s AND s.snapshot->>'shop_id'=%s''',
                (id, self.settings.shop_id))).fetchone()
            if not row:
                return None
            row['events'] = await (await connection.execute('SELECT sequence,kind,payload,created_at FROM trace_events WHERE trace_id=%s ORDER BY sequence', (id,))).fetchall()
            row['model_calls'] = [r['payload'] for r in await (await connection.execute('SELECT payload FROM model_calls WHERE trace_id=%s ORDER BY created_at', (id,))).fetchall()]
            return row

    async def evidence(self, index_id, chunk_id):
        async with self.connection() as connection:
            row = await (await connection.execute('''SELECT c.*,d.source_path,d.source_text,d.source_hash,d.version AS document_version,
                d.stable_id AS document_id FROM knowledge_chunks c JOIN knowledge_documents d USING(document_key)
                WHERE c.index_id=%s AND c.id=%s AND c.metadata->>'shop_id'=%s''', (index_id, chunk_id, self.settings.shop_id))).fetchone()
            if row:
                row.pop('embedding', None)
            return row

    async def index_stats(self, index_id):
        async with self.connection() as connection:
            return await (await connection.execute('''SELECT count(*) FILTER(WHERE role='child') AS children,
                count(*) FILTER(WHERE role='parent') AS parents,
                COALESCE(sum(pg_column_size(embedding)),0) AS vector_bytes,
                COALESCE(sum(pg_column_size(c)),0) AS row_bytes,
                COALESCE(sum(octet_length(embedding_text)),0) AS encoded_text_bytes
                FROM knowledge_chunks c WHERE index_id=%s''', (index_id,))).fetchone()

    async def recover_stale(self):
        async with self.connection() as connection:
            await connection.execute('''UPDATE request_traces SET status='interrupted',error='{"code":"process_interrupted"}',finished_at=now()
                WHERE status='running' AND started_at < now() - (%s * interval '1 second')
                AND NOT EXISTS (SELECT 1 FROM agent_tasks a WHERE a.id=request_traces.id)''', (self.settings.request_timeout_seconds + 30,))
            await connection.execute("SELECT pg_advisory_xact_lock(8721002)")
            await connection.execute("UPDATE ingestion_jobs SET status='interrupted',error='{\"code\":\"process_interrupted\"}',finished_at=now() WHERE status='running'")
            await connection.execute("UPDATE knowledge_indexes SET status='failed' WHERE status='building'")

    async def prune(self):
        async with self.connection() as connection:
            result = await connection.execute("DELETE FROM request_traces t WHERE status<>'running' AND started_at < now() - (%s * interval '1 day') AND NOT EXISTS (SELECT 1 FROM agent_tasks a WHERE a.id=t.id)", (self.settings.trace_retention_days,))
            return result.rowcount
