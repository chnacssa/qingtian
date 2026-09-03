"""
OpenClaw Adapter — DB Token 认证 + 推送

认证方式：Authorization: Bearer <zt_ns_xxx>
  现有 token_service.authenticate() 逻辑的适配器封装。
  支持缓存（复用 middleware.py 的 _token_cache 模式）。

推送方式：POST <endpoint><path_template>
  与 osskill/push_api.py 的 PUSH_TARGETS 兼容。
  path_template 支持 {agent_id} 占位符。

参考：
  - gateway/middleware.py RoleCheckMiddlewareASGI (line 74)
  - zhenyue/token_service.py authenticate() (line 98)
  - osskill/push_api.py _push_to_framework() (line 63)
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from common.db import get_pool
from zhenyue.token_service import authenticate

from .base import AgentAdapter, AgentIdentity, PushResult
from .errors import AdapterAuthFailed
from .registry import register

logger = logging.getLogger("gateway.adapters.openclaw")

# ── Token 缓存（复用 middleware.py 模式） ──
_token_cache: dict[str, tuple[dict, float]] = {}
TOKEN_CACHE_TTL = 60  # seconds


class OpenClawAdapter(AgentAdapter):
    name = "openclaw"
    display_name = "OpenClaw Agent Framework"
    version = "1.0.0"
    identity_namespace = "sys-eng"
    priority = 10
    config_section = "gateway.adapters.openclaw"

    def __init__(self):
        super().__init__()
        self._http: Optional[httpx.AsyncClient] = None
        self._push_endpoint: str = ""
        self._push_token: str = ""
        self._push_path_template: str = ""

    # ── 生命周期 ─────────────────────────

    async def on_load(self, config: dict) -> None:
        await super().on_load(config)
        self._http = httpx.AsyncClient(timeout=5)
        push_cfg = config.get("push", {})
        self._push_endpoint = push_cfg.get("endpoint", "")
        self._push_token = push_cfg.get("token", "")
        self._push_path_template = push_cfg.get(
            "path_template", "/api/sessions/{agent_id}/messages"
        )
        logger.info("OpenClawAdapter loaded, push=%s",
                     bool(self._push_endpoint))

    async def on_unload(self) -> None:
        if self._http:
            await self._http.aclose()
        await super().on_unload()

    # ── 认证（DB Token） ─────────────────

    async def authenticate(self, scope: dict) -> Optional[AgentIdentity]:
        """从 ASGI scope 提取 Bearer token → 调用 token_service 认证

        与 RoleCheckMiddlewareASGI.__call__() (middleware.py:89) 逻辑一致：
          1. 从 scope["headers"] 提取 Authorization header
          2. 查内存缓存（60s TTL）
          3. 缓存未命中 → authenticate(conn, token)
          4. 返回 AgentIdentity
        """
        token = self._extract_bearer_token(scope)
        if not token:
            return None  # 无凭据，下一个适配器

        # 查缓存
        identity = self._get_cached(token)
        if identity is None:
            # 缓存未命中 → DB 查询
            try:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    identity = await authenticate(conn, token)
                if identity:
                    self._set_cached(token, identity)
            except Exception as e:
                logger.warning("OpenClaw auth DB error: %s", e)
                return None

        if identity is None:
            raise AdapterAuthFailed("OpenClaw: token 无效或已过期")

        return AgentIdentity(
            agent_id=identity["agent_id"],
            role=identity.get("role", "agent"),
            capabilities=identity.get("capabilities", []),
            namespace=f"{self.identity_namespace}:{identity['agent_id']}",
            adapter_name=self.name,
            ttl_seconds=TOKEN_CACHE_TTL,
        )

    # ── 推送 ─────────────────────────────

    MAX_RETRIES = 3

    async def push(self, agent_id: str, event_type: str,
                   payload: dict) -> PushResult:
        """向 OpenClaw 会话 API 推送事件通知

        对应 osskill/push_api.py _push_to_framework() (line 63) 的 PUSH_TARGETS 模式。
        内置指数退避重试（3 次）。
        """
        if not self._http or not self._push_endpoint or not self._push_token:
            return PushResult(ok=False, error="OpenClaw push 未配置")

        url = (
            f"{self._push_endpoint.rstrip('/')}"
            f"{self._push_path_template.replace('{agent_id}', agent_id)}"
        )
        content = payload.get("content", "") or payload.get("body", "")
        title = payload.get("title", "")
        if title:
            content = f"[{title}]\n{content}"

        body = {
            "content": content,
            "metadata": {
                "source": payload.get("source", "qingtian"),
                "event_type": event_type,
            },
        }
        headers = {"Authorization": f"Bearer {self._push_token}"}

        last_error = ""
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self._http.post(url, json=body, headers=headers)
                if resp.status_code in (200, 202):
                    return PushResult(ok=True, status_code=resp.status_code)
                last_error = f"HTTP {resp.status_code}"
            except Exception as e:
                last_error = str(e)[:200]

            if attempt < self.MAX_RETRIES - 1:
                wait = 0.5 * (2 ** attempt)
                logger.warning("OpenClaw push 重试 %d/%d (%ss): %s",
                               attempt + 1, self.MAX_RETRIES, wait, last_error)
                await asyncio.sleep(wait)

        return PushResult(ok=False, error=last_error)

    # ── 内部工具 ─────────────────────────

    @staticmethod
    def _extract_bearer_token(scope: dict) -> str:
        """从 ASGI scope headers 提取 Bearer token (middleware.py:99-103)"""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                if auth.startswith("Bearer "):
                    return auth[7:]
        return ""

    @staticmethod
    def _get_cached(token: str) -> Optional[dict]:
        """查询 token 缓存"""
        entry = _token_cache.get(token)
        if entry and (time.monotonic() - entry[1]) < TOKEN_CACHE_TTL:
            return entry[0]
        if entry:
            _token_cache.pop(token, None)
        return None

    @staticmethod
    def _set_cached(token: str, identity: dict):
        """写入 token 缓存，超限时清理过期项"""
        _token_cache[token] = (identity, time.monotonic())
        if len(_token_cache) > 1000:
            now = time.monotonic()
            stale = [k for k, v in _token_cache.items()
                     if (now - v[1]) >= TOKEN_CACHE_TTL]
            for k in stale:
                _token_cache.pop(k, None)


# ── 模块导入时自注册 ──
register("openclaw", OpenClawAdapter)


def clear_token_cache() -> int:
    """清空 token 缓存，返回清除的条目数。供管理端点调用。"""
    global _token_cache
    n = len(_token_cache)
    _token_cache.clear()
    logger.info("Token cache cleared: %d entries", n)
    return n
