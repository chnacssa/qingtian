"""审批服务 —— 拦截 high/critical 操作，通知通道审批。"""

import json
import uuid
import logging
from typing import Callable

import asyncpg

from . import config as cfg
from .audit_service import write_audit
from .alert_service import alert_channel

logger = logging.getLogger("zhenyue.approval")


async def create_approval(conn: asyncpg.Connection, agent_id: str, action: str,
                          target_type: str = "", target_id: str = "",
                          severity: str = "high", pending_request: dict | None = None,
                          caller_channel: str = "") -> dict:
    schema = cfg.get_schema_name()
    request_id = uuid.uuid4()

    chain = cfg.get_approver_chains().get(action, [])
    if not chain:
        chain = cfg.get_approver_chains().get("critical", [])

    # 计算 TTL
    ttl_map = cfg.get_approval_ttl()
    ttl_seconds = ttl_map.get(severity, ttl_map.get("high", 3600))
    pending_json = json.dumps(pending_request) if pending_request else None

    await conn.execute(
        f"INSERT INTO {schema}.approvals (request_id, agent_id, action, target_type, target_id, "
        f"severity, status, approver_chain, created_at, expires_at, pending_request) "
        f"VALUES ($1,$2,$3,$4,$5,$6,'pending',$7,NOW(),NOW() + interval '1 second' * $8,$9)",
        request_id, agent_id, action, target_type, target_id, severity,
        json.dumps(chain), ttl_seconds, pending_json,
    )

    await write_audit(conn, {
        "agent_id": agent_id,
        "agent_role": "agent",
        "action": f"approval_request:{action}",
        "target_type": target_type,
        "target_id": target_id,
        "severity": severity,
        "detail": {"request_id": str(request_id), "ttl_seconds": ttl_seconds},
        "approval_status": "pending",
        "approval_chain": chain,
    })

    # 审批通知由 Gateway 插件层发送（插件有飞书/微信 Bot 通道），
    # Python 后端不持有 Gateway Bot 的访问能力，此处仅记录。
    # 插件创建审批后，自行调用 Gateway API 发送卡片通知。

    return {
        "request_id": str(request_id),
        "status": "pending",
        "expires_at_seconds": ttl_seconds,
    }


async def auto_reject_expired(conn: asyncpg.Connection) -> int:
    """自动拒绝所有已过期的待处理审批。返回拒绝数量。"""
    schema = cfg.get_schema_name()
    rows = await conn.fetch(
        f"UPDATE {schema}.approvals SET status = 'rejected', "
        f"resolved_at = NOW(), resolution = 'auto_rejected:expired' "
        f"WHERE status = 'pending' AND expires_at < NOW() "
        f"RETURNING request_id, agent_id, action, severity"
    )
    for row in rows:
        await write_audit(conn, {
            "agent_id": row["agent_id"],
            "agent_role": "system",
            "action": f"approval_expired:{row['action']}",
            "severity": row["severity"],
            "detail": {"request_id": row["request_id"]},
            "approval_status": "rejected",
        })
    return len(rows)


