"""Token 服务 —— SHA-256 哈希存储。"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import asyncpg

from . import config as cfg


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _role_capabilities(role: str) -> list[str]:
    """将角色映射为能力列表，供中间件能力检查使用。"""
    caps_map = {
        "admin": ["admin", "ops_admin"],
        "ops_admin": ["ops_admin"],
        "agent": [],
    }
    return caps_map.get(role, [])


async def create_token(conn: asyncpg.Connection, agent_id: str, role: str = "agent") -> dict:
    prefix = "zt_adm_" if role == "admin" else "zt_ns_"
    raw_token = prefix + secrets.token_hex(32)
    token_hash = _hash_token(raw_token)
    token_prefix = raw_token[:12]
    schema = cfg.get_schema_name()

    # A6 (R11): admin/ops_admin 令牌 24h 过期——高危角色必须限时，
    # 防止 session token 永久有效被滥用；agent 会话令牌保持长生命周期（可撤销）。
    expires_at = None
    if role in ("admin", "ops_admin"):
        # 直接传 aware datetime 对象，asyncpg timestamptz 列只收 datetime；
        # .isoformat() 字符串会被 asyncpg 拒绝（R11 A6 线上 500，2026-08-17）
        expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    await conn.execute(
        f"""INSERT INTO {schema}.tokens (agent_id, token_hash, token_prefix, role, expires_at, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW(), NOW())""",
        agent_id, token_hash, token_prefix, role, expires_at,
    )

    return {
        "token": raw_token,
        "agent_id": agent_id,
        "role": role,
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }


async def validate_token(conn: asyncpg.Connection, token: str) -> dict | None:
    token_hash = _hash_token(token)
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT agent_id, role, expires_at, revoked FROM {schema}.tokens WHERE token_hash = $1",
        token_hash,
    )
    if row is None:
        return None
    if row["revoked"]:
        return None
    if row["expires_at"]:
        now = datetime.now(timezone.utc)
        if row["expires_at"].replace(tzinfo=timezone.utc) < now:
            return None
    caps = _role_capabilities(row["role"])
    return {"agent_id": row["agent_id"], "role": row["role"], "capabilities": caps}


async def revoke_token(conn: asyncpg.Connection, token: str) -> bool:
    token_hash = _hash_token(token)
    schema = cfg.get_schema_name()
    result = await conn.execute(
        f"UPDATE {schema}.tokens SET revoked = TRUE, updated_at = NOW() WHERE token_hash = $1",
        token_hash,
    )
    return result != "UPDATE 0"


async def revoke_all_agent_tokens(conn: asyncpg.Connection, agent_id: str) -> int:
    schema = cfg.get_schema_name()
    result = await conn.execute(
        f"UPDATE {schema}.tokens SET revoked = TRUE, updated_at = NOW() WHERE agent_id = $1",
        agent_id,
    )
    parts = result.split()
    return int(parts[1]) if len(parts) > 1 else 0


async def validate_bootstrap(token: str) -> dict | None:
    bootstrap = cfg.get_bootstrap_admin_token()
    if bootstrap and hmac.compare_digest(token, bootstrap):
        return {"agent_id": "bootstrap:admin", "role": "admin", "capabilities": _role_capabilities("admin")}
    return None


async def authenticate(conn: asyncpg.Connection, token: str) -> dict | None:
    result = await validate_token(conn, token)
    if result:
        return result
    return await validate_bootstrap(token)
