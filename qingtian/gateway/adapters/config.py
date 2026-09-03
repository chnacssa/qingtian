"""
Adapter 配置工具 — 从 config.yaml 读取 gateway.adapters.* 段

遵循 zhenyue/config.py 模式，通过 common.config.get() 读取。
"""
from typing import Any

from common.config import get


def get_adapter_config(adapter_name: str) -> dict:
    """获取指定适配器的配置"""
    return get(f"gateway.adapters.{adapter_name}", {})


def is_adapter_enabled(adapter_name: str) -> bool:
    """适配器是否启用"""
    return get(f"gateway.adapters.{adapter_name}.enabled", False)


def get_adapters_enabled() -> bool:
    """适配器认证链总开关"""
    return get("gateway.adapters.enabled", False)


def get_adapter_names() -> list[str]:
    """从配置中发现启用的适配器名称"""
    adapters = get("gateway.adapters", {})
    return [
        k for k, v in adapters.items()
        if isinstance(v, dict) and v.get("enabled", False)
    ]


def get_adapter_priority(adapter_name: str) -> int:
    """获取适配器优先级"""
    return get(f"gateway.adapters.{adapter_name}.priority", 100)


def get_adapter_push_config(adapter_name: str) -> dict:
    """获取适配器的推送配置（endpoint / token / path_template）"""
    return get(f"gateway.adapters.{adapter_name}.push", {})
