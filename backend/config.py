from pathlib import Path
import json
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from backend.index_config import ChunkConfig, IndexConfig
from backend.retrieval_config import RetrievalConfig
from backend.answer_config import AnswerConfig
from backend.agent_contract import AgentConfig
from backend.review_contract import ReviewConfig
from backend.agent_loop.contracts import LoopConfig

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore', frozen=True)

    database_url: SecretStr = SecretStr('postgresql://rag:rag-local-only@127.0.0.1:5433/rag')
    rag_mode: str = 'real'
    shop_id: str = 'SHOP-DEMO-001'
    api_access_token: SecretStr = SecretStr('')
    thinking_model_provider: str = 'third-party'
    thinking_model_base_url: str = ''
    thinking_model_api_key: SecretStr = SecretStr('')
    thinking_model_id: str = 'deepseek-v4.1-flash'
    thinking_model_enable_thinking: bool = False
    generation_enable_thinking: bool = False
    structured_response_protocol: str = 'strict-json-v1'
    embedding_model_provider: str = 'aliyun'
    embedding_model_base_url: str = ''
    embedding_model_api_key: SecretStr = SecretStr('')
    embedding_model_id: str = 'text-embedding-v4'
    embedding_model_dimensions: int = 1024
    embedding_document_encoding: str = 'title-scope-rule-v1'
    embedding_query_encoding: str = 'raw-v1'
    embedding_normalization: str = 'provider'
    embedding_model_revision: str | None = None
    index_config_path: str | None = None
    index_config: IndexConfig | None = Field(None, exclude=True)
    retrieval_config_path: str | None = None
    retrieval_config: RetrievalConfig | None = Field(None, exclude=True)
    answer_config_path: str | None = None
    answer_config: AnswerConfig | None = Field(None, exclude=True)
    bm25_dictionary: dict | None = Field(None, exclude=True)
    agent_config_path: str | None = None
    agent_config: AgentConfig = Field(default_factory=AgentConfig, exclude=True)
    review_config_path: str | None = None
    review_config: ReviewConfig = Field(default_factory=ReviewConfig, exclude=True)
    runtime_version: str = 'legacy'
    loop_config_path: str | None = None
    loop_config: LoopConfig | None = Field(None, exclude=True)
    agent_customer_id: str = 'CUST-001'
    business_snapshot_id: str = 'SNAP-DEMO-20261009'
    rerank_model_provider: str = 'configured-provider'
    rerank_model_url: str = ''
    rerank_model_api_key: SecretStr = SecretStr('')
    rerank_model_id: str = 'qwen3-rerank'
    rerank_timeout_seconds: float = Field(15, gt=0)
    chunk_strategy: str = 'rule-v1'
    embedding_batch_size: int = Field(10, ge=1, le=16)
    chunk_target_tokens: int = Field(400, ge=100)
    chunk_max_tokens: int = Field(600, ge=100)
    chunk_overlap_tokens: int = Field(60, ge=0)
    parent_max_tokens: int = 1200
    retrieval_top_k: int = Field(20, ge=1, le=100)
    context_top_k: int = Field(6, ge=1, le=20)
    evidence_token_budget: int = Field(6000, ge=200)
    input_token_budget: int = Field(12000, ge=500)
    max_output_tokens: int = Field(1200, ge=100, le=4096)
    model_timeout_seconds: float = Field(30, gt=0)
    embedding_timeout_seconds: float = Field(15, gt=0)
    request_timeout_seconds: float = Field(120, gt=0)
    model_retry_count: int = Field(1, ge=0, le=1)
    trace_retention_days: int = Field(30, ge=1)
    prompt_version: str = 'rag-evidence-v1'
    tokenizer_version: str = 'tiktoken-0.9.0-cl100k_base-estimate'

    @model_validator(mode='after')
    def validate_config(self):
        if self.review_config_path:
            path = Path(self.review_config_path)
            object.__setattr__(self, 'review_config', ReviewConfig.read(path if path.is_absolute() else ROOT / path))
        if self.agent_config_path:
            path = Path(self.agent_config_path)
            object.__setattr__(self, 'agent_config', AgentConfig.read(path if path.is_absolute() else ROOT / path))
        if self.loop_config_path:
            path = Path(self.loop_config_path)
            object.__setattr__(self, 'loop_config', LoopConfig.read(path if path.is_absolute() else ROOT / path))
        self.validate_runtime()
        if self.review_config.correction == 'targeted_lookup' and self.agent_config.strategy in {'fast', 'fixed'} and self.agent_config.experiment:
            raise ValueError('定向补查需要动态策略')
        if self.answer_config_path:
            path = Path(self.answer_config_path)
            object.__setattr__(self, 'answer_config', AnswerConfig.read(path if path.is_absolute() else ROOT / path))
        if self.answer_config:
            for field, value in self.answer_config.generation.model_dump().items():
                object.__setattr__(self, field, value)
        if self.retrieval_config_path:
            path = Path(self.retrieval_config_path)
            profile = RetrievalConfig.read(path if path.is_absolute() else ROOT / path)
            object.__setattr__(self, 'retrieval_config', profile)
            object.__setattr__(self, 'context_top_k', profile.final_top_k)
            if profile.mode != 'vector':
                from backend.bm25 import ChineseTokenizer
                dictionary_path = (ROOT / profile.bm25.dictionary_path).resolve()
                if not dictionary_path.is_relative_to(ROOT / 'configs' / 'lexicons'):
                    raise ValueError('术语词典必须在 configs/lexicons 内')
                dictionary = json.loads(dictionary_path.read_text(encoding='utf-8-sig'))
                ChineseTokenizer(dictionary)
                if dictionary['version'] != profile.bm25.dictionary_version:
                    raise ValueError('词典版本与 BM25 配置不一致')
                object.__setattr__(self, 'bm25_dictionary', dictionary)
        if self.index_config_path:
            path = Path(self.index_config_path)
            profile = IndexConfig.read(path if path.is_absolute() else ROOT / path)
            object.__setattr__(self, 'index_config', profile)
            for field, value in {
                'chunk_strategy': profile.chunk.strategy, 'chunk_target_tokens': profile.chunk.target_tokens,
                'chunk_max_tokens': profile.chunk.max_tokens, 'chunk_overlap_tokens': profile.chunk.overlap_tokens,
                'parent_max_tokens': profile.chunk.parent_max_tokens, 'tokenizer_version': profile.chunk.tokenizer,
                'embedding_model_provider': profile.embedding.provider, 'embedding_model_id': profile.embedding.model,
                'embedding_model_dimensions': profile.embedding.dimensions,
                'embedding_document_encoding': profile.embedding.document_encoding,
                'embedding_query_encoding': profile.embedding.query_encoding,
                'embedding_normalization': profile.embedding.normalization,
                'embedding_model_revision': profile.embedding.revision,
            }.items():
                object.__setattr__(self, field, value)
        if self.rag_mode not in {'real', 'mock'}:
            raise ValueError('RAG_MODE 只能为 real 或 mock')
        if self.structured_response_protocol not in {'strict-json-v1', 'json-or-sse-v1', 'sse-json-v1'}:
            raise ValueError('不支持的结构化响应协议')
        if not 1 <= self.embedding_model_dimensions <= 4096:
            raise ValueError('维度必须在 1～4096 内，供应商支持需另行验证')
        self.chunk_contract()
        if self.embedding_document_encoding not in {'title-scope-rule-v1', 'title-rule-v2'} or self.embedding_query_encoding not in {'raw-v1', 'query-prefix-v1'} or self.embedding_normalization not in {'provider', 'l2'}:
            raise ValueError('不支持的 Embedding 编码或归一化约定')
        if not self.chunk_overlap_tokens < self.chunk_target_tokens <= self.chunk_max_tokens:
            raise ValueError('分块参数需要 overlap < target <= max')
        if not self.retrieval_config and self.context_top_k > self.retrieval_top_k:
            raise ValueError('上下文候选不能超过召回数量')
        for url in [self.thinking_model_base_url, self.embedding_model_base_url, self.rerank_model_url]:
            if url:
                parsed = urlsplit(url)
                if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError('模型地址必须为不含凭证、查询参数的 HTTP(S) 地址')
        return self

    def validate_runtime(self):
        if self.runtime_version not in {'legacy', 'agent-loop-v1'}:
            raise ValueError('unsupported_runtime_version')
        if self.runtime_version == 'legacy':
            if self.loop_config is not None:
                raise ValueError('loop_configuration_requires_explicit_runtime')
            return
        if self.loop_config is None:
            raise ValueError('loop_configuration_required')
        if self.loop_config.ablation and (not self.agent_config.experiment or self.agent_config.strategy != 'dynamic'):
            raise ValueError('loop_ablation_requires_explicit_dynamic_experiment')
        if self.review_config.precheck or not self.review_config.postcheck or self.review_config.correction != 'none':
            raise ValueError('loop_requires_post_only_without_legacy_correction')
        if self.loop_config.decision_tokens > self.agent_config.planner_tokens or self.loop_config.max_tool_actions > self.agent_config.max_steps:
            raise ValueError('loop_limits_exceed_existing_limits')

    def snapshot(self):
        data = self.model_dump(mode='json', exclude={'database_url', 'api_access_token', 'thinking_model_api_key', 'embedding_model_api_key', 'rerank_model_api_key'})
        data.update(credential_refs=['THINKING_MODEL_API_KEY', 'EMBEDDING_MODEL_API_KEY'],
                    strategy='single_turn_vector_rag', query_rewrite=False, context_expansion=False,
                    rerank=False, bm25=False, reviewer=False, tokenizer_usage='estimate_not_provider_usage')
        data['index_configuration'] = self.index_config.model_dump(mode='json') if self.index_config else None
        data['answer_configuration'] = self.answer_config.model_dump(mode='json') if self.answer_config else None
        if self.answer_config:
            data.update(query_rewrite=self.answer_config.rewrite.enabled,
                        context_expansion=self.answer_config.context.strategy == 'parent-related')
        data['chunk_contract'] = self.chunk_contract()
        data['agent_configuration'] = self.agent_config.model_dump(mode='json')
        data['review_configuration'] = self.review_config.model_dump(mode='json')
        data['loop_configuration'] = self.loop_config.model_dump(mode='json') if self.loop_config else None
        data['reviewer'] = self.review_config.postcheck
        data['retrieval_configuration'] = self.retrieval_config.model_dump(mode='json') if self.retrieval_config else None
        if self.retrieval_config:
            from backend.knowledge import digest
            data['bm25_dictionary_hash'] = digest(self.bm25_dictionary) if self.bm25_dictionary else None
            data.update(strategy='single_turn_configurable_rag', bm25=self.retrieval_config.mode != 'vector',
                        rerank=self.retrieval_config.rerank.enabled)
            data['credential_refs'].append('RERANK_MODEL_API_KEY')
        return data

    def chunk_contract(self):
        return ChunkConfig(strategy=self.chunk_strategy, target_tokens=self.chunk_target_tokens,
            max_tokens=self.chunk_max_tokens, overlap_tokens=self.chunk_overlap_tokens,
            parent_max_tokens=self.parent_max_tokens, tokenizer=self.tokenizer_version).model_dump(mode='json')

    def encode_query(self, question):
        return question if self.embedding_query_encoding == 'raw-v1' else '查询：' + question

    def embedding_contract(self):
        contract = {'mode': self.rag_mode, 'provider': self.embedding_model_provider,
                'base_url': self.embedding_model_base_url, 'model': self.embedding_model_id,
                'dimensions': self.embedding_model_dimensions, 'encoding': self.embedding_document_encoding,
                'mock_version': 'hash-char-bigram-v1' if self.rag_mode == 'mock' else None}
        # 保留第二阶段默认契约的精确形式，使旧索引仍可读取。
        if self.embedding_query_encoding != 'raw-v1' or self.embedding_normalization != 'provider' or self.embedding_model_revision:
            contract.update(query_encoding=self.embedding_query_encoding, normalization=self.embedding_normalization,
                            revision=self.embedding_model_revision)
        return contract
