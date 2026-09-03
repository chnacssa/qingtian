"""认证依赖 — 复用镇岳 token 体系，siku 不做独立鉴权。"""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from common.db import get_pool
from zhenyue.token_service import authenticate
from zhenyue.models import AppError

security = HTTPBearer(auto_error=False)


async def auth_dependency(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    if credentials is None:
        raise AppError("UNAUTHORIZED", "missing or invalid Authorization header", 401)

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await authenticate(conn, credentials.credentials)

    if result is None:
        raise AppError("UNAUTHORIZED", "invalid or expired token", 401)
    return result


def require_admin():
    async def dependency(auth: dict = Depends(auth_dependency)):
        if auth["role"] != "admin":
            raise AppError("FORBIDDEN", "requires admin role", 403)
        return auth
    return Depends(dependency)


def require_agent_or_admin():
    async def dependency(auth: dict = Depends(auth_dependency)):
        if auth["role"] not in ("admin", "agent"):
            raise AppError("FORBIDDEN", "requires agent or admin role", 403)
        return auth
    return Depends(dependency)


def verify_agent_ownership(auth: dict, agent_id: str):
    """校验 agent_token 只能操作自己的资源"""
    if auth["role"] != "admin" and auth["agent_id"] != agent_id:
        raise HTTPException(403, "只允许操作本人账户")
