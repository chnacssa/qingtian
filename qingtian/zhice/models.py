"""执策数据模型 — Pydantic v2"""
from datetime import datetime
from typing import Optional
from enum import Enum
from pydantic import BaseModel, Field, model_validator


class AppError(Exception):
    """业务异常"""
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


# ── 检查规则 ────────────────────────────────────────────

VALID_CHECK_TYPES = frozenset({
    "output_contains", "manual_review",
    "file_exists", "api_health", "db_query", "run_script",
    "reasonableness",
})

# P2 (R11): api_health 实为 agent-report 类型（agent 本地探测后上报 status_code，
# 见 agent_daemon._run_local_checks）。原误归入 ENGINE_AUTO 会让"未上报"被当引擎自动
# 判定通过 → fail-open（API 健康从未真正验证）。移出后未上报走 fail-closed。
ENGINE_AUTO_TYPES = frozenset({"output_contains", "manual_review"})
AGENT_REPORT_TYPES = frozenset({"file_exists", "api_health", "db_query", "run_script"})


class AcceptanceCriterion(BaseModel):
    type: str = Field(..., description="规则类型")
    # output_contains
    field: str = Field(default="")
    keyword: str = Field(default="")
    # file_exists
    path: str = Field(default="")
    required: bool = Field(default=True)
    # api_health
    url: str = Field(default="")
    expected_status: int = Field(default=200)
    # db_query
    sql: str = Field(default="")
    expected_min: int = Field(default=0)
    # run_script
    script: str = Field(default="")
    expected_exit_code: int = Field(default=0)
    # manual_review
    reviewer: str = Field(default="")
    # multisig (§3.4.3 Layer 4)
    require_multisig: bool = Field(default=False)
    multisig_count: int = Field(default=2, ge=2, le=5)  # 含执行者在内的总验证人数


# ── v2 质量标准 ──────────────────────────────────────────

VALID_QC_CATEGORIES = frozenset({"must", "should", "nice"})


class QualityCriterion(BaseModel):
    """可对照质量标准（给 Agent 自检用，非引擎检查）"""
    category: str = Field(default="must", description="must/should/nice")
    rule_type: str = Field(..., description="规则类型，同 acceptance_criteria 的 type")
    description: str = Field(default="", max_length=500, description="自然人可读的描述")
    # output_contains
    field: str = Field(default="")
    keyword: str = Field(default="")
    # file_exists
    path: str = Field(default="")
    exists: bool = Field(default=True)
    # api_health
    url: str = Field(default="")
    expected_status: int = Field(default=200)
    # db_query
    sql: str = Field(default="")
    expected_min: int = Field(default=0)
    # run_script
    script: str = Field(default="")
    expected_exit_code: int = Field(default=0)
    # manual_review
    reviewer: str = Field(default="")

    @model_validator(mode="after")
    def _validate_category(self):
        if self.category not in VALID_QC_CATEGORIES:
            raise ValueError(f"category 必须是 must/should/nice，实际为 {self.category}")
        return self

    @model_validator(mode="after")
    def _validate_rule_type(self):
        if self.rule_type not in VALID_CHECK_TYPES:
            raise ValueError(f"rule_type 必须是 {sorted(VALID_CHECK_TYPES)}，实际为 {self.rule_type}")
        return self


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# ── Step 定义（创建时）────────────────────────────────────

class StepDef(BaseModel):
    step_index: int = Field(..., ge=1)
    title: str = Field(..., min_length=1, max_length=256)
    instruction: str = Field(..., min_length=1, max_length=5000)
    params: Optional[dict] = Field(default=None, description="步骤结构化参数（如投标文件内容），透传给 skill 执行")
    depends_on: Optional[list[int]] = Field(default=None)
    acceptance_criteria: Optional[list[AcceptanceCriterion]] = Field(default=None)
    timeout_minutes: Optional[int] = Field(default=None)
    auto_retry: int = Field(default=0, ge=0, le=10)
    assigned_agent: Optional[str] = Field(default=None)
    # v2 字段（全部可选，向后兼容）
    quality_criteria: Optional[list[QualityCriterion]] = Field(default=None)
    max_iterations: int = Field(default=3, ge=1, le=10)
    risk_level: RiskLevel = Field(default=RiskLevel.LOW)
    confirmation_required: bool = Field(default=False)


