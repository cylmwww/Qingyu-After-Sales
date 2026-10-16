from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import re

import tiktoken

from backend.config import Settings, ROOT
from backend.index_config import IndexCompatibilityError
from scripts.validate_stage1 import validate, read_json


def digest(value):
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def token_count(text):
    return len(tiktoken.get_encoding('cl100k_base').encode(text, disallowed_special=()))


def split_text(text, target, maximum, overlap, force_target=False):
    if overlap >= target or target <= 0 or maximum < target:
        raise ValueError('分块有效载荷需要 overlap < target <= max')
    if token_count(text) <= (target if force_target else maximum):
        return [(0, len(text), text)]
    result, start = [], 0
    while start < len(text):
        low, high = start + 1, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if token_count(text[start:mid]) <= target:
                low = mid
            else:
                high = mid - 1
        end = low
        if end < len(text):
            boundary = max(text.rfind('\n', start, end), text.rfind('。', start, end))
            if boundary > start + (end - start) // 2:
                end = boundary + 1
        result.append((start, end, text[start:end]))
        if end == len(text):
            break
        next_start = end
        while next_start > start + 1 and token_count(text[next_start - 1:end]) <= overlap:
            next_start -= 1
        start = max(start + 1, next_start)
    return result


@dataclass
class Document:
    key: str
    kind: str
    stable_id: str
    version: str
    source_path: str
    source_text: str
    source_hash: str
    metadata: dict


@dataclass
class Chunk:
    id: str
    document_key: str
    parent_id: str | None
    role: str
    ordinal: int
    section: str
    text: str
    embedding_text: str
    tokens: int
    source_start: int
    source_end: int
    metadata: dict


def load_knowledge(settings: Settings, root: Path = ROOT):
    report = validate(root)
    if report['result'] != 'pass' or report['human_review_status'] != 'approved':
        raise ValueError('知识包校验或人工审核门禁未通过')
    manifest = read_json(root / 'data/manifest.json')
    documents, chunks = [], []

    def append_chunks(document, section, text, offset, metadata, title):
        prefix = f'{title}\n条款/问题：{section}\n适用范围：' + json.dumps({
            key: metadata[key] for key in ['category_ids', 'selection_event', 'effective_from', 'effective_until']
        }, ensure_ascii=False) + '\n'
        if settings.embedding_document_encoding == 'title-rule-v2':
            prefix = f'{title}\n条款/问题：{section}\n'
        allowance = settings.chunk_max_tokens - token_count(prefix) - 5
        target = min(settings.chunk_target_tokens, allowance)
        if allowance < 50 or settings.chunk_overlap_tokens >= target:
            raise IndexCompatibilityError('chunk_payload_budget_incompatible')
        parents = split_text(text, settings.parent_max_tokens, settings.parent_max_tokens, 0)
        for parent_no, (pstart, pend, parent_text) in enumerate(parents):
            parent_id = 'P-' + digest([document.key, section, parent_no, parent_text])[:32]
            chunks.append(Chunk(parent_id, document.key, None, 'parent', parent_no, section, parent_text, '',
                                token_count(parent_text), offset + pstart, offset + pend, metadata))
            parts = split_text(parent_text, target, allowance, settings.chunk_overlap_tokens,
                               force_target=settings.chunk_strategy == 'token-window-v1')
            for ordinal, (start, end, child) in enumerate(parts):
                encoded = prefix + child
                chunk_id = 'C-' + digest([parent_id, ordinal, encoded])[:32]
                chunks.append(Chunk(chunk_id, document.key, parent_id, 'child', ordinal, section, child, encoded,
                                    token_count(encoded), offset + pstart + start, offset + pstart + end, metadata))

    for relative in manifest['files']['policies']:
        raw = (root / relative).read_text(encoding='utf-8-sig')
        match = re.search(r'<!-- policy-metadata\s*(.*?)\s*-->', raw, re.S)
        meta = json.loads(match.group(1))
        key = f"policy:{meta['id']}:{meta['version']}"
        doc = Document(key, 'policy', meta['id'], meta['version'], relative, raw, digest(raw), meta)
        documents.append(doc)
        anchors = list(re.finditer(r'<a id="(RULE-[A-Z0-9-]+)"></a>', raw))
        for i, anchor in enumerate(anchors):
            end = anchors[i + 1].start() if i + 1 < len(anchors) else len(raw)
            rule = next(r for r in meta['rules'] if r['id'] == anchor.group(1))
            metadata = {k: meta[k] for k in ['shop_id', 'category_ids', 'effective_from', 'effective_until', 'selection_event', 'source', 'review_status', 'lifecycle']}
            metadata.update(ref={'kind': 'policy_rule', 'id': rule['id'], 'version': meta['version']},
                            conditions=rule['conditions'], exceptions=rule['exceptions'],
                            related_refs=rule['related_refs'], evidence_refs=[], title=rule['title'])
            append_chunks(doc, rule['id'], raw[anchor.start():end], anchor.start(), metadata, meta['title'])
    faq_path = manifest['files']['faq']
    faq = read_json(root / faq_path)
    for item in faq['items']:
        raw = '问题：' + item['question'] + '\n其他表述：' + '；'.join(item['paraphrases']) + '\n回答：' + item['answer']
        key = f"faq:{item['id']}:{item['version']}"
        meta = dict(item)
        meta.update(shop_id=faq['shop_id'], **item['applicability'], source={'kind': 'fictional_shop_faq'})
        doc = Document(key, 'faq', item['id'], item['version'], faq_path + '#' + item['id'], raw, digest(raw), meta)
        documents.append(doc)
        metadata = {k: meta[k] for k in ['shop_id', 'category_ids', 'effective_from', 'effective_until', 'selection_event', 'source', 'review_status', 'lifecycle']}
        metadata.update(ref={'kind': 'faq', 'id': item['id'], 'version': item['version']},
                        conditions=item['applicability']['conditions'], evidence_refs=item['evidence_refs'],
                        evidence_time_bindings=item.get('evidence_time_bindings', []), title=item['question'])
        append_chunks(doc, item['id'], raw, 0, metadata, item['question'])
    source_fingerprint = digest([asdict(document) for document in documents])
    return manifest, documents, chunks, source_fingerprint
