"""xihe 子包 — 子进程隔离框架

Xihe（羲和）负责 Skill 子进程的生成、监控、通信和销毁。
每个 Skill 实例运行在独立子进程中，通过 IPC（JSON-RPC over STDIO）与底座通信。
"""

from .agent_runtime import ChildProcess, XiheRuntime, SkillHandle
from .config import XiheConfig
from .errors import (
    ProcessError,
    ProcessNotFoundError,
    ResourceExhaustedError,
    SkillRunnerError,
)

__all__ = [
    "XiheConfig",
    "XiheRuntime",
    "ChildProcess",
    "SkillHandle",
    "ProcessError",
    "ProcessNotFoundError",
    "ResourceExhaustedError",
    "SkillRunnerError",
]
