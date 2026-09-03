"""
infra:heartbeat-monitor — 心跳监控 Agent

定期检查 Agent 心跳状态，自动标记 stale → inactive → suspended。
长期并入羲和，当前独立运行。
"""

import asyncio
import logging

from common.db import get_pool
from huanyu.config import get_schema_name as _huanyu_schema

logger = logging.getLogger("builtin.heartbeat_monitor")

AGENT_ID = "infra:heartbeat-monitor-01"
HUANYU_SCHEMA = _huanyu_schema()


async def check_stale_agents() -> list[str]:
    """标记超时无心跳的 Agent 为 inactive"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""UPDATE {HUANYU_SCHEMA}.agents SET status = 'inactive'
                WHERE status = 'active'
                  AND last_heartbeat < NOW() - heartbeat_interval * 3
                RETURNING agent_id::text""",
        )
        return [r["agent_id"] for r in rows]


async def check_suspended_agents() -> list[str]:
    """连续 24h 无心跳 → suspended"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""UPDATE {HUANYU_SCHEMA}.agents SET status = 'suspended'
                WHERE status = 'inactive'
                  AND last_heartbeat < NOW() - INTERVAL '24 hours'
                RETURNING agent_id::text""",
        )
        return [r["agent_id"] for r in rows]


async def run_once() -> dict:
    """执行一轮心跳检查"""
    stale = await check_stale_agents()
    suspended = await check_suspended_agents()

    result = {"stale_count": len(stale), "suspended_count": len(suspended)}
    if stale:
        logger.info("Marked %d stale agents inactive: %s", len(stale), stale[:5])
    if suspended:
        logger.info("Marked %d agents suspended: %s", len(suspended), suspended[:5])
    return result


async def run(interval_seconds: int = 300):
    """心跳监控主循环"""
    logger.info("Heartbeat monitor %s started (interval=%ds)", AGENT_ID, interval_seconds)
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await run_once()
        except Exception as e:
            logger.error("Heartbeat check failed: %s", e)


def get_agent_id() -> str:
    return AGENT_ID
