"""汇川 — Pydantic v2 数据模型"""

from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── CRUD 模型 ──────────────────────────────────────────


class KnowledgeCreate(BaseModel):
    title: str
    domain: str
    content: str
    tags: list[str] = Field(default_factory=list)
    visibility: str = "public"
    owner_agent: Optional[str] = None
    authorized_agents: list[str] = Field(default_factory=list)
    source: str = "manual"
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    entry_type: str = "entity"
    original_filename: Optional[str] = None
    original_storage_path: Optional[str] = None
    original_file_sha256: Optional[str] = None
    quality: int = 3
    status: str = "active"


class KnowledgeUpdate(BaseModel):
    title: Optional[str] = None
    domain: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list[str]] = None
    visibility: Optional[str] = None
    owner_agent: Optional[str] = None
    authorized_agents: Optional[list[str]] = None
    source: Optional[str] = None
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    metadata: Optional[dict[str, Any]] = None
    entry_type: Optional[str] = None
    original_filename: Optional[str] = None
    original_storage_path: Optional[str] = None
    original_file_sha256: Optional[str] = None
    quality: Optional[int] = None
    status: Optional[str] = None
    version: int


class KnowledgeResponse(BaseModel):
    knowledge_id: str
    title: str
    domain: str
    tags: list[str]
    visibility: str
    owner_agent: Optional[str] = None
    authorized_agents: list[str]
    content: str
    source: str
    version: int
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    metadata: dict[str, Any]
    entry_type: str = "entity"
    original_filename: Optional[str] = None
    original_storage_path: Optional[str] = None
    original_file_sha256: Optional[str] = None
    quality: int
    status: str
    refined_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


# ── 搜索模型 ──────────────────────────────────────────


class SearchRequest(BaseModel):
    query: str = ""
    tags: list[str] = Field(default_factory=list)
    domain: Optional[str] = None
    visibility: Optional[str] = None
    status: str = "active"
    sort_by: str = "updated_at"
    sort_order: str = "desc"
    limit: int = Field(default=20, le=200)
    offset: int = 0
    include_expired: bool = False


class SearchResultItem(BaseModel):
    knowledge_id: str
    title: str
    domain: str
    tags: list[str]
    snippet: str
    visibility: str
    updated_at: datetime


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    count: int
    query: str
    limit: int
    offset: int


class VectorSearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=10, le=50)
    domain: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    min_similarity: float = Field(default=0.7, ge=0.0, le=1.0)


class VectorSearchResultItem(BaseModel):
    knowledge_id: str
    title: str
    domain: str
    tags: list[str]
    snippet: str
    similarity: float
    visibility: str
    updated_at: datetime


class VectorSearchResponse(BaseModel):
    results: list[VectorSearchResultItem]
    count: int
    delegated_to: str = "yongheng"


# ── 批量模型 ──────────────────────────────────────────


class BatchWriteRequest(BaseModel):
    entries: list[KnowledgeCreate]


class BatchWriteResult(BaseModel):
    index: int
    knowledge_id: Optional[str] = None
    title: str
    status: str  # "stored" | "failed"
    error: Optional[str] = None


class BatchWriteResponse(BaseModel):
    action: str = "batch_write"
    total: int
    stored: int
    failed: int
    results: list[BatchWriteResult]
    timestamp: str


# ── 订阅模型 ──────────────────────────────────────────


class SubscriptionCreate(BaseModel):
    agent_id: str
    subscription_name: str = "default"
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class SubscriptionUpdate(BaseModel):
    subscription_name: Optional[str] = None
    domains: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    active: Optional[bool] = None


class SubscriptionResponse(BaseModel):
    subscription_id: str
    agent_id: str
    subscription_name: str
    domains: list[str]
    tags: list[str]
    active: bool
    created_at: datetime
    updated_at: datetime


# ── 精炼模型 (Phase 2 完整实现) ──────────────────────────


class RefineSubmitRequest(BaseModel):
    observation: str
    context: str = ""
    domain: Optional[str] = None


