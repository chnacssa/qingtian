"""镇岳 — FastAPI 认证依赖。"""

import os
import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from common.db import get_pool
from common.ipc_auth import is_internal_ipc
from . import token_service

logger = logging.getLogger("zhenyue.auth")

_bearer = HTTPBearer(auto_error=False)


async def verify_admin_token(request: Request) -> str:
    """检查 X-Admin-Token 请求头是否与 ZHENYUE_ADMIN_TOKEN 一致。

    Returns: agent_id (固定为 'admin:console')
    Raises: HTTPException(401) 如果不匹配
    """
    admin_token = request.headers.get("X-Admin-Token", "")
    expected = os.getenv("ZHENYUE_ADMIN_TOKEN", "")
    if not expected:
        logger.warning("ZHENYUE_ADMIN_TOKEN is not set — admin auth is disabled")
        raise HTTPException(status_code=401, detail="ZHENYUE_ADMIN_TOKEN not configured")
    if not admin_token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token header")
    if admin_token != expected:
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return "admin:console"


async def verify_agent_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = None,
) -> str:
    """检查 Bearer Token 是否有效。

    Returns: agent_id
    Raises: HTTPException(401) 如果无效
    """
    # A1 (R11): 内部 IPC 不再无条件免认证——必须 loopback + 显式内部令牌
    # （QINGTIAN_INTERNAL_IPC_TOKEN / X-Internal-Token），否则走正常 Bearer。
    if is_internal_ipc(request):
        return "internal-ipc"

    token = None
    if credentials:
        token = credentials.credentials

    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await token_service.authenticate(conn, token)

    if result is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return result["agent_id"]


async def get_current_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """返回当前认证 agent 的 ID 字符串。

    用于 FastAPI dependency 注入模式。credentials 必须经 _bearer(HTTPBearer)
    安全依赖解析 Authorization: Bearer <token> 头——若无 Depends，FastAPI 会把
    HTTPAuthorizationCredentials 当作 body 字段解析，导致 Bearer 认证恒 422。
    """
    return await verify_agent_auth(request, credentials)


async def _resolve_auth_info(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> dict:
    """解析完整认证信息（agent_id / role / capabilities）。

    R11 (P1): auth_dependency 需携带 role 供 admin/ops_admin 端点鉴权——
    此前只返回 agent_id，zhice 等模块 auth.get("role") 恒 None → admin 端点恒 403。
    """
    if is_internal_ipc(request):
        return {"agent_id": "internal-ipc", "role": "admin", "capabilities": ["admin", "ops_admin"]}

    token = credentials.credentials if credentials else ""
    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await token_service.authenticate(conn, token)

    if result is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return {
        "agent_id": result.get("agent_id", ""),
        "role": result.get("role", ""),
        "capabilities": result.get("capabilities", []),
    }


async def auth_dependency(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = None,
) -> dict:
    """通用认证依赖，返回 agent 信息字典。

    用于跨模块（zhice、huichuan 等）的 FastAPI Depends 注入。
    返回: {"agent_id": str, "authenticated": bool, "role": str, "capabilities": list}
    """
    auth_info = None
    try:
        if is_internal_ipc(request):
            # 内部 IPC（loopback + 内部令牌）→ 平台可信调用方，免 Bearer
            auth_info = {"agent_id": "internal-ipc", "role": "admin", "capabilities": ["admin", "ops_admin"]}
        elif credentials:
            auth_info = await _resolve_auth_info(request, credentials)
        else:
            # 尝试从 Authorization header 手动提取
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth_header[7:])
                auth_info = await _resolve_auth_info(request, creds)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Authentication failed")

    if not auth_info or not auth_info.get("agent_id"):
        raise HTTPException(status_code=401, detail="Not authenticated")

    return {
        "agent_id": auth_info["agent_id"],
        "authenticated": True,
        "role": auth_info.get("role", ""),
        "capabilities": auth_info.get("capabilities", []),
    }
