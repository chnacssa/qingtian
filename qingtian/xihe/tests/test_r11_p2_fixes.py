"""P2 (R11) 收口 — xihe 模块回归测试

覆盖:
  P2-1  IPC 裸 TCP 握手（随机 challenge + HMAC 令牌校验），防同机进程冒充
  P2-2  _stop_child transport.send drain 无超时 → 永久阻塞
  P2-3  untrusted Skill 不再裸奔（施加 low 配额 CPU 隔离），失败明确降级
  P2-4  出站监控按 socket inode 精确归因，不再把宿主其他进程连接误报

全部为纯逻辑/单测，不真正 spawn 子进程。
"""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from xihe.agent_runtime import (
    ChildProcess,
    XiheRuntime,
    _check_egress,
    _ipc_handshake_response,
    _proc_socket_inodes,
    _verify_ipc_handshake,
)


class TestIpcHandshake:
    """P2-1: 握手应答计算与校验（父/子两侧共用同一实现）"""

    def test_response_is_hmac_sha256(self):
        expected = __import__("hmac").new(
            b"secret-tok", b"chal-123",
            __import__("hashlib").sha256,
        ).hexdigest()
        assert _ipc_handshake_response("secret-tok", "chal-123") == expected

    def test_verify_accepts_correct_response(self):
        token, challenge = "secret-tok", "chal-123"
        resp = _ipc_handshake_response(token, challenge)
        assert _verify_ipc_handshake(resp, token, challenge) is True

    def test_verify_rejects_wrong_token(self):
        token, challenge = "secret-tok", "chal-123"
        resp = _ipc_handshake_response("other-tok", challenge)
        assert _verify_ipc_handshake(resp, token, challenge) is False

    def test_verify_rejects_wrong_challenge(self):
        token, challenge = "secret-tok", "chal-123"
        resp = _ipc_handshake_response(token, "chal-999")
        assert _verify_ipc_handshake(resp, token, challenge) is False

    def test_verify_rejects_non_string_response(self):
        assert _verify_ipc_handshake(None, "tok", "chal") is False
        assert _verify_ipc_handshake(123, "tok", "chal") is False

    @pytest.mark.asyncio
    async def test_child_sends_expected_hmac_response(self):
        """skill_runner._connect_ipc_with_handshake 应答与父进程校验一致"""
        from xihe.skill_runner import _connect_ipc_with_handshake

        class _FakeReader:
            def __init__(self, line):
                self._line = line

            async def readline(self):
                return self._line

        class _FakeWriter:
            def __init__(self):
                self.data = b""

            def write(self, b):
                self.data += b

            async def drain(self):
                pass

            async def wait_closed(self):
                pass

            def close(self):
                pass

        challenge = "abc123challenge"
        reader = _FakeReader(
            (json.dumps({"v": 1, "type": "ipc.handshake", "challenge": challenge}) + "\n").encode(),
        )
        writer = _FakeWriter()

        class _FakeIPC:
            class _Tr:
                pass

            _transport = _Tr()
            _recv_task = None

            async def _dispatch(self):
                return None

        ipc = _FakeIPC()
        with patch.dict(os.environ, {
            "QINGTIAN_IPC_PORT": "19999",
            "QINGTIAN_IPC_AUTH_TOKEN": "secret-tok",
        }), patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            await _connect_ipc_with_handshake(ipc)

        expected = _ipc_handshake_response("secret-tok", challenge)
        reply = json.loads(writer.data.decode("utf-8"))
        assert reply["type"] == "ipc.handshake"
        assert reply["response"] == expected
        assert ipc._transport._reader is reader
        assert ipc._transport._writer is writer

        if ipc._recv_task is not None:
            ipc._recv_task.cancel()
            try:
                await ipc._recv_task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_child_handshake_fails_without_token(self):
        """子进程未带令牌 → 拒绝建立连接"""
        from xihe.skill_runner import _connect_ipc_with_handshake

        class _FakeIPC:
            class _Tr:
                pass

            _transport = _Tr()
            _recv_task = None

            async def _dispatch(self):
                return None

        with patch.dict(os.environ, {"QINGTIAN_IPC_PORT": "19999"}):
            os.environ.pop("QINGTIAN_IPC_AUTH_TOKEN", None)
            with pytest.raises(ConnectionError, match="AUTH_TOKEN"):
                await _connect_ipc_with_handshake(_FakeIPC())


