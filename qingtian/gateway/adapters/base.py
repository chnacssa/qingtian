"""
AgentAdapter 抽象基类 — 所有 Agent 框架接入的契约

使用方式：
  class MyAdapter(AgentAdapter):
      name = "my_framework"
      identity_namespace = "ext"
      priority = 30

      async def authenticate(self, scope) -> AgentIdentity | None:
          ...

  register("my_framework", MyAdapter)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AgentIdentity:
    """认证结果 — 通用的 Agent 身份对象

    字段说明：
      agent_id      唯一标识（如 "sys-eng-manager", "hermes-alpha"）
      role          角色（admin / ops_admin / agent，兼容现有 _role_capabilities）
      capabilities  权限列表（如 ["admin", "ops_admin"]）
      namespace     完整命名空间（如 "sys-eng:manager", "ext:hermes-alpha"）
      adapter_name  由哪个适配器解析的
      ttl_seconds   建议缓存时间
      metadata      适配器自定义扩展字段
    """
    agent_id: str
    role: str = "agent"
    capabilities: list[str] = field(default_factory=list)
    namespace: str = ""
    adapter_name: str = ""
    ttl_seconds: int = 60
    metadata: dict = field(default_factory=dict)


@dataclass
class InterceptResult:
    """消息拦截结果

    handled=True 表示适配器已处理此消息，底座不再走默认路径：
      reply     回复内容
      skip_llm  True = 不发给 LLM，直接回复
      passthrough True = 放行给 LLM 处理（秘书探针结果）

    handled=False 表示适配器不处理，交给下一个拦截器
    """
    handled: bool = False
    reply: Optional[str] = None
    skip_llm: bool = False
    passthrough: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class PushResult:
    """推送结果"""
    ok: bool
    error: Optional[str] = None
    status_code: Optional[int] = None


class AgentAdapter(ABC):
    """Agent 框架适配器 — 插拔式接口

    每个适配器封装：
      - 认证：如何从请求中解析 AgentIdentity
      - 消息拦截：如何在 LLM 前预处理消息
      - 推送：如何向框架发送事件通知
      - 命名空间：框架使用的 namespace 域

    子类至少要实现 authenticate()。
    on_load / on_unload 生命周期钩子可选覆盖。
    """

    # ── 元数据（子类覆盖） ──
    name: str = ""                     # "openclaw", "hermes"
    display_name: str = ""
    version: str = "1.0.0"
    identity_namespace: str = ""       # namespace 域，如 "sys-eng", "ext"
    priority: int = 100                # 认证链优先级，越小越先
    config_section: str = ""           # config.yaml 中的配置路径

    def __init__(self):
        self._config: dict = {}
        self._loaded = False

    # ── 生命周期 ─────────────────────────

    async def on_load(self, config: dict) -> None:
        """加载配置。连接初始化、密钥加载等。"""
        self._config = config
        self._loaded = True

    async def on_unload(self) -> None:
        """卸载。释放连接、清理资源。"""
        self._loaded = False

    # ── 认证（必须实现） ─────────────────

    @abstractmethod
    async def authenticate(self, scope: dict) -> Optional[AgentIdentity]:
        """从 ASGI scope 解析 Agent 身份。

        返回值：
          AgentIdentity   — 认证成功
          None            — 无凭据，让下一个适配器尝试
          raise AdapterAuthFailed — 凭据无效，硬拒绝（停止链）

        参数 scope 是 ASGI scope dict，与 RoleCheckMiddlewareASGI
        接收的格式一致。可通过 scope["headers"] 读取请求头。
        """
        ...

    # ── 消息拦截（可选） ─────────────────

    async def intercept_message(self, message: dict,
                                context: dict) -> InterceptResult:
        """拦截入站消息（LLM 处理前）。

        对应各框架的钩子：
          OpenClaw — message_received
          Hermes   — pre_gateway_dispatch
          Gateway  — 独立 IM Gateway 入站处理器

        默认不拦截（handled=False）。
        """
        return InterceptResult(handled=False)

    # ── 推送（可选） ─────────────────────

    async def push(self, agent_id: str, event_type: str,
                   payload: dict) -> PushResult:
        """向 Agent 框架推送事件通知。

        对应 osskill/push_api.py 的 PUSH_TARGETS 模式。
        默认不支持推送。
        """
        return PushResult(ok=False, error="push not implemented")

    # ── 生命周期管理（可选） ─────────────

    async def start_agent(self, agent_id: str, config: dict) -> dict:
        """请求框架启动 Agent"""
        return {"ok": False, "error": "start_agent not implemented"}

    async def stop_agent(self, agent_id: str) -> dict:
        """请求框架停止 Agent"""
        return {"ok": False, "error": "stop_agent not implemented"}

    async def get_agent_status(self, agent_id: str) -> Optional[dict]:
        """查询 Agent 状态"""
        return None
