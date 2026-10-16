"""单行动取证与有限纠错的严格、版本化模型协议。"""
from pathlib import Path
from typing import Annotated, Literal, ClassVar

from pydantic import ConfigDict, Field, model_validator
from backend.agent_contract import Strict

VERSION = 'agent-loop-v1'


class Contract(Strict):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    compact_json: ClassVar[bool] = True

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        # title 只是展示元数据；保留类型、枚举、长度、判别联合等全部校验约束。
        def compact(value):
            if isinstance(value, dict):
                return {k:compact(v) for k,v in value.items() if k != 'title'}
            if isinstance(value, list):
                return [compact(v) for v in value]
            return value
        return compact(super().model_json_schema(*args, **kwargs))


class LoopConfig(Contract):
    id: str = 'loop-v1-repair'
    version: Literal['1.1.0', '1.2.0', '1.3.0', '1.4.0', '1.5.0', '1.6.0', '1.7.0', '1.8.0', '1.9.0', '1.10.0'] = '1.1.0'
    runtime_version: Literal['agent-loop-v1'] = VERSION
    contract_version: Literal['loop-repair-v1', 'loop-bound-v2', 'loop-contract-recovery-v3', 'loop-contract-conditions-v4', 'loop-call-bound-v5', 'loop-scoped-view-v6', 'loop-gap-full-v7', 'loop-gap-view-v8', 'loop-evidence-v9', 'loop-obligations-v10'] = 'loop-repair-v1'
    prompt_version: Literal['loop-repair-v1', 'loop-bound-v2', 'loop-contract-recovery-v3', 'loop-contract-conditions-v4', 'loop-call-bound-v5', 'loop-scoped-view-v6', 'loop-gap-full-v7', 'loop-gap-view-v8', 'loop-evidence-v9', 'loop-obligations-v10'] = 'loop-repair-v1'
    tool_version: Literal['readonly-v1'] = 'readonly-v1'
    context_version: Literal['loop-view-v1', 'loop-view-v2', 'loop-view-v3', 'loop-view-v4', 'loop-view-v5', 'loop-view-v6', 'loop-view-v7', 'loop-view-v8', 'loop-view-v9', 'loop-view-v10'] = 'loop-view-v1'
    review_policy: Literal['required-post-v1', 'required-post-recovery-v3', 'required-post-conditions-v4', 'required-post-call-v5', 'required-post-scoped-v6', 'required-post-gap-v7', 'required-post-gap-view-v8', 'required-post-evidence-v9', 'required-post-obligations-v10'] = 'required-post-v1'
    decision_tokens: int = Field(1500, ge=100, le=1500)
    max_tool_actions: int = Field(6, ge=1, le=6)
    ablation: Literal['E2', 'E3', 'E4', 'E5'] | None = None
    execution_policy: Literal['strict', 'advisory'] = 'strict'

    @model_validator(mode='after')
    def version_identity(self):
        if self.execution_policy == 'advisory' and (self.version != '1.5.0' or self.ablation is not None):
            raise ValueError('advisory_requires_call_bound_v5_without_ablation')
        if self.ablation is not None and self.version != '1.7.0':
            raise ValueError('loop_ablation_requires_v7')
        expected = ('loop-repair-v1', 'loop-repair-v1', 'loop-view-v1') if self.version == '1.1.0' else ('loop-bound-v2', 'loop-bound-v2', 'loop-view-v2')
        if self.version == '1.3.0':
            expected = ('loop-contract-recovery-v3', 'loop-contract-recovery-v3', 'loop-view-v3')
        if self.version == '1.4.0':
            expected = ('loop-contract-conditions-v4', 'loop-contract-conditions-v4', 'loop-view-v4')
        if self.version == '1.5.0':
            expected = ('loop-call-bound-v5', 'loop-call-bound-v5', 'loop-view-v5')
        if self.version == '1.6.0':
            expected = ('loop-scoped-view-v6', 'loop-scoped-view-v6', 'loop-view-v6')
        if self.version == '1.7.0':
            expected = ('loop-gap-full-v7', 'loop-gap-full-v7', 'loop-view-v7')
        if self.version == '1.8.0':
            expected = ('loop-gap-view-v8', 'loop-gap-view-v8', 'loop-view-v8')
        if self.version == '1.9.0':
            expected = ('loop-evidence-v9', 'loop-evidence-v9', 'loop-view-v9')
        if self.version == '1.10.0':
            expected = ('loop-obligations-v10', 'loop-obligations-v10', 'loop-view-v10')
        policy = 'required-post-obligations-v10' if self.version == '1.10.0' else 'required-post-evidence-v9' if self.version == '1.9.0' else 'required-post-gap-view-v8' if self.version == '1.8.0' else 'required-post-gap-v7' if self.version == '1.7.0' else 'required-post-scoped-v6' if self.version == '1.6.0' else 'required-post-call-v5' if self.version == '1.5.0' else 'required-post-recovery-v3' if self.version == '1.3.0' else 'required-post-conditions-v4' if self.version == '1.4.0' else 'required-post-v1'
        if (self.contract_version, self.prompt_version, self.context_version) != expected or self.review_policy != policy:
            raise ValueError('loop_version_identity_mismatch')
        return self

    @classmethod
    def read(cls, path):
        return cls.model_validate_json(Path(path).read_text('utf-8-sig'))


