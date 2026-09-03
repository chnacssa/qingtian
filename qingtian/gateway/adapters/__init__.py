"""
Agent Adapter 接口层 — 框架无关的 Agent 接入标准

每个 Agent 框架（OpenClaw / Hermes 等）需要实现 AgentAdapter 接口，
在 gateway.middleware.RoleCheckMiddlewareASGI 中自动接入认证链。

快速开始：
  from gateway.adapters.registry import get_registry
  registry = get_registry()
  identity = await registry.authenticate(scope)
"""

from .base import AgentAdapter, AgentIdentity, InterceptResult, PushResult
from .registry import get_registry, register

# 导入内置适配器触发自注册（单个失败不影响整体）
try:
    from . import openclaw  # noqa: F401
except ImportError:
    pass
try:
    from . import hermes    # noqa: F401
except ImportError:
    pass

__all__ = [
    "AgentAdapter",
    "AgentIdentity",
    "InterceptResult",
    "PushResult",
    "get_registry",
    "register",
]
