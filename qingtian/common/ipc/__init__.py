"""IPC 子进程通信模块

父进程(agent_runtime) ↔ 子进程(skill_runner) 通过 STDIN/STDOUT 管道通信。
JSON-RPC 2.0 协议，每行一条消息。

用法 (子进程):
    ipc = IPCClient()
    await ipc.connect()
    async for msg in ipc._transport.iter_receive():
        ...

用法 (父进程):
    transport = StdioTransport(reader, writer)
    server = IPCServer(transport)
    await server.start()
"""

import asyncio
import json
import logging
import os
import sys
import uuid

from .protocol import Request, Response, IPCError, encode, decode

logger = logging.getLogger("common.ipc")

__all__ = [
    "IPCClient", "IPCServer", "StdioTransport",
    "Request", "Response", "IPCError",
]


# ── TCPTransport（子进程端）──

class _ChildTCPTransport:
    """子进程端 TCP 传输 — 连接父进程回环地址

    通过环境变量 QINGTIAN_IPC_PORT 获取端口，连接 127.0.0.1。
    解决了 Docker 容器内 stdio pipe 不可靠的问题。
    """

    def __init__(self):
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False

    async def connect(self) -> None:
        port = int(os.environ.get("QINGTIAN_IPC_PORT", "0"))
        if not port:
            raise ConnectionError("QINGTIAN_IPC_PORT not set")
        for attempt in range(10):
            try:
                self._reader, self._writer = await asyncio.open_connection(
                    "127.0.0.1", port,
                )
                logger.debug("TCP connected to parent on port %d", port)
                return
            except (ConnectionRefusedError, OSError):
                if attempt < 9:
                    await asyncio.sleep(0.1)
        raise ConnectionError(f"Failed to connect to parent on port {port}")

    async def send(self, msg) -> None:
        if self._closed or self._writer is None:
            raise ConnectionError("Transport closed")
        line = encode(msg) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def receive(self):
        if self._closed or self._reader is None:
            raise ConnectionError("Transport closed")
        line = await self._reader.readline()
        if not line:
            self._closed = True
            raise EOFError("Connection closed")
        text = line.decode("utf-8").strip()
        if not text:
            return await self.receive()
        try:
            return decode(text)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON IPC line: %s", text[:100])
            return await self.receive()

    async def iter_receive(self):
        while not self._closed:
            try:
                yield await self.receive()
            except (EOFError, ConnectionError):
                break

    async def close(self):
        self._closed = True
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass


# ── StdioTransport（父进程端）──

