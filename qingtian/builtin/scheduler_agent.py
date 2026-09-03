"""
infra:scheduler — 调度 Agent
功能：
  - 统一管理各模块的定时任务调度
  - 整合 xixing/zhice/zhenyue/huanyu 现有定时任务
  - 提供故障恢复和重试机制

由 ARM 自动拉起，无需手动配置。
"""

import asyncio
import httpx
import logging
import os
import random
from datetime import datetime, timezone
from typing import Optional

AGENT_ID = os.getenv("QINGTIAN_AGENT_ID", "infra:scheduler-01")
BASE_URL = os.getenv("QINGTIAN_BASE_URL", "http://localhost:1996")

logger = logging.getLogger("builtin.scheduler_agent")
logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")


# ── 定时任务注册表 ─────────────────────────────────────

class ScheduledJob:
    """一条定时任务定义"""
    def __init__(self, name: str, interval: int, coro, enabled: bool = True):
        self.name = name
        self.interval = interval      # 秒
        self.coro = coro
        self.enabled = enabled
        self.last_run: Optional[datetime] = None
        self.error_count: int = 0

    async def run(self):
        logger.info("[%s] 开始执行", self.name)
        try:
            await self.coro()
            self.error_count = 0
            self.last_run = datetime.now(timezone.utc)
            logger.info("[%s] 执行完成", self.name)
        except Exception as e:
            self.error_count += 1
            logger.error("[%s] 执行失败 (第%s次): %s", self.name, self.error_count, e)


# ── 任务实现 ──────────────────────────────────────────

async def _xixing_collect():
    """吸星知识采集（原 xixing.scheduler collect_pipeline）"""
    try:
        # review(2026-08-16): 原 import trigger_collect_pipeline 不存在 → 每次 ImportError 被吞，
        # 任务空转。改为真实内部入口 _collect_pipeline_job。
        from xixing.scheduler import _collect_pipeline_job
        await _collect_pipeline_job()
    except Exception as e:
        logger.warning("xixing collect 失败: %s", e)


async def _xixing_xizhenji():
    """吸星陷阱检测（原 xixing.scheduler xizhenji_job）"""
    try:
        # review(2026-08-16): 原 import trigger_xizhenji 不存在，改真实内部入口 _xizhenji_job
        from xixing.scheduler import _xizhenji_job
        await _xizhenji_job()
    except Exception as e:
        logger.warning("xixing xizhenji 失败: %s", e)


async def _zhice_timeout_check():
    """执策超时检查（原 zhice.timeout_checker）"""
    try:
        # review(2026-08-16): 原 import check_timeouts 不存在，改真实内部入口 _scan
        from zhice.timeout_checker import _scan as zhice_scan
        await zhice_scan()
    except Exception as e:
        logger.warning("zhice timeout check 失败: %s", e)


async def _huanyu_cleanup():
    """寰宇消息清理（原 huanyu.cron 清理任务）"""
    try:
        # review(2026-08-16): 原 import cleanup_expired_messages 不存在，改 _cleanup_messages_job
        from huanyu.cron import _cleanup_messages_job
        await _cleanup_messages_job()
    except Exception as e:
        logger.warning("huanyu cleanup 失败: %s", e)


async def _huanyu_retry():
    """寰宇消息重试投递（原 huanyu.cron 重试任务）"""
    try:
        # review(2026-08-16): 原 import retry_pending_deliveries 不存在，改 _retry_pending_deliveries_job
        from huanyu.cron import _retry_pending_deliveries_job
        await _retry_pending_deliveries_job()
    except Exception as e:
        logger.warning("huanyu retry 失败: %s", e)


async def _zhenyue_scheduler_tick():
    """镇岳调度器（原 zhenyue.scheduler）"""
    try:
        # review(2026-08-16): 原 import tick 不存在，改真实内部入口 _scan
        from zhenyue.scheduler import _scan as zhenyue_scan
        await zhenyue_scan()
    except Exception as e:
        logger.warning("zhenyue tick 失败: %s", e)


# ── 任务注册表 ─────────────────────────────────────────

JOBS = [
    # 高频任务（秒级）
    ScheduledJob("zhice_timeout", 60, _zhice_timeout_check),
    ScheduledJob("huanyu_retry", 1800, _huanyu_retry),   # 30 分钟

    # 每日任务（通过首次延迟对齐到特定时间）
    ScheduledJob("xixing_collect", 86400, _xixing_collect),    # 每天
    ScheduledJob("xixing_xizhenji", 86400, _xixing_xizhenji),  # 每天
    ScheduledJob("huanyu_cleanup", 86400, _huanyu_cleanup),    # 每天
    ScheduledJob("zhenyue_tick", 3600, _zhenyue_scheduler_tick),  # 每小时
]


# ── 主循环 ────────────────────────────────────────────

async def main():
    logger.info("Scheduler Agent 启动: %s, 管理 %s 个定时任务", AGENT_ID, len(JOBS))

    # 启动时发送心跳
    try:
        async with httpx.AsyncClient(timeout=10, base_url=BASE_URL) as client:
            await client.post(f"/v1/huanyu/agents/{AGENT_ID}/heartbeat")
    except Exception:
        pass

    # 为每个任务创建独立的执行循环
    async def job_loop(job: ScheduledJob):
        # 首次执行延迟：分散在各任务的 interval 内，避免同时爆发
        initial_delay = random.randint(0, min(job.interval, 300))
        await asyncio.sleep(initial_delay)

        while True:
            try:
                if job.enabled:
                    await job.run()
            except Exception as e:
                logger.error("[%s] 循环异常: %s", job.name, e)

            # 心跳每 5 分钟
            try:
                async with httpx.AsyncClient(timeout=10, base_url=BASE_URL) as client:
                    await client.post(f"/v1/huanyu/agents/{AGENT_ID}/heartbeat")
            except Exception:
                pass

            await asyncio.sleep(job.interval)

    # 并行启动所有任务
    tasks = [asyncio.create_task(job_loop(job)) for job in JOBS]

    # 等待所有任务（实际上不会完成，除非异常）
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
