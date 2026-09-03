"""
产品目录模块 — 定时维护任务

任务（北京时间）：
  - 价目表过期处理：每天 02:00
    检查标记了 daily_update 且 valid_until 已到期的价目表，自动 supersede 或 archive

解耦说明：
  - is_management 通过 start(is_management_role=...) 注入，不直接导入 common.config
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import config as pcfg
from .db import get_pool

logger = logging.getLogger("product.cron")

SCHEMA = pcfg.get_schema_name()

_tasks: list[asyncio.Task] = []
_running = False


def _track_task(task: asyncio.Task) -> None:
    """登记 fire-and-forget 任务：完成后自动从 _tasks 移除并消费异常。

    review(2026-08-16): 原实现直接 create_task 不登记，任务异常无人 await → 未检索告警；
    _tasks 仅记录常驻协程，子任务引用随手释放。done 回调兜底移除 + 消费异常。
    """
    _tasks.append(task)

    def _done(t: asyncio.Task):
        try:
            if t in _tasks:
                _tasks.remove(t)
            t.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("Product scheduled task %r crashed", getattr(t, "get_name", lambda: "?")(), exc_info=True)

    task.add_done_callback(_done)


_timezone: ZoneInfo | None = None
_last_run_date: dict[str, str] = {}


def _now() -> datetime:
    global _timezone
    if _timezone is None:
        try:
            _timezone = ZoneInfo("Asia/Shanghai")
        except Exception:
            _timezone = ZoneInfo("UTC")
    return datetime.now(_timezone)


def _should_run_daily(task: str, hour: int, minute: int) -> bool:
    now = _now()
    today = now.strftime("%Y-%m-%d")
    if _last_run_date.get(task) == today:
        return False
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= target


# ── 任务实现 ──────────────────────────────────────


async def _expire_price_lists_job():
    """价目表过期自动处理。

    1. daily_update = TRUE 且 valid_until < 今天的 active 价目表 → 设为 superseded
    2. superseded 超过 180 天且没有关联 active price_list 的 → 设为 archived
    """
    logger.info("Cron: product price list expiry started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 1. 设置过期的 daily_update 价目表为 superseded
            result1 = await conn.execute(
                f"""UPDATE {SCHEMA}.price_lists
                    SET status = 'superseded', updated_at = NOW()
                    WHERE status = 'active'
                      AND daily_update = TRUE
                      AND valid_until < CURRENT_DATE"""
            )
            expired = int(result1.split()[-1]) if result1 else 0
            if expired > 0:
                logger.info(
                    "Cron: %d price lists auto-superseded (daily_update expired)",
                    expired,
                )

            # 2. 清理 180 天前的旧版本
            result2 = await conn.execute(
                f"""UPDATE {SCHEMA}.price_lists
                    SET status = 'archived', updated_at = NOW()
                    WHERE status = 'superseded'
                      AND updated_at < NOW() - INTERVAL '180 days'"""
            )
            archived = int(result2.split()[-1]) if result2 else 0
            if archived > 0:
                logger.info(
                    "Cron: %d old price lists archived (>180d superseded)", archived,
                )

            total = expired + archived
            if total == 0:
                logger.info("Cron: product price list expiry — nothing to do")
    except Exception as e:
        logger.error("Cron: product price list expiry failed: %s", e)


# ── 调度主循环 ────────────────────────────────────

_CRON_SCHEDULE: list[tuple[str, int, int, Callable]] = [
    ("product_expire_price_lists", 2, 0, _expire_price_lists_job),  # 每天 02:00
]


async def _scheduler_loop():
    global _running
    _running = True

    while _running:
        try:
            for task_name, hour, minute, job_fn in _CRON_SCHEDULE:
                if _should_run_daily(task_name, hour, minute):
                    _last_run_date[task_name] = _now().strftime("%Y-%m-%d")
                    _track_task(asyncio.create_task(job_fn()))
        except Exception as e:
            logger.error("Product cron scheduler loop error: %s", e)

        await asyncio.sleep(60)


async def start(is_management_role: Optional[bool] = None):
    """启动产品目录定时任务。

    Args:
        is_management_role: 是否为 management 角色。
            传入后不再查询 common.config，实现解耦。
            不传时回退到直接导入检查。
    """
    global _running
    if _running:
        return

    if is_management_role is None:
        is_management_role = (
            os.environ.get("QINGTIAN_ROLE", "management") == "management"
        )
    if not is_management_role:
        logger.info("Product cron: skipped (not management role)")
        return

    logger.info("Starting product cron: price list expiry@02:00 daily")
    task = asyncio.create_task(_scheduler_loop())
    _tasks.append(task)


async def stop():
    """停止产品目录定时任务。"""
    global _running
    _running = False
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    logger.info("Product cron stopped")
