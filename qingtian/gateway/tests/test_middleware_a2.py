"""A2 (R11) 网关公开前缀收窄 — 单元测试

验证高危端点（zhice 写 / huanyu messages / inbox）在公开前缀下
仍需有效 Bearer token；普通公开路径不受影响。
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "gateway")
from middleware import _is_a2_protected


class TestIsA2Protected:
    """纯逻辑：哪些路径/方法进入 A2 收窄范围"""

    def test_zhice_write_protected(self):
        assert _is_a2_protected("/v1/zhice/tasks", "POST")
        assert _is_a2_protected("/v1/zhice/steps/1/submit", "POST")
        assert _is_a2_protected("/v1/zhice/multisig/1/claim", "POST")

    def test_zhice_read_not_protected(self):
        """zhice 读端点保持公开"""
        assert not _is_a2_protected("/v1/zhice/tasks", "GET")

    def test_huanyu_messages_protected(self):
        assert _is_a2_protected("/v1/huanyu/messages", "POST")
        assert _is_a2_protected("/v1/huanyu/messages/123/read", "POST")

    def test_inbox_get_protected(self):
        """读他人 inbox 需 token"""
        assert _is_a2_protected("/v1/huanyu/inbox/agent-1", "GET")

    def test_other_public_paths_unaffected(self):
        """普通公开路径不受 A2 收窄影响"""
        assert not _is_a2_protected("/v1/auth/token", "POST")
        assert not _is_a2_protected("/v1/xihe/agents/1/status", "GET")
        assert not _is_a2_protected("/health", "GET")
        assert not _is_a2_protected("/v1/huanyu/agents/register", "POST")


class TestA2GuardIntegration:
    """中间件完整流程：无 token 写高危端点被拒；带 token 通过"""

    @pytest.mark.asyncio
    async def test_write_without_token_rejected(self):
        from middleware import RoleCheckMiddlewareASGI

        responses = []

        async def _send(msg):
            if msg["type"] == "http.response.start":
                responses.append(msg["status"])

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _app(scope, receive, send):
            pass  # 不应到达

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/zhice/tasks",
            "headers": [],
            "state": {},
        }

        with patch("middleware._get_middleware_mode", return_value="enforce"):
            mw = RoleCheckMiddlewareASGI(_app)
            await mw(scope, _receive, _send)

        assert responses and responses[0] == 401

    @pytest.mark.asyncio
    async def test_write_with_valid_token_passes(self):
        from middleware import RoleCheckMiddlewareASGI

        reached = []

        async def _send(msg):
            pass

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _app(scope, receive, send):
            reached.append(scope["state"]["agent_id"])

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/zhice/tasks",
            "headers": [(b"authorization", b"Bearer valid-token")],
            "state": {},
        }

        identity = {"agent_id": "agent-1", "role": "agent", "capabilities": []}
        with (
            patch("middleware._get_middleware_mode", return_value="enforce"),
            patch("middleware._resolve_token_identity",
                  new=AsyncMock(return_value=identity)),
        ):
            mw = RoleCheckMiddlewareASGI(_app)
            await mw(scope, _receive, _send)

        assert reached == ["agent-1"]


class TestA2InternalIpcExemption:
    """A2 内部通道豁免（2026-08-25）：loopback + X-Internal-Token 免 Bearer 放行。

    背景：R11 A2 合入 opensource 基底（b91a1953）后，内部无 token 调用方
    （执策网关插件 fetch / skill 子进程 sdk 直连）POST /v1/huanyu/messages
    全被 401，订单确认→询价提示推送被拦。
    """

    def _scope(self, headers, client=("127.0.0.1", 51234)):
        return {
            "type": "http",
            "method": "POST",
            "path": "/v1/huanyu/messages",
            "headers": headers,
            "client": client,
            "state": {},
        }

    @pytest.mark.asyncio
    async def test_loopback_internal_token_passes(self, monkeypatch):
        """loopback + 正确内部令牌 → 免 Bearer 放行到达 app"""
        from middleware import RoleCheckMiddlewareASGI

        monkeypatch.setenv("QINGTIAN_INTERNAL_IPC_TOKEN", "secret-ipc")
        reached = []

        async def _send(msg):
            pass

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _app(scope, receive, send):
            reached.append(True)

        scope = self._scope([(b"x-internal-token", b"secret-ipc")])
        with patch("middleware._get_middleware_mode", return_value="enforce"):
            mw = RoleCheckMiddlewareASGI(_app)
            await mw(scope, _receive, _send)

        assert reached == [True]

    @pytest.mark.asyncio
    async def test_loopback_wrong_internal_token_rejected(self, monkeypatch):
        """loopback + 错误内部令牌 → 401"""
        from middleware import RoleCheckMiddlewareASGI

        monkeypatch.setenv("QINGTIAN_INTERNAL_IPC_TOKEN", "secret-ipc")
        responses = []

        async def _send(msg):
            if msg["type"] == "http.response.start":
                responses.append(msg["status"])

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _app(scope, receive, send):
            pass

        scope = self._scope([(b"x-internal-token", b"wrong")])
        with patch("middleware._get_middleware_mode", return_value="enforce"):
            mw = RoleCheckMiddlewareASGI(_app)
            await mw(scope, _receive, _send)

        assert responses and responses[0] == 401

    @pytest.mark.asyncio
    async def test_loopback_without_internal_token_rejected(self, monkeypatch):
        """loopback 但无内部令牌 → 401（不因 loopback 白放）"""
        from middleware import RoleCheckMiddlewareASGI

        monkeypatch.setenv("QINGTIAN_INTERNAL_IPC_TOKEN", "secret-ipc")
        responses = []

        async def _send(msg):
            if msg["type"] == "http.response.start":
                responses.append(msg["status"])

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _app(scope, receive, send):
            pass

        scope = self._scope([])
        with patch("middleware._get_middleware_mode", return_value="enforce"):
            mw = RoleCheckMiddlewareASGI(_app)
            await mw(scope, _receive, _send)

        assert responses and responses[0] == 401

    @pytest.mark.asyncio
    async def test_remote_with_internal_token_rejected(self, monkeypatch):
        """非 loopback 即使带正确内部令牌 → 401（外部不可用此豁免）"""
        from middleware import RoleCheckMiddlewareASGI

        monkeypatch.setenv("QINGTIAN_INTERNAL_IPC_TOKEN", "secret-ipc")
        responses = []

        async def _send(msg):
            if msg["type"] == "http.response.start":
                responses.append(msg["status"])

        async def _receive():
            return {"type": "http.request", "body": b""}

        async def _app(scope, receive, send):
            pass

        scope = self._scope([(b"x-internal-token", b"secret-ipc")],
                            client=("203.0.113.9", 51234))
        with patch("middleware._get_middleware_mode", return_value="enforce"):
            mw = RoleCheckMiddlewareASGI(_app)
            await mw(scope, _receive, _send)

        assert responses and responses[0] == 401
