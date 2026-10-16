"""受控错误注入：只评价金额断言/错误退出机制，不评价真实模型语义质量。"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.agent import AgentRuntime, setup_agent
from backend.agent_contract import AgentConfig, AgentRequest
from backend.agent_store import AgentStore
from backend.business_tools import BusinessService
from backend.config import Settings
from backend.db import Database
from backend.knowledge import digest
from backend.models import ModelAdapter
from backend.review_contract import ReviewConfig
from scripts.compare_stage7 import PRESETS
from scripts.compare_stage5 import save


async def compare(args):
    if not args.output.resolve().is_relative_to(ROOT/'local') or args.output.exists():
        raise ValueError('结果必须是local下的新文件')
    base=Settings(_env_file=None,rag_mode='mock',database_url=os.environ['STAGE7_DATABASE_URL'])
    db=Database(base);await db.migrate();await setup_agent(db)
    index=await db.get_index(args.index_id);db.check_index(index)
    # 标签只用于离线评分，生成夹具仅根据当前工具证据填充正/误数值。
    cases=[dict(id=f'{split}-PROBE-{i}',split=split,order=order,error=bad,expected_cents=cents)
           for split in ['development','validation']
           for i,(order,bad,cents) in enumerate([('ORD-006',True,15819),('ORD-013',True,6790),
                                               ('ORD-008',False,17900),('ORD-009',False,17900)],1)]
    report={'kind':'controlled_mock_error_injection','started_at':datetime.now(timezone.utc).isoformat(),
        'manifest':{'cases':cases,'index_id':args.index_id,'presets':PRESETS,'request_models':15,
            'source_hashes':{p:digest((ROOT/p).read_text('utf-8-sig')) for p in
              ['backend/agent.py','backend/evidence_review.py','scripts/compare_stage7_probes.py']}},
        'limitations':['仅受控数值错误机制；模拟模型，无真实语义评分。',
                       '全部组使用相同15次模型请求上限，默认12次的对照另列；不据此调整默认。',
                       '离线标签不发送至在线生成/审核；不读取独立测试集。'], 'profiles':[]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    for name,path in PRESETS.items():
        profile={'name':name,'cases':[]};report['profiles'].append(profile)
        for case in cases:
            s=base.model_copy(update={'agent_config':AgentConfig(id='stage7-probe-dynamic-15',strategy='dynamic',experiment=True,request_models=15),
                'review_config':ReviewConfig.read(ROOT/path)})
            store=AgentStore(Database(s)); sid=await store.create_session('CUST-001')
            class ProbeAdapter(ModelAdapter):
                async def generate(self,messages,evidence,record):
                    await self.before_call('generation',1200)
                    calc=next(e for e in evidence if 'total_estimated_cents' in e['text'])
                    amount=json.loads(calc['text'])['data']['total_estimated_cents']/100
                    # 只在初始答案注入错误；收到问题级修复输入后用当前证据生成。
                    repair='candidate_answer' in messages[-1]['content']
                    value=f"预估退款为{999 if case['error'] and not repair else amount:.2f}元 [{calc['citation_id']}]。非审批或到账金额。"
                    yield value
                    await record(dict(call_id=str(uuid4()),role='generation',mode='mock',status='success',
                        usage=None,usage_source='unknown',actual_model='mock-controlled-amount-fixture',elapsed_ms=0))
            body=AgentRequest(session_id=sid,request_id=uuid4(),question=case['order']+'退款金额是多少？')
            tid,_=await store.submit(body,'CUST-001',BusinessService())
            await AgentRuntime(s,await store.task(tid),ProbeAdapter(s)).run()
            task=await store.task(tid);trace=await db.trace(tid)
            answer=(trace['result'] or {}).get('answer','')
            amounts=[round(float(n)*100) for n in re.findall(r'(\d+(?:\.\d+)?)元',answer)]
            # 独立于在线passed/issues，以固定业务标签核对发布的数值；空答案不当成正确。
            wrong=any(n!=case['expected_cents'] for n in amounts)
            covered=case['expected_cents'] in amounts and not wrong
            safe_exit=any(w in answer for w in ['请联系人工','无法确认其余'])
            entry=dict(case,trace_id=tid,status=task['status'],answer=answer,offline_wrong_number=wrong,
                       offline_number_covered=covered,unnecessary_safe_exit_proxy=safe_exit,
                       budget=task['budget'],result=trace['result'],error=trace['error'],events=trace['events'],model_calls=trace['model_calls'])
            profile['cases'].append(entry);save(args.output,report)
            print(name,case['id'],task['status'],flush=True)
        profile['summary']={split:{'cases':len(rows),
            'wrong_number_cases':sum(c['offline_wrong_number'] for c in rows),
            'number_covered':sum(c['offline_number_covered'] for c in rows),
            'unnecessary_safe_exit_proxy':sum(c['unnecessary_safe_exit_proxy'] for c in rows),
            'failed':sum(c['status']=='failed' for c in rows),
            'models':sum(c['budget']['models'] for c in rows),
            'tools':sum(c['budget']['tools'] for c in rows),
            'reserved_tokens':sum(c['budget']['reserved_tokens'] for c in rows)}
            for split in ['development','validation']
            for rows in [[c for c in profile['cases'] if c['split']==split]]}
        save(args.output,report)
    report['status']='completed';save(args.output,report)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--index-id',required=True)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(compare(parser.parse_args()))
