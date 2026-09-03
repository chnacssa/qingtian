"""审计服务 —— 哈希链写入 + Ed25519 签名 + 验证。"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Callable

import asyncpg
from nacl.signing import SigningKey, VerifyKey

from common.db import get_pool
from . import config as cfg
from .encryptor import encryptor

logger = logging.getLogger("zhenyue.audit")


class AuditVerificationError(Exception):
    def __init__(self, audit_uid: str, error_type: str, detail: str = ""):
        self.audit_uid = audit_uid
        self.error_type = error_type
        self.detail = detail
        super().__init__(f"[{error_type}] audit_uid={audit_uid}: {detail}")


async def get_active_sign_key_id(conn: asyncpg.Connection) -> int:
    schema = cfg.get_schema_name()
    key_id = await conn.fetchval(
        f"SELECT id FROM {schema}.sign_keys WHERE status = 'active' LIMIT 1"
    )
    if key_id is None:
        raise RuntimeError("No active sign key found — initialize sign_keys first")
    return key_id


def _chain_lock_key(schema: str) -> int:
    """审计链 advisory lock 键（确定性 bigint，按 schema 隔离）。"""
    return int(hashlib.sha256(f"zhenyue:{schema}:audit_chain".encode()).hexdigest()[:16], 16)


async def get_sign_private_key(conn: asyncpg.Connection, key_id: int) -> str | None:
    """解密 active 审计签名私钥；无私钥（未初始化）返回 None。"""
    schema = cfg.get_schema_name()
    row = await conn.fetchval(
        f"SELECT private_key_enc FROM {schema}.sign_keys WHERE id = $1",
        key_id,
    )
    if not row:
        return None
    try:
        return encryptor.decrypt(row).get("private_key")
    except Exception as e:
        logger.error("Failed to decrypt audit sign key id=%s: %s", key_id, e)
        return None


async def get_prev_hash(conn: asyncpg.Connection) -> str:
    schema = cfg.get_schema_name()
    prev = await conn.fetchval(
        f"SELECT hash FROM {schema}.audit_log ORDER BY id DESC LIMIT 1"
    )
    if prev is None:
        return cfg.get_audit_prev_hash_genesis()
    return prev


async def write_audit(conn: asyncpg.Connection, entry: dict) -> dict:
    schema = cfg.get_schema_name()
    sign_key_id = await get_active_sign_key_id(conn)
    created_at = datetime.now(timezone.utc)

    detail_raw = entry.get("detail")
    detail_enc = encryptor.encrypt(detail_raw) if detail_raw else ""

    ts_iso = created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    private_key_hex = await get_sign_private_key(conn, sign_key_id)

    # P1 (R11): 并发写链分叉 —— 原实现无条件读 prev_hash，两个并发写可读同一 prev →
    # 同点双叉（哈希链分裂不可检出）。现持 advisory xact lock 串行化「读 prev + INSERT」。
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _chain_lock_key(schema))
        prev_hash = await get_prev_hash(conn)

        raw = f"{prev_hash}:{entry['agent_id']}:{entry['action']}:{ts_iso}:{detail_enc}"
        hash_val = hashlib.sha256(raw.encode()).hexdigest()

        # P1 (R11): 审计签名恒为占位符 "0"*128，签名从未实现。现用 active 密钥
        # Ed25519 签名 hash（验签路径 verify_single_record 对 hash 做验签）。仅当
        # 密钥未初始化（无私钥）时退回首占位，保持向后兼容。
        if private_key_hex:
            signature_val = SigningKey(bytes.fromhex(private_key_hex)).sign(hash_val.encode()).signature.hex()
        else:
            signature_val = "0" * 128

        audit_uid = await conn.fetchval(
            f"""INSERT INTO {schema}.audit_log (agent_id, agent_role, action, target_type, target_id,
                severity, detail_enc, approval_status, approval_chain,
                prev_hash, hash, signature, sign_key_id, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING audit_uid""",
            entry["agent_id"],
            entry.get("agent_role", "agent"),
            entry["action"],
            entry.get("target_type", ""),
            entry.get("target_id", ""),
            entry.get("severity", "low"),
            detail_enc,
            entry.get("approval_status", "auto"),
            json.dumps(entry.get("approval_chain", [])),
            prev_hash,
            hash_val,
            signature_val,
            sign_key_id,
            created_at,
        )

    return {
        "audit_uid": str(audit_uid),
        "created_at": created_at.isoformat(),
        "agent_id": entry["agent_id"],
        "action": entry["action"],
        "severity": entry.get("severity", "low"),
        "hash": hash_val,
    }


def verify_single_record(row: dict, public_key_hex: str, prev_hash: str) -> str:
    ts = row["created_at"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts_iso = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    raw = f"{prev_hash}:{row['agent_id']}:{row['action']}:{ts_iso}:{row.get('detail_enc') or ''}"
    expected = hashlib.sha256(raw.encode()).hexdigest()

    if expected != row["hash"]:
        raise AuditVerificationError(
            str(row["audit_uid"]), "hash_broken",
            f"expected={expected[:16]}..., got={row['hash'][:16]}..."
        )

    if row["signature"] and row["signature"] != "0" * 128:
        try:
            pk_bytes = bytes.fromhex(public_key_hex)
            if len(pk_bytes) != 32:
                raise AuditVerificationError(
                    str(row["audit_uid"]), "key_invalid",
                    f"Ed25519 public key must be 32 bytes, got {len(pk_bytes)}"
                )
            verify_key = VerifyKey(pk_bytes)
            verify_key.verify(row["hash"].encode(), bytes.fromhex(row["signature"]))
        except Exception as e:
            raise AuditVerificationError(
                str(row["audit_uid"]), "signature_invalid", str(e)
            )

    return row["hash"]


async def verify_audit_chain(conn: asyncpg.Connection) -> list[dict]:
    schema = cfg.get_schema_name()
    rows = await conn.fetch(
        f"""SELECT al.*, sk.public_key, sk.status AS key_status
            FROM {schema}.audit_log al
            JOIN {schema}.sign_keys sk ON al.sign_key_id = sk.id
            ORDER BY al.id ASC"""
    )

    prev_hash = cfg.get_audit_prev_hash_genesis()
    verified = []

    for row in rows:
        audit_uid = str(row["audit_uid"])

        if row["key_status"] != "active":
            raise AuditVerificationError(
                audit_uid, "key_revoked",
                f"sign_key_id={row['sign_key_id']} status={row['key_status']}"
            )

        prev_hash = verify_single_record(dict(row), row["public_key"], prev_hash)
        verified.append(dict(row))

    return verified


async def verify_critical_with_retry(
    audit_uid: str, conn_factory: Callable, alert_callback: Callable,
    max_attempts: int = 3, delay: int = 300,
):
    for attempt in range(1, max_attempts + 1):
        await asyncio.sleep(delay)

        async with conn_factory() as conn:
            schema = cfg.get_schema_name()
            fresh = await conn.fetchrow(
                f"""SELECT al.*, sk.public_key, sk.status AS key_status
                    FROM {schema}.audit_log al JOIN {schema}.sign_keys sk ON al.sign_key_id = sk.id
                    WHERE al.audit_uid = $1""",
                audit_uid,
            )

        if fresh is None:
            logger.error(f"Critical audit record vanished: {audit_uid}")
            await alert_callback("audit_record_missing", {"audit_uid": audit_uid})
            return

        if fresh["signature"] == "0" * 128:
            if attempt < max_attempts:
                logger.info(f"Retry {attempt}/{max_attempts}: signature still pending for {audit_uid}")
                continue
            else:
                logger.warning(f"Critical record unsigned after {max_attempts * delay}s: {audit_uid}")
                await alert_callback("audit_signature_delayed", {"audit_uid": audit_uid})
                return

        try:
            verify_single_record(dict(fresh), fresh["public_key"], fresh["prev_hash"])
            logger.info(f"Critical record verified: {audit_uid}")
        except AuditVerificationError as e:
            await alert_callback("critical_record_verify_failed", {
                "audit_uid": e.audit_uid,
                "error_type": e.error_type,
                "detail": e.detail,
            })
        return


async def write_audit_from_middleware(conn: asyncpg.Connection, entry: dict) -> dict | None:
    """中间件便捷方法 — 封装 write_audit，容错处理。

    source_layer 自动设为 'middleware'，与第一层 Plugin ('plugin') 区分。
    写入失败仅打日志，不抛异常。
    """
    try:
        entry.setdefault("agent_role", "system")
        entry["detail"] = entry.get("detail") or {}
        if isinstance(entry["detail"], dict):
            entry["detail"]["source_layer"] = "middleware"
        return await write_audit(conn, entry)
    except Exception as e:
        logger.error(f"Middleware audit write failed: {e}")
        return None


async def cleanup_old_audit_logs(retention_days: int | None = None) -> int:
    """清理过期审计日志。

    默认保留天数从配置读取 zhenyue.audit.retention_days，未配置时默认 365 天。
    返回删除的行数。

    设计：审计日志是 append-only 哈希链，整链截断（删除 oldest）。
    仅删除连续时间窗口内所有行均超过保留期的前 N 条。
    """
    if retention_days is None:
        retention_days = cfg.get_audit_retention_days()

    schema = cfg.get_schema_name()

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 找到保留边界：最晚的 N 天前的记录
        cutoff = await conn.fetchval(
            f"SELECT id FROM {schema}.audit_log "
            f"WHERE created_at < NOW() - make_interval(days => $1) "
            f"ORDER BY id DESC LIMIT 1",
            retention_days,
        )
        if cutoff is None:
            return 0

        # P1 (2026-08-26 review #14): 重锚定只重算 hash 未重签 → 旧签名对新 hash 恒
        # signature_invalid，清理一旦触发剩余记录验签全断（每日自动链校验永久告警，
        # 篡改告警可信度归零）。重锚定前取 active 密钥，重算后同步重签（与 write_audit
        # 同一口径：无私钥退 "0"*128 占位，sign_key_id 同步指向当前 active 密钥——
        # verify_audit_chain 按 al.sign_key_id JOIN 公钥，不更新则旧公钥验新签必败）。
        try:
            sign_key_id = await get_active_sign_key_id(conn)
            private_key_hex = await get_sign_private_key(conn, sign_key_id)
        except RuntimeError:
            sign_key_id, private_key_hex = None, None
            logger.warning("Audit cleanup re-anchor: no active sign key, signature falls back to placeholder")

        # 删除 boundary 及之前的所有记录
        # review(2026-08-16): trg_audit_no_delete 触发器禁止 DELETE → 原实现恒抛异常被
        # main.py except 吞掉，保留期清理从不生效。事务内 SET LOCAL app.audit_cleanup='true'
        # 显式授权本清理路径放行（block_audit_mutation 触发器配合），其余路径仍不可变。
        async with conn.transaction():
            await conn.execute("SET LOCAL app.audit_cleanup = 'true'")
            result = await conn.execute(
                f"DELETE FROM {schema}.audit_log WHERE id <= $1",
                cutoff,
            )
            # P1 (R11): 整链截断后剩余记录的 prev_hash 仍指向已删记录 → 链校验永久
            # hash_broken（R10 门修复未覆盖链概念）。删除后重锚定：新首条 prev_hash
            # 改回 genesis 并重算其后整链 hash（幂等：未受影响记录重算结果不变）。
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                remaining = await conn.fetch(
                    f"SELECT id, agent_id, action, created_at, detail_enc "
                    f"FROM {schema}.audit_log ORDER BY id ASC"
                )
                prev = cfg.get_audit_prev_hash_genesis()
                for row in remaining:
                    ts = row["created_at"]
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ts_iso = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    raw = f"{prev}:{row['agent_id']}:{row['action']}:{ts_iso}:{row['detail_enc'] or ''}"
                    new_hash = hashlib.sha256(raw.encode()).hexdigest()
                    if private_key_hex:
                        new_sig = SigningKey(bytes.fromhex(private_key_hex)).sign(new_hash.encode()).signature.hex()
                    else:
                        new_sig = "0" * 128
                    if sign_key_id is not None:
                        await conn.execute(
                            f"UPDATE {schema}.audit_log SET prev_hash = $2, hash = $3, "
                            f"signature = $4, sign_key_id = $5 WHERE id = $1",
                            row["id"], prev, new_hash, new_sig, sign_key_id,
                        )
                    else:
                        await conn.execute(
                            f"UPDATE {schema}.audit_log SET prev_hash = $2, hash = $3, "
                            f"signature = $4 WHERE id = $1",
                            row["id"], prev, new_hash, new_sig,
                        )
                    prev = new_hash
        if count > 0:
            logger.info("Audit log cleanup: removed %d records older than %d days", count, retention_days)
        return count
