"""
第三方 Agent Adapter SDK 模板
============================
复制此文件创建新适配器，替换 'my_framework' 为你的框架名。

步骤：
  1. 另存为 opensource/qingtian/gateway/adapters/my_framework.py
  2. 替换类名、name、display_name、identity_namespace
  3. 实现 authenticate() — 至少需要这个
  4. 可选实现 intercept_message() / push() / 生命周期方法
  5. 在 config.yaml 中添加配置

Config (config.yaml):
  gateway:
    adapters:
      my_framework:
        enabled: true
        priority: 30
        endpoint: "http://my-agent:8080"
        auth:
          type: bearer
          token: "${MY_FW_TOKEN}"        # 敏感值用环境变量
        namespace_domain: "ext"          # namespace 前缀
        # ... 自定义字段 ...

框架无关工具函数（在 osskill/execute_api.py 中已有）：
  POST /api/v1/skills/{skill_name}/probe   — 意图探针
  POST /api/v1/skills/{skill_name}/execute  — 技能执行

参考文件：
  - base.py       — AgentAdapter ABC 定义
  - registry.py   — 注册中心
  - errors.py     — 异常层次
  - openclaw.py   — 完整适配器实现示例
  - hermes.py     — HMAC 认证实现示例
"""

import logging
from typing import Optional

import httpx

from .base import AgentAdapter, AgentIdentity, InterceptResult, PushResult
from .errors import AdapterAuthFailed
from .registry import register

logger = logging.getLogger("gateway.adapters.my_framework")


class MyFrameworkAdapter(AgentAdapter):
    """我的自定义框架适配器

    重写类属性以定义元数据：
      name                 框架标识（配置中的 key）
      display_name         人类可读名称
      identity_namespace   namespace 前缀，默认 "ext"
      priority             认证链优先级（越小越优先）
    """
    name = "my_framework"
    display_name = "My Custom Agent Framework"
    version = "1.0.0"
    identity_namespace = "ext"
    priority = 30
    config_section = "gateway.adapters.my_framework"

    def __init__(self):
        super().__init__()
        self._http: Optional[httpx.AsyncClient] = None
        self._endpoint: str = ""
        self._auth_token: str = ""

    # ── 生命周期 ─────────────────────────

    async def on_load(self, config: dict) -> None:
        """从 config.yaml 加载自定义配置"""
        await super().on_load(config)
        self._http = httpx.AsyncClient(timeout=10)
        self._endpoint = config.get("endpoint", "")
        auth_cfg = config.get("auth", {})
        self._auth_token = auth_cfg.get("token", "")
        logger.info("MyFrameworkAdapter loaded, endpoint=%s", self._endpoint)

    async def on_unload(self) -> None:
        if self._http:
            await self._http.aclose()
        await super().on_unload()

    # ── 认证（必须实现） ─────────────────

    async def authenticate(self, scope: dict) -> Optional[AgentIdentity]:
        """从 ASGI scope 解析 Agent 身份。

        常见认证方式：
          - Bearer token → 通过第三方 API 验证
          - HMAC 签名 → 预共享密钥验证
          - mTLS → 从客户端证书提取 CN

        返回：
          AgentIdentity — 认证成功
          None          — 无凭据（下一个适配器尝试）
          raise AdapterAuthFailed — 凭据无效（硬拒绝）
        """
        # 提取 Authorization header
        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break

        if not auth:
            return None

        if auth.startswith("Bearer "):
            token = auth[7:]
            # TODO: 用你的框架 API 验证 token
            # agent_info = await self._validate_token(token)
            # if agent_info:
            #     return AgentIdentity(
            #         agent_id=agent_info["agent_id"],
            #         role=agent_info.get("role", "agent"),
            #         capabilities=agent_info.get("capabilities", []),
            #         namespace=f"{self.identity_namespace}:{agent_info['agent_id']}",
            #         adapter_name=self.name,
            #     )
            raise AdapterAuthFailed(
                "MyFramework: token 验证未实现（实现 me 后删除此异常）"
            )

        return None

    # ── 消息拦截（可选） ─────────────────

    async def intercept_message(self, message: dict,
                                context: dict) -> InterceptResult:
        """拦截入站消息（LLM 前处理）。

        调用 probe → passthrough=false 时执行 → 直接回复。
        返回 InterceptResult(handled=True, reply=..., skip_llm=True) 短路 LLM。
        返回 InterceptResult(handled=False) 放行。
        """
        return InterceptResult(handled=False)

    # ── 推送（可选） ─────────────────────

    async def push(self, agent_id: str, event_type: str,
                   payload: dict) -> PushResult:
        """向框架推送事件通知"""
        if not self._endpoint:
            return PushResult(ok=False, error="endpoint 未配置")
        if not self._http:
            return PushResult(ok=False, error="adapter 未加载")

        try:
            resp = await self._http.post(
                f"{self._endpoint}/events",
                json={"agent_id": agent_id, "type": event_type,
                       "payload": payload},
                headers={"Authorization": f"Bearer {self._auth_token}"},
            )
            if resp.is_success:
                return PushResult(ok=True, status_code=resp.status_code)
            return PushResult(
                ok=False, error=f"HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        except Exception as e:
            return PushResult(ok=False, error=str(e)[:200])


# ── 模块导入时自注册 ──
register("my_framework", MyFrameworkAdapter)