class TestStopChildSendTimeout:
    """P2-2: _stop_child 的 on_unload 发送加超时，超时直接强杀"""

    @pytest.mark.asyncio
    async def test_send_hang_does_not_block_and_force_kills(self):
        runtime = XiheRuntime()
        child = ChildProcess(skill_name="s", agent_id="a1")

        class _HangingTransport:
            async def send(self, msg):
                await asyncio.Event().wait()  # 模拟 drain 永久阻塞

            async def close(self):
                pass

        class _FakeProc:
            returncode = None
            pid = 9999
            killed = False

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def wait(self):
                if self.killed:
                    return -9
                await asyncio.sleep(10)
                return 0

        child.process = _FakeProc()
        child.transport = _HangingTransport()
        child.ipc_server = None

        with patch("xihe.agent_runtime._cleanup_cgroup"):
            # 发送超时（2s）→ 强杀；整体必须在 5s 内返回，而非永久阻塞
            await asyncio.wait_for(runtime._stop_child(child), timeout=5.0)
        assert child.process.killed


class TestApplyCpuLimit:
    """P2-3: untrusted 施加 low 配额 CPU 隔离；失败明确降级"""

    class _FakeBus:
        def __init__(self):
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)

    @pytest.mark.asyncio
    async def test_untrusted_applies_low_cpu_quota(self):
        runtime = XiheRuntime()
        child = ChildProcess(skill_name="odd", agent_id="a1", trust_level="untrusted")
        proc = SimpleNamespace(pid=123)
        bus = self._FakeBus()

        with patch("xihe.scheduler.set_cpu_weight", return_value=True) as scw, \
             patch("common.admin_message.create_admin_bus", return_value=bus):
            result = await runtime._apply_cpu_limit(child, {}, proc)

        assert result is True
        scw.assert_called_once_with(123, "low")  # untrusted 一律 low 配额
        assert bus.sent and bus.sent[0].level == "warning"
        assert "CPU 隔离失败" not in bus.sent[0].title

    @pytest.mark.asyncio
    async def test_untrusted_cpu_fail_records_degradation(self):
        runtime = XiheRuntime()
        child = ChildProcess(skill_name="odd", agent_id="a1", trust_level="untrusted")
        proc = SimpleNamespace(pid=123)
        bus = self._FakeBus()

        with patch("xihe.scheduler.set_cpu_weight", return_value=False) as scw, \
             patch("common.admin_message.create_admin_bus", return_value=bus):
            result = await runtime._apply_cpu_limit(child, {}, proc)

        assert result is False
        scw.assert_called_once_with(123, "low")
        assert bus.sent and bus.sent[0].level == "warning"
        assert "CPU 隔离失败" in bus.sent[0].title  # 明确降级原因，不静默

    @pytest.mark.asyncio
    async def test_trusted_uses_declared_priority(self):
        runtime = XiheRuntime()
        child = ChildProcess(skill_name="good", agent_id="a1", trust_level="trusted")
        proc = SimpleNamespace(pid=456)
        bus = self._FakeBus()

        with patch("xihe.scheduler.set_cpu_weight", return_value=True) as scw, \
             patch("common.admin_message.create_admin_bus", return_value=bus):
            result = await runtime._apply_cpu_limit(child, {"priority": "high"}, proc)

        assert result is True
        scw.assert_called_once_with(456, "high")
        assert bus.sent and bus.sent[0].level == "info"


