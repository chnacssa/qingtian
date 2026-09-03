"""
司库 — 账户核心服务
SELECT FOR UPDATE 原子操作 + SHA-256 哈希链
"""

import hashlib
import json as _json
import logging
from datetime import datetime, timezone

from common.db import get_pool
from . import config as cfg
from .audit import write_finance_audit

logger = logging.getLogger("siku.account_service")

SCHEMA = cfg.get_schema_name()
GENESIS = cfg.GENESIS_HASH


def _now():
    return datetime.now(timezone.utc)


async def _write_audit_entry(conn, agent_id: str, action: str, event_type: str,
                              severity: str, target_id: str = "", amount_fen: int = 0,
                              detail: dict | None = None):
    """写入 finance_audit 哈希链审计记录（account_service 内部使用）。"""
    try:
        await write_finance_audit(conn, {
            "agent_id": agent_id,
            "action": action,
            "event_type": event_type,
            "target_id": target_id,
            "amount_fen": amount_fen,
            "severity": severity,
            "detail": detail or {},
        })
    except Exception:
        logger.exception("审计写入异常: action=%s", action)


def _compute_txn_hash(prev_hash: str, agent_id: str, txn_type: str,
                      fee_type: str, amount_fen: int, balance_after: int,
                      created_at_iso: str) -> str:
    raw = f"{prev_hash}:{agent_id}:{txn_type}:{fee_type}:{amount_fen}:{balance_after}:{created_at_iso}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def ensure_account(conn, agent_id: str) -> dict:
    row = await conn.fetchrow(
        f"INSERT INTO {SCHEMA}.accounts (agent_id) VALUES ($1) "
        f"ON CONFLICT (agent_id) DO NOTHING "
        f"RETURNING agent_id, balance_fen, frozen_fen, total_recharged, created_at",
        agent_id,
    )
    if row:
        return dict(row)
    existing = await conn.fetchrow(
        f"SELECT agent_id, balance_fen, frozen_fen, total_recharged, created_at "
        f"FROM {SCHEMA}.accounts WHERE agent_id = $1",
        agent_id,
    )
    return dict(existing) if existing else {}


async def get_account(conn, agent_id: str) -> dict | None:
    row = await conn.fetchrow(
        f"SELECT agent_id, balance_fen, frozen_fen, total_recharged, created_at, updated_at "
        f"FROM {SCHEMA}.accounts WHERE agent_id = $1",
        agent_id,
    )
    if not row:
        return None
    result = dict(row)
    result["available_fen"] = result["balance_fen"] - result["frozen_fen"]
    return result


async def check_balance(conn, agent_id: str, required_fen: int) -> dict:
    account = await get_account(conn, agent_id)
    if not account:
        return {"agent_id": agent_id, "available_fen": 0, "required_fen": required_fen, "sufficient": False}
    return {
        "agent_id": agent_id,
        "available_fen": account["available_fen"],
        "required_fen": required_fen,
        "sufficient": account["available_fen"] >= required_fen,
    }


async def recharge(conn, agent_id: str, amount_fen: int,
                   idempotency_key: str = "", remark: str = "") -> dict:
    account = await conn.fetchrow(
        f"SELECT agent_id, balance_fen, frozen_fen FROM {SCHEMA}.accounts "
        f"WHERE agent_id = $1 FOR UPDATE",
        agent_id,
    )
    if not account:
        raise ValueError(f"agent {agent_id} not found")

    if idempotency_key:
        existing = await conn.fetchrow(
            f"SELECT txn_id, agent_id, txn_type, amount_fen, balance_after, fee_type, "
            f"reference_id, detail, idempotency_key, created_at "
            f"FROM {SCHEMA}.transactions WHERE idempotency_key = $1 AND agent_id = $2",
            idempotency_key, agent_id,
        )
        if existing:
            result = dict(existing)
            result["already_processed"] = True
            return result

    new_balance = account["balance_fen"] + amount_fen
    await conn.execute(
        f"UPDATE {SCHEMA}.accounts SET balance_fen = $1, total_recharged = total_recharged + $2, "
        f"updated_at = NOW() WHERE agent_id = $3",
        new_balance, amount_fen, agent_id,
    )

    prev_hash = await conn.fetchval(
        f"SELECT row_hash FROM {SCHEMA}.transactions "
        f"WHERE agent_id = $1 ORDER BY txn_id DESC LIMIT 1",
        agent_id,
    )
    prev_hash = prev_hash or GENESIS

    now_dt = _now()
    row_hash = _compute_txn_hash(prev_hash, agent_id, "recharge", "",
                                 amount_fen, new_balance, now_dt.isoformat())

    detail = _json.dumps({"remark": remark} if remark else {})
    row = await conn.fetchrow(
        f"INSERT INTO {SCHEMA}.transactions "
        f"(agent_id, txn_type, amount_fen, balance_after, fee_type, "
        f"idempotency_key, prev_hash, row_hash, detail, created_at) "
        f"VALUES ($1,'recharge',$2,$3,'',$4,$5,$6,$7,$8) "
        f"RETURNING txn_id, agent_id, txn_type, amount_fen, balance_after, "
        f"fee_type, idempotency_key, created_at",
        agent_id, amount_fen, new_balance, idempotency_key,
        prev_hash, row_hash, detail, now_dt,
    )

    result = dict(row)
    result["status"] = "ok"

    # 审计日志
    await _write_audit_entry(conn, agent_id, "account_recharge", "balance",
                             "high", target_id=agent_id, amount_fen=amount_fen,
                             detail={"txn_id": row["txn_id"], "balance_before": account["balance_fen"],
                                     "balance_after": new_balance, "idempotency_key": idempotency_key,
                                     "remark": remark})

    return result


