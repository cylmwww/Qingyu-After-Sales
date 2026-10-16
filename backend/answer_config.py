"""阶段五不可变查询/答案配置；不包含业务工具或审核。"""
from pathlib import Path
from typing import Literal

from pydantic import Field
from backend.index_config import StrictConfig


class RewriteConfig(StrictConfig):
    enabled: bool = False
    prompt_version: Literal['extractive-query-v1'] = 'extractive-query-v1'
    max_terms: int = Field(8, ge=1, le=12)
    max_output_tokens: int = Field(300, ge=100, le=600)
    rrf_k: int = Field(60, ge=1, le=1000)


class ContextConfig(StrictConfig):
    strategy: Literal['hits-only', 'parent-related'] = 'hits-only'
    organization: Literal['rank', 'anchor-grouped'] = 'rank'
    related_hops: Literal[1] = 1
    max_supplements: int = Field(12, ge=1, le=40)


class GenerationConfig(StrictConfig):
    prompt_version: Literal['rag-evidence-v1', 'rag-evidence-v2'] = 'rag-evidence-v1'
    evidence_token_budget: int = Field(6000, ge=200, le=60000)
    input_token_budget: int = Field(12000, ge=500, le=100000)
    max_output_tokens: int = Field(1200, ge=100, le=4096)


class AnswerConfig(StrictConfig):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=r'^[a-z][a-z0-9-]{1,63}$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    hypothesis: str = Field(min_length=1)
    rewrite: RewriteConfig = RewriteConfig()
    context: ContextConfig = ContextConfig()
    generation: GenerationConfig = GenerationConfig()

    @classmethod
    def read(cls, path):
        return cls.model_validate_json(Path(path).read_text(encoding='utf-8-sig'))