# ── 出站监控（P2-4）─────────────
# /proc/net/tcp 行格式：sl local rem st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode ...
_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"
# inode=12345 归属本 pid，remote 45.32.32.156:443 → 可疑（不白名单）
_TCP_SUSPICIOUS = (
    "   1: 0100007F:13AD 9C20202D:01BB 01 00000000:00000000 00:00000000 00000000 1000 0 "
    "12345 1 0000000000000000 100 0 0 10 0"
)
# inode=12345 归属本 pid，remote 0.0.0.0:0 → 未建立连接，跳过
_TCP_UNCONNECTED = (
    "   0: 0100007F:13AD 00000000:0000 0A 00000000:00000000 00:00000000 00000000 1000 0 "
    "12345 1 0000000000000000 100 0 0 10 0"
)
# inode=99999 归属本 pid，remote 192.168.1.1:80 → 白名单放行
_TCP_WHITELISTED = (
    "   2: 0100007F:13AD 0101A8C0:0050 01 00000000:00000000 00:00000000 00000000 1000 0 "
    "99999 1 0000000000000000 100 0 0 10 0"
)
# inode=77777 归属宿主其他进程，remote 8.8.8.8:53 → 必须被 inode 归因过滤（不误报）
_TCP_FOREIGN = (
    "   3: 0100007F:13AD 08080808:0035 01 00000000:00000000 00:00000000 00000000 1000 0 "
    "77777 1 0000000000000000 100 0 0 10 0"
)
_TCP_LINES = [_HEADER, _TCP_UNCONNECTED, _TCP_SUSPICIOUS, _TCP_WHITELISTED, _TCP_FOREIGN]


class _FakeFile:
    def __init__(self, lines):
        self._lines = lines

    def readlines(self):
        return self._lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestProcSocketInodes:
    """P2-4: 按 pid fd 表提取 socket inode"""

    def test_extracts_socket_inodes(self):
        entries = [SimpleNamespace(name="3"), SimpleNamespace(name="4")]

        def fake_readlink(path):
            norm = path.replace("\\", "/")  # Windows 分隔符归一
            if norm.endswith("fd/3"):
                return "socket:[12345]"
            if norm.endswith("fd/4"):
                return "socket:[99999]"
            return "pipe:[1]"

        with patch("os.scandir", return_value=iter(entries)), \
             patch("os.readlink", side_effect=fake_readlink):
            inodes = _proc_socket_inodes(4242)
        assert inodes == {12345, 99999}

    def test_returns_empty_on_scan_error(self):
        with patch("os.scandir", side_effect=PermissionError("denied")):
            assert _proc_socket_inodes(9999) == set()


class TestCheckEgress:
    """P2-4: 出站连接按 socket inode 精确归因"""

    def _patch_fds(self, inode_map):
        entries = [SimpleNamespace(name=str(i)) for i in inode_map]

        def fake_readlink(path):
            norm = path.replace("\\", "/")  # Windows 分隔符归一
            for i, ino in inode_map.items():
                if norm.endswith(f"fd/{i}"):
                    return f"socket:[{ino}]"
            return "not-a-socket"

        return entries, fake_readlink

    def test_attributes_connections_by_inode(self):
        pid = 4242
        entries, fake_readlink = self._patch_fds({3: 12345, 4: 99999})

        real_open = open

        def fake_open(path, *a, **k):
            # 同时覆盖新实现(/proc/net/*)与旧实现(/proc/<pid>/net/*)，
            # 若退化为旧的按 pid 读全量表，本测试会因 8.8.8.8 被误报而失败
            norm = path.replace("\\", "/")
            if norm.endswith("/proc/net/tcp") or norm.endswith(f"/proc/{pid}/net/tcp"):
                return _FakeFile(_TCP_LINES)
            if norm.endswith("/proc/net/udp") or norm.endswith(f"/proc/{pid}/net/udp"):
                return _FakeFile([_HEADER])
            return real_open(path, *a, **k)

        with patch("os.scandir", return_value=iter(entries)), \
             patch("os.readlink", side_effect=fake_readlink), \
             patch("builtins.open", side_effect=fake_open):
            result = _check_egress(pid)

        # 仅 inode 归属本 pid 且非白名单的连接被报告：
        #   * 45.32.32.156:443 可疑 → 报
        #   * 0.0.0.0:0 未连接 → 跳过
        #   * 192.168.1.1:80 白名单 → 放行
        #   * 8.8.8.8:53 宿主其他进程（inode 77777）→ 不误报
        assert result == [{"proto": "tcp", "remote": "45.32.32.156:443"}]

    def test_degrades_to_noop_when_cannot_scan_fds(self):
        with patch("os.scandir", side_effect=PermissionError("denied")):
            assert _check_egress(9999) == []
