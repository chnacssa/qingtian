"""WebSocket 握手鉴权 — A3 (R11) 修复

原 WS 端点无鉴权：任意客户端自报 agent_id 即可连接，窃听他人实时推送 /
注入伪造生命周期事件。改为握手时校验 Bearer token：
  - token 必须有效（zhenyue token_service.authenticate）
  - token 归属的 agent_id 必须与连接目标 agent_id 一致
  - admin 角色 token 允许连接任意 agent（管理监控通道）

客户端连接格式（query 参数）:
  ws://host/v1/ws/{agent_id}?token=<BearerToken>
  ws://host/v1/zhice/events?agent_id=<id>&token=<BearerToken>
"""

import logging

from common.db import get_pool

logger = logging.getLogger("common.ws_auth")


async def verify_ws_connection(agent_id: str, token: str) -> bool:
    """校验 WS 连接凭据。

    Returns:
        True 允许连接；False 拒绝（调用方应 ws.close(code=4401)）
    """
    if not token or not agent_id:
        return False
    try:
        from zhenyue.token_service import authenticate
        pool = await get_pool()
        async with pool.acquire() as conn:
            identity = await authenticate(conn, token)
    except Exception as e:
        logger.warning("WS 鉴权异常 (agent=%s): %s", agent_id, e)
        return False
    if not identity:
        return False
    tok_agent = identity.get("agent_id", "")
    role = identity.get("role", "agent")
    if role == "admin":
        return True
    return tok_agent == agent_id
