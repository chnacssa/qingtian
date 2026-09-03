import hmac
import os

from fastapi import Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from common.db import get_pool
from common.ipc_auth import is_internal_ipc
from .token_service import verify_token_from_db

security = HTTPBearer(auto_error=False)

# 首次部署无 token 时的 bootstrap 机制
# 设置环境变量 YONGHENG_BOOTSTRAP_TOKEN 即可创建第一个 admin token
_BOOTSTRAP_TOKEN = os.getenv("YONGHENG_BOOTSTRAP_TOKEN", "")


async def get_token_info(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    # A1 (R11): 内部 IPC 不再无条件提权 admin——必须 loopback + 显式内部令牌
    # （QINGTIAN_INTERNAL_IPC_TOKEN / X-Internal-Token），否则走正常 Bearer。
    if is_internal_ipc(request):
        return {"namespace": "internal-ipc", "level": "admin", "internal": True}

    if credentials is None:
        return {}

    token = credentials.credentials

    # Bootstrap: 环境变量令牌 → admin 权限，仅用于创建第一个正式 token
    # Python 3.12+ compare_digest 要求纯 ASCII, 非 ASCII token 抛异常 → fallback ==
    if _BOOTSTRAP_TOKEN and (
        (token.isascii() and _BOOTSTRAP_TOKEN.isascii()
         and hmac.compare_digest(token, _BOOTSTRAP_TOKEN))
        or token == _BOOTSTRAP_TOKEN
    ):
        return {"namespace": "bootstrap-admin", "level": "admin", "bootstrap": True}

    pool = await get_pool()
    async with pool.acquire() as conn:
        return await verify_token_from_db(conn, token)


def require_level(*levels: str):
    async def dependency(token_info: dict = Depends(get_token_info)):
        if not token_info:
            from .models import AppError
            raise AppError("INVALID_TOKEN", "token required", 401)
        if levels and token_info.get("level") not in levels:
            from .models import AppError
            raise AppError("FORBIDDEN", f"requires {levels} permission", 403)
        return token_info
    return Depends(dependency)
