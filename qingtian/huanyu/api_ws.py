"""
寰宇 — WebSocket 实时推送
Agent 连接 / 断连管理 + 消息主动推送
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from common.ws_auth import verify_ws_connection

logger = logging.getLogger("huanyu.ws")

router = APIRouter()

# ── 连接管理器 ────────────────────────────────────────

class ConnectionManager:
    """管理 agent_id → WebSocket 映射，支持一对多推送"""

    def __init__(self):
        # agent_id → set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}
        # WebSocket → agent_id 反向映射
        self._agent_of: dict[int, str] = {}

    async def connect(self, agent_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(agent_id, set()).add(ws)
        self._agent_of[id(ws)] = agent_id
        logger.info("ws agent=%s connected (total=%d)", agent_id, len(self._agent_of))

    async def disconnect(self, ws: WebSocket):
        agent_id = self._agent_of.pop(id(ws), None)
        if agent_id and agent_id in self._connections:
            self._connections[agent_id].discard(ws)
            if not self._connections[agent_id]:
                del self._connections[agent_id]
        logger.info("ws agent=%s disconnected (remaining=%d)", agent_id, len(self._agent_of))

    async def send_to(self, agent_id: str, data: dict) -> bool:
        """向指定 agent 的所有连接推送消息，返回是否有活跃连接"""
        sockets = self._connections.get(agent_id, set())
        if not sockets:
            return False
        payload = json.dumps(data, ensure_ascii=False, default=str)
        dead = []
        for ws in sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)
        return True

    async def broadcast(self, agent_ids: list[str], data: dict):
        """向列表内所有 agent 广播"""
        for aid in agent_ids:
            await self.send_to(aid, data)

    @property
    def online_agents(self) -> list[str]:
        return list(self._connections.keys())

    @property
    def connection_count(self) -> int:
        return len(self._agent_of)


manager = ConnectionManager()


# ── WebSocket 端点 ────────────────────────────────────

@router.websocket("/v1/ws/{agent_id}")
async def agent_websocket(ws: WebSocket, agent_id: str, token: str = Query(default="")):
    """Agent 长连接：接收实时推送 + 心跳保活

    A3 (R11): 握手校验 Bearer token（query ?token=），防窃听他人推送。
    """
    if not await verify_ws_connection(agent_id, token):
        await ws.close(code=4401, reason="unauthorized")
        return
    await manager.connect(agent_id, ws)
    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            if data.get("type") == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif data.get("type") == "ack":
                pass
            else:
                await ws.send_text(json.dumps({"type": "error", "error": "unknown message type"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await manager.disconnect(ws)


# ── 健康 ──────────────────────────────────────────────

@router.get("/v1/ws/health")
async def ws_health():
    return {
        "online_agents": manager.online_agents,
        "connections": manager.connection_count,
    }