class RefineQueueItem(BaseModel):
    id: str
    submitter: str
    domain: Optional[str] = None
    confidence: int
    status: str
    created_at: datetime


class RefineQueueResponse(BaseModel):
    action: str = "refine_queue"
    total: int
    items: list[RefineQueueItem]
    timestamp: str


# ── 统计模型 ──────────────────────────────────────────


class StatsResponse(BaseModel):
    total_entries: int
    by_domain: dict[str, int]
    by_status: dict[str, int]
    by_visibility: dict[str, int]
    pending_refinement: int
    timestamp: str


# ── 版本历史模型 ──────────────────────────────────────


class VersionHistoryItem(BaseModel):
    version_id: str
    knowledge_id: str
    version: int
    changed_by: Optional[str] = None
    created_at: datetime


class VersionHistoryResponse(BaseModel):
    knowledge_id: str
    versions: list[VersionHistoryItem]
    total: int


class VersionDetailResponse(BaseModel):
    version_id: str
    knowledge_id: str
    version: int
    content: str
    changed_by: Optional[str] = None
    created_at: datetime


# ── 精炼处理模型 ──────────────────────────────────────


class RefineProcessResponse(BaseModel):
    action: str = "refine_process"
    processed: int
    accepted: int
    rejected: int
    duration_ms: float
    timestamp: str


# ── Metrics 模型 ───────────────────────────────────────


class ApiMetrics(BaseModel):
    search_p50_ms: float = 0.0
    search_p95_ms: float = 0.0
    search_p99_ms: float = 0.0
    vector_search_p50_ms: float = 0.0
    vector_search_p95_ms: float = 0.0
    write_p50_ms: float = 0.0
    write_p95_ms: float = 0.0


class SearchMetrics(BaseModel):
    hit_rate_1h: float = 0.0
    empty_result_rate_1h: float = 0.0
    avg_results_per_query: float = 0.0


class RefinementMetrics(BaseModel):
    queue_pending: int = 0
    processed_24h: int = 0
    success_rate_24h: float = 0.0
    avg_confidence: float = 0.0


class SyncMetrics(BaseModel):
    yongheng_success_rate_1h: float = 0.0
    yongheng_backlog: int = 0
    yongheng_retry_exhausted_24h: int = 0


class StorageMetrics(BaseModel):
    total_entries: int = 0
    by_domain: dict[str, int] = Field(default_factory=dict)
    expired_not_cleaned: int = 0


class MetricsResponse(BaseModel):
    api: ApiMetrics = Field(default_factory=ApiMetrics)
    search: SearchMetrics = Field(default_factory=SearchMetrics)
    refinement: RefinementMetrics = Field(default_factory=RefinementMetrics)
    sync: SyncMetrics = Field(default_factory=SyncMetrics)
    storage: StorageMetrics = Field(default_factory=StorageMetrics)


# ── 摄入模型 (Phase 3) ────────────────────────────────


class IngestRequest(BaseModel):
    text: str
    source: str = "manual"
    original_filename: Optional[str] = None
    storage_path: Optional[str] = None


class IngestResponse(BaseModel):
    entries: int = 0
    summary: str = ""
    error: Optional[str] = None
    ingested_at: Optional[str] = None
    knowledge_ids: list[str] = Field(default_factory=list)


class IngestFileResponse(BaseModel):
    entries: int = 0
    summary: str = ""
    error: Optional[str] = None
    storage_path: Optional[str] = None
    ingested_at: Optional[str] = None
    knowledge_ids: list[str] = Field(default_factory=list)


# ── 批量导入模型 ──────────────────────────────────────


class ImportResultItem(BaseModel):
    title: str
    action: str  # created/updated/skipped/conflicted/failed
    knowledge_id: Optional[str] = None
    version: Optional[int] = None
    reason: Optional[str] = None


class ImportReportResponse(BaseModel):
    status: str  # plan_ready / completed
    total_files: int = 0
    total_items: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    conflicted: int = 0
    failed: int = 0
    results: list[ImportResultItem] = Field(default_factory=list)
    timestamp: str = ""
