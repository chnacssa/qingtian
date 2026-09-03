"""
永恒 — Pydantic v2 数据模型
"""

import json
from datetime import datetime
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

NAMESPACE_PATTERN = r'^[a-z-]+:[a-z0-9-]+$'
MEMORY_TYPES = ("episodic", "high_value", "consolidated")
SOURCE_VALUES = ("openclaw", "feishu", "script", "manual", "hermes", "qclaw", "arkclaw")
TOKEN_LEVELS = ("namespace", "master", "admin")


# ── 记忆 ──────────────────────────────────────────────

class WriteMemoryRequest(BaseModel):
    namespace: str = Field(..., max_length=64, pattern=NAMESPACE_PATTERN)
    content: str = Field(..., min_length=1, max_length=10000)
    type: str = Field(default="episodic", pattern=r'^(episodic|high_value|consolidated)$')
    source: str = Field(default="openclaw", max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def check_metadata_size(cls, v):
        if len(json.dumps(v, ensure_ascii=False)) > 2048:
            raise ValueError("metadata exceeds 2KB")
        return v


class WriteMemoryResponse(BaseModel):
    id: int
    status: str
    high_value: bool
    timestamp: datetime


class BatchWriteRequest(BaseModel):
    namespace: str = Field(..., max_length=64, pattern=NAMESPACE_PATTERN)
    memories: list[WriteMemoryRequest] = Field(..., min_length=1, max_length=20)


class BatchWriteItem(BaseModel):
    index: int
    id: Optional[int] = None
    status: Optional[str] = None
    high_value: bool = False
    error: Optional[str] = None


class BatchWriteResponse(BaseModel):
    results: list[BatchWriteItem]
    total: int
    stored: int
    failed: int


class SearchRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    query: str = Field(..., min_length=1)
    method: str = Field(default="hybrid", pattern=r'^(keyword|hybrid|agentic)$')
    top_k: int = Field(default=5, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    budget_tokens: int = Field(default=2000, ge=100, le=32000)
    filter: Optional[dict[str, Any]] = None


class SearchResultItem(BaseModel):
    id: int
    content: str
    type: str = "episodic"
    score: float
    time_decay_weight: float = 1.0
    protected: bool = False
    timestamp: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    total_matched: int
    method: str
    total_tokens: int


class ContextRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    context: str = Field(..., min_length=1)
    top_k: int = Field(default=10, ge=1, le=100)
    method: str = Field(default="contextual")


class ContextResultItem(BaseModel):
    id: int
    content: str
    type: str = "episodic"
    score: float
    timestamp: datetime
    search_hit_count: int = 0
    protected: bool = False


class ContextResponse(BaseModel):
    namespace: str
    context: str
    results: list[ContextResultItem]
    method: str = "contextual"
    total_tokens: int


class UpdateMemoryStatusRequest(BaseModel):
    review_status: str = Field(..., pattern=r'^(pending|reviewed|archived)$')


class UpdateMemoryStatusResponse(BaseModel):
    id: int
    review_status: str
    updated_at: datetime


# ── 画像 ──────────────────────────────────────────────

class LearnedItem(BaseModel):
    preference: str
    confidence: float = 0.5
    first_observed: Optional[datetime] = None
    last_confirmed: Optional[datetime] = None
    confirmations: int = 0
    contradictions: int = 0


class ProfileResponse(BaseModel):
    namespace: str
    agent_id: str = ""
    traits: dict[str, Any] = Field(default_factory=dict)
    learned: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    timeline_index: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: datetime


class UpdateProfileRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    traits: Optional[dict[str, Any]] = None
    learned_add: Optional[list[dict[str, Any]]] = None
    learned_override: Optional[list[dict[str, Any]]] = None
    state: Optional[dict[str, Any]] = None

    # P1-2（9-1 修复日）：learned 无界 → /consolidate 两两 Levenshtein 纯 Python
    # DP 同步跑在事件循环内，两条 10KB preference 即可挂起整个 1996。
    # 入模收口：条数 ≤50、单条 preference ≤200 字（LearnedItem 模板口径）。
    LEARNED_MAX_ITEMS: ClassVar[int] = 50
    LEARNED_PREF_MAX: ClassVar[int] = 200

    @model_validator(mode="after")
    def check_not_both(self):
        if self.learned_add is not None and self.learned_override is not None:
            raise ValueError("cannot provide both learned_add and learned_override")
        return self

    @model_validator(mode="after")
    def check_learned_bounds(self):
        for field_name in ("learned_add", "learned_override"):
            items = getattr(self, field_name)
            if items is None:
                continue
            if len(items) > self.LEARNED_MAX_ITEMS:
                raise ValueError(
                    f"{field_name} 至多 {self.LEARNED_MAX_ITEMS} 条，收到 {len(items)}")
            for it in items:
                pref = (it or {}).get("preference")
                if not isinstance(pref, str) or not (1 <= len(pref) <= self.LEARNED_PREF_MAX):
                    raise ValueError(
                        f"{field_name} 每条 preference 须为 1-{self.LEARNED_PREF_MAX} 字非空字符串")
        return self


# ── 轨迹 ──────────────────────────────────────────────

class AddTrajectoryRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    time: str = Field(..., min_length=4, max_length=8)
    action: str = Field(..., min_length=1)
    detail: str = ""
    result: str = ""


class TrajectoryResponse(BaseModel):
    status: str = "ok"
    namespace: str
    date: str
    actions: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    next_page_token: Optional[str] = None


class BatchMarkTrajectoryRequest(BaseModel):
    """批量标记轨迹动作为已处理（recorder 去重）。"""
    namespace: str = Field(..., max_length=64)
    date: str | None = None
    action_ids: list[str] = Field(..., min_length=1)


# ── Token ─────────────────────────────────────────────

class CreateTokenRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    level: str = Field(..., pattern=r'^(namespace|master|admin)$')


class CreateTokenResponse(BaseModel):
    namespace: str
    token: str
    level: str
    created_at: datetime


class ValidateTokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


class ValidateTokenResponse(BaseModel):
    valid: bool
    namespace: str = ""
    level: str = ""
    expires_at: Optional[datetime] = None


class RevokeTokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


class RevokeTokenResponse(BaseModel):
    status: str = "revoked"
    namespace: str
    level: str
    revoked_at: datetime


# ── 整理 ──────────────────────────────────────────────

class ConsolidateRequest(BaseModel):
    namespace: str = Field(..., max_length=64)


class ConsolidateResponse(BaseModel):
    status: str
    namespace: str = ""
    records_before: int = 0
    records_after: int = 0
    digest_id: Optional[int] = None
    timeline_added: bool = False
    reason: str = ""


# ── 会话聚合 ──────────────────────────────────────────

class SessionStartRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    context: str = ""
    top_k: int = Field(default=5, ge=1, le=50)
    agent_id: str = ""  # 可选：用于能力分发加权匹配（从寰宇查 category + capabilities）


class SessionStartResponse(BaseModel):
    namespace: str
    context_results: list[ContextResultItem] = Field(default_factory=list)
    profile: Optional[ProfileResponse] = None
    trajectory: Optional[TrajectoryResponse] = None


class SessionEndRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    summary: str = Field(..., min_length=1)
    state: Optional[dict[str, Any]] = None


class SessionEndResponse(BaseModel):
    memory_id: int
    memory_status: str
    profile_updated: bool
    timestamp: datetime


# ── 记忆迁移 ──────────────────────────────────────────

class TransferRequest(BaseModel):
    source_namespace: str = Field(..., max_length=64)
    target_namespace: str = Field(..., max_length=64)
    mode: str = Field(default="copy", pattern="^(copy|move)$")


class TransferResponse(BaseModel):
    transferred: int
    source_namespace: str
    target_namespace: str
    mode: str
    timestamp: datetime


# ── 会话恢复 ──────────────────────────────────────────

class RecoverSessionRequest(BaseModel):
    namespace: str = Field(..., max_length=64)
    since: Optional[str] = None       # ISO 时间，只恢复此时间之后的记忆


class RecoverSessionResponse(BaseModel):
    namespace: str
    recent_memories: list[dict[str, Any]] = Field(default_factory=list)
    total_recovered: int
    profile: Optional[ProfileResponse] = None
    last_session: Optional[dict[str, Any]] = None
    timestamp: datetime


# ── Hook 摄入 ──────────────────────────────────────────

class HookIngestEvent(BaseModel):
    event: str = Field(..., min_length=1, max_length=64)
    agent_id: str = Field(default="")
    session_id: str = Field(default="")
    namespace: str = Field(default="")
    content: str = Field(default="", max_length=10000)
    tool_name: str = Field(default="")
    tool_result: Optional[str] = Field(default=None, max_length=5000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HookIngestRequest(BaseModel):
    events: list[HookIngestEvent] = Field(..., min_length=1, max_length=50)


class HookIngestResult(BaseModel):
    event: str
    status: str = "ok"
    routed_to: list[str] = Field(default_factory=list)
    agent_id: str = ""
    error: Optional[str] = None


class HookIngestResponse(BaseModel):
    total: int
    trajectories: int
    memories: int
    skipped: int
    status: str
    results: list[HookIngestResult] = Field(default_factory=list)


# ── 通用错误 ──────────────────────────────────────────

class ErrorDetail(BaseModel):
    code: str
    message: str
    status: int


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str


# ── 业务异常 ──────────────────────────────────────────

class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
