"""
智能体工具注册与调用路由 — 对标 GB/Z 185.7-2026

统一管理所有智能体暴露的工具，提供：
  1. 工具描述 schema（名称、描述、参数）
  2. 工具发现（按能力/Agent查询可用工具）
  3. 工具调用路由（转发调用请求到目标Agent）
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field

logger = logging.getLogger("huanyu.tool_registry")


# ═══════════════════════════════════════════
# Tool Description Schema (GB/Z 185.7 §4)
# ═══════════════════════════════════════════

class ToolParameter(BaseModel):
    """工具参数定义"""
    name: str = Field(..., description="参数名")
    type: str = Field(default="string", description="参数类型")
    required: bool = Field(default=False, description="是否必填")
    description: str = Field(default="", description="参数说明")
    default: Any = Field(default=None, description="默认值")


class ToolDescription(BaseModel):
    """工具描述 — 对标 GB/Z 185.7"""
    tool_id: str = Field(..., description="工具唯一标识, 格式: {agent_id}/{tool_name}")
    name: str = Field(..., description="工具名称")
    description: str = Field(default="", description="工具功能描述")
    agent_id: str = Field(..., description="所属智能体ID")
    parameters: list[ToolParameter] = Field(default_factory=list, description="输入参数列表")
    output_type: str = Field(default="json", description="输出类型: json/text/binary")
    invocation_method: str = Field(default="sync", description="调用方式: sync/async")
    endpoint: str = Field(default="", description="调用端点")
    tags: list[str] = Field(default_factory=list, description="标签, 用于按能力搜索")
    registered_at: str = Field(default="", description="注册时间")


# ═══════════════════════════════════════════
# GB/Z 185.7 协议交互数据格式（表2-6）
# ═══════════════════════════════════════════

class ToolRequest(BaseModel):
    """工具请求 — 表2：请求智能体向工具提供方发起调用"""
    request_id: str = Field(..., description="请求唯一标识")
    tool_id: str = Field(..., description="目标工具ID")
    requester_ain: str = Field(..., description="请求方AIN")
    parameters: dict[str, Any] = Field(default_factory=dict, description="调用参数")
    timeout_ms: int = Field(default=30000, description="超时(毫秒)")
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ToolSync(BaseModel):
    """工具同步 — 表3：工具提供方定期向发现服务同步工具列表"""
    agent_id: str = Field(..., description="提供方Agent ID")
    tools: list[ToolDescription] = Field(..., description="当前可用工具列表")
    sync_version: int = Field(default=1, description="同步版本号（递增）")
    sync_time: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ToolUpdate(BaseModel):
    """工具更新 — 表4：工具描述变更通知"""
    tool_id: str = Field(..., description="变更的工具ID")
    action: str = Field(..., description="register / update / unregister")
    description: ToolDescription | None = Field(default=None, description="新描述(action=register/update时必填)")
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ToolInvocation(BaseModel):
    """工具调用 — 表5：工具提供方执行调用"""
    invocation_id: str = Field(..., description="调用唯一标识")
    request_id: str = Field(..., description="关联的请求ID")
    status: str = Field(default="pending", description="pending / running / completed / failed")
    started_at: str = Field(default="", description="开始执行时间")
    completed_at: str = Field(default="", description="完成时间")


class ToolResult(BaseModel):
    """工具结果 — 表6：调用结果返回"""
    invocation_id: str = Field(..., description="关联的调用ID")
    status: str = Field(..., description="success / error / timeout")
    data: Any = Field(default=None, description="成功时返回的数据")
    error: str | None = Field(default=None, description="失败时的错误信息")
    output_type: str = Field(default="json", description="输出类型")
    produced_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ═══════════════════════════════════════════
# In-Memory Tool Registry
# ═══════════════════════════════════════════

class ToolRegistry:
    """内存工具注册表 — Agent 启动时注册, 运行时查询"""

    def __init__(self):
        self._tools: dict[str, ToolDescription] = {}  # tool_id → description

    def register(self, tool: ToolDescription):
        """注册工具"""
        self._tools[tool.tool_id] = tool
        logger.info("工具注册: %s", tool.tool_id)

    def unregister(self, tool_id: str):
        """注销工具"""
        self._tools.pop(tool_id, None)

    def get(self, tool_id: str) -> ToolDescription | None:
        """按 ID 查询工具"""
        return self._tools.get(tool_id)

    def list_by_agent(self, agent_id: str) -> list[ToolDescription]:
        """查询某 Agent 的所有工具"""
        return [t for t in self._tools.values() if t.agent_id == agent_id]

    def list_by_tag(self, tag: str) -> list[ToolDescription]:
        """按标签搜索工具"""
        return [t for t in self._tools.values() if tag in t.tags]

    def list_all(self) -> list[ToolDescription]:
        """列出所有已注册工具"""
        return list(self._tools.values())

    def search(self, keyword: str) -> list[ToolDescription]:
        """按关键字搜索工具（名称/描述/标签）"""
        kw = keyword.lower()
        return [
            t for t in self._tools.values()
            if kw in t.name.lower() or kw in t.description.lower() or kw in " ".join(t.tags).lower()
        ]


# ═══════════════════════════════════════════
# Global Instance
# ═══════════════════════════════════════════

_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _registry


# ═══════════════════════════════════════════
# Agent Registration Helpers
# ═══════════════════════════════════════════

async def register_agent_tools(agent_id: str, tools: list[dict]):
    """Agent 注册时批量注册工具（从 capabilities 解析）"""
    reg = get_registry()
    for t in tools:
        params = [ToolParameter(**p) for p in t.get("parameters", [])]
        tool = ToolDescription(
            tool_id=f"{agent_id}/{t['name']}",
            name=t["name"],
            description=t.get("description", ""),
            agent_id=agent_id,
            parameters=params,
            invocation_method=t.get("invocation_method", "sync"),
            endpoint=t.get("endpoint", f"/v1/{agent_id}/{t['name']}"),
            tags=t.get("tags", []),
            registered_at=datetime.now(timezone.utc).isoformat(),
        )
        reg.register(tool)
    logger.info("Agent %s: %d 个工具已注册", agent_id, len(tools))


async def unregister_agent_tools(agent_id: str):
    """Agent 注销时清理工具注册"""
    reg = get_registry()
    to_remove = [tid for tid, t in reg._tools.items() if t.agent_id == agent_id]
    for tid in to_remove:
        reg.unregister(tid)
    logger.info("Agent %s: %d 个工具已注销", agent_id, len(to_remove))
