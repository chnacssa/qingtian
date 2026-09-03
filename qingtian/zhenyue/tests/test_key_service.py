"""镇岳密钥服务测试 — generate_keypair / get_public_key / get_private_key / revoke"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


def _mock_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


class TestGenerateKeypair:
    @pytest.mark.asyncio
    async def test_generates_and_returns_public_key(self):
        conn = _mock_conn()
        conn.fetchval = AsyncMock(return_value=1)  # key_id
        from zhenyue.key_service import generate_keypair
        result = await generate_keypair(conn, "agent:test")
        assert result["agent_id"] == "agent:test"
        assert result["algorithm"] == "ed25519"
        assert result["status"] == "active"
        assert len(result["public_key"]) == 64  # 32 bytes hex
        assert "private_key" not in result     # 私钥不在返回值中

    @pytest.mark.asyncio
    async def test_revokes_previous_active_keys(self):
        conn = _mock_conn()
        conn.fetchval = AsyncMock(return_value=2)  # key_id
        from zhenyue.key_service import generate_keypair
        await generate_keypair(conn, "agent:test")
        # 应先撤销旧密钥
        revoke_call = conn.execute.call_args_list[0]
        assert "revoked" in str(revoke_call)
        assert "agent:test" in str(revoke_call)


class TestGetPublicKey:
    @pytest.mark.asyncio
    async def test_returns_key_when_active(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(return_value={
            "key_id": 1, "public_key": "ab" * 32, "algorithm": "ed25519",
            "created_at": datetime(2026, 1, 1),
        })
        from zhenyue.key_service import get_public_key
        result = await get_public_key(conn, "agent:test")
        assert result is not None
        assert result["public_key"] == "ab" * 32

    @pytest.mark.asyncio
    async def test_returns_none_when_no_active_key(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        from zhenyue.key_service import get_public_key
        result = await get_public_key(conn, "agent:test")
        assert result is None


class TestGetPrivateKey:
    @pytest.mark.asyncio
    async def test_decrypts_and_returns_private_key(self):
        conn = _mock_conn()
        # 模拟 encryptor 加密的数据
        from zhenyue.encryptor import encryptor
        encrypted = encryptor.encrypt({"private_key": "cd" * 32})
        conn.fetchrow = AsyncMock(return_value={"private_key": encrypted})
        from zhenyue.key_service import get_private_key
        result = await get_private_key(conn, "agent:test")
        assert result == "cd" * 32

    @pytest.mark.asyncio
    async def test_returns_none_when_no_key(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        from zhenyue.key_service import get_private_key
        result = await get_private_key(conn, "agent:test")
        assert result is None


class TestRevokeKeypair:
    @pytest.mark.asyncio
    async def test_revokes_and_returns_true(self):
        conn = _mock_conn()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        from zhenyue.key_service import revoke_keypair
        result = await revoke_keypair(conn, "agent:test")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_active_key(self):
        conn = _mock_conn()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        from zhenyue.key_service import revoke_keypair
        result = await revoke_keypair(conn, "agent:test")
        assert result is False
