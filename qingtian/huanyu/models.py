"""
寰宇 — Pydantic v2 数据模型
涵盖 Agent 目录、消息、谈判、协议、评分、底座互联
"""

from enum import Enum
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# ── 枚举 ──────────────────────────────────────────────

# 角色码 = 领域:角色，领域可扩展
AGENT_CATEGORIES = (
    "biz:buyer", "biz:seller", "biz:broker", "biz:inspector", "biz:assistant",
    "infra:scheduler", "infra:monitor", "infra:resolver", "infra:notifier",
    "infra:gateway", "infra:archive", "infra:finance",
    "sys:admin", "sys:root", "sys:observer", "sys:bridge",
)
AGENT_CATEGORY_PATTERN = r'^(biz:buyer|biz:seller|biz:broker|biz:inspector|biz:assistant|infra:scheduler|infra:monitor|infra:resolver|infra:notifier|infra:gateway|infra:archive|infra:finance|sys:admin|sys:root|sys:observer|sys:bridge)$'
AGENT_STATUSES = ("active", "inactive", "suspended", "deleted")
MESSAGE_TYPES = ("inquiry", "quote", "counter", "accept", "reject", "clarify", "cancel", "info", "payment_notify", "payment_confirm", "file", "image", "structured_data")
MESSAGE_PRIORITIES = ("low", "normal", "high", "urgent")
MESSAGE_STATUSES = ("unread", "read", "archived")
NEGOTIATION_STATUSES = ("active", "accepted", "rejected", "cancelled", "expired", "counter_proposed")
AGREEMENT_STATUSES = ("active", "signed", "completed", "cancelled", "disputed")


class TierEnum(str, Enum):
    free = "free"
    pro = "pro"
    enterprise = "enterprise"
    alliance = "alliance"


# ── Agent ────────────────────────────────────────────

class RegisterAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    category: str = Field(..., pattern=AGENT_CATEGORY_PATTERN)
    subcategory: str = Field(default="")
    capabilities: list[str] = Field(default_factory=list)
    contact_info: str = Field(default="")
    server_host: str = Field(default="")
    metadata: dict[str, Any] = Field(default_factory=dict)
    instance: Optional[str] = Field(default=None, max_length=36, description="自定义实例号，留空则自动递增")
    uscc: str = Field(default="", max_length=18, description="统一社会信用代码（18位），C1 认证用")
    agent_id: Optional[str] = Field(default=None, max_length=64, description="指定 agent_id，留空则自动生成 UUID")
    company_name: str = Field(default="", max_length=256, description="企业全称，C1 认证用")


class AgentResponse(BaseModel):
    agent_id: str
    ain: Optional[str] = None
    name: str
    category: str
    subcategory: str = ""
    capabilities: list[str] = Field(default_factory=list)
    server_host: str
    status: str
    trust_level: str = "basic"
    industry: str = ""
    c_level: str = "C0"
    scale: str = ""
    reputation: float = 3.0
    last_heartbeat: Optional[datetime] = None
    created_at: Optional[datetime] = None


# ── 消息 ──────────────────────────────────────────────

class SendMessageRequest(BaseModel):
    from_agent: str = Field(..., min_length=1)
    to_agent: str = Field(..., min_length=1)
    message_type: str = Field(default="info", pattern=r'^(inquiry|quote|counter|accept|reject|clarify|cancel|info|payment_notify|payment_confirm|file|image|structured_data|counter_response)$')
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: str = Field(default="normal", pattern=r'^(low|normal|high|urgent)$')
    reply_to: Optional[str] = None
    negotiation_id: Optional[str] = None
    topic: str = Field(default="")
    idempotency_key: str = Field(default="")  # 显式幂等键（进度广播去重），透传落库


class MessageResponse(BaseModel):
    message_id: str
    from_agent: str
    to_agent: str
    message_type: str
    status: str
    created_at: datetime


class InboxResponse(BaseModel):
    agent_id: str
    unread: int
    messages: list[dict[str, Any]]


# ── 谈判 ──────────────────────────────────────────────

class StartNegotiationRequest(BaseModel):
    buyer_id: str = Field(..., min_length=1)
    supplier_id: str = Field(..., min_length=1)
    product_category: str = Field(default="")
    initial_inquiry: dict[str, Any] = Field(default_factory=dict)
    max_counters: int = Field(default=5, ge=1, le=20)


