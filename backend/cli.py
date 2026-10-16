import argparse
import asyncio
import json
from uuid import UUID
from pydantic import ValidationError

from backend.config import Settings
from backend.db import Database
from backend.ingest import ingest
from backend.models import ModelAdapter, ModelError
from backend.index_config import IndexCompatibilityError
from backend.rag import RAGService


async def run(command, config=None, index_id=None, no_publish=False, question=None, retrieval_config=None, answer_config=None):
    overrides = {'index_config_path': config} if config else {}
    if retrieval_config:
        overrides['retrieval_config_path'] = retrieval_config
    if answer_config:
        overrides['answer_config_path'] = answer_config
    settings = Settings(**overrides)
    db, adapter = Database(settings), ModelAdapter(settings)
    try:
        if command == 'migrate':
            await db.migrate()
            return {'status': 'migrated'}
        if command == 'seed':
            from backend.agent import setup_agent
            from backend.business_tools import BusinessService
            await db.migrate()
            await setup_agent(db)
            business = BusinessService(settings.shop_id, settings.business_snapshot_id)
            index = await db.current_index()
            if index:
                db.check_index(index)
                result = {'index_id': str(index['id']), 'status': 'existing_compatible_index_reused'}
            else:
                result = await ingest(settings, db, adapter)
            return dict(result, business_snapshot_id=business.snapshot_id, business_fingerprint=business.fingerprint,
                        business_storage='versioned_read_only_seed_file')
        if command == 'ingest':
            await db.migrate()
            return await ingest(settings, db, adapter, publish=not no_publish)
        if command == 'indexes':
            return await db.list_indexes()
        if command == 'build-bm25':
            await db.migrate()
            index = await db.get_index(index_id) if index_id else await db.current_index()
            db.check_index(index)
            return await db.build_bm25(str(index['id']))
        if command == 'select-index':
            return await db.select_index(index_id)
        if command == 'query':
            trace_id, index = await db.new_trace(question, {}, index_id)
            await RAGService(settings, db, adapter).run(trace_id, index, question, {})
            return await db.trace(trace_id)
        if command == 'prune-traces':
            return {'deleted_traces': await db.prune(), 'retention_days': settings.trace_retention_days}
    finally:
        await adapter.close()


def main():
    parser = argparse.ArgumentParser(description='基础 RAG 数据库与知识操作')
    parser.add_argument('command', choices=['migrate', 'seed', 'ingest', 'prune-traces', 'indexes', 'select-index', 'query', 'build-bm25'])
    parser.add_argument('--retrieval-config', help='召回融合重排 JSON 配置，省略保留原纯向量基线')
    parser.add_argument('--answer-config', help='查询改写、上下文与生成预算 JSON 配置；省略保留第四阶段基线')
    parser.add_argument('--config', help='版本化索引 JSON 配置；省略时沿用第二阶段默认')
    parser.add_argument('--index-id', type=UUID)
    parser.add_argument('--no-publish', action='store_true', help='构建可查询索引，但不切换默认指针')
    parser.add_argument('--question')
    args = parser.parse_args()
    if args.command == 'select-index' and not args.index_id:
        parser.error('选择索引必须提供 --index-id')
    if args.command == 'query' and not args.question:
        parser.error('查询必须提供 --question')
    try:
        result = asyncio.run(run(args.command, args.config, args.index_id, args.no_publish, args.question, args.retrieval_config, args.answer_config))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if args.command == 'query' and result['status'] != 'completed':
            raise SystemExit(1)
    except (ModelError, IndexCompatibilityError) as error:
        print(json.dumps({'status': 'failed', 'error': error.code}, ensure_ascii=False))
        raise SystemExit(1)
    except ValidationError as error:
        print(json.dumps({'status':'failed', 'error':'index_configuration_invalid',
            'fields':[list(e['loc']) for e in error.errors(include_input=False)]},ensure_ascii=False))
        raise SystemExit(1)
    except ValueError:
        print(json.dumps({'status': 'failed', 'error': 'configuration_or_ingestion_invalid'}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
