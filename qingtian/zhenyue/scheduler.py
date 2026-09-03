"""镇岳后台调度器 — 审批过期自动拒绝 + 告警静默队列 flush

启动方式（在 FastAPI startup 事件中调用）：
  await scheduler.start()

停止方式（在 FastAPI shutdown 事件中调用）：
  await scheduler.stop()
"""
import asyncio
import logging

from common.db import get_pool

logger = logging.getLogger("zhenyue.scheduler")

_running = False
_task: asyncio.Task | None = None
_interval: int = 60  # 扫描间隔（秒）


async def _scan():
    """单次扫描：过期审批 + 静默队列 flush。"""
    # ── 审批过期自动拒绝 ──
    try:
        from .approval_service import auto_reject_expired
        from .alert_service import alert_channel

        pool = await get_pool()
        async with pool.acquire() as conn:
            count = await auto_reject_expired(conn)
            if count > 0:
                logger.info("scheduler: auto-rejected %d expired approvals", count)

        # ── 静默时段队列 flush ──
        await alert_channel.flush_silent_queue()
    except Exception:
        logger.exception("scheduler: scan error")


async def _loop():
    global _running
    _running = True
    logger.info("Zhenyue scheduler started (interval=%ds)", _interval)
    while _running:
        try:
            await asyncio.sleep(_interval)
            await _scan()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("scheduler: loop error")
    logger.info("Zhenyue scheduler stopped")


async def start():
    global _task, _running
    if _running:
        return _task
    _task = asyncio.create_task(_loop())
    return _task


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
