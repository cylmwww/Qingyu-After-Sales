"""可重建的应用侧 BM25。索引不保存身份推断，不读取评测标准。"""
from collections import Counter
import math
import re
import unicodedata

from backend.knowledge import digest
from backend.index_config import IndexCompatibilityError


class ChineseTokenizer:
    version = 'zh-dictionary-bigram-v1'

    def __init__(self, dictionary):
        if set(dictionary) != {'version', 'terms'} or not isinstance(dictionary['terms'], list):
            raise ValueError('术语词典结构无效')
        terms = dictionary['terms']
        if not terms or any(not isinstance(t, str) or not t.strip() for t in terms) or len(set(terms)) != len(terms):
            raise ValueError('术语必须为非空且不重复的字符串')
        self.dictionary = dictionary
        self.terms = sorted((self.normalize(t) for t in terms), key=lambda t: (-len(t), t))

    @staticmethod
    def normalize(text):
        return unicodedata.normalize('NFKC', text).lower()

    def tokenize(self, text):
        text = self.normalize(text)
        # 显式最大匹配术语；未登录中文连续串采用单字和相邻双字，避免依赖可漂移的外部分词模型。
        tokens = []
        for segment in re.findall(r'[\u3400-\u9fff]+|[a-z0-9]+(?:[._-][a-z0-9]+)*', text):
            if not re.match(r'[\u3400-\u9fff]', segment):
                tokens.append(segment)
                continue
            pos, pending = 0, ''
            def flush(value):
                return list(value) + [value[i:i+2] for i in range(len(value)-1)]
            while pos < len(segment):
                term = next((t for t in self.terms if segment.startswith(t, pos)), None)
                if term:
                    tokens.extend(flush(pending)); pending = ''
                    tokens.append(term); pos += len(term)
                else:
                    pending += segment[pos]; pos += 1
            tokens.extend(flush(pending))
        return tokens


def corpus_fingerprint(rows):
    return digest([{k: r[k] for k in ('chunk_id', 'document_key', 'document_version', 'section', 'text',
        'metadata', 'source_start', 'source_end')} for r in sorted(rows, key=lambda r: r['chunk_id'])])


def build_artifact(index, rows, config, dictionary):
    tokenizer = ChineseTokenizer(dictionary)
    if dictionary['version'] != config.dictionary_version:
        raise IndexCompatibilityError('bm25_dictionary_version_mismatch')
    documents = {}
    for row in rows:
        terms = tokenizer.tokenize(row['section'] + '\n' + row['text'])
        documents[row['chunk_id']] = {'length': len(terms), 'tf': dict(Counter(terms))}
    frequencies = Counter(term for doc in documents.values() for term in doc['tf'])
    return {'format': 'bm25-artifact-v1', 'index_id': str(index['id']), 'shop_id': index['shop_id'],
        'knowledge_version': index['knowledge_version'], 'source_fingerprint': index['source_fingerprint'],
        'corpus_fingerprint': corpus_fingerprint(rows), 'configuration': config.model_dump(mode='json'),
        'dictionary': dictionary, 'dictionary_hash': digest(dictionary), 'documents': documents,
        'document_frequency': dict(frequencies), 'average_length': sum(d['length'] for d in documents.values()) / max(1, len(documents))}


def search_artifact(artifact, rows, question, top_k, config):
    query = set(ChineseTokenizer(artifact['dictionary']).tokenize(question))
    n, avg = len(artifact['documents']), artifact['average_length'] or 1
    scored = []
    for row in rows:
        doc = artifact['documents'].get(row['chunk_id'])
        if doc is None:
            raise IndexCompatibilityError('bm25_chunk_version_mismatch')
        score = 0.
        for term in query:
            tf = doc['tf'].get(term, 0)
            if tf:
                df = artifact['document_frequency'][term]
                idf = math.log(1 + (n - df + .5) / (df + .5))
                score += idf * tf * (config.k1 + 1) / (tf + config.k1 * (1 - config.b + config.b * doc['length']/avg))
        if score > 0:
            scored.append(dict(row, bm25_score=score, score_type='bm25'))
    scored.sort(key=lambda r: (-r['bm25_score'], r['chunk_id']))
    results = [dict(r, rank=i) for i, r in enumerate(scored[:top_k], 1)]
    reasons = {r['chunk_id']: 'bm25_no_term_match' for r in rows if r['chunk_id'] not in {s['chunk_id'] for s in scored}}
    reasons.update({r['chunk_id']: 'bm25_top_k' for r in scored[top_k:]})
    return results, reasons