class NegotiationResponse(BaseModel):
    negotiation_id: str
    buyer_id: str
    supplier_id: str
    status: str
    counter_count: int
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


# ── 协议 ──────────────────────────────────────────────

class CreateAgreementRequest(BaseModel):
    negotiation_id: str = Field(..., min_length=1)
    buyer_id: str = Field(..., min_length=1)
    supplier_id: str = Field(..., min_length=1)
    product: str = Field(..., min_length=1)
    quantity: str = Field(..., min_length=1)
    unit_price: str = Field(..., min_length=1)
    total_price: str = Field(..., min_length=1)
    terms: dict[str, Any] = Field(default_factory=dict)
    buyer_finance_ain: str = Field(default="", description="买方财务Agent的AIN")
    seller_finance_ain: str = Field(default="", description="卖方财务Agent的AIN")


class AgreementResponse(BaseModel):
    agreement_id: str
    negotiation_id: str
    product: str
    quantity: str
    total_price: str
    status: str
    buyer_finance_ain: str = ""
    seller_finance_ain: str = ""
    created_at: Optional[datetime] = None


# ── 评分 ──────────────────────────────────────────────

class SubmitRatingRequest(BaseModel):
    from_agent: str = Field(..., min_length=1)
    to_agent: str = Field(..., min_length=1)
    agreement_id: Optional[str] = None
    score: float = Field(..., ge=1.0, le=5.0, description="评分 1.0-5.0，支持一位小数")
    dimensions: dict[str, float] = Field(default_factory=dict)
    comment: str = Field(default="", max_length=500)


class RatingResponse(BaseModel):
    rating_id: str
    from_agent: str
    to_agent: str
    score: float
    created_at: Optional[datetime] = None


# ── 广播 / Topic ──────────────────────────────────────

class TopicSubscribeRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    topics: list[str] = Field(..., min_length=1)


class TopicPublishRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    message_type: str = Field(default="inquiry")
    payload: dict[str, Any] = Field(default_factory=dict)
    from_agent: str = Field(..., min_length=1)


# ── 通用 ──────────────────────────────────────────────

class StatsResponse(BaseModel):
    total_agents: int = 0
    active_agents: int = 0
    total_messages: int = 0
    active_negotiations: int = 0
    total_agreements: int = 0
    total_ratings: int = 0


# ── AIN 解析 ──────────────────────────────────────────

class ResolveRequest(BaseModel):
    ain: str = Field(..., min_length=10, description="要解析的 AIN")


# ── QACP 标准消息包（v0.4 四层结构）───────────────────

class QACPMessage(BaseModel):
    """QACP v0.4 消息包模型 — 路由层 / 签名层 / 内容层 / 传输层"""

    # ── 路由层 ──
    message_id: str = ""
    from_ain: str = Field(..., min_length=10, description="发送方 AIN")
    to_ain: str = Field(..., min_length=10, description="接收方 AIN")
    timestamp: Optional[datetime] = None
    ttl: int = Field(default=300, ge=1, le=86400, description="TTL 秒数")

    # ── 签名层 ──
    algorithm: str = Field(default="ed25519", description="签名算法")
    nonce: str = Field(default="", description="一次性防重放随机数")
    signature: str = Field(default="", description="Ed25519 签名 base64")
    cert_ref: str = Field(default="", description="证书指纹引用")

    # ── 内容层 ──
    message_type: str = Field(default="info", pattern=r'^(inquiry|quote|counter|accept|reject|clarify|cancel|info|payment_notify|payment_confirm|file|image|structured_data|counter_response)$')
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: str = Field(default="normal", pattern=r'^(low|normal|high|urgent)$')

    # ── 传输层 ──
    delivery_status: str = Field(default="local", pattern=r'^(local|pending|delivered|failed)$')
    idempotency_key: str = Field(default="")
    reply_to: Optional[str] = None

    # ── 扩展（向后兼容，标记 deprecated）─────────────
    negotiation_id: Optional[str] = Field(default=None, description="[deprecated] 使用 payload.negotiation_id")
    topic: str = Field(default="", description="[deprecated] 使用 payload.topic")
