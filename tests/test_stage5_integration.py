import os

import pytest

from backend.answer_context import prepare_context
from backend.config import Settings
from backend.db import Database
from backend.ingest import ingest
from backend.index_config import IndexCompatibilityError
from backend.models import ModelAdapter
from backend.rag import RAGService, make_context, SYSTEM_PROMPT
from backend.retrieval import retrieve

pytestmark=[pytest.mark.asyncio,pytest.mark.skipif(os.getenv('RUN_DB_TESTS')!='1',reason='显式隔离数据库')]


def settings(profile='q0-baseline',**extra):
    return Settings(**dict({'rag_mode':'mock','database_url':os.environ['TEST_DATABASE_URL'],
        'index_config_path':None,'retrieval_config_path':'configs/retrieval/r4-hybrid-rrf-rerank.v1.0.0.json',
        'answer_config_path':f'configs/answers/{profile}.v1.0.0.json'},**extra))


async def test_switches_merged_rerank_context_versions_and_freeze():
    s=settings(); db=Database(s); adapter=ModelAdapter(s)
    try:
        await db.migrate(); await db.migrate()
        built=await ingest(s,db,adapter,publish=False); index=await db.get_index(built['index_id'])
        await db.build_bm25(index['id'])
        question='我买了数码配件，没激活，质量问题还没核实，能退吗？'
        filters={'category_id':'digital_accessories'}
        async def noop(*args): pass
        baseline=await retrieve(s,db,adapter,index,question,filters,noop,noop)
        for profile in ['q0-baseline','q1-rewrite','q2-parent-related','q3-grouped','q4-prompt']:
            cs=settings(profile); cb=Database(cs); ca=ModelAdapter(cs)
            try:
                tid,frozen=await cb.new_trace(question,filters,index['id'])
                await RAGService(cs,cb,ca).run(tid,frozen,question,filters)
                trace=await cb.trace(tid)
                assert trace['status']=='completed',trace['error']
                evidence=trace['result']['evidence']
                assert len({r['chunk_id'] for r in evidence})==len(evidence)
                assert all('digital_accessories' in r['metadata']['category_ids'] for r in evidence)
                context=next(e['payload'] for e in trace['events'] if e['kind']=='context')
                assert context['input_token_estimate']<=cs.input_token_budget
                assert context['messages'][-1]['content'].endswith(question)
                roles=[c['role'] for c in trace['model_calls']]
                assert roles.count('rerank')==1 and roles.count('generation')==1
                assert roles.count('query_rewrite')==(profile=='q1-rewrite')
                if profile=='q0-baseline': assert context['actual_context']==make_context(baseline,s)[1]
                assembly=next(e['payload'] for e in trace['events'] if e['kind']=='context_assembly')
                assert set(assembly['citation_mapping'])=={r['citation_id'] for r in evidence}
            finally: await ca.close()
        with pytest.raises(IndexCompatibilityError):
            await Database(settings(shop_id='FOREIGN')).parent_rows(index['id'],[baseline[0]['chunk_id']],{})
        pool=await db.chunk_rows(index['id'],{'purchased_at':'2026-06-30T23:59:59+08:00'})
        cs=settings('q2-parent-related'); cb=Database(cs)
        evidence,_,_,_=await prepare_context(pool,cs,cb,index,{'purchased_at':'2026-06-30T23:59:59+08:00'},'运费',SYSTEM_PROMPT,noop)
        assert all(r['document_version']=='1.0.0' for r in evidence if r['document_id']=='POL-SHIPPING')
        # 相同id/version不同内容禁止覆盖；新版本追加。
        changed=s.answer_config.model_copy(update={'hypothesis':'同版本变更'})
        with pytest.raises(IndexCompatibilityError,match='answer_config_version_conflict'):
            await Database(s.model_copy(update={'answer_config':changed})).new_trace('政策',{},index['id'])
    finally: await adapter.close()


async def test_small_children_restore_bounded_parent_without_double_quote():
    s=settings('q2-parent-related',index_config_path='configs/indexes/window-200.v1.0.0.json')
    db=Database(s); adapter=ModelAdapter(s)
    try:
        await db.migrate(); built=await ingest(s,db,adapter,publish=False); index=await db.get_index(built['index_id'])
        pool=await db.chunk_rows(index['id'],{})
        hit=next(r for r in pool if sum(c['parent_id']==r['parent_id'] for c in pool)>1)
        async def noop(*args): pass
        evidence,_,removed,_=await prepare_context([hit,hit],s,db,index,{},'规则',SYSTEM_PROMPT,noop)
        assert any(r['reason']=='duplicate_same_source_range' for r in removed)
        assert any(r['source_start']<=hit['source_start'] and r['source_end']>=hit['source_end'] for r in evidence)
        assert any(r['chunk_id']==hit['parent_id'] and r['context_kind']=='parent' for r in evidence)
        assert len({(r['document_key'],r['source_start'],r['source_end']) for r in evidence})==len(evidence)
    finally: await adapter.close()