async def deduct(conn, agent_id: str, amount_fen: int,
                 fee_type: str = "", reference_id: str = "",
                 idempotency_key: str = "") -> dict:
    account = await conn.fetchrow(
        f"SELECT agent_id, balance_fen, frozen_fen FROM {SCHEMA}.accounts "
        f"WHERE agent_id = $1 FOR UPDATE",
        agent_id,
    )
    if not account:
        raise ValueError(f"agent {agent_id} not found")

    if idempotency_key:
        existing = await conn.fetchrow(
            f"SELECT txn_id, agent_id, txn_type, amount_fen, balance_after, fee_type, "
            f"reference_id, detail, idempotency_key, created_at "
            f"FROM {SCHEMA}.transactions WHERE idempotency_key = $1 AND agent_id = $2",
            idempotency_key, agent_id,
        )
        if existing:
            result = dict(existing)
            result["already_processed"] = True
            return result

    available = account["balance_fen"] - account["frozen_fen"]
    if available < amount_fen:
        return {
            "status": "error",
            "error": "INSUFFICIENT_BALANCE",
            "available_fen": available,
            "required_fen": amount_fen,
        }

    new_balance = account["balance_fen"] - amount_fen
    await conn.execute(
        f"UPDATE {SCHEMA}.accounts SET balance_fen = $1, updated_at = NOW() "
        f"WHERE agent_id = $2",
        new_balance, agent_id,
    )

    prev_hash = await conn.fetchval(
        f"SELECT row_hash FROM {SCHEMA}.transactions "
        f"WHERE agent_id = $1 ORDER BY txn_id DESC LIMIT 1",
        agent_id,
    )
    prev_hash = prev_hash or GENESIS

    now_dt = _now()
    row_hash = _compute_txn_hash(prev_hash, agent_id, "deduct", fee_type,
                                 amount_fen, new_balance, now_dt.isoformat())

    detail = _json.dumps({"fee_type": fee_type, "reference_id": reference_id})
    row = await conn.fetchrow(
        f"INSERT INTO {SCHEMA}.transactions "
        f"(agent_id, txn_type, amount_fen, balance_after, fee_type, "
        f"reference_id, idempotency_key, prev_hash, row_hash, detail, created_at) "
        f"VALUES ($1,'deduct',$2,$3,$4,$5,$6,$7,$8,$9,$10) "
        f"RETURNING txn_id, agent_id, txn_type, amount_fen, balance_after, "
        f"fee_type, reference_id, idempotency_key, created_at",
        agent_id, amount_fen, new_balance, fee_type, reference_id,
        idempotency_key, prev_hash, row_hash, detail, now_dt,
    )

    result = dict(row)
    result["status"] = "ok"

    # 审计日志
    await _write_audit_entry(conn, agent_id, "account_deduct", "balance",
                             "high", target_id=agent_id, amount_fen=amount_fen,
                             detail={"txn_id": row["txn_id"], "balance_before": account["balance_fen"],
                                     "balance_after": new_balance, "fee_type": fee_type,
                                     "reference_id": reference_id, "idempotency_key": idempotency_key})

    return result


