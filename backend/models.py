import asyncio
import hashlib
from datetime import datetime, timezone
import json
import math
import re
import time
import uuid

import httpx

from backend.config import Settings
from backend.knowledge import digest


class ModelError(RuntimeError):
    def __init__(self, code, retryable=False, status=None):
        super().__init__(code)
        self.code, self.retryable, self.status = code, retryable, status


def mock_vector(text, dimensions=1024):
    text = re.sub(r'\s+', '', text.lower())
    vector = [0.0] * dimensions
    for term in list(text) + [text[i:i + 2] for i in range(len(text) - 1)]:
        h = int(hashlib.sha256(term.encode()).hexdigest()[:8], 16)
        vector[h % dimensions] += 1 if h & 1024 else -1
    norm = math.sqrt(sum(x * x for x in vector)) or 1
    return [x / norm for x in vector]


class ModelAdapter:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(follow_redirects=False)

    async def before_call(self, role, tokens):
        # 单轮基线为空钩子；Agent适配在外部调用前持久预留共享预算。
        pass

    async def close(self):
        await self.client.aclose()

    def endpoint(self, role):
        s = self.settings
        if role == 'embedding':
            url, key, model = s.embedding_model_base_url, s.embedding_model_api_key, s.embedding_model_id
        else:
            url, key, model = s.thinking_model_base_url, s.thinking_model_api_key, s.thinking_model_id
        if not url or not key.get_secret_value():
            raise ModelError('model_configuration_missing')
        return url.rstrip('/'), {'Authorization': 'Bearer ' + key.get_secret_value()}, model

    def safe_error(self, error):
        if isinstance(error, ModelError):
            return error
        if isinstance(error, (httpx.TimeoutException, TimeoutError)):
            return ModelError('model_timeout', True)
        if isinstance(error, httpx.HTTPStatusError):
            code = error.response.status_code
            return ModelError('model_http_error', code in {429, 500, 502, 503, 504}, code)
        if isinstance(error, httpx.RequestError):
            return ModelError('model_connection_error', True)
        return ModelError('model_invalid_response')

    async def embed(self, texts, record):
        s = self.settings
        for attempt in range(s.model_retry_count + 1):
            from backend.knowledge import token_count
            await self.before_call('embedding', sum(token_count(t) for t in texts))
            started = time.perf_counter()
            call = {'call_id': str(uuid.uuid4()), 'started_at': datetime.now(timezone.utc).isoformat(), 'role': 'embedding', 'attempt': attempt + 1,
                    'input_count': len(texts), 'input_hash': digest(texts),
                    'model': s.embedding_model_id, 'provider': s.embedding_model_provider,
                    'mode': s.rag_mode, 'usage': None, 'cost': None, 'usage_source': 'unknown'}
            try:
                if s.rag_mode == 'mock':
                    vectors = [mock_vector(text, s.embedding_model_dimensions) for text in texts]
                    call['actual_model'] = 'mock-hash-char-bigram-v1'
                else:
                    url, headers, model = self.endpoint('embedding')
                    async with asyncio.timeout(s.embedding_timeout_seconds):
                        response = await self.client.post(url + '/embeddings', headers=headers,
                            json={'model': model, 'input': texts, 'dimensions': s.embedding_model_dimensions,
                                  'encoding_format': 'float'}, timeout=s.embedding_timeout_seconds)
                    response.raise_for_status()
                    data = response.json()
                    rows = data['data']
                    if len(rows) != len(texts) or sorted(row['index'] for row in rows) != list(range(len(texts))):
                        raise ModelError('embedding_index_mismatch')
                    vectors = [row['embedding'] for row in sorted(rows, key=lambda row: row['index'])]
                    call.update(actual_model=data.get('model'), usage=data.get('usage'),
                                usage_source='provider' if data.get('usage') is not None else 'unknown',
                                provider_request_id=response.headers.get('x-request-id'))
                if any(len(v) != s.embedding_model_dimensions or
                       any(type(x) not in {int, float} or not math.isfinite(x) for x in v) or
                       not any(x != 0 for x in v) for v in vectors):
                    raise ModelError('embedding_vector_invalid')
                if s.embedding_normalization == 'l2':
                    normalized = []
                    for vector in vectors:
                        norm = math.sqrt(sum(x * x for x in vector))
                        if not math.isfinite(norm) or norm == 0:
                            raise ModelError('embedding_vector_invalid')
                        normalized.append([x / norm for x in vector])
                    vectors = normalized
                call.update(status='success', elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
                await record(call)
                return vectors
            except asyncio.CancelledError:
                call.update(status='interrupted', error={'code': 'model_cancelled'},
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
                await record(call)
                raise
            except Exception as error:
                safe = self.safe_error(error)
                call.update(status='failed', error={'code': safe.code, 'http_status': safe.status},
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
                await record(call)
                if not safe.retryable or attempt == s.model_retry_count:
                    raise safe from None
                await asyncio.sleep(1)

    async def rewrite(self, messages, max_tokens, record):
        s = self.settings
        for attempt in range(s.model_retry_count + 1):
            from backend.knowledge import token_count
            await self.before_call('query_rewrite', sum(token_count(m['content']) + 8 for m in messages) + max_tokens)
            started = time.perf_counter()
            call = {'call_id': str(uuid.uuid4()), 'started_at': datetime.now(timezone.utc).isoformat(),
                    'role': 'query_rewrite', 'attempt': attempt + 1, 'model': s.thinking_model_id,
                    'provider': s.thinking_model_provider, 'mode': s.rag_mode, 'messages': messages,
                    'temperature': 0, 'max_tokens': max_tokens, 'enable_thinking': False,
                    'usage': None, 'usage_source': 'unknown', 'cost': None,
                    'prompt_version': 'extractive-query-v1'}
            try:
                if s.rag_mode == 'mock':
                    # 只用于机制验证，不把规则抽取当真实模型改写效果。
                    terms = [t for t in re.split(r'[，。！？；\n,;?!\s]+', messages[-1]['content']) if t and len(t) <= 200]
                    maximum = s.answer_config.rewrite.max_terms if s.answer_config else 8
                    raw = json.dumps({'terms': terms[:maximum]}, ensure_ascii=False)
                    call['actual_model'] = 'mock-extractive-query-v1'
                else:
                    url, headers, model = self.endpoint('generation')
                    async with asyncio.timeout(s.model_timeout_seconds):
                        response = await self.client.post(url + '/chat/completions', headers=headers,
                            json={'model': model, 'messages': messages, 'stream': False, 'temperature': 0,
                                  'max_tokens': max_tokens, 'enable_thinking': False, 'thinking': {'type': 'disabled'}},
                            timeout=s.model_timeout_seconds)
                    response.raise_for_status()
                    call['response_content_type'] = response.headers.get('content-type')
                    if response.text.lstrip().startswith('data:'):
                        # 此角色明确请求非流式JSON。供应商仍返回SSE时记录已返回用量，
                        # 不能把只有usage、没有choices/正文的200响应当有效改写。
                        has_content = False
                        for line in response.text.splitlines():
                            if not line.startswith('data:') or line[5:].strip() == '[DONE]':
                                continue
                            frame = json.loads(line[5:].strip())
                            call['actual_model'] = frame.get('model', call.get('actual_model'))
                            if frame.get('usage') is not None:
                                call.update(usage=frame['usage'], usage_source='provider')
                            has_content = has_content or any(c.get('delta', {}).get('content') for c in frame.get('choices', []))
                        raise ModelError('rewrite_protocol_mismatch' if has_content else 'rewrite_response_empty')
                    data = response.json()
                    call.update(actual_model=data.get('model'), usage=data.get('usage'),
                                usage_source='provider' if data.get('usage') is not None else 'unknown',
                                provider_request_id=response.headers.get('x-request-id'))
                    choice = data['choices'][0]
                    call['finish_reason'] = choice.get('finish_reason')
                    if choice.get('finish_reason') == 'length':
                        raise ModelError('rewrite_output_truncated')
                    raw = choice['message']['content']
                if not isinstance(raw, str) or len(raw) > 16000:
                    raise ModelError('rewrite_response_invalid')
                call.update(status='success', output=raw, elapsed_ms=(time.perf_counter()-started)*1000)
                await record(call)
                return raw
            except asyncio.CancelledError:
                call.update(status='interrupted', error={'code': 'model_cancelled'}, elapsed_ms=(time.perf_counter()-started)*1000)
                await record(call)
                raise
            except Exception as error:
                safe = self.safe_error(error)
                call.update(status='failed', error={'code': safe.code, 'http_status': safe.status},
                            elapsed_ms=(time.perf_counter()-started)*1000)
                await record(call)
                if not safe.retryable or attempt == s.model_retry_count:
                    raise safe from None
                await asyncio.sleep(1)

    async def generate(self, messages, evidence, record):
        s = self.settings
        for attempt in range(s.model_retry_count + 1):
            from backend.knowledge import token_count
            await self.before_call('generation', sum(token_count(m['content']) + 8 for m in messages) + s.max_output_tokens)
            started, emitted, completed = time.perf_counter(), False, False
            call = {'call_id': str(uuid.uuid4()), 'started_at': datetime.now(timezone.utc).isoformat(), 'role': 'generation', 'attempt': attempt + 1,
                    'model': s.thinking_model_id, 'provider': s.thinking_model_provider,
                    'mode': s.rag_mode, 'messages': messages, 'usage': None, 'cost': None,
                    'usage_source': 'unknown', 'temperature': 0, 'max_tokens': s.max_output_tokens,
                    'enable_thinking': s.generation_enable_thinking, 'actual_model': None}
            try:
                if s.rag_mode == 'mock':
                    call['actual_model'] = 'mock-evidence-excerpt-v1'
                    answer = '【模拟模式：仅验证链路，不代表真实模型回答】\n\n' + '\n\n'.join(
                        f"证据摘录 [{item['citation_id']}]：\n{item['text']}" for item in evidence[:2])
                    for i in range(0, len(answer), 30):
                        emitted = True
                        yield answer[i:i + 30]
                    completed = True
                    call['finish_reason'] = 'mock_complete'
                else:
                    url, headers, model = self.endpoint('generation')
                    payload = {'model': model, 'messages': messages, 'stream': True,
                               'stream_options': {'include_usage': True}, 'temperature': 0,
                               'max_tokens': s.max_output_tokens, 'enable_thinking': s.generation_enable_thinking,
                               'thinking': {'type': 'enabled' if s.generation_enable_thinking else 'disabled'}}
                    async with self.client.stream('POST', url + '/chat/completions', headers=headers,
                                                  json=payload, timeout=s.model_timeout_seconds) as response:
                        response.raise_for_status()
                        call['provider_request_id'] = response.headers.get('x-request-id')
                        lines = response.aiter_lines().__aiter__()
                        while True:
                            remaining = s.model_timeout_seconds - (time.perf_counter() - started)
                            if remaining <= 0:
                                raise ModelError('model_timeout', True)
                            try:
                                line = await asyncio.wait_for(lines.__anext__(), remaining)
                            except StopAsyncIteration:
                                break
                            if not line.startswith('data:'):
                                continue
                            body = line[5:].strip()
                            if body == '[DONE]':
                                completed = True
                                break
                            data = json.loads(body)
                            call['actual_model'] = data.get('model', call['actual_model'])
                            if data.get('usage') is not None:
                                call.update(usage=data['usage'], usage_source='provider')
                            for choice in data.get('choices', []):
                                if choice.get('finish_reason'):
                                    call['finish_reason'] = choice['finish_reason']
                                text = choice.get('delta', {}).get('content')
                                if text:
                                    emitted = True
                                    yield text
                    if not completed or not emitted:
                        raise ModelError('model_stream_incomplete')
                    if call.get('finish_reason') == 'length':
                        raise ModelError('model_output_truncated')
                call.update(status='success', elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
                await record(call)
                return
            except asyncio.CancelledError:
                call.update(status='interrupted', error={'code': 'model_cancelled'},
                            elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
                await record(call)
                raise
            except Exception as error:
                safe = self.safe_error(error)
                call.update(status='failed', error={'code': safe.code, 'http_status': safe.status},
                            partial_output=emitted, elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
                await record(call)
                if emitted or not safe.retryable or attempt == s.model_retry_count:
                    raise safe from None
                await asyncio.sleep(1)