class GoalProposal(Contract):
    local_id: str = Field(pattern=r'^new-[a-z0-9-]{1,24}$')
    text: str = Field(min_length=1, max_length=200)
    parent_id: str
    origin: Literal['derived_dependency']


class Assessment(Contract):
    goal_id: str
    status: Literal['open', 'supported', 'conflicted', 'unavailable']
    statement: str = Field(min_length=1, max_length=200)
    support_refs: list[str] = Field(max_length=8)
    conflict_refs: list[str] = Field(max_length=8)
    unknowns: list[str] = Field(max_length=4)

    @model_validator(mode='after')
    def referenced(self):
        if self.status in {'supported', 'unavailable'} and not self.support_refs:
            raise ValueError('判断必须有来源')
        if self.status == 'conflicted' and not self.conflict_refs:
            raise ValueError('冲突必须有来源')
        return self


class OrderArgs(Contract):
    order_id_ref: Literal['fact:order_id']


class SearchArgs(Contract):
    query: str = Field(min_length=1, max_length=8000)
    gap_ref: str | None = None


class ContextArgs(Contract):
    chunk_refs: list[str] = Field(min_length=1, max_length=20)


class ToolBase(Contract):
    kind: Literal['tool']
    targets: list[str] = Field(min_length=1, max_length=8)
    expected_information: str = Field(min_length=1, max_length=200)
    evidence_basis: list[str] = Field(max_length=8)


class OrderTool(ToolBase):
    tool: Literal['get_order', 'get_after_sales', 'get_logistics', 'calculate_refund']
    arguments: OrderArgs


class SearchTool(ToolBase):
    tool: Literal['search_knowledge']
    arguments: SearchArgs


class ContextTool(ToolBase):
    tool: Literal['read_policy_context']
    arguments: OrderArgs | ContextArgs


ToolAction = Annotated[OrderTool | SearchTool | ContextTool, Field(discriminator='tool')]


class FindingResponse(Contract):
    finding_id: str
    explanation: str = Field(min_length=1, max_length=200)
    support_refs: list[str] = Field(min_length=1, max_length=8)
    block_id: str | None = None


class DraftAction(Contract):
    kind: Literal['draft']
    coverage: Literal['full', 'partial']
    addressed_goals: list[str] = Field(min_length=1, max_length=8)
    support_refs: list[str] = Field(min_length=1, max_length=20)
    limitations: list[str] = Field(max_length=4)
    finding_responses: list[FindingResponse] = Field(default_factory=list, max_length=6)
    reuse_candidate_id: str | None = None


class ClarifyAction(Contract):
    kind: Literal['clarify']
    goal_ids: list[str] = Field(min_length=1, max_length=8)
    fields: list[str] = Field(min_length=1, max_length=4)
    reason: str = Field(min_length=1, max_length=200)


class FinishAction(Contract):
    kind: Literal['finish']
    reason: Literal['unsupported', 'insufficient_evidence', 'inability']
    unresolved_goal_ids: list[str] = Field(max_length=8)


class Decision(Contract):
    based_on_state_version: int = Field(ge=0)
    goal_proposals: list[GoalProposal] = Field(max_length=4)
    assessment_updates: list[Assessment] = Field(max_length=4)
    action: Annotated[ToolAction | DraftAction | ClarifyAction | FinishAction, Field(discriminator='kind')]

    @model_validator(mode='after')
    def update_limit(self):
        if len(self.goal_proposals) + len(self.assessment_updates) > 4:
            raise ValueError('单次最多四项状态更新')
        return self


