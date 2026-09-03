"""
寰宇测试 — 共享 fixtures 和 mock 对象
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ── Mock asyncpg connection ──────────────────────────

class _ConnCtx:
    """模拟 async with conn.acquire() as conn 的上下文管理器"""
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


class _TxCtx:
    """模拟 asyncpg conn.transaction() 返回的事务对象（async context manager）"""
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _MockPool:
    """模拟 asyncpg Pool"""
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _ConnCtx(self._conn)

    async def close(self):
        pass


@pytest.fixture
def mock_conn():
    """模拟 asyncpg connection"""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    # 事务：asyncpg 的 conn.transaction() 同步返回 Transaction 对象
    # （AsyncMock 调用返回 coroutine，无法直接 async with，需显式 mock）
    conn.transaction = MagicMock(return_value=_TxCtx(conn))
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    """模拟 asyncpg pool，返回正确的 async context manager"""
    return _MockPool(mock_conn)


@pytest.fixture(autouse=True)
def clean_env():
    """每个测试前清除环境变量"""
    old = os.environ.pop("HUANYU_SIGN_KEY", None)
    old_config = os.environ.pop("QINGTIAN_CONFIG", None)
    # review(2026-08-24 P0-4): 签名密钥改 fail-closed 后，测试统一注入测试密钥
    os.environ["HUANYU_SIGN_KEY"] = "test-sign-key-2026"
    yield
    os.environ.pop("HUANYU_SIGN_KEY", None)
    if old:
        os.environ["HUANYU_SIGN_KEY"] = old
    if old_config:
        os.environ["QINGTIAN_CONFIG"] = old_config


# ── 样例数据 ─────────────────────────────────────────

@pytest.fixture
def sample_agent_row():
    return {
        "agent_id": "550e8400-e29b-41d4-a716-446655440001",
        "name": "采购Agent-1",
        "category": "biz:buyer",
        "subcategory": "steel",
        "capabilities": ["inquiry", "negotiate"],
        "contact_info": "",
        "server_host": "procurement",
        "status": "active",
        "trust_level": "verified",
        "metadata": {},
        "last_heartbeat": None,
        "deleted_at": None,
        "created_at": None,
        "updated_at": None,
        "heartbeat_interval": None,
    }


@pytest.fixture
def sample_message_row():
    return {
        "message_id": "660e8400-e29b-41d4-a716-446655440002",
        "from_agent_id": "550e8400-e29b-41d4-a716-446655440001",
        "to_agent_id": "550e8400-e29b-41d4-a716-446655440003",
        "message_type": "inquiry",
        "payload": {"product": "螺纹钢", "quantity": "200吨"},
        "negotiation_id": None,
        "reply_to": None,
        "priority": "normal",
        "status": "unread",
        "delivery_status": "local",
        "idempotency_key": "abc123",
        "signature": "hmac_hex",
        "created_at": None,
        "read_at": None,
        "expires_at": None,
    }
