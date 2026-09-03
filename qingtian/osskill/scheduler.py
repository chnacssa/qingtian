"""定时调度器 — 后台周期性任务

任务列表：
  - 黑板名单轮询: 每 24 小时
  - License 刷新: 每 12 小时
  - 离线计数器重置: 每 30 天（可选）

通过 asyncio.create_task 运行，不依赖外部 cron。
"""

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("osskill.scheduler")


@dataclass
class ScheduledTask:
    """定时任务描述"""
    name: str
    interval: float  # 秒
    callback: callable
    run_immediately: bool = True


class SkillScheduler:
    """Skill 后台任务调度器

    用法:
        scheduler = SkillScheduler()
        scheduler.add_task("blacklist_poll", 24, revocation_service.poll_once)
        scheduler.add_task("license_refresh", 12, license_refresh_func)
        await scheduler.start()
        ...
        await scheduler.stop()
    """

    def __init__(self):
        self._tasks: list[ScheduledTask] = []
        self._running = False
        self._jobs: list[asyncio.Task] = []

    def add_task(
        self,
        name: str,
        interval_hours: float,
        callback: callable,
        run_immediately: bool = True,
    ):
        """添加定时任务

        Args:
            name: 任务名（用于日志）
            interval_hours: 间隔（小时）
            callback: 异步回调（无参）
            run_immediately: 是否立即执行一次
        """
        self._tasks.append(ScheduledTask(
            name=name,
            interval=interval_hours * 3600,
            callback=callback,
            run_immediately=run_immediately,
        ))

    async def start(self):
        """启动所有定时任务"""
        self._running = True
        for task in self._tasks:
            job = asyncio.create_task(
                self._run_loop(task),
                name=f"scheduler-{task.name}",
            )
            self._jobs.append(job)
        logger.info("SkillScheduler started (%d tasks)", len(self._tasks))

    async def stop(self):
        """停止所有定时任务"""
        self._running = False
        for job in self._jobs:
            job.cancel()
            try:
                await job
            except asyncio.CancelledError:
                pass
        self._jobs.clear()
        logger.info("SkillScheduler stopped")

    async def _run_loop(self, task: ScheduledTask):
        """执行单个定时任务的循环"""
        if task.run_immediately and self._running:
            try:
                await task.callback()
            except Exception as e:
                logger.error(
                    "Task '%s' initial run failed: %s", task.name, e,
                )

        while self._running:
            await asyncio.sleep(task.interval)
            try:
                await task.callback()
                logger.debug("Task '%s' completed", task.name)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Task '%s' failed: %s", task.name, e,
                )
