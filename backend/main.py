import asyncio
from contextlib import asynccontextmanager
import json
import secrets
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from backend.config import Settings
from backend.db import Database
from backend.models import ModelAdapter
from backend.rag import RAGService
from backend.index_config import IndexCompatibilityError


from backend.request_contract import ChatRequest
from backend.agent_contract import AgentRequest
from backend.agent_store import AgentStore, AgentError
from backend.agent import AgentRuntime, setup_agent
from backend.agent_dispatch import create_runtime
from backend.business_tools import BusinessService


def public_event(kind, payload):
    """候选内容只在有权限的开发者追踪中查看，不进入对话SSE。"""
    if payload.get('runtime_version') == 'agent-loop-v1':
        fields = {
            'agent_decision': {'runtime_version','state_version','kind','action_id'},
            'observation_applied': {'runtime_version','state_version','tool','status','reused','action_id'},
            'candidate_created': {'runtime_version','candidate_id','bundle_id'},
            'candidate_reused': {'runtime_version','candidate_id','bundle_id','repair_episode'},
            'draft_readiness': {'runtime_version','admitted'},
            'runtime_dispatch': {'runtime_version','configured_runtime','reason'},
        }
        if kind in fields:
            return {k:v for k,v in payload.items() if k in fields[kind]}
        if kind == 'review_result':
            return dict(runtime_version='agent-loop-v1', passed=payload.get('verdict')=='pass',
                issue_count=payload.get('unresolved_count',len(payload.get('findings',[])))+payload.get('program_issue_count',0),
                repair_episode=payload.get('repair_episode',0), execution=payload.get('execution'), verdict=payload.get('verdict'))
    if kind == 'done':
        return {k:v for k,v in payload.items() if k in {'task_id','trace_id','answer','evidence','mode','reviewed',
            'prechecked','outcome','reason','runtime_version','coverage','correction_rounds','path','plan_version'}}
    if kind == 'tool_start':
        return {k:v for k,v in payload.items() if k in {'tool','step_id','plan_version','runtime_version','action_id'}}
    if kind == 'tool_result':
        result = payload.get('result') or {}
        data = (result.get('data') or {})
        return {'tool':payload.get('tool'), 'result':{'status':result.get('status'),
            'data':{'lines':data.get('lines',[])} if payload.get('tool')=='get_order' else {}}}
    if kind == 'context':
        return {'prompt_version':payload.get('prompt_version')}
    if kind == 'plan':
        return {k:v for k,v in payload.items() if k not in {'reason','steps'}} | {
            'steps':[{k:v for k,v in step.items() if k!='goal'} for step in payload['steps']]}
    if kind == 'review_result':
        # 只公开状态计数；具体问题与候选留在开发者追踪，避免未通过稿进入对话流。
        return {k:v for k,v in payload.items() if k not in {'candidate_answer','program_issues','report','result'}} | {
            'passed':payload['report']['passed'], 'issue_count':len(payload['report']['issues']),
            'execution':(payload.get('result') or {}).get('execution'),
            'verdict':(payload.get('result') or {}).get('verdict')}
    if kind in {'answer_repair','correction_start','correction_evidence','review_exit','issue_lifecycle','budget_admission','repair_plan','review_incomplete','evidence_bundle'}:
        return {k:v for k,v in payload.items() if k in {
            'round','strategy','outcome','candidate_withheld','budget','admitted','required','remaining',
            'denied_reason','execution','issue_count','remaining_unknown','bundle_id','item_count',
            'token_estimate','incomplete','new_evidence_count','global_replan'}}
    if kind == 'done' and 'review_summary' in payload:
        # 人工/部分出口也不能在终止事件中夹带未通过稿的结论；详情保留于开发者追踪。
        return {k:v for k,v in payload.items() if k not in {'review_summary','handoff_summary'}}
    fields = {'route': {'intent','path'}, 'submission':{'action','request_id'},
        'status':{'message','mode'}, 'clarification':{'missing','message','mode','task_id','outcome','reviewed'},
        'error':{'code','resumable','task_id'}, 'review_failure':{'code','candidate_withheld'},
        'replan':{'version'}, 'runtime_dispatch':{'runtime_version','configured_runtime','reason'}}
    return {k:v for k,v in payload.items() if k in fields.get(kind,set())}


