"""分块与向量空间契约。配置文件不能携带凭证或生成参数。"""
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


class ChunkConfig(StrictConfig):
    strategy: Literal['rule-v1', 'token-window-v1'] = 'rule-v1'
    target_tokens: int = Field(400, ge=50)
    max_tokens: int = Field(600, ge=200)
    overlap_tokens: int = Field(60, ge=0)
    parent_max_tokens: int = Field(1200, ge=200)
    parent_relation: Literal['bounded-rule-v1'] = 'bounded-rule-v1'
    tokenizer: Literal['tiktoken-0.9.0-cl100k_base-estimate'] = 'tiktoken-0.9.0-cl100k_base-estimate'

    @model_validator(mode='after')
    def limits(self):
        if not self.overlap_tokens < self.target_tokens <= self.max_tokens:
            raise ValueError('分块参数需要 overlap < target <= max')
        return self


class EmbeddingConfig(StrictConfig):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    dimensions: int = Field(1024, ge=1, le=4096)
    document_encoding: Literal['title-scope-rule-v1', 'title-rule-v2'] = 'title-scope-rule-v1'
    query_encoding: Literal['raw-v1', 'query-prefix-v1'] = 'raw-v1'
    normalization: Literal['provider', 'l2'] = 'provider'
    revision: str | None = None


class IndexConfig(StrictConfig):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{1,63}$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    hypothesis: str = Field(min_length=1)
    chunk: ChunkConfig
    embedding: EmbeddingConfig
    search: Literal['exact_cosine'] = 'exact_cosine'

    @classmethod
    def read(cls, path):
        return cls.model_validate(json.loads(Path(path).read_text(encoding='utf-8-sig')))


class IndexCompatibilityError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
