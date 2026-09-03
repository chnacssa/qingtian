"""镇岳 — Pydantic v2 模型。"""

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field, field_validator


class RegisterAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    category: str = Field(default="")
    contact: str = Field(default="")
    description: str = Field(default="", max_length=500)


class AgentResponse(BaseModel):
    agent_id: str
    name: str
    category: str
    trust_level: str
    status: str
    registered_at: datetime


class ReviewAgentRequest(BaseModel):
    agent_id: str
    decision: str


class SendMessageRequest(BaseModel):
    to_agent_id: str = Field(..., min_length=1)
    payload: dict = Field(default_factory=dict)
    message_id: str = Field(..., min_length=1)
    timestamp: int = Field(..., gt=0)
    signature: str = Field(..., min_length=1)


class MessageResponse(BaseModel):
    message_id: str
    status: str


class AuditEntryRequest(BaseModel):
    agent_id: str
    agent_role: str = "agent"
    action: str
    target_type: str = ""
    target_id: str = ""
    severity: str = "low"
    detail: dict | None = None
    approval_status: str = "auto"
    approval_chain: list | None = None

    @field_validator("severity")
    @classmethod
    def check_severity(cls, v):
        if v not in ("low", "medium", "high", "critical"):
            raise ValueError("severity must be low/medium/high/critical")
        return v


class AuditEntryResponse(BaseModel):
    audit_uid: str
    created_at: datetime
    agent_id: str
    action: str
    severity: str
    hash: str


class AuditVerifyResponse(BaseModel):
    status: str
    total_records: int
    first_audit_uid: str | None = None
    last_audit_uid: str | None = None
    error: str | None = None


class ApprovalRequest(BaseModel):
    agent_id: str
    action: str
    target_type: str = ""
    target_id: str = ""
    severity: str = "high"


class CreateApprovalRequest(BaseModel):
    """应用层业务审批创建请求（Agent 主动触发，不走中间件）。"""
    agent_id: str
    action: str
    target_type: str = ""
    target_id: str = ""
    severity: str = "high"
    pending_request: dict | None = None


class ApprovalResponse(BaseModel):
    request_id: str
    status: str
    approver: str = ""


class ApprovalCallback(BaseModel):
    request_id: str
    decision: str
    approver: str
    comment: str = ""


class CreateTokenRequest(BaseModel):
    agent_id: str
    role: str = "agent"


class CreateTokenResponse(BaseModel):
    token: str
    agent_id: str
    role: str
    issued_at: datetime


class ValidateTokenRequest(BaseModel):
    token: str


class ValidateTokenResponse(BaseModel):
    valid: bool
    agent_id: str = ""
    role: str = ""


class RevokeTokenRequest(BaseModel):
    token: str


class BreakGlassRequest(BaseModel):
    token: str
    action: str
    target: str

    @field_validator("action")
    @classmethod
    def check_action(cls, v):
        if v not in ("stop_agent", "isolate_agent", "block_ip"):
            raise ValueError("action must be stop_agent/isolate_agent/block_ip")
        return v


# ── 审批门控 ───────────────────────────────────────

class PendingRequest(BaseModel):
    """审批挂起时保存的原始请求数据"""
    method: str
    path: str
    headers: dict = Field(default_factory=dict)
    body: Any = None
    query_params: dict = Field(default_factory=dict)


class ApprovalPollResponse(BaseModel):
    """审批状态轮询响应"""
    request_id: str
    status: str  # pending / approved / rejected / executed / expired
    result: Any = None
    reason: str = ""


class ApprovalExecuteResponse(BaseModel):
    """审批通过后内部 re-issue 的执行结果"""
    request_id: str
    status: str  # executed / failed
    result: Any = None
    error: str = ""


# ── 工具规则（第一层 Plugin） ────────────────────────

class ToolRuleRequest(BaseModel):
    """动态工具规则创建/更新"""
    tool: str = Field(..., min_length=1)
    match: str = ""  # 参数匹配模式（正则字符串）
    severity: str = "log_only"  # block / require_approval / log_only
    approval_severity: str = "high"  # require_approval 时的审批严重度
    reason: str = ""

    @field_validator("severity")
    @classmethod
    def check_severity(cls, v):
        if v not in ("block", "require_approval", "log_only"):
            raise ValueError("severity must be block/require_approval/log_only")
        return v


class ToolRuleResponse(BaseModel):
    """工具规则响应"""
    id: str
    tool: str
    match: str
    severity: str
    approval_severity: str
    reason: str
    enabled: bool = True


# ── 通用 ───────────────────────────────────────────

class NotifyApprovalRequest(BaseModel):
    """审批推送通知请求 — guard 插件 block 时调用。"""
    open_id: str
    message: str
    agent_id: str = ""
    action: str = ""


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
    request_id: str = ""


# ── Agent 密钥对 ──────────────────────────────────────

class GenerateKeypairResponse(BaseModel):
    key_id: int
    agent_id: str
    public_key: str
    algorithm: str = "ed25519"
    status: str = "active"
    created_at: str = ""


class PublicKeyResponse(BaseModel):
    key_id: int
    agent_id: str
    public_key: str
    algorithm: str = "ed25519"
    created_at: str = ""


class PrivateKeyResponse(BaseModel):
    key_id: int
    agent_id: str
    private_key: str
    algorithm: str = "ed25519"


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