# ── 请求模型 ─────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(default="", max_length=5000)
    priority: str = Field(default="P2", pattern=r"^P[0-4]$")
    steps: list[StepDef] = Field(default_factory=list, max_length=100)
    acceptance_criteria: Optional[list[AcceptanceCriterion]] = Field(default=None)
    expected_outputs: Optional[list[str]] = Field(default=None)
    timeout_minutes: Optional[int] = Field(default=None, ge=1)
    workflow_id: Optional[int] = Field(default=None)
    workflow_version: Optional[int] = Field(default=None)
    source_workflow_name: Optional[str] = Field(default=None, max_length=256, description="按名称引用 Workflow 模板")
    created_by: str = Field(..., min_length=1, max_length=256)
    skip_clarity: bool = Field(default=False, description="秘书 probe 已高置信度匹配时跳过模糊检测")


class StartStepRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=256)


class HeartbeatRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=256)
    progress: str = Field(default="")
    status: str = Field(default="")
    status_reason: str = Field(default="executing", pattern=r"^(executing|waiting_input|blocked)$")
    outputs: dict = Field(default_factory=dict)


class SubmitRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=256)
    status: str = Field(default="completed", pattern=r"^(completed|failed)$")
    summary: str = Field(default="", max_length=5000)
    outputs: dict = Field(default_factory=dict)
    idempotency_key: str = Field(..., min_length=1, max_length=64)
    signature: str = Field(default="", max_length=256)  # Ed25519 签名（128 hex chars，§3.4.3）
    # v2 字段（全部可选）
    iteration_log: Optional[list[dict]] = Field(default=None)


class IssueRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=256)
    issue_type: str = Field(..., pattern=r"^(blocked_by_dependency|need_clarification|resource_insufficient)$")
    description: str = Field(..., min_length=1, max_length=5000)
    severity: str = Field(default="blocking", pattern=r"^(blocking|warning)$")


class AssignStepRequest(BaseModel):
    step_index: int = Field(..., ge=1)
    assigned_agent: str = Field(..., min_length=1, max_length=256)
    requested_by: str = Field(..., min_length=1, max_length=256)


class PauseTaskRequest(BaseModel):
    reason: str = Field(default="")


class ReviewRequest(BaseModel):
    verification_id: int = Field(..., ge=1)
    decision: str = Field(..., pattern=r"^(approved|rejected)$")
    notes: str = Field(default="")


class CreateWorkflowRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    description: str = Field(default="")
    definition: dict = Field(...)
    created_by: str = Field(..., min_length=1, max_length=256)

    @staticmethod
    def validate_definition(definition: dict) -> str | None:
        """校验 definition 结构，返回错误信息或 None"""
        steps = definition.get("steps")
        if not isinstance(steps, list) or len(steps) == 0:
            return "definition.steps 必须是非空数组"
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                return f"definition.steps[{i}] 必须是对象"
            if "step_index" not in s:
                return f"definition.steps[{i}] 缺少 step_index"
            if "title" not in s:
                return f"definition.steps[{i}] 缺少 title"
            if "instruction" not in s:
                return f"definition.steps[{i}] 缺少 instruction"
        return None


class UpdateWorkflowRequest(BaseModel):
    name: str = Field(default="")
    description: str = Field(default="")
    definition: dict = Field(default=None)


class RejectStepRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)
    reset_retry: int = Field(default=1, ge=1, le=10)
    # v2 精准反馈（全部可选）
    failed_rules: Optional[list[dict]] = Field(default=None)
    retry_hint: str = Field(default="", max_length=2000)


# ── 行为规范（v1.10）──────────────────────────────────────

class PolicyRule(BaseModel):
    """行为规范规则定义"""
    allow: list[str] = Field(default_factory=list, description="scope 白名单")
    deny: list[str] = Field(default_factory=list, description="scope 黑名单")
    keywords: list[str] = Field(default_factory=list, description="keyword 关键词")
    patterns: list[str] = Field(default_factory=list, description="pattern 正则列表")


class CreatePolicyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    agent_id: str = Field(default="", max_length=256)
    category: str = Field(default="", max_length=256)
    policy_type: str = Field(..., pattern=r'^(scope|keyword|pattern)$')
    rule: PolicyRule
    action: str = Field(default="block", pattern=r'^(block|warn|log_only)$')
    reject_message: str = Field(default="", max_length=1000)
    priority: int = Field(default=0, ge=0, le=100)
    created_by: str = Field(..., min_length=1, max_length=256)


class UpdatePolicyRequest(BaseModel):
    name: str = Field(default="")
    agent_id: str = Field(default="")
    category: str = Field(default="")
    policy_type: str = Field(default="", pattern=r'^(scope|keyword|pattern)?$')
    action: str = Field(default="", pattern=r'^(block|warn|log_only)?$')
    reject_message: str = Field(default="")
    priority: int = Field(default=-1)
    enabled: bool | None = None


class PolicyCheckRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=256)
    title: str = Field(default="")
    description: str = Field(default="")
    steps: list[dict] = Field(default_factory=list)


class ConfirmStepRequest(BaseModel):
    """确认高风险 Step（v2 Phase 3）"""
    confirmed_by: str = Field(..., min_length=1, max_length=256)
    notes: str = Field(default="", max_length=1000)


# ── 响应模型 ─────────────────────────────────────────────

class StepResponse(BaseModel):
    step_id: int
    step_index: int
    title: str
    instruction: str
    params: Optional[dict] = None
    exec_type: str = "shell"
    status: str
    status_reason: Optional[str] = None
    assigned_agent: Optional[str] = None
    depends_on: Optional[list[int]] = None
    acceptance_criteria: Optional[list[dict]] = None
    auto_retry: int
    retries_left: int
    timeout_minutes: Optional[int] = None
    summary: Optional[str] = None
    outputs: Optional[dict] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    # v2 字段（全部可选，向后兼容）
    quality_criteria: Optional[list[dict]] = None
    max_iterations: int = 3
    risk_level: str = "low"
    confirmation_required: bool = False
    iteration_log: Optional[list[dict]] = None


class TaskResponse(BaseModel):
    task_id: int
    title: str
    description: str
    priority: str
    status: str
    created_by: str
    participants: list[str]
    progress: int
    total_steps: int
    completed_steps: int
    failed_steps: int
    timeout_minutes: Optional[int] = None
    result: Optional[str] = None
    steps: list[StepResponse] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class NextStepResponse(BaseModel):
    task_id: int
    task_status: str
    current_step: Optional[StepResponse] = None
    progress: str = ""
    upcoming_steps: list[dict] = Field(default_factory=list)
    context: Optional[dict] = None  # v2 Phase 1：已完成 Steps 摘要 + 任务全局信息


class SubmitResponse(BaseModel):
    step_id: int
    status: str
    verification_result: str
    retries_left: int
    failed_rules: list[dict] = Field(default_factory=list)
    retry_hint: str = Field(default="")  # v2 精准反馈
    qc_warnings: list[dict] = Field(default_factory=list)  # v2 Phase A: quality_criteria 引擎重检告警


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]
    total: int
    limit: int
    offset: int


# ── Phase C: 质量可观测性 ──────────────────────────────────

class QualityStatsResponse(BaseModel):
    """单 Task 质量统计"""
    task_id: int
    title: str
    has_quality_criteria: bool = False
    iteration_count: int = 0
    max_iterations: int = 0
    engine_recheck_fails: int = 0
    self_check_passed: bool | None = None
    failure_patterns: list[dict] | None = None
    step_iterations: list[dict] = Field(default_factory=list)
    created_at: datetime | None = None


class QualityTrendItem(BaseModel):
    task_id: int
    title: str
    has_quality_criteria: bool = False
    iteration_count: int = 0
    engine_recheck_fails: int = 0
    completed_at: datetime | None = None


class QualityTrendsResponse(BaseModel):
    trends: list[QualityTrendItem]
    summary: dict = Field(default_factory=dict)
