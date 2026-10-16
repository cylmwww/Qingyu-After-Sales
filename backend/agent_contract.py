"""第六阶段的严格配置、用户事实及模型输出契约。"""
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from backend.request_contract import ChatRequest
from backend.plan_contract import TOOL_CONTRACTS, TOOLS


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class AgentConfig(Strict):
    id: str = 'agent-v1'
    version: str = '1.1.0'
    strategy: Literal['auto', 'fast', 'fixed', 'dynamic'] = 'auto'
    experiment: bool = False
    max_steps: int = Field(6, ge=1, le=6)
    max_replans: int = Field(2, ge=0, le=2)
    no_progress_limit: int = Field(2, ge=1, le=3)
    request_tools: int = Field(10, ge=1, le=25)
    request_models: int = Field(12, ge=1, le=30)
    task_tools: int = Field(25, ge=1, le=25)
    task_models: int = Field(30, ge=1, le=30)
    token_budget: int = Field(60000, ge=1000, le=60000)
    task_seconds: float = Field(300, gt=0, le=300)
    business_timeout: float = Field(5, gt=0, le=5)
    planner_tokens: int = Field(1500, ge=100, le=1500)
    intent_tokens: int = Field(300, ge=100, le=1000)
    executor_tokens: int = Field(500, ge=100, le=1000)
    context_items: int = Field(20, ge=1, le=32)
    prompt_version: Literal['agent-readonly-v1'] = 'agent-readonly-v1'

    @model_validator(mode='after')
    def locked(self):
        if self.experiment and self.strategy == 'auto':
            raise ValueError('实验必须锁定 fast/fixed/dynamic 策略')
        return self

    @classmethod
    def read(cls, path):
        return cls.model_validate_json(Path(path).read_text('utf-8-sig'))


class Selection(Strict):
    line_id: str = Field(pattern=r'^LINE-\d{3}-\d{2}$')
    unit_indices: list[int] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique(self):
        if any(type(i) is not int or i < 1 for i in self.unit_indices) or len(set(self.unit_indices)) != len(self.unit_indices):
            raise ValueError('商品单元序号必须是唯一的正整数')
        return self


class Facts(ChatRequest):
    # 继承既有时区/品类验证，但事实中没有问题或索引。
    question: str = '用户确认事实'
    index_id: None = None
    order_id: str | None = Field(None, pattern=r'^ORD-\d{3}$')
    after_sale_id: str | None = Field(None, pattern=r'^AS-\d{3}$')
    signed_at: str | None = None
    return_reason: Literal['no_reason', 'quality'] | None = None
    selections: list[Selection] | None = Field(None, max_length=16)
    gift_returned_line_ids: list[str] | None = None
    tags_intact: bool | None = None
    product_intact: bool | None = None
    used_outdoors: bool | None = None
    packaging_intact: bool | None = None
    activated: bool | None = None
    accessories_complete: bool | None = None
    quality_status: Literal['none', 'reported'] | None = None

    @model_validator(mode='after')
    def facts_only(self):
        if self.question != '用户确认事实':
            raise ValueError('facts 中不能嵌套问题')
        if self.signed_at:
            from scripts.validate_stage1 import timestamp
            timestamp(self.signed_at)
        return self

    def values(self):
        # 明确提供null表示撤回旧事实；未提供字段不改变已有事实。
        return self.model_dump(mode='json', exclude={'question', 'index_id'}, exclude_unset=True)


class AgentRequest(Strict):
    session_id: UUID
    request_id: UUID
    question: str = Field(min_length=1, max_length=8000)
    facts: Facts = Field(default_factory=Facts)
    index_id: UUID | None = None
    action: Literal['new', 'clarify', 'revise', 'resume'] = 'new'
    task_id: UUID | None = None

    @model_validator(mode='after')
    def nonempty(self):
        if not self.question.strip():
            raise ValueError('问题不能为空')
        if self.action != 'new' and not self.task_id:
            raise ValueError('续接必须指定任务')
        return self


class Intent(Strict):
    intent: Literal['policy', 'order', 'refund_amount', 'refund_progress', 'logistics', 'unsupported']
    needs_business: bool


