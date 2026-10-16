"""只读核对正式库索引及来源链，不调用任何模型。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.config import Settings
from backend.db import Database
from backend.knowledge import digest


async def verify(output, previous):
    db=Database(Settings(index_config_path=None))
    index=await db.current_index()
    db.check_index(index)
    async with db.connection() as connection:
        chunks=await (await connection.execute('''SELECT id,document_key,parent_id,role,source_start,source_end,
            text,embedding::text AS vector FROM knowledge_chunks WHERE index_id=%s ORDER BY id''',(index['id'],))).fetchall()
        documents=await (await connection.execute('''SELECT d.document_key,d.source_hash FROM knowledge_documents d
            WHERE d.document_key IN (SELECT document_key FROM knowledge_chunks WHERE index_id=%s) ORDER BY d.document_key''',(index['id'],))).fetchall()
        refs=await (await connection.execute('''SELECT count(*) AS invalid FROM knowledge_chunks c JOIN knowledge_documents d
            USING(document_key) WHERE c.index_id=%s AND substring(d.source_text FROM c.source_start+1
            FOR c.source_end-c.source_start)<>c.text''',(index['id'],))).fetchone()
    result={'index_id':str(index['id']),'knowledge_version':index['knowledge_version'],
        'embedding_contract':index['embedding_contract'],'chunk_vector_hash':digest(chunks),'document_hash':digest(documents),
        'source_range_errors':refs['invalid'],'children':sum(c['role']=='child' for c in chunks),
        'parents':sum(c['role']=='parent' for c in chunks),'compatible':True}
    if previous:
        result['baseline_unchanged']=all(result[k]==v for k,v in json.loads(previous.read_text(encoding='utf-8')).items()
            if k!='baseline_unchanged')
        if not result['baseline_unchanged']: raise ValueError('正式索引基线发生变化')
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='只读第二阶段索引完整性检查，不调用模型')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--previous',type=Path)
    args=parser.parse_args()
    if not args.output.resolve().is_relative_to(ROOT/'local') or args.output.exists():
        parser.error('结果必须追加到 local，不覆盖已有记录')
    asyncio.run(verify(args.output,args.previous))
