"""
智能体描述 — 对标 GB/Z 185.4-2026

标准 schema: 基本属性 + 能力声明 + 接口描述 + 服务质量属性
"""
from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


# ═══════════════════════════════════════════
# GB/Z 185.4 Agent Description Schema
# ═══════════════════════════════════════════

class AgentCapability(BaseModel):
    """能力声明 — 对标 GB/Z 185.4 §5.2"""
    name: str = Field(..., description="能力名称, 如 negotiation/bidding/document_generation")
    description: str = Field(default="", description="能力描述")
    version: str = Field(default="1.0", description="能力版本")
    input_schema: dict[str, Any] | None = Field(default=None, description="输入参数 schema")
    output_schema: dict[str, Any] | None = Field(default=None, description="输出结果 schema")
    invocation_method: str = Field(default="sync", description="调用方式: sync/async/callback")


class AgentInterface(BaseModel):
    """接口描述 — 对标 GB/Z 185.4 §5.3"""
    protocol: str = Field(default="REST", description="接口协议: REST/WebSocket/gRPC")
    endpoint: str = Field(default="", description="接口地址")
    port: int | None = Field(default=None, description="端口号")
    authentication: str = Field(default="bearer", description="认证方式: bearer/hmac/none")
    rate_limit: int | None = Field(default=None, description="速率限制(次/分钟)")


class AgentQoS(BaseModel):
    """服务质量 — 对标 GB/Z 185.4 §5.4"""
    avg_response_ms: int = Field(default=0, description="平均响应时间(毫秒)")
    availability: float = Field(default=0.99, ge=0, le=1, description="可用性 0-1")
    max_concurrent: int = Field(default=10, description="最大并发处理数")
    reliability: float = Field(default=0.99, ge=0, le=1, description="可靠性 0-1")


class AgentSkill(BaseModel):
    """技能声明 — 对标 GB/Z 185.4 §5.2 表2"""
    skill_id: str = Field(..., description="技能唯一标识")
    name: str = Field(..., description="技能名称")
    description: str = Field(default="", description="技能描述")
    tags: list[str] = Field(default_factory=list, description="标签")
    examples: list[str] = Field(default_factory=list, description="使用示例")
    input_types: list[str] = Field(default_factory=lambda: ["text"], description="输入类型")
    output_types: list[str] = Field(default_factory=lambda: ["text"], description="输出类型")
    dependencies: list[str] = Field(default_factory=list, description="依赖的其他技能ID")


class AgentDescription(BaseModel):
    """智能体描述 — 对标 GB/Z 185.4 完整 schema"""
    # 基本属性 (§5.1)
    agent_id: str = Field(..., description="智能体唯一标识(身份码)")
    name: str = Field(..., description="智能体名称")
    category: str = Field(default="", description="智能体类别")
    version: str = Field(default="1.0", description="智能体版本")
    description: str = Field(default="", description="文字描述")

    # GB/Z 185.4 表1 必填字段
    provider: str = Field(default="", description="提供方(组织名称)")
    default_input_types: list[str] = Field(default_factory=lambda: ["text"], description="默认输入类型, 如 [text, file, image]")
    default_output_types: list[str] = Field(default_factory=lambda: ["text"], description="默认输出类型")
    alias: str = Field(default="", description="别名/简称")
    icon_address: str = Field(default="", description="图标地址")
    serving_area: str = Field(default="", description="服务区域")
    access_method: str = Field(default="api", description="访问方式: api/web/mobile")

    # 能力声明 (§5.2)
    capabilities: list[AgentCapability] = Field(default_factory=list, description="能力列表")
    skills: list[AgentSkill] = Field(default_factory=list, description="技能列表(GB/Z 185.4 表2)")

    # 接口描述 (§5.3)
    interfaces: list[AgentInterface] = Field(default_factory=list, description="接口列表")

    # 服务质量 (§5.4)
    qos: AgentQoS | None = Field(default=None, description="服务质量属性")

    # 元数据
    owner: str = Field(default="", description="所属组织")
    created_at: datetime | None = Field(default=None, description="注册时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")


# ═══════════════════════════════════════════
# 兼容转换: 旧 capabilities(JSONB字符串数组) → 新 AgentCapability 列表
# ═══════════════════════════════════════════

def from_legacy_capabilities(legacy: list[str] | None) -> list[AgentCapability]:
    """旧的字符串数组 → 标准 AgentCapability 列表"""
    if not legacy:
        return []
    result = []
    for cap in legacy:
        if isinstance(cap, str):
            result.append(AgentCapability(name=cap, description=cap))
        elif isinstance(cap, dict):
            result.append(AgentCapability(**cap))
    return result


def to_legacy_capabilities(caps: list[AgentCapability]) -> list[str]:
    """标准 AgentCapability 列表 → 旧的字符串数组(向后兼容)"""
    return [c.name for c in caps]


def from_agent_row(row: dict) -> AgentDescription:
    """从 huanyu.agents 表行转为标准 AgentDescription"""
    caps = []
    legacy_caps = row.get("capabilities")
    if isinstance(legacy_caps, list):
        caps = from_legacy_capabilities(legacy_caps)

    return AgentDescription(
        agent_id=row.get("agent_id", ""),
        name=row.get("name", ""),
        category=row.get("category", ""),
        version=row.get("metadata", {}).get("version", "1.0") if isinstance(row.get("metadata"), dict) else "1.0",
        description=row.get("metadata", {}).get("description", "") if isinstance(row.get("metadata"), dict) else "",
        capabilities=caps,
        interfaces=[AgentInterface(
            endpoint=row.get("server_host", ""),
            authentication="bearer",
        )],
        owner=row.get("company_name", ""),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