async def admin_adjust(conn, agent_id: str, amount_fen: int,
                       reason: str = "", idempotency_key: str = "") -> dict:
    """管理员调账（正=补款，负=扣回），走 admin_adjust 类型"""
    account = await conn.fetchrow(
        f"SELECT agent_id, balance_fen, frozen_fen FROM {SCHEMA}.accounts "
        f"WHERE agent_id = $1 FOR UPDATE",
        agent_id,
    )
    if not account:
        raise ValueError(f"agent {agent_id} not found")

    if idempotency_key:
        existing = await conn.fetchrow(
            f"SELECT txn_id, agent_id, txn_type, amount_fen, balance_after, fee_type, "
            f"reference_id, detail, idempotency_key, created_at "
            f"FROM {SCHEMA}.transactions WHERE idempotency_key = $1 AND agent_id = $2",
            idempotency_key, agent_id,
        )
        if existing:
            result = dict(existing)
            result["already_processed"] = True
            return result

    new_balance = account["balance_fen"] + amount_fen
    if new_balance < 0:
        return {
            "status": "error",
            "error": "INSUFFICIENT_BALANCE",
            "available_fen": account["balance_fen"],
            "required_fen": -amount_fen,
        }

    await conn.execute(
        f"UPDATE {SCHEMA}.accounts SET balance_fen = $1, updated_at = NOW() "
        f"WHERE agent_id = $2",
        new_balance, agent_id,
    )

    prev_hash = await conn.fetchval(
        f"SELECT row_hash FROM {SCHEMA}.transactions "
        f"WHERE agent_id = $1 ORDER BY txn_id DESC LIMIT 1",
        agent_id,
    )
    prev_hash = prev_hash or GENESIS

    now_dt = _now()
    row_hash = _compute_txn_hash(prev_hash, agent_id, "admin_adjust", "",
                                 amount_fen, new_balance, now_dt.isoformat())

    detail = _json.dumps({"reason": reason})
    row = await conn.fetchrow(
        f"INSERT INTO {SCHEMA}.transactions "
        f"(agent_id, txn_type, amount_fen, balance_after, fee_type, "
        f"idempotency_key, prev_hash, row_hash, detail, created_at) "
        f"VALUES ($1,'admin_adjust',$2,$3,'',$4,$5,$6,$7,$8) "
        f"RETURNING txn_id, agent_id, txn_type, amount_fen, balance_after, "
        f"fee_type, idempotency_key, created_at",
        agent_id, amount_fen, new_balance, idempotency_key,
        prev_hash, row_hash, detail, now_dt,
    )

    result = dict(row)
    result["status"] = "ok"

    # 审计日志
    await _write_audit_entry(conn, agent_id, "account_admin_adjust", "balance",
                             "critical", target_id=agent_id, amount_fen=amount_fen,
                             detail={"txn_id": row["txn_id"], "balance_before": account["balance_fen"],
                                     "balance_after": new_balance, "reason": reason,
                                     "idempotency_key": idempotency_key})

    return result


async def get_transactions(conn, agent_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT txn_id, agent_id, txn_type, amount_fen, balance_after, "
        f"fee_type, reference_id, idempotency_key, detail, created_at "
        f"FROM {SCHEMA}.transactions WHERE agent_id = $1 "
        f"ORDER BY txn_id DESC LIMIT $2 OFFSET $3",
        agent_id, limit, offset,
    )
    return [dict(r) for r in rows]


async def verify_chain(conn, agent_id: str) -> dict:
    rows = await conn.fetch(
        f"SELECT txn_id, prev_hash, row_hash, agent_id, txn_type, fee_type, "
        f"amount_fen, balance_after, created_at "
        f"FROM {SCHEMA}.transactions "
        f"WHERE agent_id = $1 ORDER BY txn_id",
        agent_id,
    )
    if not rows:
        return {"valid": True, "agent_id": agent_id, "total_txns": 0}

    for i, row in enumerate(rows):
        expected_prev = GENESIS if i == 0 else rows[i - 1]["row_hash"]
        if row["prev_hash"] != expected_prev:
            return {
                "valid": False,
                "agent_id": agent_id,
                "broken_at_txn_id": row["txn_id"],
                "expected_prev_hash": expected_prev,
                "stored_prev_hash": row["prev_hash"],
            }
        computed = _compute_txn_hash(
            row["prev_hash"], row["agent_id"], row["txn_type"],
            row["fee_type"], row["amount_fen"], row["balance_after"],
            row["created_at"].isoformat(),
        )
        if computed != row["row_hash"]:
            return {
                "valid": False,
                "agent_id": agent_id,
                "broken_at_txn_id": row["txn_id"],
                "expected_row_hash": computed,
                "stored_row_hash": row["row_hash"],
            }

    return {"valid": True, "agent_id": agent_id, "total_txns": len(rows)}