def create_app(settings=None, database=None, model=None):
    settings = settings or Settings()
    db = database or Database(settings)
    adapter = model or ModelAdapter(settings)
    service = RAGService(settings, db, adapter)
    tasks = set()
    capacity = asyncio.Semaphore(8)
    agent_store = AgentStore(db)
    agent_tasks = {}

    @asynccontextmanager
    async def lifespan(app):
        await db.migrate()
        await db.recover_stale()
        await setup_agent(db)
        await agent_store.recover_expired()
        async def recover_loop():
            while True:
                await asyncio.sleep(5)
                await agent_store.recover_expired()
        recovery = asyncio.create_task(recover_loop())
        yield
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.close()

    app = FastAPI(title='青屿售后 · 证据问答', version='0.8.0', lifespan=lifespan)

    def authorize(request: Request):
        expected = settings.api_access_token.get_secret_value()
        if expected:
            provided = request.headers.get('Authorization', '')
            if not secrets.compare_digest(provided, 'Bearer ' + expected):
                raise HTTPException(401, '缺少有效的本地访问令牌')
        elif request.client and request.client.host not in {'127.0.0.1', '::1', 'localhost', 'testclient'}:
            raise HTTPException(403, '无访问令牌时只允许回环地址，请配置 API_ACCESS_TOKEN')

    from backend.management import register_management
    effective_settings = register_management(app, settings, db, authorize, tasks)

    @app.get('/api/health')
    async def health():
        try:
            index = await db.current_index()
        except Exception:
            raise HTTPException(503, '数据库未就绪') from None
        compatible = False
        if index:
            try:
                db.check_index(index)
                compatible = True
            except IndexCompatibilityError:
                pass
        current = await effective_settings()
        return {'status': 'ok', 'mode': settings.rag_mode, 'version': '0.8.0', 'index_id': str(index['id']) if index else None,
                'knowledge_version': index['knowledge_version'] if index else None,
                'index_compatible': compatible, 'review_configuration':current.review_config.model_dump(mode='json')}

    @app.get('/api/indexes', dependencies=[Depends(authorize)])
    async def indexes():
        return jsonable_encoder(await db.list_indexes())

    async def execute(trace_id, index, question, filters, current):
        current_adapter = ModelAdapter(current) if current != settings else adapter
        try:
            await RAGService(current, Database(current), current_adapter).run(trace_id, index, question, filters)
        finally:
            if current_adapter is not adapter:
                await current_adapter.close()
            capacity.release()

    async def stream_events(trace_id, request):
        try:
            last = max(0, int(request.query_params.get('after', request.headers.get('Last-Event-ID', '0'))))
        except ValueError:
            last = 0
        while True:
            if await request.is_disconnected():
                return
            trace = await db.trace(trace_id)
            for event in trace['events']:
                if event['sequence'] > last:
                    last = event['sequence']
                    payload = json.dumps(jsonable_encoder(public_event(event['kind'],event['payload'])
                        if trace.get('filters',{}).get('agent') else event['payload']), ensure_ascii=False)
                    yield f"id: {last}\nevent: {event['kind']}\ndata: {payload}\n\n"
            terminal = trace['status'] != 'running'
            if terminal and any(event['kind'] in {'done', 'error', 'clarification'} for event in trace['events']):
                return
            if terminal:
                await asyncio.sleep(0.05)
                trace = await db.trace(trace_id)
                if not any(event['kind'] in {'done', 'error', 'clarification'} for event in trace['events']):
                    yield 'event: error\ndata: {"code":"terminal_event_missing"}\n\n'
                    return
            yield ': keepalive\n\n'
            await asyncio.sleep(0.15)

    @app.post('/api/chat', dependencies=[Depends(authorize)])
    async def chat(body: ChatRequest, request: Request):
        if capacity.locked():
            raise HTTPException(429, '本地请求并发已达上限')
        await capacity.acquire()
        filters = body.model_dump(mode='json', exclude={'question', 'index_id'}, exclude_none=True)
        try:
            current = await effective_settings()
            current_db = Database(current)
            if body.index_id:
                trace_id, index = await current_db.new_trace(body.question, filters, str(body.index_id))
            else:
                trace_id, index = await current_db.new_trace(body.question, filters)
        except IndexCompatibilityError as error:
            capacity.release()
            raise HTTPException(409, error.code) from None
        except Exception:
            capacity.release()
            raise HTTPException(503, '无法保存请求追踪') from None
        task = asyncio.create_task(execute(trace_id, index, body.question, filters, current))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return StreamingResponse(stream_events(trace_id, request), media_type='text/event-stream',
            headers={'X-Trace-ID': trace_id, 'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.get('/api/traces/{trace_id}', dependencies=[Depends(authorize)])
    async def trace(trace_id: UUID):
        await trace_owner(str(trace_id))
        result = await db.trace(str(trace_id))
        if not result:
            raise HTTPException(404, '追踪不存在')
        return jsonable_encoder(result)

    async def trace_owner(trace_id):
        async with db.connection() as c:
            row = await (await c.execute('SELECT id FROM agent_tasks WHERE id=%s', (trace_id,))).fetchone()
        if row:
            try:
                await agent_store.task(trace_id, settings.agent_customer_id)
            except AgentError:
                raise HTTPException(404, '追踪不存在') from None

    def agent_http(error):
        code = getattr(error, 'code', 'agent_service_unavailable')
        return HTTPException(404 if code in {'session_not_found', 'task_not_found'} else 409 if isinstance(error, (AgentError, IndexCompatibilityError)) else 503, code)

    @app.post('/api/agent/sessions', dependencies=[Depends(authorize)])
    async def new_session():
        try:
            business = BusinessService(settings.shop_id, settings.business_snapshot_id)
            if not business.record('customer', settings.agent_customer_id):
                raise AgentError('configured_customer_unavailable')
            return {'session_id': await agent_store.create_session(settings.agent_customer_id),
                    'customer_id': settings.agent_customer_id, 'business_mode': 'mock_read_only'}
        except Exception as error:
            raise agent_http(error) from None

    @app.get('/api/agent/sessions/{session_id}', dependencies=[Depends(authorize)])
    async def get_session(session_id: UUID):
        try:
            session = await agent_store.session(str(session_id), settings.agent_customer_id)
            async with db.connection() as c:
                rows = await (await c.execute('SELECT id,status,state,question FROM agent_tasks WHERE session_id=%s ORDER BY updated_at DESC LIMIT 20', (session_id,))).fetchall()
            return jsonable_encoder({'session': session, 'tasks': rows})
        except Exception as error:
            raise agent_http(error) from None

    @app.get('/api/agent/sessions', dependencies=[Depends(authorize)])
    async def list_sessions():
        async with db.connection() as c:
            rows = await (await c.execute('''SELECT id,created_at FROM agent_sessions
                WHERE shop_id=%s AND customer_id=%s ORDER BY created_at DESC LIMIT 30''',
                (settings.shop_id, settings.agent_customer_id))).fetchall()
        return jsonable_encoder({'items': rows})

    @app.post('/api/agent/chat', dependencies=[Depends(authorize)])
    async def agent_chat(body: AgentRequest, request: Request):
        acquired = False
        try:
            current = await effective_settings()
            current_store = AgentStore(Database(current))
            await agent_store.recover_expired()
            if capacity.locked():
                from backend.knowledge import digest
                await agent_store.session(str(body.session_id), settings.agent_customer_id)
                async with db.connection() as c:
                    replay = await (await c.execute('SELECT task_id,fingerprint FROM agent_submissions WHERE session_id=%s AND request_id=%s', (body.session_id,body.request_id))).fetchone()
                if not replay:
                    raise HTTPException(429, '本地请求并发已达上限')
                if replay['fingerprint'] != digest(body.model_dump(mode='json')):
                    raise AgentError('idempotency_conflict')
                tid, launch = str(replay['task_id']), False
            else:
                await capacity.acquire()
                acquired = True
                business = BusinessService(current.shop_id, current.business_snapshot_id)
                tid, launch = await current_store.submit(body, current.agent_customer_id, business)
            if launch:
                async def run_agent():
                    saved = None
                    try:
                        saved = await agent_store.task(tid, settings.agent_customer_id)
                        await create_runtime(current, saved).run()
                    except Exception as error:
                        if saved:
                            try:
                                await agent_store.finish(tid, saved['attempt'], 'failed', {}, code=getattr(error,'code','agent_initialization_failed'))
                            except AgentError:
                                pass
                    finally:
                        capacity.release()
                task = asyncio.create_task(run_agent())
                tasks.add(task)
                agent_tasks[tid] = task
                def cleanup(done):
                    tasks.discard(done)
                    if agent_tasks.get(tid) is done:
                        agent_tasks.pop(tid, None)
                task.add_done_callback(cleanup)
                acquired = False
            elif acquired:
                capacity.release()
                acquired = False
        except Exception as error:
            if acquired:
                capacity.release()
            if isinstance(error, HTTPException):
                raise
            raise agent_http(error) from None
        return StreamingResponse(stream_events(tid, request), media_type='text/event-stream',
            headers={'X-Trace-ID': tid, 'X-Task-ID': tid, 'Cache-Control': 'no-cache'})

    @app.get('/api/agent/tasks/{task_id}', dependencies=[Depends(authorize)])
    async def agent_task(task_id: UUID):
        try:
            await agent_store.recover_expired()
            return jsonable_encoder(await agent_store.task(str(task_id), settings.agent_customer_id))
        except Exception as error:
            raise agent_http(error) from None

    @app.post('/api/agent/tasks/{task_id}/cancel', dependencies=[Depends(authorize)])
    async def cancel_task(task_id: UUID):
        try:
            await agent_store.cancel(str(task_id), settings.agent_customer_id)
            pending = agent_tasks.get(str(task_id))
            if pending:
                pending.cancel()
            return {'status': 'cancelled'}
        except Exception as error:
            raise agent_http(error) from None

    @app.get('/api/traces/{trace_id}/events', dependencies=[Depends(authorize)])
    async def events(trace_id: UUID, request: Request):
        await trace_owner(str(trace_id))
        if not await db.trace(str(trace_id)):
            raise HTTPException(404, '追踪不存在')
        return StreamingResponse(stream_events(str(trace_id), request), media_type='text/event-stream')

    @app.get('/api/evidence/{index_id}/{chunk_id}', dependencies=[Depends(authorize)])
    async def evidence(index_id: UUID, chunk_id: str):
        result = await db.evidence(str(index_id), chunk_id)
        if not result:
            raise HTTPException(404, '证据不存在')
        return jsonable_encoder(result)

    return app


app = create_app()
