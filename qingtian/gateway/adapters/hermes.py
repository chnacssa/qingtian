"""
Hermes Adapter — 内置服务 HMAC 认证 + 推送

认证方式：X-Zhenyue-Service + HMAC 签名
  实现镇岳安全文档 §6.5 内置服务身份认证。
  Header 格式：
    X-Zhenyue-Service: hermes
    X-Zhenyue-Timestamp: <unix_ts>
    X-Zhenyue-Signature: base64url(hmac-sha256(psk, method+path+timestamp+body))
    X-Agent-ID: <agent_id>

推送方式：POST <endpoint> （框架事件 API）

参考：
  - docs/镇岳安全拦截架构设计.md §6.5 (line 423)
  - zhenyue/config.py get_builtin_services() (line 122)
"""

import base64
import hashlib
import hmac
import logging
import time
from typing import Optional

import httpx

from .base import AgentAdapter, AgentIdentity, PushResult
from .errors import AdapterAuthFailed
from .registry import register

logger = logging.getLogger("gateway.adapters.hermes")

# 镇岳文档 §6.5: timestamp drift <= 300s
MAX_TIMESTAMP_DRIFT = 300


class HermesAdapter(AgentAdapter):
    name = "hermes"
    display_name = "Hermes Communication Agent"
    version = "1.0.0"
    identity_namespace = "ext"
    priority = 20
    config_section = "gateway.adapters.hermes"

    def __init__(self):
        super().__init__()
        self._http: Optional[httpx.AsyncClient] = None
        self._service_name: str = ""
        self._pre_shared_key: str = ""
        self._default_agent_id: str = ""
        self._default_role: str = "agent"
        self._default_capabilities: list[str] = []

    # ── 生命周期 ─────────────────────────

    async def on_load(self, config: dict) -> None:
        await super().on_load(config)
        self._http = httpx.AsyncClient(timeout=5)
        self._service_name = config.get("builtin_service_name", "hermes")
        self._pre_shared_key = config.get("pre_shared_key", "")
        self._default_agent_id = config.get("agent_id", "builtin-hermes-001")
        self._default_role = config.get("role", "admin")
        self._default_capabilities = config.get("capabilities", [])
        logger.info("HermesAdapter loaded, service=%s agent=%s",
                     self._service_name, self._default_agent_id)

    async def on_unload(self) -> None:
        if self._http:
            await self._http.aclose()
        await super().on_unload()

    # ── 认证（HMAC 签名） ────────────────

    async def authenticate(self, scope: dict) -> Optional[AgentIdentity]:
        """HMAC 签名认证

        实现镇岳文档 §6.5 认证流程：
          1. 检查 X-Zhenyue-Service header
          2. 提取 X-Zhenyue-Timestamp, X-Zhenyue-Signature, X-Agent-ID
          3. 时间戳偏差 <= 300s
          4. 重新计算 HMAC-SHA256(PSK, method+path+timestamp+body)
          5. 常量时间比对签名
        """
        headers = self._extract_headers(scope)

        # Step 1: 检查服务标识 header
        service_name = headers.get("x-zhenyue-service", "")
        if not service_name:
            return None  # 非 Hermes 请求
        if service_name != self._service_name:
            return None

        # Step 2: 提取必需 header
        ts_str = headers.get("x-zhenyue-timestamp", "")
        signature = headers.get("x-zhenyue-signature", "")
        agent_id = headers.get("x-agent-id", "")

        if not ts_str or not signature or not agent_id:
            raise AdapterAuthFailed("Hermes: 缺少必需 HMAC headers")

        # Step 3: 时间戳偏差检查
        try:
            ts = int(ts_str)
        except ValueError:
            raise AdapterAuthFailed("Hermes: timestamp 格式无效")
        if abs(time.time() - ts) > MAX_TIMESTAMP_DRIFT:
            raise AdapterAuthFailed(
                f"Hermes: timestamp 偏差超过 {MAX_TIMESTAMP_DRIFT}s"
            )

        # Step 4: 重算 HMAC
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        # P1 (R11): 签名必须覆盖 body——原实现 body=""，攻击者持合法签名
        # 可任意篡改请求内容（提交结果/认领任务等）而签名仍有效。
        # middleware 已把 body 读入 scope['_drained_body'] 供此处参与签名。
        body_bytes = scope.get("_drained_body", b"") or b""
        body = body_bytes.decode("utf-8", "replace") if body_bytes else ""

        string_to_sign = f"{method}{path}{ts}{body}"
        expected = base64.urlsafe_b64encode(
            hmac.new(
                self._pre_shared_key.encode(),
                string_to_sign.encode(),
                hashlib.sha256,
            ).digest()
        ).decode().rstrip("=")

        # Step 5: 常量时间比对
        if not hmac.compare_digest(expected, signature):
            raise AdapterAuthFailed("Hermes: HMAC 签名不匹配")

        return AgentIdentity(
            agent_id=agent_id,
            role=self._default_role,
            capabilities=list(self._default_capabilities),
            namespace=f"{self.identity_namespace}:{agent_id}",
            adapter_name=self.name,
            ttl_seconds=60,
        )

    # ── 推送 ─────────────────────────────

    async def push(self, agent_id: str, event_type: str,
                   payload: dict) -> PushResult:
        """向 Hermes 事件 API 推送通知"""
        endpoint = self._config.get("push", {}).get("endpoint", "")
        token = self._config.get("push", {}).get("token", "")
        if not endpoint:
            return PushResult(ok=False, error="Hermes push 未配置")

        if not self._http:
            return PushResult(ok=False, error="Hermes adapter 未加载")

        try:
            resp = await self._http.post(
                endpoint,
                json={
                    "agent_id": agent_id,
                    "event_type": event_type,
                    "payload": payload,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.is_success:
                return PushResult(ok=True, status_code=resp.status_code)
            return PushResult(
                ok=False, error=f"HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        except Exception as e:
            return PushResult(ok=False, error=str(e)[:200])

    # ── 内部工具 ─────────────────────────

    @staticmethod
    def _extract_headers(scope: dict) -> dict[str, str]:
        """将 ASGI scope headers 转为 dict（小写 key）"""
        headers: dict[str, str] = {}
        for name, value in scope.get("headers", []):
            key = name.decode("latin-1").lower()
            headers[key] = value.decode("latin-1")
        return headers


# ── 模块导入时自注册 ──
register("hermes", HermesAdapter)
