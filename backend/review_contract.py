"""可选检查配置与问题级审核契约；不改变第六阶段角色契约。

输出规模与审核预算对齐：最多6条问题、短说明，避免模型在1200输出token内被截断。
检查执行状态(complete/incomplete/protocol_error/timeout)与语义结论(pass/issues_found)分离，
只有执行完成且语义通过、且程序检查无问题才允许发布。
"""
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator
from backend.agent_contract import Strict, Facts

CLARIFIABLE_FIELDS = sorted(set(Facts.model_fields) - {'question', 'index_id'})
REVIEW_TOOLS = ('search_knowledge', 'read_policy_context', 'get_order', 'get_after_sales', 'get_logistics', 'calculate_refund')
PRE_ACTIONS = ('lookup', 'clarify', 'handoff')
POST_ACTIONS = ('rewrite', 'lookup', 'clarify', 'handoff')
# 与1200输出预算匹配的单轮问题上限；超出部分由程序标记检查不完整。
MAX_REVIEW_ISSUES = 6


class ReviewConfig(Strict):
    id: str = 'review-off'
    version: str = '1.0.0'
    precheck: bool = False
    postcheck: bool = False
    correction: Literal['none', 'answer_only', 'targeted_lookup'] = 'none'
    max_rounds: int = Field(2, ge=0, le=2)
    review_tokens: int = Field(1200, ge=100, le=1500)
    repair_tokens: int = Field(1200, ge=100, le=1500)
    timeout_seconds: float = Field(30, gt=0, le=30)
    prompt_version: Literal['evidence-review-v1'] = 'evidence-review-v1'

    @model_validator(mode='after')
    def dependencies(self):
        if self.correction != 'none' and not (self.precheck or self.postcheck):
            raise ValueError('纠错需要至少开启一项检查')
        if self.correction == 'answer_only' and not self.postcheck:
            raise ValueError('仅修改答案需要后置审核')
        if self.correction != 'none' and not self.max_rounds:
            raise ValueError('纠错需要正轮次上限')
        return self

    @classmethod
    def read(cls, path):
        return cls.model_validate_json(Path(path).read_text('utf-8-sig'))


class ReviewIssue(Strict):
    code: str = Field(min_length=1, max_length=64)
    dimension: Literal['coverage', 'fact', 'policy', 'number', 'citation']
    conclusion: str = Field(min_length=1, max_length=240,
        description='问题所在的结论或缺口，复用输入措辞，不要复制完整政策或业务JSON。')
    evidence_ids: list[str] = Field(default_factory=list, max_length=4,
        description='只引用输入给出的证据标识，最多4个。')
    detail: str = Field(min_length=1, max_length=400)
    action: Literal['rewrite', 'lookup', 'clarify', 'handoff']
    suggestion: str = Field(min_length=1, max_length=200)
    suggestion_source: Literal['model_v1', 'model_change', 'issue_detail'] = 'model_v1'
    missing_fields: list[str] = Field(default_factory=list, max_length=4,
        json_schema_extra={'items': {'type': 'string', 'enum': CLARIFIABLE_FIELDS}})
    tool: Literal['search_knowledge', 'read_policy_context', 'get_order', 'get_after_sales', 'get_logistics', 'calculate_refund'] | None = None
    query: str | None = Field(None, min_length=1, max_length=200,
        description='知识补查的检索查询；必须是可直接检索的短查询，不要粘贴大段原文。')

    @model_validator(mode='after')
    def actionable(self):
        allowed = set(CLARIFIABLE_FIELDS)
        if not set(self.missing_fields) <= allowed:
            raise ValueError('澄清字段不属于用户事实契约')
        if self.action == 'clarify' and not self.missing_fields:
            raise ValueError('澄清必须指出缺失字段')
        if self.action == 'lookup' and not self.tool:
            raise ValueError('补查必须指明现有工具')
        if self.action == 'lookup' and self.tool == 'search_knowledge' and not self.query:
            raise ValueError('知识补查必须指明查询')
        return self


class ReviewReport(Strict):
    """v1历史/内部报告契约；当前模型调用使用下方按阶段的v2契约。"""
    passed: bool
    complete: bool = Field(description='是否已完成全部检查且未省略问题；未完成不得通过。')
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=6)

    @model_validator(mode='after')
    def coherent(self):
        if self.passed != (self.complete and len(self.issues) == 0):
            raise ValueError('通过必须无问题；未通过必须有具体问题')
        return self


class ReviewResult(Strict):
    """程序生成的检查结果：执行状态与语义结论分离，供发布门禁使用。"""
    stage: Literal['pre', 'post']
    execution: Literal['complete', 'incomplete', 'protocol_error', 'timeout']
    verdict: Literal['pass', 'issues_found']
    issues: list[ReviewIssue] = Field(default_factory=list)
    candidate_withheld: bool = True
    protocol_repairs: int = Field(0, ge=0, le=1)

    @model_validator(mode='after')
    def gating(self):
        if self.verdict == 'pass' and (self.issues or self.execution != 'complete'):
            raise ValueError('只有执行完成且无问题才能通过')
        if self.execution != 'complete' and not self.candidate_withheld:
            raise ValueError('检查未完成必须扣留候选')
        return self

    @property
    def released(self):
        return self.execution == 'complete' and self.verdict == 'pass' and not self.candidate_withheld


class RepairedAnswer(Strict):
    answer: str = Field(min_length=1, max_length=50000)


