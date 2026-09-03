"""
API 限流 — Token Bucket 实现

拦截点设在父进程代理层（Bus 的 API 网关），
不与子进程内的 ctx API 门控重复。

集成计划（Phase 4 — 网关集成阶段）：
  1. 在 gateway/middleware.py 新增 RateLimitMiddleware
  2. 中间件从请求头提取 skill_name，调 registry.get_or_create(skill_name, rpm)
  3. acquire() 失败 → 返回 429
  4. RPM 值从 skill.json 的 resources.api_calls_per_minute 读取
"""

import time
import asyncio
from collections import defaultdict


class TokenBucket:
    """令牌桶限流器"""

    def __init__(self, rate: float, capacity: int):
        self.rate = rate              # 令牌/秒
        self.capacity = capacity      # 桶容量
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """获取一个令牌，返回是否成功"""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class RateLimiterRegistry:
    """全局限流器注册表（按 Skill 名称索引）"""

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}

    def get_or_create(self, skill_name: str, rpm: int) -> TokenBucket:
        """获取或创建 Skill 对应的令牌桶

        Args:
            skill_name: Skill 名称
            rpm: 每分钟允许的最大请求数

        Returns:
            对应 Skill 的 TokenBucket 实例
        """
        if skill_name not in self._buckets:
            self._buckets[skill_name] = TokenBucket(rate=rpm / 60, capacity=rpm)
        return self._buckets[skill_name]