class OriginalSearchArgs(Contract):
    mode: Literal['original']


class TargetedSearchArgs(Contract):
    mode: Literal['targeted']
    query: str = Field(min_length=1, max_length=400)
    gap_ref: str = Field(min_length=1, max_length=80)


class BoundSearchTool(ToolBase):
    tool: Literal['search_knowledge']
    arguments: Annotated[OriginalSearchArgs | TargetedSearchArgs, Field(discriminator='mode')]


BoundToolAction = Annotated[OrderTool | BoundSearchTool | ContextTool, Field(discriminator='tool')]


class BoundDecision(Decision):
    action: Annotated[BoundToolAction | DraftAction | ClarifyAction | FinishAction, Field(discriminator='kind')]


def bound_contract(host):
    return 'bound' in capabilities(host)


def decision_schema(host):
    if advisory_contract(host):
        return AdvisoryDecision
    return ObligationDecision if obligation_contract(host) else FullDecision if full_contract(host) else BoundDecision if bound_contract(host) else Decision


class AdvisoryDraftAction(DraftAction):
    support_refs: list[str] = Field(default_factory=list, max_length=20)


class AdvisoryDecision(BoundDecision):
    action: Annotated[BoundToolAction | AdvisoryDraftAction | ClarifyAction | FinishAction, Field(discriminator='kind')]


def advisory_contract(host):
    config = getattr(getattr(host, 'settings', None), 'loop_config', None)
    return getattr(config, 'execution_policy', 'strict') == 'advisory'


class Block(Contract):
    block_id: str = Field(pattern=r'^b[1-8]$')
    text: str = Field(min_length=1, max_length=1500)
    goal_ids: list[str] = Field(min_length=1, max_length=8)
    support_refs: list[str] = Field(max_length=8)
    kind: Literal['claim', 'limitation']


class Draft(Contract):
    blocks: list[Block] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def unique(self):
        if len({b.block_id for b in self.blocks}) != len(self.blocks):
            raise ValueError('正文块标识必须唯一')
        if any(b.kind == 'claim' and not b.support_refs for b in self.blocks):
            raise ValueError('主张必须有支持引用')
        return self


class Finding(Contract):
    kind: Literal['unsupported_claim', 'contradiction', 'number', 'citation', 'missing_requirement', 'applicability']
    block_id: str | None
    goal_id: str | None
    evidence_refs: list[str] = Field(max_length=8)
    detail: str = Field(min_length=1, max_length=200)

    @model_validator(mode='after')
    def location(self):
        if self.kind == 'missing_requirement' and not self.goal_id:
            raise ValueError('遗漏要求必须定位目标')
        if self.kind != 'missing_requirement' and not self.block_id:
            raise ValueError('正文问题必须定位块')
        return self


class Disposition(Contract):
    finding_id: str
    status: Literal['remains', 'resolved', 'not_applicable']
    block_id: str | None
    evidence_refs: list[str] = Field(max_length=8)
    detail: str = Field(min_length=1, max_length=200)

    @model_validator(mode='after')
    def closure_location(self):
        if self.status != 'remains' and (not self.block_id or not self.evidence_refs):
            raise ValueError('关闭发现必须定位当前正文和证据')
        return self


class Review(Contract):
    candidate_id: str
    bundle_id: str
    complete: bool
    findings: list[Finding] = Field(max_length=6)
    dispositions: list[Disposition] = Field(default_factory=list, max_length=6)

    @model_validator(mode='after')
    def bounded(self):
        if len(self.findings)+len(self.dispositions)>6:
            raise ValueError('审核发现和处理判定合计最多六项')
        return self


REVIEW_DOMAINS = ('request_coverage', 'facts_and_unknowns', 'support_and_applicability', 'numbers_and_readonly', 'prior_findings')


def recovery_contract(host):
    return 'recovery' in capabilities(host)


class ReviewExecution(Contract):
    status: Literal['completed', 'incomplete']
    reason: Literal['not_finished', 'capacity', 'uncheckable'] | None
    checked_domains: list[Literal['request_coverage', 'facts_and_unknowns', 'support_and_applicability', 'numbers_and_readonly', 'prior_findings']] = Field(max_length=5)
    unchecked_blocks: list[str] = Field(max_length=8)
    unchecked_goals: list[str] = Field(max_length=8)
    unchecked_findings: list[str] = Field(max_length=6)

    @model_validator(mode='after')
    def coherent(self):
        if len(set(self.checked_domains)) != len(self.checked_domains):
            raise ValueError('检查域不得重复')
        missing = self.unchecked_blocks or self.unchecked_goals or self.unchecked_findings
        if self.status == 'completed':
            if self.reason is not None or missing or set(self.checked_domains) != set(REVIEW_DOMAINS):
                raise ValueError('完成检查须覆盖全部检查域且无未检查引用；发现问题不表示未完成')
        elif self.reason is None or not missing:
            raise ValueError('未完成须给原因和未检查对象，不能仅因发现错误标为未完成')
        return self


