"""Agent Ed25519 密钥对服务 — 生成、存储、查询、撤销。

执策 Phase 2 前置依赖（§3.4.3）：
  - 镇岳在 Agent 注册时生成 Ed25519 密钥对，托管加密私钥
  - Agent 获取私钥用于 check_results 签名
  - 执策引擎获取公钥用于验签
"""
import logging
from datetime import datetime, timezone

import asyncpg
from nacl.signing import SigningKey

from . import config as cfg
from .encryptor import encryptor

logger = logging.getLogger("zhenyue.key_service")


async def generate_keypair(
    conn: asyncpg.Connection,
    agent_id: str,
) -> dict:
    """为 Agent 生成 Ed25519 密钥对，返回 key_id + public_key。

    private_key 加密后存储，不在返回值中暴露。
    Agent 需通过 GET /agents/{id}/private-key 单独获取。
    """
    schema = cfg.get_schema_name()

    # 撤销已有活跃密钥
    await conn.execute(
        f"UPDATE {schema}.agent_keys SET status = 'revoked', revoked_at = NOW() "
        f"WHERE agent_id = $1 AND status = 'active'",
        agent_id,
    )

    # 生成 Ed25519 密钥对
    sk = SigningKey.generate()
    vk = sk.verify_key
    public_key_hex = bytes(vk).hex()       # 32 bytes → 64 hex
    private_key_hex = bytes(sk).hex()      # 32 bytes → 64 hex

    # 加密私钥后存储
    encrypted_sk = encryptor.encrypt({"private_key": private_key_hex})

    key_id = await conn.fetchval(
        f"INSERT INTO {schema}.agent_keys (agent_id, public_key, private_key, algorithm, status) "
        f"VALUES ($1, $2, $3, 'ed25519', 'active') RETURNING key_id",
        agent_id, public_key_hex, encrypted_sk,
    )

    logger.info("Agent keypair generated: agent=%s, key_id=%s", agent_id, key_id)

    return {
        "key_id": key_id,
        "agent_id": agent_id,
        "public_key": public_key_hex,
        "algorithm": "ed25519",
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def get_public_key(conn: asyncpg.Connection, agent_id: str) -> dict | None:
    """获取 Agent 当前活跃公钥（验签用）。"""
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT key_id, public_key, algorithm, created_at "
        f"FROM {schema}.agent_keys "
        f"WHERE agent_id = $1 AND status = 'active' "
        f"ORDER BY created_at DESC LIMIT 1",
        agent_id,
    )
    if row is None:
        return None
    return {
        "key_id": row["key_id"],
        "agent_id": agent_id,
        "public_key": row["public_key"],
        "algorithm": row["algorithm"],
        "created_at": row["created_at"].isoformat(),
    }


async def get_private_key(conn: asyncpg.Connection, agent_id: str) -> str | None:
    """解密并返回 Agent 私钥（签名用）。

    仅 Agent 本人在通过 Bearer Token 认证后可调用。
    """
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT private_key FROM {schema}.agent_keys "
        f"WHERE agent_id = $1 AND status = 'active' "
        f"ORDER BY created_at DESC LIMIT 1",
        agent_id,
    )
    if row is None or not row["private_key"]:
        return None
    try:
        data = encryptor.decrypt(row["private_key"])
        return data.get("private_key")
    except Exception as e:
        logger.error("Failed to decrypt private key for agent=%s: %s", agent_id, e)
        return None


async def revoke_keypair(conn: asyncpg.Connection, agent_id: str) -> bool:
    """撤销 Agent 所有活跃密钥。"""
    schema = cfg.get_schema_name()
    result = await conn.execute(
        f"UPDATE {schema}.agent_keys SET status = 'revoked', revoked_at = NOW() "
        f"WHERE agent_id = $1 AND status = 'active'",
        agent_id,
    )
    count = int(result.split()[-1]) if result else 0
    logger.info("Agent keypair revoked: agent=%s, count=%s", agent_id, count)
    return count > 0