async def approve_and_execute(
    conn: asyncpg.Connection,
    request_id: str,
    approver: str,
    execute_func: Callable | None = None,
    comment: str = "",
    delay_seconds: int = 0,
) -> dict:
    """审批通过 + 延迟执行（反悔窗口）。

    若 delay_seconds > 0，设置 scheduled_execute_at = NOW() + delay，
    期间可通过 cancel_approval() 取消。到期后由 execute_scheduled_approvals() 执行。
    若 delay_seconds = 0，立即执行（向后兼容）。
    """
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT * FROM {schema}.approvals WHERE request_id = $1", request_id
    )
    if row is None:
        return {"status": "not_found"}
    if row["status"] != "pending":
        return {"status": row["status"]}

    # 1. 更新为 approved
    resolution_text = f"approved by {approver}" + (f": {comment}" if comment else "")
    if delay_seconds > 0:
        await conn.execute(
            f"UPDATE {schema}.approvals SET status = 'approved', resolved_at = NOW(), "
            f"scheduled_execute_at = NOW() + interval '1 second' * $1, "
            f"resolution = $2 WHERE request_id = $3",
            delay_seconds, resolution_text, request_id,
        )
    else:
        await conn.execute(
            f"UPDATE {schema}.approvals SET status = 'approved', resolved_at = NOW(), "
            f"resolution = $1 WHERE request_id = $2",
            resolution_text, request_id,
        )

    await write_audit(conn, {
        "agent_id": row["agent_id"],
        "agent_role": "agent",
        "action": f"approval_approved:{row['action']}",
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "severity": row["severity"],
        "detail": {"request_id": request_id, "approver": approver, "comment": comment,
                   "delay_seconds": delay_seconds},
        "approval_status": "approved",
    })

    # 2. 无延迟 → 立即执行
    if delay_seconds == 0 and execute_func and row["pending_request"]:
        try:
            pending = json.loads(row["pending_request"]) if isinstance(row["pending_request"], str) else row["pending_request"]
            result = await execute_func(pending)
            await conn.execute(
                f"UPDATE {schema}.approvals SET executed_at = NOW() WHERE request_id = $1",
                request_id,
            )
            await write_audit(conn, {
                "agent_id": row["agent_id"],
                "agent_role": "system",
                "action": f"approval_executed:{row['action']}",
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "severity": row["severity"],
                "detail": {"request_id": request_id, "result": str(result)[:500]},
                "approval_status": "executed",
            })
        except Exception as e:
            logger.error(f"Re-issue failed for {request_id}: {e}")
            result = {"error": str(e)}
            return {
                "request_id": request_id,
                "status": "approved",
                "approver": approver,
                "result": result,
            }

    return {
        "request_id": request_id,
        "status": "approved",
        "approver": approver,
        "delay_seconds": delay_seconds,
        "scheduled_execute_at": (
            row["scheduled_execute_at"].isoformat()
            if delay_seconds > 0 and hasattr(row, "scheduled_execute_at") and row.get("scheduled_execute_at")
            else None
        ),
    }


async def cancel_approval(conn: asyncpg.Connection, request_id: str,
                          cancelled_by: str = "admin") -> dict:
    """取消已批准但未执行的审批（反悔窗口内）。"""
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT * FROM {schema}.approvals WHERE request_id = $1", request_id
    )
    if row is None:
        return {"status": "not_found"}
    if row["status"] != "approved" or row["executed_at"] is not None:
        return {"status": row["status"], "error": "只能在批准后、执行前取消"}

    await conn.execute(
        f"UPDATE {schema}.approvals SET status = 'cancelled', resolved_at = NOW(), "
        f"resolution = $1 WHERE request_id = $2",
        f"cancelled by {cancelled_by}", request_id,
    )

    await write_audit(conn, {
        "agent_id": row["agent_id"],
        "agent_role": "admin",
        "action": f"approval_cancelled:{row['action']}",
        "severity": row["severity"],
        "detail": {"request_id": request_id, "cancelled_by": cancelled_by},
        "approval_status": "cancelled",
    })

    return {"request_id": request_id, "status": "cancelled"}


async def execute_scheduled_approvals(
    conn: asyncpg.Connection,
    execute_func: Callable | None = None,
) -> int:
    """执行所有到期但未执行的批准（scheduled_execute_at <= NOW()）。

    P1 (2026-08-26 review #10): 原实现解析 pending_request 后注释称"通过内部 re-issue
    执行"，实际只 UPDATE executed_at + 写审计——没有任何执行调用，延迟执行的审批到期后
    全部静默空转（7 天反悔窗口形同虚设）。现与 approve_and_execute 立即路径同构：

    - execute_func 由调用方注入（与立即执行路径同一 Callable 约定，入参 pending dict），
      有则真实执行，成功才标 executed_at；
    - 无 execute_func（调用方未提供执行能力）→ **不再假装成功**：保持 approved 不标
      executed_at，写 high 级审计 approval_execution_missed + error 日志，等待调用方
      补执行能力后重试（行仍在到期集合内，幂等重扫）；
    - 执行抛异常 → 同样不标成功，审计记录失败原因。
    """
    schema = cfg.get_schema_name()
    rows = await conn.fetch(
        f"SELECT * FROM {schema}.approvals "
        f"WHERE status = 'approved' AND executed_at IS NULL "
        f"AND scheduled_execute_at IS NOT NULL "
        f"AND scheduled_execute_at <= NOW()"
    )
    executed = 0
    for row in rows:
        if not row["pending_request"]:
            await conn.execute(
                f"UPDATE {schema}.approvals SET executed_at = NOW() WHERE request_id = $1",
                row["request_id"],
            )
            executed += 1
            continue
        pending = json.loads(row["pending_request"]) if isinstance(row["pending_request"], str) else row["pending_request"]
        if execute_func is None:
            # 无执行能力：明确暴露而非空转骗人（status 枚举无 failed，保持 approved 待重试）
            logger.error(
                "Scheduled approval %s到期但未提供 execute_func，跳过（不标已执行，待补执行能力后重试）",
                row["request_id"],
            )
            await write_audit(conn, {
                "agent_id": row["agent_id"],
                "agent_role": "system",
                "action": f"approval_execution_missed:{row['action']}",
                "severity": "high",
                "detail": {"request_id": row["request_id"], "reason": "no execute_func provided"},
                "approval_status": "approved",
            })
            continue
        try:
            result = await execute_func(pending)
            await conn.execute(
                f"UPDATE {schema}.approvals SET executed_at = NOW() WHERE request_id = $1",
                row["request_id"],
            )
            executed += 1
            await write_audit(conn, {
                "agent_id": row["agent_id"],
                "agent_role": "system",
                "action": f"approval_executed_scheduled:{row['action']}",
                "severity": row["severity"],
                "detail": {"request_id": row["request_id"], "result": str(result)[:500]},
                "approval_status": "executed",
            })
        except Exception as e:
            logger.error(f"Scheduled execution failed for {row['request_id']}: {e}")
            await write_audit(conn, {
                "agent_id": row["agent_id"],
                "agent_role": "system",
                "action": f"approval_execution_failed:{row['action']}",
                "severity": "high",
                "detail": {"request_id": row["request_id"], "error": str(e)[:500]},
                "approval_status": "approved",
            })
    return executed


