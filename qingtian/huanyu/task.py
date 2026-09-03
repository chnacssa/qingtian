"""
Task 任务模型 — 对标 GB/Z 185.6 第7章

GB/Z 185.6 定义了 Session → Task → Message → Data 四层结构。
Task 状态机: accepted → in_progress → completed/failed/cancelled，中间可发送 progress_info。
"""

from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("huanyu.task")


# ── Task 状态枚举 ──────────────────────────────

class TaskState(str, Enum):
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    PROGRESS_INFO = "progress_info"   # 中间进度更新
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Task 状态机 ──────────────────────────────

_TASK_TRANSITIONS = {
    TaskState.ACCEPTED:       {TaskState.IN_PROGRESS, TaskState.CANCELLED},
    TaskState.IN_PROGRESS:    {TaskState.PROGRESS_INFO, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PROGRESS_INFO:  {TaskState.IN_PROGRESS, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.COMPLETED:      set(),
    TaskState.FAILED:         set(),
    TaskState.CANCELLED:      set(),
}


class TaskStateMachine:
    """Task 状态机 — 状态转移验证"""

    @staticmethod
    def can_transition(current: TaskState, target: TaskState) -> bool:
        return target in _TASK_TRANSITIONS.get(current, set())

    @staticmethod
    def transition(current: TaskState, target: TaskState) -> TaskState:
        if not TaskStateMachine.can_transition(current, target):
            raise ValueError(f"Task 状态不可从 {current.value} 转移到 {target.value}")
        return target


# ── Task 模型 ──────────────────────────────

class Task(BaseModel):
    """GB/Z 185.6 第7章 Task 模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Task 唯一标识")
    session_id: str = Field(..., description="所属 Session ID")
    state: TaskState = Field(default=TaskState.ACCEPTED, description="当前状态")
    state_changed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="状态变更时间"
    )
    messages: list[str] = Field(default_factory=list, description="关联的消息 ID 列表")
    artifacts: list[dict[str, Any]] = Field(default_factory=list, description="工作成果列表")
    progress_description: str = Field(default="", description="进度描述（progress_info 状态时必填）")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def update_state(self, new_state: TaskState, progress: str = "") -> None:
        TaskStateMachine.transition(self.state, new_state)
        self.state = new_state
        self.state_changed_at = datetime.now(timezone.utc).isoformat()
        self.updated_at = self.state_changed_at
        if new_state == TaskState.PROGRESS_INFO and progress:
            self.progress_description = progress
        logger.info("Task %s: %s → %s", self.id[:8], self.state.value if hasattr(self.state, 'value') else self.state, new_state.value)

    def add_artifact(self, artifact_type: str, uri: str, description: str = "") -> None:
        self.artifacts.append({
            "type": artifact_type,
            "uri": uri,
            "description": description,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
