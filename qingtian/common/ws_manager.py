"""
ACSSA 智能体操作系统 — 统一 WebSocket 连接管理器

替代 zhice/ws.py 和 huanyu/api_ws.py。
Agent 生命周期事件通过 WS 自动追加到 MessageBus 的 per-agent buffer。
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from common.bus import bus

logger = logging.getLogger("common.ws_manager")


class WSManager:
    """统一 WebSocket 连接管理器

    管理所有 Agent 的 WS 连接，提供发送/广播/健康检查。
    Agent 发来的生命周期事件自动追加到 MessageBus 的 per-agent buffer。
    """

    def __init__(self, max_connections: int = 500):
        self._connections: dict[str, WebSocket] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._seq_counters: dict[str, int] = {}
        self._max_connections = max_connections
        # 健康检查: agent_id → last_pong
        self._last_pong: dict[str, datetime] = {}
        self._health_task: Optional[asyncio.Task] = None

    # ── 连接管理 ──────────────────────────────────────────

    def _get_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._locks:
            self._locks[agent_id] = asyncio.Lock()
        return self._locks[agent_id]

    async def register(self, agent_id: str, ws: WebSocket):
        """注册 Agent 的 WS 连接。同一 agent_id 重复注册 → 关闭旧连接"""
        if len(self._connections) >= self._max_connections:
            logger.warning("[WS] max connections reached (%d), rejecting %s",
                           self._max_connections, agent_id)
            await ws.close(code=1013, reason="too_many_connections")
            return

        async with self._get_lock(agent_id):
            old = self._connections.get(agent_id)
            if old:
                try:
                    await old.close(code=1001, reason="replaced_by_new_connection")
                except Exception:
                    pass
            self._connections[agent_id] = ws
            self._last_pong[agent_id] = datetime.now(timezone.utc)

        logger.info("[WS] %s registered (total=%d)", agent_id, len(self._connections))

        # WS 重连后 flush 待推送队列
        try:
            pending = await bus.flush_pending(agent_id)
            if pending:
                for event in pending:
                    try:
                        await ws.send_json(event)
                    except Exception:
                        # 发送失败重新入队
                        await bus.enqueue_pending(agent_id, event)
                        break
                logger.info("[WS] flushed %d pending events to %s", len(pending), agent_id)
        except Exception:
            pass

    async def unregister(self, agent_id: str):
        """注销 Agent 的 WS 连接"""
        async with self._get_lock(agent_id):
            old = self._connections.pop(agent_id, None)
            self._last_pong.pop(agent_id, None)
            if old:
                try:
                    await old.close(code=1000, reason="normal_close")
                except Exception:
                    pass
        logger.info("[WS] %s unregistered (total=%d)", agent_id, len(self._connections))

    async def send(self, agent_id: str, event: dict) -> bool:
        """向 Agent 发送事件。返回 True=已发送，False=需要降级"""
        async with self._get_lock(agent_id):
            ws = self._connections.get(agent_id)
            if not ws:
                return False
            try:
                await ws.send_json(event)
                return True
            except Exception:
                # 连接已断开，移除
                self._connections.pop(agent_id, None)
                self._last_pong.pop(agent_id, None)
                return False

    async def broadcast(self, event_type: str, payload: dict):
        """广播给所有已接管且 WS 在线的 Agent"""
        event = {
            "type": event_type,
            "source": "bus",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        tasks = []
        for agent_id in list(self._connections.keys()):
            tasks.append(self.send(agent_id, event))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed = sum(1 for r in results if r is False or isinstance(r, Exception))
            if failed:
                logger.warning("[WS] broadcast: %d/%d delivery failed", failed, len(tasks))

    # ── 状态查询 ─────────────────────────────────────────

    def is_online(self, agent_id: str) -> bool:
        return agent_id in self._connections

    def online_count(self) -> int:
        return len(self._connections)

    async def get_agents(self) -> list[str]:
        return list(self._connections.keys())

    # ── 消息处理 ──────────────────────────────────────────

    def _next_seq(self, agent_id: str) -> int:
        self._seq_counters[agent_id] = self._seq_counters.get(agent_id, 0) + 1
        return self._seq_counters[agent_id]

    async def on_message(self, agent_id: str, raw: str):
        """Agent WS 消息 → 校验 → 自动写入 bus buffer"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[WS] %s sent malformed data, closing", agent_id)
            await self.unregister(agent_id)
            return

        event_type = data.get("type", "")

        # 生命周期事件 → buffer 自动追加
        if event_type.startswith("lifecycle:"):
            event = {
                "seq_id": self._next_seq(agent_id),
                "type": event_type,
                "namespace": f"agent:{agent_id}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "content": data.get("content", ""),
                "session_id": data.get("session_id", ""),
                "tool_name": data.get("tool_name", ""),
                "tool_result": data.get("tool_result"),
            }
            await bus.buffer_append(agent_id, event)

        # 心跳消息
        elif event_type == "ping":
            self._last_pong[agent_id] = datetime.now(timezone.utc)
            try:
                ws = self._connections.get(agent_id)
                if ws:
                    await ws.send_json({"type": "pong"})
            except Exception:
                pass

        # request-response 类型 → 由 Agent 框架层处理
        elif event_type.startswith("request:"):
            pass  # 后续由 Agent 框架的 request handler 处理

    # ── 健康检查 ──────────────────────────────────────────

    async def start_health_check(self, interval_seconds: int = 60,
                                 timeout_seconds: int = 30):
        """启动定期健康检查，清理死连接"""
        if self._health_task and not self._health_task.done():
            return

        async def _check_loop():
            while True:
                await asyncio.sleep(interval_seconds)
                await self._health_check(timeout_seconds)

        self._health_task = asyncio.create_task(_check_loop())
        logger.info("[WS] health check started (interval=%ds, timeout=%ds)",
                     interval_seconds, timeout_seconds)

    async def _health_check(self, timeout_seconds: int = 30):
        """检查所有 WS 连接的健康状态，清理死连接"""
        now = datetime.now(timezone.utc)
        to_remove = []

        for agent_id, ws in list(self._connections.items()):
            # 超过 timeout 无 pong → 尝试 ping
            last = self._last_pong.get(agent_id, now)
            if (now - last).total_seconds() > timeout_seconds:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    to_remove.append(agent_id)

        # 清理确实断开的连接
        for agent_id in to_remove:
            logger.info("[WS] health check: removing stale %s", agent_id)
            await self.unregister(agent_id)

        if to_remove:
            logger.info("[WS] health check: removed %d stale connections", len(to_remove))

    async def stop_health_check(self):
        """停止健康检查"""
        if self._health_task:
            self._health_task.cancel()
            self._health_task = None


# ── FastAPI WebSocket 端点 ────────────────────────────

ws_manager = WSManager()


async def ws_endpoint(websocket: WebSocket, agent_id: str, token: str = ""):
    """统一 WebSocket 端点: /v1/ws/{agent_id}

    A3 (R11): 握手校验 Bearer token，防窃听/伪造。
    """
    from .ws_auth import verify_ws_connection
    if not await verify_ws_connection(agent_id, token):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    await ws_manager.register(agent_id, websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            await ws_manager.on_message(agent_id, raw)
    except WebSocketDisconnect:
        await ws_manager.unregister(agent_id)
    except Exception as e:
        logger.error("[WS] %s error: %s", agent_id, e)
        await ws_manager.unregister(agent_id)
