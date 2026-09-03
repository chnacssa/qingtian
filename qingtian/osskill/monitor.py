"""Skill 监控 — 每 Skill 统计（调用量/延迟/错误率/RSS）

指标说明：
  - invoke_count: 调用次数
  - success_count: 成功次数
  - error_count: 失败次数
  - avg_latency_ms: 平均延迟（毫秒）
  - last_latency_ms: 最近一次调用延迟
  - max_latency_ms: 最大延迟
  - rss_bytes: 子进程 RSS 内存（需要 psutil）
  - error_rate: 自动计算（error_count / invoke_count）

数据存储：
  - 运行时 metrics: 内存字典
  - 每日统计: 写入 skill_usage_stats 表
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from common.db import get_pool

from .database import SCHEMA

logger = logging.getLogger("osskill.monitor")


@dataclass
class SkillMetrics:
    """单个 Skill 的运行时指标"""
    invoke_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_latency_ms: float = 0.0
    last_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    last_error: str = ""
    last_called_at: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        if self.invoke_count == 0:
            return 0.0
        return round(self.total_latency_ms / self.invoke_count, 1)

    @property
    def error_rate(self) -> float:
        if self.invoke_count == 0:
            return 0.0
        return round(self.error_count / self.invoke_count, 4)

    def record_success(self, latency_ms: float) -> None:
        self.invoke_count += 1
        self.success_count += 1
        self.total_latency_ms += latency_ms
        self.last_latency_ms = latency_ms
        self.max_latency_ms = max(self.max_latency_ms, latency_ms)
        self.last_called_at = time.time()

    def record_error(self, error: str, latency_ms: float = 0) -> None:
        self.invoke_count += 1
        self.error_count += 1
        self.total_latency_ms += latency_ms
        self.last_latency_ms = latency_ms
        self.max_latency_ms = max(self.max_latency_ms, latency_ms)
        self.last_error = error[:200]
        self.last_called_at = time.time()

    def reset(self) -> None:
        self.invoke_count = 0
        self.success_count = 0
        self.error_count = 0
        self.total_latency_ms = 0.0
        self.last_latency_ms = 0.0
        self.max_latency_ms = 0.0
        self.last_error = ""
        self.last_called_at = 0.0

    def to_dict(self) -> dict:
        return {
            "invoke_count": self.invoke_count,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "avg_latency_ms": self.avg_latency_ms,
            "last_latency_ms": self.last_latency_ms,
            "max_latency_ms": self.max_latency_ms,
            "error_rate": self.error_rate,
            "last_error": self.last_error,
            "last_called_at": self.last_called_at,
        }


class Monitor:
    """Skill 监控器

    用法:
        monitor = Monitor()
        monitor.before_call("bidding")
        # ... execute ...
        monitor.after_call("bidding", success=True, latency_ms=150)

        # 获取统计
        stats = monitor.get_stats("bidding")

        # 写入数据库
        await monitor.flush_to_db()
    """

    def __init__(self):
        self._metrics: dict[str, SkillMetrics] = defaultdict(SkillMetrics)
        self._timers: dict[str, float] = {}
        # P2 (R11): 已落库的累计基准 {skill_name: (invoke, success, total_latency_ms)}。
        # flush_to_db 改增量累加后，据此计算"自上次落库以来的增量"，避免同进程重复
        # flush 时 ON CONFLICT 累加造成重复计数。
        self._flushed_counts: dict[str, tuple[int, int, float]] = {}

    def before_call(self, skill_name: str) -> None:
        """记录调用开始时间"""
        self._timers[skill_name] = time.perf_counter()

    def after_call(
        self,
        skill_name: str,
        success: bool = True,
        error: str = "",
    ) -> None:
        """记录调用结果

        Args:
            skill_name: Skill 名称
            success: 是否成功
            error: 错误消息（仅失败时）
        """
        start = self._timers.pop(skill_name, None)
        latency_ms = 0.0
        if start is not None:
            latency_ms = round((time.perf_counter() - start) * 1000, 1)

        metrics = self._metrics[skill_name]
        if success:
            metrics.record_success(latency_ms)
        else:
            metrics.record_error(error, latency_ms)

    def get_stats(self, skill_name: str) -> dict:
        """获取指定 Skill 的统计"""
        metrics = self._metrics.get(skill_name)
        if metrics is None:
            return {"skill_name": skill_name, "invoke_count": 0}
        result = metrics.to_dict()
        result["skill_name"] = skill_name

        # 尝试获取 RSS
        try:
            result["rss_bytes"] = self._get_rss(skill_name)
        except Exception:
            result["rss_bytes"] = 0

        return result

    def get_all_stats(self) -> list[dict]:
        """获取所有 Skill 的统计"""
        return [self.get_stats(name) for name in self._metrics]

    def reset(self, skill_name: str | None = None) -> None:
        """重置统计"""
        if skill_name:
            metrics = self._metrics.get(skill_name)
            if metrics:
                metrics.reset()
            # P2 (R11): 计数归零后落库基准同步清零，下一轮 flush 按全量增量写入
            self._flushed_counts.pop(skill_name, None)
        else:
            for m in self._metrics.values():
                m.reset()
            self._flushed_counts.clear()

    def _get_rss(self, skill_name: str) -> int:
        """获取子进程 RSS 内存（需要 psutil）"""
        try:
            import psutil
            # 通过 xihe 运行时查子进程
            from xihe import XiheRuntime
            # 此方法在 XiheRuntime 集成后使用
            return 0
        except ImportError:
            return 0

    # ── 数据库持久化 ──

    async def flush_to_db(self) -> int:
        """将当前统计写入 skill_usage_stats 表

        P2 (R11) 修复说明：
          - 原 date.today() 用本地日期 → 多时区节点把不同"今天"写进同一 stat_date，
            互相覆盖；改用 UTC 日期（与市场/授权体系统一时区口径）。
          - 原 ON CONFLICT 直接覆盖（invoke_count = EXCLUDED.invoke_count）→
            多节点各持部分计数，后写者覆盖先写者 → 丢数据；改为增量加和
            （skill_usage_stats.x + EXCLUDED.x），配合本进程的落库基准
            _flushed_counts 计算自上次落库以来的增量，避免同进程重复 flush 重复计数。
          - avg_latency_ms 用加权平均合并（old_avg*old_n + new_avg*new_n）/ (old_n+new_n）。

        Returns:
            写入的记录数
        """
        pool = await get_pool()
        today = datetime.now(timezone.utc).date()
        count = 0

        async with pool.acquire() as conn:
            for skill_name, metrics in self._metrics.items():
                try:
                    invoke_count = metrics.invoke_count
                    success_count = metrics.success_count
                    total_latency = metrics.total_latency_ms
                    last_invoke, last_success, last_latency = self._flushed_counts.get(
                        skill_name, (0, 0, 0.0))
                    # 计数回退（reset/换天）→ 视作新的增量基准，不为负
                    delta_invoke = max(invoke_count - last_invoke, 0)
                    delta_success = max(success_count - last_success, 0)
                    delta_latency = max(total_latency - last_latency, 0.0)
                    if delta_invoke <= 0 and delta_success <= 0:
                        continue  # 无新增数据，跳过
                    delta_avg = round(delta_latency / delta_invoke, 1) if delta_invoke else 0.0

                    await conn.execute(
                        f"""INSERT INTO {SCHEMA}.skill_usage_stats
                           (skill_name, agent_id, invoke_count, success_count,
                            avg_confidence, avg_latency_ms, stat_date)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)
                           ON CONFLICT (skill_name, agent_id, stat_date)
                           DO UPDATE SET
                               invoke_count = skill_usage_stats.invoke_count + EXCLUDED.invoke_count,
                               success_count = skill_usage_stats.success_count + EXCLUDED.success_count,
                               avg_latency_ms = CASE
                                   WHEN skill_usage_stats.invoke_count + EXCLUDED.invoke_count = 0 THEN 0
                                   ELSE ROUND(
                                       (skill_usage_stats.avg_latency_ms * skill_usage_stats.invoke_count
                                        + EXCLUDED.avg_latency_ms * EXCLUDED.invoke_count)
                                       / (skill_usage_stats.invoke_count + EXCLUDED.invoke_count)
                                   )
                               END""",
                        skill_name,
                        "all",
                        delta_invoke,
                        delta_success,
                        0.0,  # avg_confidence（暂不填充）
                        int(delta_avg),
                        today,
                    )
                    count += 1
                    self._flushed_counts[skill_name] = (invoke_count, success_count, total_latency)
                except Exception as e:
                    logger.warning(
                        "Failed to flush metrics for '%s': %s",
                        skill_name, e,
                    )

        if count:
            logger.info("Flushed %d skill stats to DB", count)
        return count
