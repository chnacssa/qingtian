"""执策状态机 — 原子状态转换

所有状态变更使用原子 UPDATE + WHERE 条件过滤，
WHERE 子句的 status 条件天然保证互斥——看门狗和 Agent 同时操作
同一个 Step 时，只有一个 UPDATE 能匹配到行（另一个 RETURNING 为空）。
"""
import json
import logging
from datetime import datetime, timezone
from common.db import get_pool
from . import config as cfg

logger = logging.getLogger("zhice.status_machine")
SCHEMA = cfg.get_schema_name()

# 合法状态
VALID_TASK_STATUS = frozenset({"pending", "running", "paused", "completed", "failed", "cancelled"})
VALID_STEP_STATUS = frozenset({"pending", "assigned", "in_progress", "completed", "failed", "timed_out", "rejected", "retry", "skipped", "cancelled"})
STEP_TERMINAL = frozenset({"completed", "failed", "skipped", "cancelled"})


def _now():
    return datetime.now(timezone.utc)


# ── Step 状态转换 ─────────────────────────────────────────

async def step_assign(conn, step_id: int, agent_id: str) -> dict | None:
    """pending → assigned"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'assigned', assigned_agent = $2, "
        f"assigned_at = NOW(), updated_at = NOW() "
        f"WHERE step_id = $1 AND status = 'pending' RETURNING *",
        step_id, agent_id,
    )
    return dict(row) if row else None


async def step_start(conn, step_id: int, agent_id: str) -> dict | None:
    """assigned → in_progress（校验 caller == assigned_agent）"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'in_progress', status_reason = 'executing', "
        f"started_at = NOW(), updated_at = NOW() "
        f"WHERE step_id = $1 AND status = 'assigned' AND assigned_agent = $2 "
        f"RETURNING *",
        step_id, agent_id,
    )
    return dict(row) if row else None


async def step_heartbeat(conn, step_id: int, agent_id: str,
                         status_reason: str = "executing") -> dict | None:
    """更新心跳时间戳 + status_reason"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET last_heartbeat_at = NOW(), status_reason = $3, "
        f"updated_at = NOW() "
        f"WHERE step_id = $1 AND status = 'in_progress' AND assigned_agent = $2 "
        f"RETURNING step_id, status, status_reason, last_heartbeat_at",
        step_id, agent_id, status_reason,
    )
    return dict(row) if row else None


async def step_complete(conn, step_id: int, agent_id: str, summary: str,
                        outputs: dict) -> dict | None:
    """in_progress → completed"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'completed', status_reason = NULL, "
        f"summary = $3, outputs = $4, completed_at = NOW(), updated_at = NOW() "
        f"WHERE step_id = $1 AND status = 'in_progress' AND assigned_agent = $2 "
        f"RETURNING *",
        step_id, agent_id, summary,
        json.dumps(outputs, ensure_ascii=False) if isinstance(outputs, dict) else outputs,
    )
    return dict(row) if row else None


