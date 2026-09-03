"""
司库 — 财务审计日志（哈希链，不可篡改）

每条资金变动的关键决策节点都必须写入 finance_audit 表。
表由数据库触发器强制保护：hash 链断裂拒绝插入，禁止 UPDATE/DELETE。

用法:
    from siku.audit import write_finance_audit

    await write_finance_audit(conn, {
        "agent_id": "finance-agent-001",
        "action": "outgoing_forward",
        "event_type": "payment_notify",
        "target_id": "txn_123",
        "amount_fen": 99600,
        "severity": "high",
        "detail": {"from_ain": "...", "to_ain": "...", "message_id": "..."},
    })
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

from common.db import get_pool
from . import config as cfg

SCHEMA = cfg.get_schema_name()
GENESIS = "0" * 64

logger = logging.getLogger("siku.audit")


def _now_iso() -> str:
    # strftime 而非 isoformat()：DB trigger 期望 'Z' 后缀而非 '+00:00'
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _compute_audit_hash(
    prev_hash: str, agent_id: str, action: str, event_type: str,
    created_at_iso: str, detail_json: str,
) -> str:
    raw = f"{prev_hash}:{agent_id}:{action}:{event_type}:{created_at_iso}:{detail_json}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def get_prev_audit_hash(conn) -> str:
    prev = await conn.fetchval(
        f"SELECT row_hash FROM {SCHEMA}.finance_audit ORDER BY id DESC LIMIT 1"
    )
    return prev or GENESIS


async def write_finance_audit(conn, entry: dict) -> dict:
    """写入一条财务审计记录到 finance_audit 哈希链。

    必需字段:
        agent_id  — 执行操作的 agent ID
        action    — 操作类型 (recharge/deduct/outgoing_forward/incoming_confirm/...)
        event_type — 事件分类 (payment_notify/payment_confirm/challenge/annual_fee/...)
        severity  — info/warning/high/critical

    可选字段:
        target_id — 关联对象 ID (agent_id/txn_id/ain)
        amount_fen — 涉及金额（分）
        detail    — 结构化详情 dict
    """
    try:
        prev_hash = await get_prev_audit_hash(conn)
        created_at_dt = datetime.now(timezone.utc)
        created_at_iso = created_at_dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        detail = entry.get("detail") or {}
        detail_json = json.dumps(detail, sort_keys=True, ensure_ascii=False)

        row_hash = _compute_audit_hash(
            prev_hash,
            entry["agent_id"],
            entry["action"],
            entry.get("event_type", ""),
            created_at_iso,
            detail_json,
        )

        row = await conn.fetchrow(
            f"INSERT INTO {SCHEMA}.finance_audit "
            f"(agent_id, action, event_type, target_id, amount_fen, severity, "
            f"detail, prev_hash, row_hash, created_at) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) "
            f"RETURNING id, agent_id, action, event_type, amount_fen, severity, row_hash, created_at",
            entry["agent_id"],
            entry["action"],
            entry.get("event_type", ""),
            entry.get("target_id", ""),
            entry.get("amount_fen", 0),
            entry.get("severity", "info"),
            detail_json,
            prev_hash,
            row_hash,
            created_at_dt,
        )

        result = dict(row)
        logger.info(
            "审计: agent=%s action=%s event=%s severity=%s hash=%s...",
            entry["agent_id"], entry["action"],
            entry.get("event_type", ""), entry.get("severity", "info"),
            row_hash[:12],
        )
        return result
    except Exception:
        logger.exception(
            "Audit write failed (non-fatal): agent=%s action=%s — "
            "hash validation may have rejected the record. "
            "Check DB trigger hash format vs Python isoformat().",
            entry.get("agent_id", "?"),
            entry.get("action", "?"),
        )
        return {"error": "audit_write_failed", "agent_id": entry.get("agent_id"),
                "action": entry.get("action"), "status": "logged_only"}


async def verify_finance_audit_chain(conn) -> dict:
    """校验 finance_audit 哈希链完整性。

    逐行验证 prev_hash 链接和 row_hash 正确性。
    任何断裂或篡改都会被检测到。
    """
    rows = await conn.fetch(
        f"SELECT id, agent_id, action, event_type, target_id, amount_fen, "
        f"severity, detail, prev_hash, row_hash, created_at "
        f"FROM {SCHEMA}.finance_audit ORDER BY id"
    )
    if not rows:
        return {"valid": True, "total_records": 0}

    prev_hash = GENESIS
    for row in rows:
        if row["prev_hash"] != prev_hash:
            return {
                "valid": False,
                "broken_at_id": row["id"],
                "reason": "prev_hash chain broken",
                "expected_prev_hash": prev_hash,
                "stored_prev_hash": row["prev_hash"],
            }
        detail = row["detail"] or "{}"
        if isinstance(detail, dict):
            detail = json.dumps(detail, sort_keys=True, ensure_ascii=False)
        created_at_iso = row["created_at"].strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        computed = _compute_audit_hash(
            prev_hash, row["agent_id"], row["action"], row["event_type"],
            created_at_iso, detail,
        )
        if computed != row["row_hash"]:
            return {
                "valid": False,
                "broken_at_id": row["id"],
                "reason": "row_hash mismatch (data tampered)",
                "expected_hash": computed,
                "stored_hash": row["row_hash"],
            }
        prev_hash = row["row_hash"]

    return {"valid": True, "total_records": len(rows)}


async def query_finance_audit(
    conn,
    agent_id: str = "",
    action: str = "",
    event_type: str = "",
    severity: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """查询财务审计日志，支持多条件过滤。"""
    clauses = []
    params = []
    idx = 1

    if agent_id:
        clauses.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1
    if action:
        clauses.append(f"action = ${idx}")
        params.append(action)
        idx += 1
    if event_type:
        clauses.append(f"event_type = ${idx}")
        params.append(event_type)
        idx += 1
    if severity:
        clauses.append(f"severity = ${idx}")
        params.append(severity)
        idx += 1

    where = " AND ".join(clauses) if clauses else "TRUE"
    params.extend([limit, offset])

    rows = await conn.fetch(
        f"SELECT id, agent_id, action, event_type, target_id, amount_fen, "
        f"severity, detail, prev_hash, row_hash, created_at "
        f"FROM {SCHEMA}.finance_audit "
        f"WHERE {where} ORDER BY id DESC LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )
    return [dict(r) for r in rows]
