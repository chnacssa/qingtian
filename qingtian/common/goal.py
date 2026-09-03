"""G2 目标设定与监控 — 轻量 Goal 对象 + 进度追踪（设计文档 §11.4/11.5）

零依赖纯内存：状态转换全同步可单测；持久化走注入的 persist_hook（async），
由 GoalTracker 在每次状态变化后自动调度（asyncio.create_task），
hook 可接 osskill.goals 表 / 内存 / 日志（默认日志）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger("common.goal")

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class Goal:
    """轻量目标对象。状态机：pending → running → done | failed。

    Args:
        goal_id: 唯一标识（tracker.create 自动生成 uuid hex）
        objective: 目标描述
        subgoals: 子目标 [{id, desc, done}]（可选）
        on_change: 状态/进度变化后的同步回调（tracker 注入 persist 调度用）
    """

    goal_id: str
    objective: str
    subgoals: list[dict] = field(default_factory=list)
    status: str = STATUS_PENDING
    progress: float = 0.0           # 0-1，单调递增
    created_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    on_change: Callable[[Goal], None] | None = None

    def __post_init__(self):
        now = time.time()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now  # 快照重建时保留原 updated_at

    def start(self):
        """pending -> running（幂等：非 pending 时跳过，支持复盘重试复用）。"""
        if self.status == STATUS_PENDING:
            self.status = STATUS_RUNNING
            self._touch()

    def update_progress(self, ratio: float):
        """夹取 0-1、单调不倒退（重试轮进度不回退）。"""
        r = max(0.0, min(float(ratio), 1.0))
        if r > self.progress:
            self.progress = r
            self._touch()

    def complete(self):
        """running -> done，progress=1.0。"""
        self.status = STATUS_DONE
        self.progress = 1.0
        self._touch()

    def fail(self, error: str):
        """-> failed，记录失败原因。"""
        self.status = STATUS_FAILED
        self.error = error
        self._touch()

    def _touch(self):
        self.updated_at = time.time()
        if self.on_change:
            try:
                self.on_change(self)
            except Exception as e:  # 回调异常不得阻断状态机
                logger.warning("goal on_change 异常: %s", e)

    def to_dict(self) -> dict:
        return {
            "goal_id": self.goal_id,
            "objective": self.objective,
            "subgoals": self.subgoals,
            "status": self.status,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


class GoalTracker:
    """内存 Goal 仓库。persist_hook 注入异步持久化，状态变化自动触发。"""

    def __init__(self, persist_hook: Callable[[Goal], Awaitable[None]] | None = None):
        self._store: dict[str, Goal] = {}
        self._hook = persist_hook

    def create(self, objective: str, subgoals: list[dict] | None = None) -> Goal:
        """创建 Goal 并登记。自动接线 on_change → persist 调度。"""
        goal = Goal(goal_id=uuid.uuid4().hex, objective=objective,
                    subgoals=list(subgoals or []))
        goal.on_change = self._schedule_persist
        self._store[goal.goal_id] = goal
        return goal

    def get(self, goal_id: str) -> Goal | None:
        return self._store.get(goal_id)

    def list(self, status: str | None = None) -> list[Goal]:
        goals = list(self._store.values())
        if status:
            goals = [g for g in goals if g.status == status]
        return goals

    def _schedule_persist(self, goal: Goal):
        """状态变化后调度异步落库。

        用"变化时刻的快照"而非活的 goal 引用：fire-and-forget 任务若读活引用，
        同一事件循环内多次变化会 coalesce 成终态，丢失中间进度记录。
        无运行中事件循环（如同步单测）→ 退化同步记日志。
        """
        snapshot = goal.to_dict()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.info("[goal] %s → %s (progress=%.2f)", goal.goal_id,
                        goal.status, goal.progress)
            return
        asyncio.create_task(self._persist(snapshot))

    async def _persist(self, snapshot: dict):
        try:
            if self._hook:
                # 传给 hook 的是快照重建的 Goal（含变化时刻的状态）
                await self._hook(Goal(**snapshot))
            else:
                logger.info("[goal] persist %s status=%s progress=%.2f",
                            snapshot["goal_id"], snapshot["status"],
                            snapshot["progress"])
        except Exception as e:
            logger.warning("goal persist 失败: %s", e)