async def step_fail(conn, step_id: int, agent_id: str, summary: str) -> dict | None:
    """in_progress → failed（Agent 自己报告失败）"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'failed', status_reason = NULL, "
        f"summary = $3, completed_at = NOW(), updated_at = NOW() "
        f"WHERE step_id = $1 AND status = 'in_progress' AND assigned_agent = $2 "
        f"RETURNING *",
        step_id, agent_id, summary,
    )
    return dict(row) if row else None


async def step_reject(conn, step_id: int, forced: bool = False) -> dict | None:
    """in_progress → rejected（检查不通过）或 forced 时任意状态 → rejected"""
    if forced:
        row = await conn.fetchrow(
            f"UPDATE {SCHEMA}.steps SET status = 'rejected', status_reason = NULL, "
            f"updated_at = NOW() "
            f"WHERE step_id = $1 AND status NOT IN ('completed', 'cancelled', 'skipped') "
            f"RETURNING *",
            step_id,
        )
    else:
        row = await conn.fetchrow(
            f"UPDATE {SCHEMA}.steps SET status = 'rejected', status_reason = NULL, "
            f"updated_at = NOW() "
            f"WHERE step_id = $1 AND status = 'in_progress' "
            f"RETURNING *",
            step_id,
        )
    return dict(row) if row else None


async def step_retry(conn, step_id: int) -> dict | None:
    """failed/rejected/timed_out → 按源状态跳转（消耗 auto_retry）

    - failed → in_progress（Agent 重做）
    - rejected → pending（回收重分派）
    - timed_out → pending（回收重分派）

    P1 (R?): rejected 原本 → assigned（assigned_agent=NULL），而认领循环只认
    status='pending' → 被 reject 的 Step 重试后永久孤儿，卡死任务。改回 pending。
    """
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET "
        f"status = CASE status "
        f"  WHEN 'failed' THEN 'in_progress' "
        f"  WHEN 'rejected' THEN 'pending' "
        f"  WHEN 'timed_out' THEN 'pending' "
        f"  ELSE 'in_progress' END, "
        f"status_reason = CASE WHEN status = 'failed' THEN 'executing' ELSE NULL END, "
        f"assigned_agent = CASE WHEN status != 'failed' THEN NULL ELSE assigned_agent END, "
        f"assigned_at = CASE WHEN status != 'failed' THEN NULL ELSE assigned_at END, "
        f"auto_retry = auto_retry - 1, updated_at = NOW() "
        f"WHERE step_id = $1 AND status IN ('failed', 'rejected', 'timed_out') "
        f"AND auto_retry > 0 "
        f"RETURNING *",
        step_id,
    )
    return dict(row) if row else None


async def step_timeout(conn, step_id: int) -> dict | None:
    """assigned | in_progress → timed_out（看门狗触发）"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'timed_out', status_reason = NULL, "
        f"completed_at = NOW(), updated_at = NOW() "
        f"WHERE step_id = $1 AND status IN ('assigned', 'in_progress') "
        f"RETURNING *",
        step_id,
    )
    return dict(row) if row else None


async def step_skip(conn, step_id: int) -> dict | None:
    """pending → skipped（前置步骤失败导致跳过）"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'skipped', completed_at = NOW(), "
        f"updated_at = NOW() "
        f"WHERE step_id = $1 AND status = 'pending' "
        f"RETURNING *",
        step_id,
    )
    return dict(row) if row else None


async def step_reject_reset(conn, step_id: int, reset_retry: int = 1) -> dict | None:
    """创建者手动打回：failed/rejected/timed_out → pending（重置 auto_retry）

    原子操作：一步完成 reject→pending + 重置 auto_retry + 释放 assigned_agent
    """
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'pending', status_reason = NULL, "
        f"auto_retry = $2, assigned_agent = NULL, assigned_at = NULL, "
        f"updated_at = NOW() "
        f"WHERE step_id = $1 AND status IN ('failed', 'rejected', 'timed_out') "
        f"RETURNING *",
        step_id, reset_retry,
    )
    return dict(row) if row else None


async def step_cancel(conn, step_id: int) -> dict | None:
    """pending | assigned | in_progress → cancelled"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET status = 'cancelled', status_reason = NULL, "
        f"completed_at = NOW(), updated_at = NOW() "
        f"WHERE step_id = $1 AND status IN ('pending', 'assigned', 'in_progress') "
        f"RETURNING *",
        step_id,
    )
    return dict(row) if row else None


async def step_confirm(conn, step_id: int, confirmed_by: str) -> dict | None:
    """C6 (R11): 高风险 Step 人工确认——登记确认人与确认时间。

    调用方（/steps/{id}/confirm）已校验 confirmation_required 与任务状态；
    此处幂等：已确认的 step 不重复覆盖 confirmed_by。"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.steps SET confirmed_by = $2, confirmed_at = NOW(), "
        f"updated_at = NOW() "
        f"WHERE step_id = $1 AND (confirmed_by IS NULL OR confirmed_by = '') "
        f"RETURNING *",
        step_id, confirmed_by,
    )
    return dict(row) if row else None


# ── Task 状态转换 ─────────────────────────────────────────

async def task_start(conn, task_id: int) -> dict | None:
    """pending → running"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.tasks SET status = 'running', started_at = NOW(), "
        f"updated_at = NOW() "
        f"WHERE task_id = $1 AND status = 'pending' "
        f"RETURNING *",
        task_id,
    )
    return dict(row) if row else None


