"""阶段四检索配置；业务过滤只接受结构化确认字段。"""
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from backend.index_config import StrictConfig


class BM25Config(StrictConfig):
    tokenizer: Literal['zh-dictionary-bigram-v1'] = 'zh-dictionary-bigram-v1'
    dictionary_path: str = 'configs/lexicons/product-terms.v1.0.0.json'
    dictionary_version: str = '1.0.0'
    index_version: str = '1.0.0'
    k1: float = Field(1.2, gt=0, le=5)
    b: float = Field(.75, ge=0, le=1)


class RerankConfig(StrictConfig):
    enabled: bool = False
    candidates: int = Field(30, ge=1, le=200)
    protocol: Literal['flat-results-v1', 'dashscope-v1'] = 'flat-results-v1'
    document_encoding: Literal['title-scope-text-v1'] = 'title-scope-text-v1'


class RetrievalConfig(StrictConfig):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{1,63}$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    hypothesis: str = Field(min_length=1)
    mode: Literal['vector', 'bm25', 'hybrid'] = 'vector'
    vector_top_k: int = Field(20, ge=1, le=100)
    bm25_top_k: int = Field(20, ge=1, le=100)
    fusion: Literal['direct', 'rrf', 'ordered-merge'] = 'direct'
    merge_order: Literal['vector-first', 'bm25-first'] = 'vector-first'
    rrf_k: int = Field(60, ge=1, le=1000)
    vector_weight: float = Field(1., gt=0, le=10)
    bm25_weight: float = Field(1., gt=0, le=10)
    final_top_k: int = Field(6, ge=1, le=20)
    business_filter_fields: tuple[Literal['category_id', 'purchased_at', 'after_sales_applied_at'], ...] = (
        'category_id', 'purchased_at', 'after_sales_applied_at')
    bm25: BM25Config = BM25Config()
    rerank: RerankConfig = RerankConfig()

    @model_validator(mode='after')
    def dependencies(self):
        if (self.mode == 'hybrid') == (self.fusion == 'direct'):
            raise ValueError('单路必须 direct，双路必须显式 RRF 或 ordered-merge')
        capacity = self.vector_top_k if self.mode == 'vector' else self.bm25_top_k
        if self.mode == 'hybrid':
            capacity = self.vector_top_k + self.bm25_top_k
        if self.final_top_k > capacity or (self.rerank.enabled and self.final_top_k > self.rerank.candidates):
            raise ValueError('最终数量不能超过召回容量或重排候选上限')
        if len(set(self.business_filter_fields)) != len(self.business_filter_fields):
            raise ValueError('过滤字段不能重复')
        return self

    @classmethod
    def read(cls, path):
        # JSON模式支持数组转为不可变 tuple，仍拒绝未知字段及错误数值类型。
        return cls.model_validate_json(Path(path).read_text(encoding='utf-8-sig'))

    def filters(self, confirmed):
        unknown = set(confirmed) - {'category_id', 'purchased_at', 'after_sales_applied_at'}
        if unknown:
            raise ValueError('不支持的业务过滤字段')
        return {k: v for k, v in confirmed.items() if k in self.business_filter_fields and v is not None}
