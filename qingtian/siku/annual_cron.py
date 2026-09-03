"""
司库 — 年费到期定时检查
每 24h 扫描一次，过期 → 通过寰宇状态机标记 inactive
"""

import asyncio
import logging

from common.db import get_pool
from huanyu.config import get_schema_name as _huanyu_schema
from . import config as cfg

logger = logging.getLogger("siku.annual_cron")
SCHEMA = cfg.get_schema_name()
HUANYU_SCHEMA = _huanyu_schema()

_running = False
_task: asyncio.Task | None = None


async def check_annual_fee_expiry() -> int:
    """扫描过期年费 → 标记 inactive + is_expired"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT agent_id FROM {SCHEMA}.annual_fee_status "
            f"WHERE expires_at < NOW() AND is_expired = false"
        )
        count = 0
        for row in rows:
            agent_id = row["agent_id"]
            async with conn.transaction():
                await conn.execute(
                    f"UPDATE {SCHEMA}.annual_fee_status SET "
                    f"is_expired = true, expired_at = NOW(), updated_at = NOW() "
                    f"WHERE agent_id = $1",
                    agent_id,
                )
                await conn.execute(
                    f"UPDATE {HUANYU_SCHEMA}.agents SET status = 'inactive', updated_at = NOW() "
                    f"WHERE agent_id = $1 AND status = 'active'",
                    agent_id,
                )
            logger.info("annual fee expired: %s → inactive", agent_id)
            count += 1
    return count


async def _run_loop(interval_seconds: int = 86400):
    global _running
    _running = True
    while _running:
        try:
            expired_count = await check_annual_fee_expiry()
            if expired_count:
                logger.info("annual cron: %s agent(s) marked inactive", expired_count)
        except Exception:
            logger.exception("annual cron check failed")
        await asyncio.sleep(interval_seconds)


async def start(interval_seconds: int = 86400):
    global _task
    if _task is not None:
        return
    _task = asyncio.create_task(_run_loop(interval_seconds))
    logger.info("annual fee cron started (interval=%ss)", interval_seconds)


async def stop():
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
