"""Token 服务 —— 创建 / 验证 / 吊销。"""

import hashlib
import secrets
from datetime import datetime, timezone

import asyncpg

from .models import AppError
from . import config as cfg

TOKEN_PREFIX_MAP = {
    "namespace": "yh_ns_",
    "master": "yh_mst_",
    "admin": "yh_adm_",
}


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token(level: str) -> tuple[str, str]:
    prefix = TOKEN_PREFIX_MAP.get(level, "yh_ns_")
    token = prefix + secrets.token_hex(12)
    return token, prefix


async def create_token(conn: asyncpg.Connection, namespace: str, level: str, created_by: str = "") -> dict:
    token, prefix = generate_token(level)
    token_hash = hash_token(token)
    schema = cfg.get_schema_name()

    await conn.execute(
        f"""INSERT INTO {schema}.tokens (namespace, token_hash, token_prefix, level, created_by, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW(), NOW())""",
        namespace, token_hash, prefix, level, created_by,
    )

    return {
        "namespace": namespace,
        "token": token,
        "level": level,
        "created_at": datetime.now(timezone.utc),
    }


async def validate_token(conn: asyncpg.Connection, token: str) -> dict:
    token_hash = hash_token(token)
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT namespace, level, revoked, expires_at FROM {schema}.tokens WHERE token_hash = $1",
        token_hash,
    )
    if not row:
        return {"valid": False, "namespace": "", "level": "", "expires_at": None}
    if row["revoked"]:
        return {"valid": False, "namespace": row["namespace"], "level": row["level"], "expires_at": row["expires_at"]}
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        return {"valid": False, "namespace": row["namespace"], "level": row["level"], "expires_at": row["expires_at"]}
    return {"valid": True, "namespace": row["namespace"], "level": row["level"], "expires_at": row["expires_at"]}


async def revoke_token(conn: asyncpg.Connection, token: str):
    token_hash = hash_token(token)
    schema = cfg.get_schema_name()
    result = await conn.execute(
        f"UPDATE {schema}.tokens SET revoked = TRUE, updated_at = NOW() WHERE token_hash = $1 AND revoked = FALSE",
        token_hash,
    )
    if result == "UPDATE 0":
        raise AppError("NOT_FOUND", "token not found or already revoked", 404)

    row = await conn.fetchrow(
        f"SELECT namespace, level FROM {schema}.tokens WHERE token_hash = $1",
        token_hash,
    )
    if row is None:
        raise AppError("NOT_FOUND", "token not found", 404)
    return {
        "status": "revoked",
        "namespace": row["namespace"],
        "level": row["level"],
        "revoked_at": datetime.now(timezone.utc),
    }


async def verify_token_from_db(conn: asyncpg.Connection, token: str) -> dict:
    token_hash = hash_token(token)
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT namespace, level, revoked, expires_at FROM {schema}.tokens WHERE token_hash = $1",
        token_hash,
    )
    if not row:
        raise AppError("INVALID_TOKEN", "token not found", 401)
    if row["revoked"]:
        raise AppError("INVALID_TOKEN", "token revoked", 401)
    if row["expires_at"] and row["expires_at"] < datetime.now(timezone.utc):
        raise AppError("INVALID_TOKEN", "token expired", 401)
    return {"namespace": row["namespace"], "level": row["level"]}


def check_namespace_match(token_namespace: str, request_namespace: str):
    if token_namespace != request_namespace:
        raise AppError("FORBIDDEN", "namespace mismatch with token binding", 403)
