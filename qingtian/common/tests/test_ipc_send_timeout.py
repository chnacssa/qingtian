"""IPC 子进程端 call() send 超时回归（2026-09-02 新服卡死实锤）。

事故链：父进程停读 IPC socket → _ChildTCPTransport.send 的 writer.drain() 无界
背压永不返回 → IPCClient.call 的 send 在 timeout 保护之外 → 进度播报/终态播报
（api.post）永久挂起 → 生成任务协程挂在 _stop_progress，任务行永不标 failed，
进程 0% CPU 睡在 epoll（py-spy 只见主循环 select，协程不可见）。

修复：send 与响应等待同受 timeout 约束（wait_for 包裹，同父进程侧
agent_runtime transport.send 口径）。本测试钉死：send 阻塞 → 超时抛错而非挂起。
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from common.ipc import IPCClient
from common.ipc import Request  # noqa: F401  (传输层编码依赖，保留导入自检)


class _JamTransport:
    """模拟父进程停读：send 永久阻塞（drain 无界背压）。"""

    async def send(self, msg):
        await asyncio.sleep(3600)


class _OkThenJamTransport:
    """send 正常但响应永不来（父进程处理协程丢失）。"""

    def __init__(self):
        self.sent = []
        self._writer = object()  # 模式2 就绪检查通过
        self._reader = object()

    async def send(self, msg):
        self.sent.append(msg)

    async def receive(self):
        await asyncio.sleep(3600)  # 响应永不回来


class TestIPCCallSendTimeout(unittest.TestCase):
    def test_send_jam_raises_timeout_not_hang(self):
        """send 永久阻塞 → timeout 秒内抛 TimeoutError，绝不无限挂起。"""
        client = IPCClient()
        client._transport = _JamTransport()  # 替换传输层

        async def _run():
            return await client.call("api.post", {"path": "/x"}, timeout=0.2)

        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(_run())

    def test_response_wait_still_bounded(self):
        """send 成功但响应永不来（dispatch 未启动路径）→ ConnectionError 超时。"""
        client = IPCClient()
        client._transport = _OkThenJamTransport()
        client._recv_task = None  # 走模式2

        async def _run():
            return await client.call("ping", timeout=0.2)

        with self.assertRaises((ConnectionError, asyncio.TimeoutError)):
            asyncio.run(_run())

    def test_dispatch_mode_response_timeout(self):
        """模式1（dispatch 已启动）：响应 future 超时同样有界（既有行为回归）。"""
        client = IPCClient()
        client._transport = _OkThenJamTransport()
        client._recv_task = MagicMock(spec=asyncio.Task)  # 非 None 即走模式1

        async def _run():
            return await client.call("ping", timeout=0.2)

        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
