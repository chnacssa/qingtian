"""镇岳 — 速率限制（内存滑动窗口）。

基于 Python stdlib only，无 Redis 依赖。
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, Response

logger = logging.getLogger(__name__)


class RateLimiter:
    """速率限制 — 基于滑动窗口计数器。

    每个 key 独立计数，窗口内超过 limit 则拒绝。
    定时清理过期记录，避免内存泄漏。
    """

    def __init__(self, limit: int = 100, window_seconds: int = 60):
        self.limit = limit
        self.window = window_seconds
        self._buckets: dict[str, list[datetime]] = defaultdict(list)
        self._last_cleanup = datetime.now()

    def _cleanup(self, key: str) -> list[datetime]:
        """清理 key 的过期记录。"""
        now = datetime.now()
        cutoff = now - timedelta(seconds=self.window)
        self._buckets[key] = [t for t in self._buckets[key] if t > cutoff]
        return self._buckets[key]

    def _global_cleanup(self):
        """全局清理过期 key。"""
        now = datetime.now()
        if now - self._last_cleanup < timedelta(seconds=300):
            return
        self._last_cleanup = now
        cutoff = now - timedelta(seconds=self.window)
        empty_keys = [
            k for k, v in self._buckets.items()
            if all(t <= cutoff for t in v)
        ]
        for k in empty_keys:
            del self._buckets[k]

    async def check(self, key: str) -> bool:
        """检查是否超出限制。"""
        self._cleanup(key)
        self._global_cleanup()
        if len(self._buckets[key]) >= self.limit:
            return False
        self._buckets[key].append(datetime.now())
        return True

    async def remaining(self, key: str) -> int:
        """返回当前窗口内的剩余可用次数。"""
        records = self._cleanup(key)
        return max(0, self.limit - len(records))

    async def reset(self, key: str):
        """重置指定 key 的计数。"""
        self._buckets[key] = []


rate_limiter = RateLimiter()


# ── 滑动窗口限流中间件 ──

from . import config as cfg

_agent_windows: dict[str, list[float]] = {}
_global_window: list[float] = []
_last_agent_cleanup: float = time.time()


def _clean_window(entries: list[float], window_seconds: int = 60) -> list[float]:
    now = time.time()
    return [t for t in entries if now - t < window_seconds]


def _cleanup_agent_windows():
    """定期删除无活跃请求的 agent key，防止内存无限增长。

    每 300s 扫描一次，删除窗口内没有任何请求的 key。
    """
    global _last_agent_cleanup
    now = time.time()
    if now - _last_agent_cleanup < 300:
        return
    _last_agent_cleanup = now
    cutoff = now - 60
    stale = [k for k, v in _agent_windows.items() if not v or max(v) < cutoff]
    for k in stale:
        del _agent_windows[k]


async def rate_limit_middleware(request: Request, call_next):
    """FastAPI middleware: per-agent + global rate limiting."""
    if not request.url.path.startswith("/v1/zhenyue/"):
        return await call_next(request)

    if request.url.path in ("/v1/zhenyue/health", "/v1/zhenyue/status"):
        return await call_next(request)

    # P1 (R11): 限流 key 不信任客户端自报的 X-Agent-ID——攻击者每次换一个
    # X-Agent-ID 即可无限绕过 per-agent 限流。改用 RoleCheck 注入的已认证身份；
    # 无认证身份时退回客户端 IP（伪造成本远高于自报 header）。
    agent_id = (
        getattr(request.state, "agent_id", "") or ""
    ) or (request.client.host if request.client else "unknown")
    now = time.time()

    _cleanup_agent_windows()

    per_agent_rpm = cfg.get_rate_limit_per_agent()
    global_rpm = cfg.get_rate_limit_global()

    window = _clean_window(_agent_windows.get(agent_id, []))
    if len(window) >= per_agent_rpm:
        logger.warning("Rate limit hit: agent=%s, rpm=%d", agent_id, per_agent_rpm)
        return Response(
            content='{"error":"RATE_LIMITED","detail":"per-agent rate limit exceeded"}',
            status_code=429,
            media_type="application/json",
        )
    _agent_windows[agent_id] = window + [now]

    global _global_window
    global_win = _clean_window(_global_window)
    if len(global_win) >= global_rpm:
        logger.warning("Rate limit hit: global, rpm=%d", global_rpm)
        return Response(
            content='{"error":"RATE_LIMITED","detail":"global rate limit exceeded"}',
            status_code=429,
            media_type="application/json",
        )
    _global_window = global_win + [now]

    return await call_next(request)
