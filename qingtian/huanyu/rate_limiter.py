"""
寰宇 — per-AIN 限流
Tier（资源配额）与 C-Level（信任权重）正交

Tier 配额（QACP v0.6 §3.7 资源配额参考）：
  free:       20 msg/s, 最多 3 AIN
  pro:        100 msg/s, 最多 50 AIN
  enterprise: 不限
"""

import logging
from typing import Optional

from common.db import get_pool
from . import config as hcfg

logger = logging.getLogger("huanyu.rate_limiter")

SCHEMA = hcfg.get_schema_name()

# ── Tier 配额 ───────────────────────────────────────────

TIER_LIMITS = {
    "free": {"msg_per_sec": 20, "max_ains": 3},
    "pro": {"msg_per_sec": 100, "max_ains": 50},
    "enterprise": {"msg_per_sec": 0, "max_ains": 0},  # 0 = 不限
    "alliance": {"msg_per_sec": 0, "max_ains": 0},
}


async def _get_redis():
    """获取 Redis 连接，复用 huanyu peers 模式"""
    try:
        from .peers import get_engine
        engine = get_engine()
        return engine._redis
    except Exception:
        return None


async def check_rate(ain: str) -> bool:
    """
    滑动窗口计数器限流
    key: huanyu:ratelimit:{ain}:{second_bucket}
    返回 True = 放行, False = 触发 429

    Redis 不可用时 fail-open（仅 WARNING 日志，不限流）
    """
    import time
    redis = await _get_redis()
    if redis is None:
        logger.warning("Redis 不可用，限流 fail-open")
        return True

    try:
        second_bucket = int(time.time())
        key = f"huanyu:ratelimit:{ain}:{second_bucket}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 2)  # 2 秒过期，覆盖边界情况

        # 从 trust_level 查 Tier（后续智采独立管理）
        pool = await get_pool()
        async with pool.acquire() as conn:
            trust_level = await conn.fetchval(
                f"SELECT trust_level FROM {SCHEMA}.agents WHERE ain = $1", ain
            )
        tier = tier_from_trust_level(trust_level) if trust_level else "free"
        limit = TIER_LIMITS.get(tier, TIER_LIMITS["free"])["msg_per_sec"]

        if limit == 0:
            return True  # 不限

        return count <= limit
    except Exception:
        logger.exception("限流检查异常，fail-open")
        return True


async def check_ain_limit(server_host: str, tier: str = "free") -> bool:
    """
    检查同一底座下 AIN 数量是否超 Tier 上限
    返回 True = 未超限, False = 已超限
    """
    max_ains = TIER_LIMITS.get(tier, TIER_LIMITS["free"])["max_ains"]
    if max_ains == 0:
        return True

    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.agents "
            f"WHERE server_host = $1 AND status != 'deleted'",
            server_host,
        )
    return count < max_ains


def tier_from_trust_level(trust_level: str) -> str:
    """trust_level → Tier 映射（兼容现有数据）"""
    mapping = {"basic": "free", "verified": "pro", "trusted": "enterprise", "admin": "alliance"}
    return mapping.get(trust_level, "free")
