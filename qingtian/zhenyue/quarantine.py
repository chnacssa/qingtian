"""镇岳 — 删除隔离区系统。

将 Agent 删除的文件移入隔离区，支持恢复和自动过期清理。
"""

import logging
import os
import re
import shutil
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from . import config as zcfg

logger = logging.getLogger("zhenyue.quarantine")

QUARANTINE_BASE = "/opt/qingtian/quarantine"

# R11 (P1): agent_id 拼进隔离路径 —— 只允许安全字符，防路径穿越
_SAFE_AGENT_ID = re.compile(r"^[A-Za-z0-9_@.\-]+$")


async def quarantine_file(agent_id: str, source: str, original_path: str,
                          metadata: Optional[dict] = None) -> dict:
    """将文件移入隔离区。

    Args:
        agent_id: 执行删除的 Agent ID
        source: 来源标识（如 'agent_delete', 'guard_enforce'）
        original_path: 被删除文件的原始路径
        metadata: 可选的额外元数据

    Returns:
        {"status": "ok", "quarantine_id": "...", "quarantine_path": "..."}
        或 {"status": "error", "error": "..."}
    """
    # R11 (P1): agent_id 拼进隔离路径 —— 非法字符（含 ../）直接拒绝
    if not agent_id or not _SAFE_AGENT_ID.match(agent_id):
        return {"status": "error", "error": f"Invalid agent_id: {agent_id!r}"}

    # 确认文件存在
    if not os.path.exists(original_path):
        return {"status": "error", "error": f"File not found: {original_path}"}

    # 生成 quarantine_id
    quarantine_id = str(uuid_mod.uuid4())
    basename = os.path.basename(original_path)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    # 构建目标路径
    dest_dir = os.path.join(QUARANTINE_BASE, agent_id, date_str)
    dest_path = os.path.join(dest_dir, f"{quarantine_id}_{basename}")

    original_size = os.path.getsize(original_path)

    # R11 (P1): 先写 DB 记录再移动文件 —— 原实现先 move 后 INSERT，DB 失败时文件
    # 已移走却无记录 → 文件丢失。DB 失败则文件仍在原位，可重试；move 失败则把
    # 记录标记为 orphaned（文件未离开原路径）。
    schema = zcfg.get_schema_name()
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {schema}.quarantine "
                f"(quarantine_id, agent_id, source, original_path, quarantine_path, original_size, metadata) "
                f"VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)",
                quarantine_id, agent_id, source, original_path, dest_path,
                original_size, metadata or {},
            )
    except Exception as e:
        logger.error("Quarantine DB insert failed for %s: %s", original_path, e)
        return {"status": "error", "error": f"DB record failed: {e}"}

    os.makedirs(dest_dir, exist_ok=True)
    try:
        shutil.move(original_path, dest_path)
    except OSError as e:
        logger.error("Quarantine move failed: %s -> %s: %s", original_path, dest_path, e)
        # 文件未移动，DB 记录作废（防悬挂记录）
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {schema}.quarantine SET status = 'orphaned' "
                    f"WHERE quarantine_id = $1::uuid",
                    quarantine_id,
                )
        except Exception:
            pass
        return {"status": "error", "error": f"Move failed: {e}"}

    logger.info("File quarantined: agent=%s, path=%s -> %s", agent_id, original_path, dest_path)

    return {
        "status": "ok",
        "quarantine_id": quarantine_id,
        "quarantine_path": dest_path,
    }


async def restore_file(quarantine_id: str) -> dict:
    """从隔离区恢复文件到原位置。

    Args:
        quarantine_id: 隔离记录 ID

    Returns:
        {"status": "ok", "original_path": "..."}
        或 {"status": "error", "error": "..."}
    """
    schema = zcfg.get_schema_name()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {schema}.quarantine WHERE quarantine_id = $1::uuid",
            quarantine_id,
        )

    if row is None:
        return {"status": "error", "error": f"Quarantine record not found: {quarantine_id}"}

    record = dict(row)

    if record["status"] != "quarantined":
        return {"status": "error", "error": f"Quarantine record status is '{record['status']}', not 'quarantined'"}

    quarantine_path = record["quarantine_path"]
    original_path = record["original_path"]

    if not os.path.exists(quarantine_path):
        return {"status": "error", "error": f"Quarantine file not found on disk: {quarantine_path}"}

    # 确保原目录存在
    os.makedirs(os.path.dirname(original_path), exist_ok=True)

    try:
        shutil.move(quarantine_path, original_path)
    except OSError as e:
        logger.error("Quarantine restore failed: %s -> %s: %s", quarantine_path, original_path, e)
        return {"status": "error", "error": f"Restore failed: {e}"}

    # 更新 DB 状态
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {schema}.quarantine SET status = 'restored', restored_at = NOW() "
            f"WHERE quarantine_id = $1::uuid",
            quarantine_id,
        )

    logger.info("File restored: quarantine=%s -> %s", quarantine_id, original_path)

    return {"status": "ok", "original_path": original_path}


async def purge_expired(max_age_days: int = 30):
    """清理过期隔离文件。

    查找 expires_at < NOW() 的记录，删除物理文件，标记为 purged。

    Args:
        max_age_days: 过期天数
    """
    schema = zcfg.get_schema_name()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT quarantine_id, quarantine_path FROM {schema}.quarantine "
            f"WHERE expires_at < NOW() AND status = 'quarantined'"
        )

    purged_count = 0
    for row in rows:
        quarantine_path = row["quarantine_path"]
        if os.path.exists(quarantine_path):
            try:
                os.remove(quarantine_path)
            except OSError as e:
                logger.warning("Failed to remove expired quarantine file %s: %s", quarantine_path, e)
                continue

        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {schema}.quarantine SET status = 'purged' "
                f"WHERE quarantine_id = $1::uuid",
                row["quarantine_id"],
            )
        purged_count += 1

    if purged_count > 0:
        logger.info("Purged %d expired quarantine files", purged_count)

    return purged_count


async def list_quarantine(agent_id: str = "", status: str = "quarantined") -> list[dict]:
    """列出隔离区文件。

    Args:
        agent_id: 按 Agent 筛选（可选）
        status: 按状态筛选，默认 'quarantined'

    Returns:
        隔离记录列表
    """
    schema = zcfg.get_schema_name()
    pool = await get_pool()

    if agent_id:
        query = f"SELECT * FROM {schema}.quarantine WHERE agent_id = $1 AND status = $2 ORDER BY created_at DESC"
        params = (agent_id, status)
    else:
        query = f"SELECT * FROM {schema}.quarantine WHERE status = $1 ORDER BY created_at DESC"
        params = (status,)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return [dict(r) for r in rows]
