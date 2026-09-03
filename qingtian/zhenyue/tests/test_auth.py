"""
auth.py 单元测试
验证 admin token 和 agent auth 依赖
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from zhenyue.auth import get_current_agent, verify_admin_token, verify_agent_auth


class TestVerifyAdminToken:
    @pytest.mark.asyncio
    async def test_match(self):
        """匹配的 X-Admin-Token → 返回 agent_id"""
        os.environ["ZHENYUE_ADMIN_TOKEN"] = "secret-admin-token"

        request = AsyncMock()
        request.headers = {"X-Admin-Token": "secret-admin-token"}

        result = await verify_admin_token(request)
        assert result == "admin:console"

    @pytest.mark.asyncio
    async def test_mismatch(self):
        """不匹配的 X-Admin-Token → 401"""
        os.environ["ZHENYUE_ADMIN_TOKEN"] = "secret-admin-token"

        request = AsyncMock()
        request.headers = {"X-Admin-Token": "wrong-token"}

        with pytest.raises(HTTPException) as exc:
            await verify_admin_token(request)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_env(self):
        """未配置 ZHENYUE_ADMIN_TOKEN → 401"""
        # 确保环境变量不存在
        os.environ.pop("ZHENYUE_ADMIN_TOKEN", None)

        request = AsyncMock()
        request.headers = {"X-Admin-Token": "anything"}

        with pytest.raises(HTTPException) as exc:
            await verify_admin_token(request)
        assert exc.value.status_code == 401
        assert "not configured" in exc.value.detail

    @pytest.mark.asyncio
    async def test_missing_header(self):
        """缺少 X-Admin-Token 头 → 401"""
        os.environ["ZHENYUE_ADMIN_TOKEN"] = "secret-admin-token"

        request = AsyncMock()
        request.headers = {}

        with pytest.raises(HTTPException) as exc:
            await verify_admin_token(request)
        assert exc.value.status_code == 401
        assert "Missing" in exc.value.detail


class TestVerifyAgentAuth:
    @pytest.mark.asyncio
    async def test_valid_token(self, mock_conn, mock_pool):
        """有效 Bearer Token → 返回 agent_id"""
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-token")
        request = AsyncMock(client=None)

        mock_conn.fetchrow.side_effect = [
            {"agent_id": "agent-001", "role": "agent", "expires_at": None, "revoked": False},
        ]

        with (
            patch("zhenyue.auth.get_pool", return_value=mock_pool),
            patch("zhenyue.token_service.validate_token", return_value={
                "agent_id": "agent-001", "role": "agent", "capabilities": [],
            }),
        ):
            result = await verify_agent_auth(request, credentials)
            assert result == "agent-001"

    @pytest.mark.asyncio
    async def test_expired_token(self, mock_conn, mock_pool):
        """过期 Token → 401"""
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="expired-token")
        request = AsyncMock(client=None)

        with (
            patch("zhenyue.auth.get_pool", return_value=mock_pool),
            patch("zhenyue.token_service.validate_token", return_value=None),
        ):
            with pytest.raises(HTTPException) as exc:
                await verify_agent_auth(request, credentials)
            assert exc.value.status_code == 401
            assert "Invalid or expired" in exc.value.detail

    @pytest.mark.asyncio
    async def test_missing_token(self):
        """缺少 Token → 401"""
        with pytest.raises(HTTPException) as exc:
            await verify_agent_auth(AsyncMock(client=None), None)
        assert exc.value.status_code == 401
        assert "Missing" in exc.value.detail

    @pytest.mark.asyncio
    async def test_localhost_bypass_with_token(self):
        """A1: loopback + 内部令牌 → 免认证返回 internal-ipc"""
        os.environ["QINGTIAN_INTERNAL_IPC_TOKEN"] = "test-ipc-token"
        request = AsyncMock()
        request.client = SimpleNamespace(host="127.0.0.1")
        request.headers = {"X-Internal-Token": "test-ipc-token"}
        result = await verify_agent_auth(request)
        assert result == "internal-ipc"
        os.environ.pop("QINGTIAN_INTERNAL_IPC_TOKEN", None)

    @pytest.mark.asyncio
    async def test_localhost_without_token_rejected(self):
        """A1: loopback 但无内部令牌 → 不再免认证（401）"""
        os.environ["QINGTIAN_INTERNAL_IPC_TOKEN"] = "test-ipc-token"
        try:
            request = AsyncMock()
            request.client = SimpleNamespace(host="127.0.0.1")
            request.headers = {}
            with pytest.raises(HTTPException) as exc:
                await verify_agent_auth(request)
            assert exc.value.status_code == 401
        finally:
            os.environ.pop("QINGTIAN_INTERNAL_IPC_TOKEN", None)

    @pytest.mark.asyncio
    async def test_localhost_no_token_configured_rejected(self):
        """A1: 未配置内部令牌 → loopback 免认证关闭（401）"""
        os.environ.pop("QINGTIAN_INTERNAL_IPC_TOKEN", None)
        request = AsyncMock()
        request.client = SimpleNamespace(host="127.0.0.1")
        request.headers = {"X-Internal-Token": "anything"}
        with pytest.raises(HTTPException) as exc:
            await verify_agent_auth(request)
        assert exc.value.status_code == 401


class TestGetCurrentAgent:
    @pytest.mark.asyncio
    async def test_returns_agent_id(self, mock_conn, mock_pool):
        """get_current_agent 返回 agent_id 字符串"""
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid-token")
        request = AsyncMock(client=None)

        with (
            patch("zhenyue.auth.get_pool", return_value=mock_pool),
            patch("zhenyue.token_service.validate_token", return_value={
                "agent_id": "agent-001", "role": "agent", "capabilities": [],
            }),
        ):
            result = await get_current_agent(request, credentials)
            assert result == "agent-001"


class TestAuthDependencyRole:
    """R11 (P?): auth_dependency 返回 role/capabilities — admin 端点鉴权不再恒 403"""

    @pytest.mark.asyncio
    async def test_returns_role_for_admin(self, mock_conn, mock_pool):
        """admin token → role=admin，zhice 的 admin 端点可放行"""
        from zhenyue.auth import auth_dependency

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="admin-token")
        request = AsyncMock(client=None)

        with (
            patch("zhenyue.auth.is_internal_ipc", return_value=False),
            patch("zhenyue.auth.get_pool", return_value=mock_pool),
            patch("zhenyue.auth.token_service.authenticate", new=AsyncMock(return_value={
                "agent_id": "admin", "role": "admin", "capabilities": ["admin", "ops_admin"],
            })),
        ):
            info = await auth_dependency(request, credentials)

        assert info["authenticated"] is True
        assert info["agent_id"] == "admin"
        assert info["role"] == "admin"
        assert "ops_admin" in info["capabilities"]

    @pytest.mark.asyncio
    async def test_returns_role_for_agent(self, mock_conn, mock_pool):
        """普通 agent token → role=agent（无 admin 能力）"""
        from zhenyue.auth import auth_dependency

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="agent-token")
        request = AsyncMock(client=None)

        with (
            patch("zhenyue.auth.is_internal_ipc", return_value=False),
            patch("zhenyue.auth.get_pool", return_value=mock_pool),
            patch("zhenyue.auth.token_service.authenticate", new=AsyncMock(return_value={
                "agent_id": "agent-1", "role": "agent", "capabilities": [],
            })),
        ):
            info = await auth_dependency(request, credentials)

        assert info["role"] == "agent"
        assert info["capabilities"] == []

    @pytest.mark.asyncio
    async def test_internal_ipc_maps_to_admin(self, mock_conn, mock_pool):
        """内部 IPC（loopback + 内部令牌）→ admin 角色，且无需 Bearer"""
        from zhenyue.auth import auth_dependency

        request = AsyncMock(client=None)

        with patch("zhenyue.auth.is_internal_ipc", return_value=True):
            info = await auth_dependency(request, None)

        assert info["agent_id"] == "internal-ipc"
        assert info["role"] == "admin"

    @pytest.mark.asyncio
    async def test_unauth_rejected(self, mock_conn, mock_pool):
        """无凭据无 Bearer 非 IPC → 401"""
        from fastapi import HTTPException
        from zhenyue.auth import auth_dependency

        request = AsyncMock(client=None)
        request.headers = {}

        with patch("zhenyue.auth.is_internal_ipc", return_value=False):
            with pytest.raises(HTTPException) as exc:
                await auth_dependency(request, None)
        assert exc.value.status_code == 401
