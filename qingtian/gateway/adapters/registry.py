"""
Adapter 注册中心 — 配置驱动注册 + 认证链调度

使用方式：
  # 第三方适配器在模块级别自注册：
  from .registry import register
  register("my_framework", MyFrameworkAdapter)

  # 中间件中调用认证链：
  registry = get_registry()
  identity = await registry.authenticate(scope)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .base import AgentAdapter, AgentIdentity
from .errors import AdapterAuthFailed

logger = logging.getLogger("gateway.adapters.registry")


class AdapterRegistry:
    """适配器注册中心

    持有所有注册的 AgentAdapter，按优先级排序。
    提供 authenticate() 链式调度。
    """

    def __init__(self):
        self._adapters: dict[str, AgentAdapter] = {}
        self._initialized = False

    def register(self, name: str, adapter: AgentAdapter) -> None:
        """注册适配器实例"""
        self._adapters[name] = adapter
        logger.info("Adapter registered: %s (v%s, priority=%s)",
                     name, adapter.version, adapter.priority)

    def get(self, name: str) -> Optional[AgentAdapter]:
        """按名称获取适配器"""
        return self._adapters.get(name)

    def list(self) -> list[str]:
        """列出所有已注册的适配器"""
        return list(self._adapters.keys())

    def all(self) -> list[AgentAdapter]:
        """所有适配器实例（按优先级排序）"""
        return sorted(
            self._adapters.values(),
            key=lambda a: a.priority,
        )

    async def initialize_from_config(self, config: dict = None) -> None:
        """从配置加载适配器并调用 on_load

        两种用法：
          # 手动传配置
          await registry.initialize_from_config({"gateway": {"adapters": {...}}})

          # 自动读 common.config（main.py 启动时调用推荐）
          await registry.initialize_from_config()

        Config 格式：
          gateway:
            adapters:
              openclaw:
                enabled: true
                priority: 10
                ...
              hermes:
                enabled: true
                priority: 20
                ...
        """
        if self._initialized:
            return

        if config is None:
            from common.config import get as cfg_get
            adapters_config = cfg_get("gateway.adapters", {})
            config = {"gateway": {"adapters": adapters_config}}

        adapters_config = config.get("gateway", {}).get("adapters", {})
        for name, cfg in adapters_config.items():
            if not isinstance(cfg, dict):
                continue
            if not cfg.get("enabled", False):
                continue
            adapter = self._adapters.get(name)
            if adapter is None:
                logger.warning("Adapter '%s' 已配置但未注册，跳过", name)
                continue
            try:
                await adapter.on_load(cfg)
                logger.info("Adapter '%s' initialized from config", name)
            except Exception as e:
                logger.warning("Adapter '%s' on_load 失败，跳过: %s", name, e)

        self._initialized = True

    async def authenticate(self, scope: dict) -> Optional[AgentIdentity]:
        """认证链：按优先级依次尝试所有适配器

        规则：
          - 第一个返回 AgentIdentity 的适配器胜出
          - 无适配器能认证 → 返回 None（匿名 fallback）
          - 任一适配器抛出 AdapterAuthFailed → 传播（硬拒绝）
        """
        for adapter in self.all():
            try:
                identity = await adapter.authenticate(scope)
                if identity is not None:
                    identity.adapter_name = adapter.name
                    logger.debug("Auth resolved by '%s': %s",
                                 adapter.name, identity.agent_id)
                    return identity
            except AdapterAuthFailed:
                logger.warning("Adapter '%s' auth hard-rejected", adapter.name)
                return None
            except Exception as e:
                logger.warning("Adapter '%s' auth error: %s",
                               adapter.name, e)
        return None


# ── 全局单例 ──

_registry: Optional[AdapterRegistry] = None


def get_registry() -> AdapterRegistry:
    """获取全局 AdapterRegistry 单例"""
    global _registry
    if _registry is None:
        _registry = AdapterRegistry()
    return _registry


def register(name: str, adapter_cls: type) -> None:
    """便利函数：实例化并注册一个适配器类

    适配器在模块导入时自注册：
      from .registry import register
      register("openclaw", OpenClawAdapter)
    """
    instance = adapter_cls()
    instance.name = name
    get_registry().register(name, instance)
