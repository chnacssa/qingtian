"""执策 WebSocket 统一推送端点

Agent 连接 ws://host/v1/zhice/events?agent_id=<id> 后实时接收：
  - assigned, timed_out, retry_exhausted, issue_reported
  - reverify_requested, multisig_failed, task_completed, task_failed

取代 dispatcher 中零散的 ws_notify() 调用。huanyu WS 保留为 fallback。
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from common.db import get_pool
from common.ws_auth import verify_ws_connection
from . import config as cfg

logger = logging.getLogger("zhice.ws")
router = APIRouter()

# ── 连接管理 ──────────────────────────────────────────

class ConnectionManager:
    """Agent → WebSocket 连接池。每个 agent_id 可有一个活跃连接。"""
    def __init__(self):
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, agent_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[agent_id] = ws
        logger.info("WS connected: agent=%s (total=%d)", agent_id, len(self._connections))

    def disconnect(self, agent_id: str):
        if agent_id in self._connections:
            del self._connections[agent_id]
            logger.info("WS disconnected: agent=%s (total=%d)", agent_id, len(self._connections))
            # 立即回收该 Agent 的 assigned 步骤
            asyncio.create_task(_recycle_assigned_steps(agent_id))

    async def send_to(self, agent_id: str, data: dict) -> bool:
        """推送 JSON 消息到指定 Agent。成功返回 True。"""
        ws = self._connections.get(agent_id)
        if ws is None:
            return False
        try:
            await ws.send_json(data)
            return True
        except Exception as e:
            logger.warning("WS send failed for agent=%s: %s", agent_id, e)
            self.disconnect(agent_id)
            return False

    def is_connected(self, agent_id: str) -> bool:
        return agent_id in self._connections


async def _recycle_assigned_steps(agent_id: str):
    """Agent 断线时回收其所有 assigned 步骤为 pending，供其他 Agent 认领。"""
    try:
        schema = cfg.get_schema_name()
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE {schema}.steps SET status = 'pending', "
                f"assigned_agent = NULL, assigned_at = NULL, updated_at = NOW() "
                f"WHERE assigned_agent = $1 AND status = 'assigned'",
                agent_id,
            )
            count = int(result.split()[-1]) if result else 0
            if count > 0:
                logger.info("WS disconnect: recycled %d assigned steps from %s", count, agent_id)
    except Exception:
        logger.exception("recycle_assigned_steps failed for %s", agent_id)


manager = ConnectionManager()


@router.websocket("/v1/zhice/events")
async def zhice_events(ws: WebSocket, agent_id: str = Query(default=""),
                       token: str = Query(default="")):
    """执策事件流 — Agent 连接后自动订阅自身事件。

    A3 (R11): 握手校验 Bearer token（query ?token=），token 归属 agent 必须与
    agent_id 一致，防窃听/伪造事件流。
    """
    if not agent_id:
        await ws.close(code=4000, reason="agent_id required")
        return
    if not await verify_ws_connection(agent_id, token):
        await ws.close(code=4401, reason="unauthorized")
        return

    await manager.connect(agent_id, ws)
    try:
        await ws.send_json({"type": "connected", "agent_id": agent_id})
        # 保持连接，等待推送
        while True:
            try:
                data = await ws.receive_text()
                # Agent 可发送心跳/ping，忽略即可
                if data == "ping":
                    await ws.send_json({"type": "pong"})
            except WebSocketDisconnect:
                break
            except Exception:
                break
    except Exception as e:
        logger.debug("WS event loop for agent=%s ended: %s", agent_id, e)
    finally:
        manager.disconnect(agent_id)


# ── 推送辅助 ──────────────────────────────────────────

async def ws_notify(agent_id: str, event_type: str, data: dict) -> bool:
    """首选 zhice 自有 WS，失败则 fallback 到 huanyu。"""
    sent = await manager.send_to(agent_id, {
        "type": f"zhice:{event_type}",
        **data,
    })
    if sent:
        return True

    # fallback: huanyu WS
    try:
        from huanyu.api_ws import manager as huanyu_manager
        fallback_data = {
            "type": f"zhice:{event_type}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        return await huanyu_manager.send_to(agent_id, fallback_data)
    except ImportError:
        return False