class StdioTransport:
    """父进程端传输 — 已有 StreamReader/StreamWriter，直接包装"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def send(self, msg) -> None:
        if self._closed:
            raise ConnectionError("Transport closed")
        line = encode(msg) + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def receive(self):
        if self._closed:
            raise ConnectionError("Transport closed")
        line = await self._reader.readline()
        if not line:
            self._closed = True
            raise EOFError("Pipe closed")
        text = line.decode("utf-8").strip()
        if not text:
            return await self.receive()
        try:
            return decode(text)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON IPC line: %s", text[:100])
            return await self.receive()

    async def iter_receive(self):
        while not self._closed:
            try:
                yield await self.receive()
            except (EOFError, ConnectionError):
                break

    async def close(self):
        self._closed = True


# ── IPCClient（子进程端）──

class IPCClient:
    """子进程端 IPC 客户端

    消息分发机制：
      - 所有入站消息通过 _dispatch() 统一接收
      - Response → 匹配 _pending futures → resolve
      - Request → 放入 _request_queue → 主循环消费
      - call() → 发送请求 → 等待 future → 返回结果
    """

    def __init__(self):
        self._transport = _ChildTCPTransport()
        self._pending: dict[str, asyncio.Future] = {}
        self._request_queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: asyncio.Task | None = None

    async def connect(self) -> None:
        await self._transport.connect()
        self._recv_task = asyncio.create_task(self._dispatch(), name="ipc-dispatch")
        logger.debug("IPC client connected")

    @property
    def transport(self):
        return self._transport

    async def _dispatch(self) -> None:
        """后台协程：统一接收消息，Response→pending, Request→queue"""
        try:
            async for msg in self._transport.iter_receive():
                if isinstance(msg, Response):
                    future = self._pending.pop(msg.id, None)
                    if future and not future.done():
                        if msg.error:
                            future.set_exception(
                                IPCError(msg.error.get("message", "IPC error"),
                                         data=msg.error.get("data")))
                        else:
                            future.set_result(msg.result if msg.result is not None else {})
                elif isinstance(msg, Request):
                    await self._request_queue.put(msg)
        except (EOFError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            # 清理所有 pending
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(ConnectionError("IPC connection lost"))
            self._pending.clear()
            # 通知主循环：dispatch 已退出
            await self._request_queue.put(None)

    async def recv(self) -> Request:
        """从队列取下一个 Request（供主循环调用）。dispatch 退出时返回 None"""
        msg = await self._request_queue.get()
        if msg is None:
            raise EOFError("IPC dispatch exited")
        return msg

    async def call(self, method: str, params: dict | None = None,
                   timeout: float = 30.0) -> dict:
        """发送请求并等待响应

        兼容两种模式：
          - _dispatch 已启动 → 通过未来等待响应（正常模式）
          - _dispatch 未启动 → 直接 iter_receive 同步等待（on_load 阶段早期调用）

        2026-09-02 新服实锤（小智 py-spy + fd 取证）：send 必须与响应等待同受
        timeout 约束——_ChildTCPTransport.send 内 `writer.drain()` 在父进程停读
        IPC socket 时永不返回（无界背压），发送若不加超时，调用方（如 skill 进度
        播报 api.post → _stop_progress 终态播报）会永久挂起，任务行永不标 failed。
        父进程侧同款发送早已 wait_for 包裹（xihe/agent_runtime.py transport.send），
        本处补齐子进程端。send 超时 → TimeoutError，调用方 except 走降级路径。
        """
        import uuid
        req_id = uuid.uuid4().hex[:16]
        request = Request(method=method, params=params or {}, id=req_id)
        await asyncio.wait_for(self._transport.send(request), timeout=timeout)

        # 模式1: _dispatch 已启动 → 用 future
        if self._recv_task is not None:
            future = asyncio.get_running_loop().create_future()
            self._pending[req_id] = future
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            finally:
                self._pending.pop(req_id, None)

        # 模式2: _dispatch 未启动（on_load 阶段早期调用）→ 直接读取
        # 如果 transport 未就绪（writer/reader 为空），直接抛错，不等超时
        if self._transport._writer is None or self._transport._reader is None:
            raise ConnectionError(f"IPC call '{method}' failed: transport not ready")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                msg = await asyncio.wait_for(
                    self._transport.receive(),
                    timeout=deadline - asyncio.get_running_loop().time(),
                )
            except asyncio.TimeoutError:
                raise ConnectionError(f"IPC call '{method}' timed out")
            if isinstance(msg, Response) and msg.id == req_id:
                if msg.error:
                    raise IPCError(msg.error.get("message", "IPC error"))
                return msg.result if msg.result is not None else {}

        raise ConnectionError(f"IPC call '{method}' failed: no response")

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        await self._transport.close()


# ── IPCServer（父进程端）──

class IPCServer:
    """父进程端 IPC 服务器 — 处理来自子进程的请求"""

    def __init__(self, transport: StdioTransport | None = None):
        self._transport = transport
        self._recv_task: asyncio.Task | None = None
        self._handlers: dict[str, callable] = {}

    def register_handler(self, method: str, handler: callable) -> None:
        self._handlers[method] = handler

    @property
    def transport(self):
        return self._transport

    @transport.setter
    def transport(self, value):
        self._transport = value

    async def start(self) -> None:
        if self._recv_task:
            return
        self._recv_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        """持续接收子进程请求"""
        if not self._transport:
            return
        try:
            async for msg in self._transport.iter_receive():
                if isinstance(msg, Request) and not msg.is_notification():
                    handler = self._handlers.get(msg.method)
                    if handler:
                        try:
                            result = await handler(msg.params)
                            await self._transport.send(Response(id=msg.id, result=result))
                        except Exception as e:
                            await self._transport.send(Response(
                                id=msg.id, error={"code": -32002, "message": str(e)[:500]},
                            ))
                    else:
                        await self._transport.send(Response(
                            id=msg.id, error={"code": -32601, "message": f"Method not found: {msg.method}"},
                        ))
        except (EOFError, ConnectionError, asyncio.CancelledError):
            pass

    async def stop(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