async def task_complete(conn, task_id: int, result: str = "") -> dict | None:
    """running → completed"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.tasks SET status = 'completed', progress = 100, "
        f"result = $2, completed_at = NOW(), updated_at = NOW() "
        f"WHERE task_id = $1 AND status = 'running' "
        f"RETURNING *",
        task_id, result,
    )
    return dict(row) if row else None


async def task_fail(conn, task_id: int, result: str = "") -> dict | None:
    """running → failed"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.tasks SET status = 'failed', result = $2, "
        f"completed_at = NOW(), updated_at = NOW() "
        f"WHERE task_id = $1 AND status = 'running' "
        f"RETURNING *",
        task_id, result,
    )
    return dict(row) if row else None


async def task_pause(conn, task_id: int, reason: str = "") -> dict | None:
    """running → paused"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.tasks SET status = 'paused', "
        f"result = COALESCE(result, '') || $2, "
        f"updated_at = NOW() "
        f"WHERE task_id = $1 AND status = 'running' "
        f"RETURNING *",
        task_id, f"[paused] {reason}" if reason else "[paused]",
    )
    return dict(row) if row else None


async def task_resume(conn, task_id: int) -> dict | None:
    """paused → running"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.tasks SET status = 'running', "
        f"updated_at = NOW() "
        f"WHERE task_id = $1 AND status = 'paused' "
        f"RETURNING *",
        task_id,
    )
    return dict(row) if row else None


async def task_cancel(conn, task_id: int) -> dict | None:
    """任意非终态 → cancelled"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.tasks SET status = 'cancelled', completed_at = NOW(), "
        f"updated_at = NOW() "
        f"WHERE task_id = $1 AND status NOT IN ('completed', 'failed', 'cancelled') "
        f"RETURNING *",
        task_id,
    )
    return dict(row) if row else None


async def task_update_progress(conn, task_id: int) -> dict | None:
    """根据所有 Step 状态计算进度：completed / total * 100"""
    row = await conn.fetchrow(
        f"UPDATE {SCHEMA}.tasks SET "
        f"progress = ("
        f"  SELECT COUNT(*) FILTER (WHERE status IN ('completed', 'skipped')) * 100 / "
        f"  GREATEST(COUNT(*), 1) "
        f"  FROM {SCHEMA}.steps WHERE task_id = $1"
        f"), updated_at = NOW() "
        f"WHERE task_id = $1 "
        f"RETURNING *",
        task_id,
    )
    return dict(row) if row else None


# ── 辅助查询 ─────────────────────────────────────────────

async def get_step(conn, step_id: int) -> dict | None:
    row = await conn.fetchrow(
        f"SELECT * FROM {SCHEMA}.steps WHERE step_id = $1", step_id,
    )
    return dict(row) if row else None


async def get_task(conn, task_id: int) -> dict | None:
    row = await conn.fetchrow(
        f"SELECT * FROM {SCHEMA}.tasks WHERE task_id = $1", task_id,
    )
    return dict(row) if row else None


async def get_task_steps(conn, task_id: int) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT * FROM {SCHEMA}.steps WHERE task_id = $1 ORDER BY step_index",
        task_id,
    )
    return [dict(r) for r in rows]


async def all_steps_terminal(conn, task_id: int) -> bool:
    """检查 Task 下所有 Step 是否均为终态"""
    row = await conn.fetchrow(
        f"SELECT COUNT(*) FILTER (WHERE status NOT IN ('completed', 'failed', 'skipped', 'cancelled')) "
        f"AS remaining FROM {SCHEMA}.steps WHERE task_id = $1",
        task_id,
    )
    return row["remaining"] == 0 if row else True