class CheckIssue(Strict):
    code: str = Field(min_length=1, max_length=64)
    dimension: Literal['coverage', 'fact', 'policy', 'number', 'citation']
    conclusion: str = Field(min_length=1, max_length=120, description='定位缺口或待修正的结论。')
    detail: str = Field(min_length=1, max_length=200, description='一句话说明原因，不重复政策全文。')
    evidence_ids: list[str] = Field(default_factory=list, max_length=4)


class LookupIssue(CheckIssue):
    action: Literal['lookup']
    tool: Literal['search_knowledge', 'read_policy_context', 'get_order', 'get_after_sales', 'get_logistics', 'calculate_refund']
    query: str | None = Field(None, min_length=1, max_length=160)

    @model_validator(mode='after')
    def query_required(self):
        if self.tool == 'search_knowledge' and not self.query:
            raise ValueError('知识补查必须给出短查询')
        return self


class ClarifyIssue(CheckIssue):
    action: Literal['clarify']
    missing_fields: list[str] = Field(min_length=1, max_length=4,
        json_schema_extra={'items': {'type': 'string', 'enum': CLARIFIABLE_FIELDS}})

    @model_validator(mode='after')
    def fields_allowed(self):
        if not set(self.missing_fields) <= set(CLARIFIABLE_FIELDS):
            raise ValueError('只能澄清用户事实字段')
        return self


class RewriteIssue(CheckIssue):
    action: Literal['rewrite']
    change: str = Field(min_length=1, max_length=160, description='需要如何修正该结论。')


class HandoffIssue(CheckIssue):
    action: Literal['handoff']


PreIssue = Annotated[LookupIssue | ClarifyIssue | HandoffIssue, Field(discriminator='action')]
PostIssue = Annotated[LookupIssue | ClarifyIssue | RewriteIssue | HandoffIssue, Field(discriminator='action')]


class CheckReport(Strict):
    passed: bool
    complete: bool = Field(description='所有检查是否完成且未省略问题；发现缺证据本身不表示检查未完成。')

    @model_validator(mode='after')
    def coherent(self):
        if self.passed != (self.complete and not self.issues):
            raise ValueError('通过必须检查完成且没有问题')
        return self


class PreReviewReport(CheckReport):
    issues: list[PreIssue] = Field(default_factory=list, max_length=MAX_REVIEW_ISSUES)


class PostReviewReport(CheckReport):
    issues: list[PostIssue] = Field(default_factory=list, max_length=MAX_REVIEW_ISSUES)


class PolicyLookupIssue(LookupIssue):
    tool: Literal['search_knowledge', 'read_policy_context']


POLICY_FIELDS = [f for f in CLARIFIABLE_FIELDS if f not in {'order_id','after_sale_id','selections','gift_returned_line_ids'}]


class PolicyClarifyIssue(ClarifyIssue):
    missing_fields: list[str] = Field(min_length=1,max_length=4,
        json_schema_extra={'items':{'type':'string','enum':POLICY_FIELDS}})

    @model_validator(mode='after')
    def no_transaction_ids(self):
        if not set(self.missing_fields)<=set(POLICY_FIELDS):
            raise ValueError('条件性政策解释不能要求订单或售后ID')
        return self


PolicyPreIssue = Annotated[PolicyLookupIssue | PolicyClarifyIssue | HandoffIssue, Field(discriminator='action')]
PolicyPostIssue = Annotated[PolicyLookupIssue | PolicyClarifyIssue | RewriteIssue | HandoffIssue, Field(discriminator='action')]


class PolicyPreReviewReport(CheckReport):
    issues: list[PolicyPreIssue] = Field(default_factory=list,max_length=MAX_REVIEW_ISSUES)


class PolicyPostReviewReport(CheckReport):
    issues: list[PolicyPostIssue] = Field(default_factory=list,max_length=MAX_REVIEW_ISSUES)


def review_schema(stage, conditional_policy=False):
    if conditional_policy:
        return PolicyPreReviewReport if stage=='pre' else PolicyPostReviewReport
    return PreReviewReport if stage=='pre' else PostReviewReport


def model_report(report):
    """只用于程序问题和显式测试桩：生成v2模型桩；真实响应由Schema严格解析。"""
    issues = []
    for item in report['issues']:
        value = {k: item[k] for k in ('code', 'dimension', 'conclusion', 'detail', 'evidence_ids', 'action')}
        value['conclusion'], value['detail'] = value['conclusion'][:120], value['detail'][:200]
        if item['action'] == 'lookup':
            value.update(tool=item.get('tool'), query=(item.get('query') or None))
            if value['query']:
                value['query'] = value['query'][:160]
        elif item['action'] == 'clarify':
            value['missing_fields'] = item.get('missing_fields', [])
        elif item['action'] == 'rewrite':
            value['change'] = (item.get('change') or item.get('suggestion') or item['detail'])[:160]
        issues.append(value)
    return dict(passed=report['passed'], complete=report['complete'], issues=issues)


def internal_issue(item):
    """已验证的v2结果映射到内部展示格式；建议来源标记，不能冒充原始模型字段。"""
    return dict({k:v for k,v in item.items() if k != 'change'}, missing_fields=item.get('missing_fields', []), tool=item.get('tool'),
                query=item.get('query'), suggestion=item.get('suggestion') or item.get('change') or item['detail'],
                suggestion_source='model_change' if item.get('change') else 'issue_detail')
