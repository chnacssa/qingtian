"""
吸星 — Pydantic v2 数据模型
"""

from datetime import datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field


# ── 通用 ──────────────────────────────────────────────

class AppError(Exception):
    """业务异常（统一模式）"""
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


# ── 知识源 ────────────────────────────────────────────

class SourceCreate(BaseModel):
    id: str
    name: str
    url: str
    source_type: str = "custom"
    schedule: str = "daily"
    day_of_week: Optional[int] = None
    categories: List[str] = Field(default_factory=list)
    notes: str = ""
    enabled: bool = True


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    source_type: Optional[str] = None
    schedule: Optional[str] = None
    day_of_week: Optional[int] = None
    categories: Optional[List[str]] = None
    notes: Optional[str] = None
    enabled: Optional[bool] = None
    reputation: Optional[float] = None


class SourceResponse(BaseModel):
    id: str
    name: str
    url: str
    source_type: str
    schedule: str
    day_of_week: Optional[int] = None
    categories: List[str] = Field(default_factory=list)
    notes: str = ""
    enabled: bool = True
    reputation: float = 0.5
    last_fetched_at: Optional[str] = None
    last_status: str = "pending"
    consecutive_errors: int = 0


# ── 采集 ──────────────────────────────────────────────

class CollectRequest(BaseModel):
    dry_run: bool = False
    source_ids: Optional[List[str]] = None


class CollectionResult(BaseModel):
    source_id: str
    status: str
    content_hash: Optional[str] = None
    content_size: Optional[int] = None
    error: Optional[str] = None


class CollectResponse(BaseModel):
    action: str
    dry_run: bool
    sources_total: int
    sources_collected: int
    sources_failed: int
    results: List[CollectionResult] = Field(default_factory=list)
    timestamp: str


# ── 吸收 / 质量门 ─────────────────────────────────────

class IngestRequest(BaseModel):
    dry_run: bool = False
    date: Optional[str] = None
    run_ids: Optional[List[int]] = None


class GateResult(BaseModel):
    gate: str
    passed: bool
    score: Optional[float] = None
    detail: str = ""


class IngestResponse(BaseModel):
    action: str
    dry_run: bool
    date: str
    total_items: int
    passed: int
    rejected: int
    injected: int
    results: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: str


# ── 注入永恒 ──────────────────────────────────────────

class IngestToYonghengRequest(BaseModel):
    dry_run: bool = False
    date: Optional[str] = None
    force: bool = False


# ── Agent 经验 ────────────────────────────────────────

class LearnRequest(BaseModel):
    content: str
    memory_type: str = "episodic"
    source: str = "agent-self-report"
    protected: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LearnResponse(BaseModel):
    action: str
    agent_id: str
    namespace: str
    memory_id: Optional[int] = None
    status: str
    timestamp: str


# ── 踩坑 ──────────────────────────────────────────────

class XizhenjiCreate(BaseModel):
    title: str
    description: str
    root_cause: str = ""
    solution: str = ""
    severity: str = "medium"
    source: str = "manual"
    category: str = ""
    related_agent: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    learned_at: Optional[str] = None


class XizhenjiUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    root_cause: Optional[str] = None
    solution: Optional[str] = None
    severity: Optional[str] = None
    category: Optional[str] = None
    resolved: Optional[bool] = None
    tags: Optional[List[str]] = None


class XizhenjiResponse(BaseModel):
    id: int
    title: str
    description: str
    root_cause: str = ""
    solution: str = ""
    severity: str
    source: str
    category: str = ""
    related_agent: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    learned_at: Optional[str] = None
    resolved: bool = False
    injected_to_yongheng: bool = False


class ReportPitfallRequest(BaseModel):
    title: str
    description: str
    severity: str = "medium"
    tags: List[str] = Field(default_factory=list)


# ── 竞品扫描 ──────────────────────────────────────────

class ScanRequest(BaseModel):
    deep: bool = False
    since: Optional[int] = None


class ScanResultItem(BaseModel):
    skill_name: str
    function_cluster: Optional[str] = None
    differentiation_score: float = 0   # 高=与吸星差异大，值得学
    overlap_score: float = 0           # 高=与吸星重叠多，低优先级
    difference: str = ""
    description: str = ""
    url: str = ""
    actionable: bool = False


class ScanResponse(BaseModel):
    action: str
    total_scanned: int
    top_n: int
    results: List[ScanResultItem] = Field(default_factory=list)
    timestamp: str


# ── 蒸馏 ──────────────────────────────────────────────

class DistillRequest(BaseModel):
    namespace: str = "global"
    max_source_memories: int = 500
    model: Optional[str] = None


class DistillResponse(BaseModel):
    action: str
    namespace: str
    source_count: int
    produced_count: int
    llm_model: Optional[str] = None
    token_used: int = 0
    status: str
    timestamp: str


# ── 运行状态 ──────────────────────────────────────────

class RunRequest(BaseModel):
    dry_run: bool = False


# ── Skill 提案 ─────────────────────────────────────────


class EvolveRequest(BaseModel):
    dry_run: bool = False
    full_scan: bool = False


class EvolveResponse(BaseModel):
    proposals: list[dict]
    total: int
    dry_run: bool