class Step(Strict):
    id: str = Field(pattern=r'^step[1-6]$')
    tool: Literal['search_knowledge', 'read_policy_context', 'get_order', 'get_after_sales', 'get_logistics', 'calculate_refund']
    depends_on: list[str] = Field(default_factory=list, max_length=6,
        description='仅含本步骤之前已出现的步骤id；第一步为空数组。不能包含本步骤或尚未出现的步骤。')
    completion: Literal['success', 'observed'] = 'success'
    goal: str = Field(min_length=1, max_length=250)


class Plan(Strict):
    steps: list[Step] = Field(min_length=1, max_length=6)
    reason: str = Field(max_length=500)

    @model_validator(mode='after')
    def dependencies(self):
        seen = set()
        for step in self.steps:
            if step.id in seen or not set(step.depends_on) <= seen:
                raise ValueError('步骤依赖必须唯一、串行且指向之前步骤')
            if step.completion == 'observed' and step.tool != 'get_after_sales':
                raise ValueError('只有售后查询允许用明确无记录作为完成条件')
            seen.add(step.id)
        return self


class Execution(Strict):
    step_id: str
    tool: str
    arguments: dict = Field(default_factory=dict)


def frozen_settings(settings):
    """保存非秘密的可恢复配置；凭证恢复时仍使用服务器环境。"""
    settings.validate_runtime()
    excluded = {'database_url', 'api_access_token', 'thinking_model_api_key', 'embedding_model_api_key', 'rerank_model_api_key'}
    data = settings.model_dump(mode='json', exclude=excluded)
    for field in ['index_config', 'retrieval_config', 'answer_config', 'agent_config', 'review_config', 'loop_config']:
        value = getattr(settings, field)
        data[field] = value.model_dump(mode='json') if value else None
    for field in ['index_config_path', 'retrieval_config_path', 'answer_config_path', 'agent_config_path', 'review_config_path', 'loop_config_path']:
        data[field] = None
    data['bm25_dictionary'] = settings.bm25_dictionary
    data['_runtime_fingerprint'] = runtime_fingerprint()
    return data


def runtime_fingerprint():
    from backend.config import ROOT
    from backend.knowledge import digest
    from importlib.metadata import version
    paths = ['backend/agent.py','backend/agent_contract.py','backend/agent_model.py','backend/agent_store.py',
             'backend/business_tools.py','backend/models.py','backend/config.py','backend/query.py',
             'backend/retrieval.py','backend/answer_context.py','backend/rerank.py','backend/evidence_bundle.py',
             'backend/plan_contract.py','backend/review_contract.py','backend/evidence_review.py','backend/planner_context.py']
    paths += ['backend/agent_dispatch.py'] + [p.relative_to(ROOT).as_posix() for p in sorted((ROOT/'backend/agent_loop').glob('*.py'))]
    return digest({'sources':{p:digest((ROOT/p).read_text('utf-8-sig')) for p in paths},
                   'dependencies':{p:version(p) for p in ['langgraph','langgraph-checkpoint-postgres','langgraph-checkpoint']}})


def restore_settings(current, frozen):
    from backend.config import Settings
    data = current.model_dump()
    data.update(frozen)
    # 历史快照缺字段时绝不能继承当前服务器的新运行时。
    data.update(runtime_version=frozen.get('runtime_version', 'legacy'),
                loop_config=frozen.get('loop_config'), loop_config_path=None)
    # PostgreSQL JSON快照将tuple编码为array；严格模型需按JSON契约还原，
    # 不能用Python dict验证而把合法数组误判成tuple类型错误。
    from backend.index_config import IndexConfig
    from backend.retrieval_config import RetrievalConfig
    from backend.answer_config import AnswerConfig
    from backend.review_contract import ReviewConfig
    from backend.agent_loop.contracts import LoopConfig
    for field, model in {'index_config': IndexConfig, 'retrieval_config': RetrievalConfig,
                         'answer_config': AnswerConfig, 'agent_config': AgentConfig, 'review_config': ReviewConfig,
                         'loop_config': LoopConfig}.items():
        if data.get(field) is not None:
            data[field] = model.model_validate_json(json.dumps(data[field], ensure_ascii=False))
    return Settings(**data)