class RecoveryReview(Contract):
    candidate_id: str
    bundle_id: str
    execution: ReviewExecution
    findings: list[Finding] = Field(max_length=6)
    dispositions: list[Disposition] = Field(max_length=6)

    @model_validator(mode='after')
    def bounded(self):
        if len(self.findings)+len(self.dispositions)>6:
            raise ValueError('审核发现和处理判定合计最多六项')
        return self


def review_schema(host):
    if call_bound_contract(host):
        return CallBoundReview
    return RecoveryReview if recovery_contract(host) else Review


def conditions_contract(host):
    return 'conditions' in capabilities(host)


class ConditionCoverage(Contract):
    used_fact_refs: list[str] = Field(max_length=30)
    unknown_fact_refs: list[str] = Field(max_length=30)
    derived_condition_refs: list[str] = Field(max_length=2)
    fact_scope: Literal['user_conditions']
    overall_eligibility: Literal['not_computed']


class ConditionsDraft(Draft):
    condition_coverage: ConditionCoverage


def draft_schema(host):
    return ConditionsDraft if conditions_contract(host) else Draft


class CallBoundReview(Contract):
    execution: ReviewExecution
    findings: list[Finding] = Field(max_length=6)
    dispositions: list[Disposition] = Field(max_length=6)

    @model_validator(mode='after')
    def bounded(self):
        if len(self.findings)+len(self.dispositions)>6:
            raise ValueError('审核发现和处理判定合计最多六项')
        return self


def call_bound_contract(host):
    return 'call_bound' in capabilities(host)


def scoped_contract(host):
    return 'scoped' in capabilities(host)


class FullDraftAction(DraftAction):
    coverage: Literal['full']


class FullDecision(BoundDecision):
    action: Annotated[BoundToolAction | FullDraftAction | ClarifyAction | FinishAction, Field(discriminator='kind')]


def full_contract(host):
    return 'full' in capabilities(host)


def gap_view_contract(host):
    return 'gap_view' in capabilities(host)

# Explicit capability table preserves all legacy profiles, including v7 without scoped view.
CAPABILITIES = {
    'loop-bound-v2': frozenset({'bound'}),
    'loop-contract-recovery-v3': frozenset({'bound', 'recovery'}),
    'loop-contract-conditions-v4': frozenset({'bound', 'recovery', 'conditions'}),
    'loop-call-bound-v5': frozenset({'bound', 'recovery', 'conditions', 'call_bound'}),
    'loop-scoped-view-v6': frozenset({'bound', 'recovery', 'conditions', 'call_bound', 'scoped'}),
    'loop-gap-full-v7': frozenset({'bound', 'recovery', 'conditions', 'call_bound', 'full'}),
    'loop-gap-view-v8': frozenset({'bound', 'recovery', 'conditions', 'call_bound', 'scoped', 'full', 'gap_view'}),
    'loop-evidence-v9': frozenset({'bound', 'recovery', 'conditions', 'call_bound', 'scoped', 'full', 'gap_view', 'evidence'}),
}


def capabilities(host):
    if advisory_contract(host):
        return frozenset({'bound', 'recovery', 'call_bound'})
    config = getattr(getattr(host, "settings", None), "loop_config", None)
    return CAPABILITIES.get(getattr(config, "contract_version", None), frozenset())

def evidence_contract(host):
    return "evidence" in capabilities(host)


class EvidenceNeedUpdate(Contract):
    source: str = Field(min_length=1, max_length=100)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)
    purpose: Literal['direct', 'dependency']


class ObligationDecision(FullDecision):
    evidence_need_updates: list[EvidenceNeedUpdate] = Field(default_factory=list, max_length=6)


CAPABILITIES['loop-obligations-v10'] = CAPABILITIES['loop-evidence-v9'] | {'obligations'}


def obligation_contract(host):
    return 'obligations' in capabilities(host)