async def resolve_approval(conn: asyncpg.Connection, request_id: str,
                           decision: str, approver: str, comment: str = "") -> dict:
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT * FROM {schema}.approvals WHERE request_id = $1", request_id
    )
    if row is None:
        return {"status": "not_found"}
    if row["status"] != "pending":
        return {"status": row["status"]}

    new_status = "approved" if decision == "approved" else "rejected"
    await conn.execute(
        f"UPDATE {schema}.approvals SET status = $1, resolved_at = NOW(), resolution = $2 WHERE request_id = $3",
        new_status, f"{decision} by {approver}", request_id,
    )

    await write_audit(conn, {
        "agent_id": row["agent_id"],
        "agent_role": "agent",
        "action": f"approval_{new_status}:{row['action']}",
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "severity": row["severity"],
        "detail": {"request_id": request_id, "approver": approver, "comment": comment},
        "approval_status": new_status,
    })

    return {
        "request_id": request_id,
        "status": new_status,
        "approver": approver,
        "agent_id": row["agent_id"],
        "action": row["action"],
    }


def check_approval_required(action: str) -> dict:
    severity_map = {
        "delete_agent": "high",
        "modify_agent_role": "high",
        "cancel_agreement": "high",
        "create_agreement": "high",
        "sign_agreement": "high",
        "register_agent": "medium",
        "approve_agent_registration": "high",
        "suspend_agent": "critical",
        "mass_suspend": "critical",
        "approve_transaction_50w_plus": "high",
        "approve_transaction_100w_plus": "critical",
        "system_config": "critical",
        "reset_all_agents": "critical",
    }
    severity = severity_map.get(action, "low")
    requires_approval = severity in ("high", "critical")
    return {
        "action": action,
        "severity": severity,
        "requires_approval": requires_approval,
    }


async def escalate_approval(conn: asyncpg.Connection, request_id: str) -> dict:
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT * FROM {schema}.approvals WHERE request_id = $1 AND status = 'pending'",
        request_id,
    )
    if row is None:
        return {"status": "not_found"}

    chain = json.loads(row["approver_chain"]) if isinstance(row["approver_chain"], str) else row["approver_chain"]
    current = row["current_level"] + 1

    if current >= len(chain) or chain[current].get("fallback") in ("auto_deny", "auto_reject"):
        return await resolve_approval(conn, request_id, "rejected", "system:auto_deny")

    await conn.execute(
        f"UPDATE {schema}.approvals SET current_level = $1 WHERE request_id = $2",
        current, request_id,
    )

    next_step = chain[current]
    try:
        await alert_channel.send_approval({
            "request_id": request_id,
            "agent_id": row["agent_id"],
            "action": row["action"],
            "severity": row["severity"],
            "escalated": True,
            "level": current,
            "target_role": next_step.get("role", ""),
        })
    except Exception:
        pass

    return {"status": "escalated", "level": current}
