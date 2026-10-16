"""通过已启动API复现政策引用、澄清续接和取消；使用虚构示例，不执行交易。"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'local') or output.exists():
        raise ValueError('使用local内的新结果文件，保留历史演示')
    token = os.getenv('API_ACCESS_TOKEN')
    if not token:
        raise ValueError('先设置API_ACCESS_TOKEN；不会写入结果')
    headers = {'Authorization': 'Bearer ' + token}
    report = {'source': 'live_http_demo', 'scenarios': [], 'business': 'fictional_read_only'}
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=180) as client:
        health = await client.get('/api/health'); health.raise_for_status(); report['health'] = health.json()
        async def session():
            r = await client.post('/api/agent/sessions'); r.raise_for_status(); return r.json()['session_id']
        async def chat(sid, question, facts=None, **extra):
            r = await client.post('/api/agent/chat', json={'session_id': sid, 'request_id': str(uuid4()), 'question': question, 'facts': facts or {}, **extra})
            r.raise_for_status(); tid = r.headers['X-Task-ID']
            trace = await client.get('/api/traces/' + tid); trace.raise_for_status()
            return tid, trace.json()
        sid = await session()
        tid, first = await chat(sid, '退款金额是多少？')
        assert first['status'] == 'waiting' and 'order_id' in first['result']['missing']
        _, final = await chat(sid, '请查询ORD-006的退款预估。', {'order_id': 'ORD-006'}, action='clarify', task_id=tid)
        assert final['status'] == 'completed'
        assert any(e['kind'] == 'tool_result' and e['payload']['tool'] == 'calculate_refund' for e in final['events'])
        assert final['snapshot'] == first['snapshot']
        report['scenarios'].append({'name': 'clarification_and_business_tools', 'trace': final, 'checks': ['order_clarification', 'frozen_configuration', 'calculate_refund_readonly']})
        r = await client.post('/api/chat', json={'question': '定制商品的无理由退货限制是什么？', 'category_id': 'customized', 'purchased_at': '2026-10-01T10:00:00+08:00'})
        r.raise_for_status(); tid = r.headers['X-Trace-ID']; trace = (await client.get('/api/traces/' + tid)).json()
        assert trace['status'] == 'completed' and trace['result']['evidence']
        for e in trace['result']['evidence']:
            evidence = await client.get(f"/api/evidence/{trace['index_id']}/{e['chunk_id']}"); evidence.raise_for_status()
            v = evidence.json(); assert v['source_text'][v['source_start']:v['source_end']] == v['text']
        report['scenarios'].append({'name': 'policy_citations_original_source', 'trace': trace})
        sid = await session()
        # 得到任务ID后立即取消，不依赖请求执行恰好有多慢。
        async with client.stream('POST', '/api/agent/chat', json={'session_id': sid, 'request_id': str(uuid4()), 'question': '退款金额是多少？'}) as response:
            response.raise_for_status(); tid = response.headers['X-Task-ID']
            cancelled = await client.post(f'/api/agent/tasks/{tid}/cancel'); cancelled.raise_for_status()
            await response.aread()
        task = (await client.get('/api/agent/tasks/' + tid)).json()
        assert task['status'] == 'cancelled'
        report['scenarios'].append({'name': 'cancel', 'trace_id': tid, 'status': task['status']})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'output': str(output), 'mode': report['health']['mode'], 'scenarios': [s['name'] for s in report['scenarios']]}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8008')
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
