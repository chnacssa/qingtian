"""寰宇 — 底座级 WebSocket 客户端（企业端，连官方 Hub）。

企业底座主动长连官方 Hub 的 `/v1/hub/connect`（设计 v0.6 §二/§八）：

- 只出站，不要求公网入站（解决 NAT/无固定公网 IP）
- 心跳 ping 保活（服务端据此更新连接注册表 last_seen）
- 断线重连：指数退避 + jitter（防重连风暴，贪狼 8.1 坑5）
- 接收 Hub 反向推的消息 → on_message 回调

依赖 `websockets`（与 zhice/agent_daemon.py 一致）。
"""

import asyncio
import json
import logging
import random
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("huanyu.hub_client")

# 进程内单例（main.py 启动时 set_hub_client 注入；messaging 经 get_hub_client 发送）
_hub_client: Optional["HubClient"] = None

# on_message 并发上限：防洪峰时无界 task 堆积（有界信号量，满则排队等待）
_DISPATCH_SEM = asyncio.Semaphore(64)


def set_hub_client(client: Optional["HubClient"]) -> None:
    """注入/清除全局 HubClient 单例（启动/关闭钩子调用）。"""
    global _hub_client
    _hub_client = client


def get_hub_client() -> Optional["HubClient"]:
    """取全局 HubClient 单例（messaging._send_cross_org 经它发送）。"""
    return _hub_client


class HubClient:
    """企业底座 → Hub 的长连客户端（自动重连）。"""

    def __init__(
        self,
        org_id: str,
        token: str,
        hub_url: str,
        on_message: Optional[Callable[[dict], Awaitable[None]]] = None,
        heartbeat_interval: float = 30.0,
        max_backoff: float = 60.0,
    ):
        self.org_id = org_id
        self.token = token
        self.hub_url = hub_url.rstrip("/")
        self.on_message = on_message
        self.heartbeat_interval = heartbeat_interval
        self.max_backoff = max_backoff
        self._ws = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def ws_url(self) -> str:
        base = self.hub_url.replace("http://", "ws://").replace("https://", "wss://")
        return f"{base}/v1/hub/connect?org_id={self.org_id}&token={self.token}"

    async def start(self) -> None:
        """启动长连（后台任务，自动重连）。"""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="hub-client")

    async def stop(self) -> None:
        """停止长连。"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _run_loop(self) -> None:
        import websockets

        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    backoff = 1.0  # 连接成功重置退避
                    logger.info("[trace] hub_client connected org=%s", self.org_id)
                    # 重连后发 resync（对齐点续投，设计 §15.4 / 贪狼方案）
                    await self._send_resync(ws)
                    hb_task = asyncio.create_task(self._heartbeat_loop(ws), name="hub-heartbeat")
                    try:
                        async for raw in ws:
                            await self._handle(raw)
                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[trace] hub_client disconnected org=%s err=%s", self.org_id, e)
            finally:
                self._ws = None

            # 指数退避 + jitter，防重连风暴
            if self._running:
                jitter = random.uniform(0, backoff * 0.3)
                wait = min(backoff + jitter, self.max_backoff)
                logger.info("[trace] hub_client reconnect org=%s in %.1fs", self.org_id, wait)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, self.max_backoff)

    async def _send_resync(self, ws) -> None:
        """重连后发 resync（对齐点续投，设计 §15.4 / 贪狼方案）。

        last_continuous 为 {from_org: 连续窗口}，Hub 据此补推断线期间的消息。
        """
        try:
            from .messaging import _get_all_last_continuous
            last_continuous = await _get_all_last_continuous()
            await ws.send(json.dumps({
                "type": "resync",
                "org_id": self.org_id,
                "last_continuous": last_continuous,
            }))
            logger.info("[trace] hub_client resync org=%s last=%s", self.org_id, last_continuous)
        except Exception as e:
            logger.warning("[trace] hub_client resync fail err=%s", e)

    async def _heartbeat_loop(self, ws) -> None:
        while self._running:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                break

    async def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if msg.get("type") == "pong":
            return
        if msg.get("type") == "hub_ack":
            # Hub 已收信封（可靠投递第一层确认），resolve 发送方等待
            from .messaging import _resolve_ack
            _resolve_ack(msg.get("nonce", ""), "hub_ack")
            return
        if self.on_message:
            # 派发独立 task，慢回调不阻塞本连接接收循环
            asyncio.create_task(self._dispatch(self.on_message, msg))

    @staticmethod
    async def _dispatch(fn, msg: dict) -> None:
        async with _DISPATCH_SEM:
            try:
                await fn(msg)
            except Exception as e:
                logger.warning("hub_client on_message handler error: %s", e)

    async def send_json(self, msg: dict) -> bool:
        """发消息给 Hub（跨企业发送通道）。未连接/发送失败 → False。"""
        ws = self._ws
        if not ws:
            return False
        try:
            await asyncio.wait_for(ws.send(json.dumps(msg, ensure_ascii=False)), timeout=5.0)
            return True
        except Exception:
            return False
