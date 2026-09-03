"""执策看门狗 — Phase 2 完整版

五类扫描：
  1. assigned 超时未 start → timed_out
  2. in_progress 超时未 submit（含 status_reason 差异化）
  3. 心跳丢失（executing + last_heartbeat 超时）
  4. 分配未响应回收（assigned + assigned_at 超时 → 回收 pending）
  5. Task 超时（running + started_at 超时 → task failed）
"""
import asyncio
import logging
from datetime import datetime, timezone
from common.db import get_pool
from . import config as cfg
from . import status_machine as sm
from .dispatcher import ws_notify

logger = logging.getLogger("zhice.timeout_checker")
SCHEMA = cfg.get_schema_name()

_running = False
_task: asyncio.Task | None = None


def _now():
    return datetime.now(timezone.utc)


async def _scan():
    """扫描超时的 Step 和 Task"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ═══ Scan 1: assigned 超时未 start ═══
        assigned_rows = await conn.fetch(
            f"SELECT s.step_id, s.step_index, s.title, s.task_id, s.assigned_agent, "
            f"s.assigned_at, s.timeout_minutes, s.auto_retry, t.created_by "
            f"FROM {SCHEMA}.steps s "
            f"JOIN {SCHEMA}.tasks t ON s.task_id = t.task_id "
            f"WHERE s.status = 'assigned' "
            f"AND s.assigned_at IS NOT NULL "
            f"AND s.assigned_at + make_interval(mins => s.timeout_minutes) < NOW()",
        )
        for row in assigned_rows:
            result = await sm.step_timeout(conn, row["step_id"])
            if result:
                logger.warning(
                    f"[watchdog] Step {row['step_id']} (task={row['task_id']}, "
                    f"idx={row['step_index']}) assigned→timed_out "
                    f"(agent={row['assigned_agent']})"
                )
                if row["assigned_agent"]:
                    await ws_notify(row["assigned_agent"], "timed_out", {
                        "task_id": row["task_id"],
                        "step_id": row["step_id"],
                        "step_index": row["step_index"],
                        "title": row["title"],
                        "reason": "assigned_timeout",
                    })

                # auto_retry > 0 → 回收为 pending，递减 auto_retry
                if result.get("auto_retry", 0) > 0:
                    await conn.execute(
                        f"UPDATE {SCHEMA}.steps SET status = 'pending', "
                        f"assigned_agent = NULL, assigned_at = NULL, "
                        f"auto_retry = auto_retry - 1, "
                        f"status_reason = NULL, updated_at = NOW() "
                        f"WHERE step_id = $1",
                        row["step_id"],
                    )
                    logger.info(
                        f"[watchdog] Step {row['step_id']} timed_out → pending "
                        f"(auto_retry={result['auto_retry'] - 1})"
                    )
                else:
                    # auto_retry=0: 通知创建者重试耗尽
                    if row["created_by"]:
                        await ws_notify(row["created_by"], "retry_exhausted", {
                            "task_id": row["task_id"],
                            "step_id": row["step_id"],
                            "step_index": row["step_index"],
                            "title": row["title"],
                            "reason": "assigned 超时且 auto_retry 已耗尽",
                        })

        # ═══ Scan 2: in_progress 超时未 submit ═══
        inprog_rows = await conn.fetch(
            f"SELECT s.step_id, s.step_index, s.title, s.task_id, s.assigned_agent, "
            f"s.started_at, s.timeout_minutes, s.status_reason, s.last_heartbeat_at, "
            f"s.auto_retry, t.created_by "
            f"FROM {SCHEMA}.steps s "
            f"JOIN {SCHEMA}.tasks t ON s.task_id = t.task_id "
            f"WHERE s.status = 'in_progress' "
            f"AND s.started_at IS NOT NULL "
            f"AND s.started_at + make_interval(mins => s.timeout_minutes) < NOW()",
        )
        for row in inprog_rows:
            reason = row["status_reason"] or "executing"

            # waiting_input → 3× timeout 宽容
            if reason == "waiting_input":
                timeout = (row["timeout_minutes"] or 30) * 3
                elapsed = (_now() - row["started_at"]).total_seconds() / 60
                if elapsed < timeout:
                    continue

            # blocked → 不按 Step 超时，但 10 分钟无心跳或 2× timeout 后仍 timed_out
            if reason == "blocked":
                if row["last_heartbeat_at"]:
                    since_beat = (_now() - row["last_heartbeat_at"]).total_seconds()
                    if since_beat < 600:
                        continue
                else:
                    # 无心跳数据：用 2× timeout 作为 fallback
                    fallback = (row["timeout_minutes"] or 30) * 2
                    elapsed = (_now() - row["started_at"]).total_seconds() / 60
                    if elapsed < fallback:
                        continue

            result = await sm.step_timeout(conn, row["step_id"])
            if result:
                logger.warning(
                    f"[watchdog] Step {row['step_id']} (task={row['task_id']}, "
                    f"idx={row['step_index']}) in_progress→timed_out "
                    f"(agent={row['assigned_agent']}, reason={reason})"
                )
                if row["assigned_agent"]:
                    await ws_notify(row["assigned_agent"], "timed_out", {
                        "task_id": row["task_id"],
                        "step_id": row["step_id"],
                        "step_index": row["step_index"],
                        "title": row["title"],
                        "reason": f"execution_timeout ({reason})",
                    })

                if result.get("auto_retry", 0) > 0:
                    await conn.execute(
                        f"UPDATE {SCHEMA}.steps SET status = 'pending', "
                        f"assigned_agent = NULL, assigned_at = NULL, "
                        f"auto_retry = auto_retry - 1, "
                        f"status_reason = NULL, updated_at = NOW() "
                        f"WHERE step_id = $1",
                        row["step_id"],
                    )
                    logger.info(
                        f"[watchdog] Step {row['step_id']} timed_out → pending "
                        f"(auto_retry={result['auto_retry'] - 1})"
                    )
                else:
                    if row["created_by"]:
                        await ws_notify(row["created_by"], "retry_exhausted", {
                            "task_id": row["task_id"],
                            "step_id": row["step_id"],
                            "step_index": row["step_index"],
                            "title": row["title"],
                            "reason": f"execution 超时且 auto_retry 已耗尽 ({reason})",
                        })

        # ═══ Scan 3: 心跳丢失 ═══
        beat_loss = cfg.get_heartbeat_loss_minutes()
        lost_rows = await conn.fetch(
            f"SELECT s.step_id, s.step_index, s.title, s.task_id, s.assigned_agent, "
            f"s.started_at, s.last_heartbeat_at, s.auto_retry, t.created_by "
            f"FROM {SCHEMA}.steps s "
            f"JOIN {SCHEMA}.tasks t ON s.task_id = t.task_id "
            f"WHERE s.status = 'in_progress' "
            f"AND s.status_reason = 'executing' "
            f"AND s.last_heartbeat_at IS NOT NULL "
            f"AND s.last_heartbeat_at + make_interval(mins => $1) < NOW()",
            beat_loss,
        )
        for row in lost_rows:
            result = await sm.step_timeout(conn, row["step_id"])
            if result:
                logger.warning(
                    f"[watchdog] Step {row['step_id']} (task={row['task_id']}, "
                    f"idx={row['step_index']}) 心跳丢失 {beat_loss}min → timed_out"
                )
                if row["assigned_agent"]:
                    await ws_notify(row["assigned_agent"], "timed_out", {
                        "task_id": row["task_id"],
                        "step_id": row["step_id"],
                        "step_index": row["step_index"],
                        "title": row["title"],
                        "reason": "heartbeat_lost",
                    })

                if result.get("auto_retry", 0) > 0:
                    await conn.execute(
                        f"UPDATE {SCHEMA}.steps SET status = 'pending', "
                        f"assigned_agent = NULL, assigned_at = NULL, "
                        f"auto_retry = auto_retry - 1, "
                        f"status_reason = NULL, updated_at = NOW() "
                        f"WHERE step_id = $1",
                        row["step_id"],
                    )
                else:
                    if row["created_by"]:
                        await ws_notify(row["created_by"], "retry_exhausted", {
                            "task_id": row["task_id"],
                            "step_id": row["step_id"],
                            "step_index": row["step_index"],
                            "title": row["title"],
                            "reason": "心跳丢失且 auto_retry 已耗尽",
                        })

        # ═══ Scan 4: 分配未响应回收 ═══
        assign_timeout = cfg.get_assignment_timeout_minutes()
        unclaimed = await conn.fetch(
            f"SELECT s.step_id, s.step_index, s.title, s.task_id, s.assigned_agent, "
            f"s.assigned_at, t.created_by "
            f"FROM {SCHEMA}.steps s "
            f"JOIN {SCHEMA}.tasks t ON s.task_id = t.task_id "
            f"WHERE s.status = 'assigned' "
            f"AND s.assigned_at IS NOT NULL "
            f"AND s.assigned_at + make_interval(mins => $1) < NOW()",
            assign_timeout,
        )
        for row in unclaimed:
            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET status = 'pending', "
                f"assigned_agent = NULL, assigned_at = NULL, "
                f"status_reason = NULL, updated_at = NOW() "
                f"WHERE step_id = $1",
                row["step_id"],
            )
            logger.warning(
                f"[watchdog] Step {row['step_id']} (task={row['task_id']}, "
                f"idx={row['step_index']}) assigned {assign_timeout}min 未响应 → 回收 pending"
            )

            # WS 通知原 assigned_agent + task 创建者
            if row["assigned_agent"]:
                await ws_notify(row["assigned_agent"], "reclaimed", {
                    "task_id": row["task_id"],
                    "step_id": row["step_id"],
                    "step_index": row["step_index"],
                    "title": row["title"],
                    "reason": f"assigned {assign_timeout}min 未 start，已回收",
                })
            if row["created_by"]:
                await ws_notify(row["created_by"], "reclaimed", {
                    "task_id": row["task_id"],
                    "step_id": row["step_id"],
                    "step_index": row["step_index"],
                    "title": row["title"],
                    "reason": f"Step 分配给 {row['assigned_agent']} 后 {assign_timeout}min 未响应，已回收",
                })

        # ═══ Scan 5: Task 超时 ═══
        task_rows = await conn.fetch(
            f"SELECT task_id, title, created_by, started_at, timeout_minutes "
            f"FROM {SCHEMA}.tasks "
            f"WHERE status = 'running' "
            f"AND started_at IS NOT NULL "
            f"AND timeout_minutes IS NOT NULL "
            f"AND started_at + make_interval(mins => timeout_minutes) < NOW()",
        )
        for trow in task_rows:
            # Task → failed
            await conn.execute(
                f"UPDATE {SCHEMA}.tasks SET status = 'failed', "
                f"result = 'Task 超时', completed_at = NOW(), updated_at = NOW() "
                f"WHERE task_id = $1 AND status = 'running'",
                trow["task_id"],
            )

            # 所有非终态 Step → timed_out
            steps = await conn.fetch(
                f"SELECT step_id FROM {SCHEMA}.steps "
                f"WHERE task_id = $1 AND status NOT IN "
                f"('completed', 'failed', 'skipped', 'cancelled', 'timed_out')",
                trow["task_id"],
            )
            for s in steps:
                await sm.step_timeout(conn, s["step_id"])

            logger.warning(
                f"[watchdog] Task {trow['task_id']} '{trow['title']}' 超时 → failed "
                f"({len(steps)} steps → timed_out)"
            )

            if trow["created_by"]:
                await ws_notify(trow["created_by"], "task_failed", {
                    "task_id": trow["task_id"],
                    "title": trow["title"],
                    "reason": "task_timeout",
                })


async def _loop(interval_seconds: int):
    global _running
    _running = True
    logger.info(f"Timeout checker started (interval={interval_seconds}s, Phase 2 full)")
    while _running:
        try:
            await asyncio.sleep(interval_seconds)
            await _scan()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[watchdog] scan error")


async def start():
    """启动看门狗后台循环"""
    global _task
    interval = cfg.get_timeout_check_interval()
    _task = asyncio.create_task(_loop(interval))
    return _task


async def stop():
    """停止看门狗"""
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
