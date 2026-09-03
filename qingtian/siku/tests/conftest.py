"""
司库测试 — 共享 fixtures 和 mock 对象
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class _ConnCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


class _MockPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _ConnCtx(self._conn)

    async def close(self):
        pass


class _MockTransaction:
    """模拟 async with conn.transaction()"""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    conn.transaction = lambda: _MockTransaction()
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    return _MockPool(mock_conn)


@pytest.fixture
def sample_account():
    return {
        "agent_id": "a1",
        "balance_fen": 500000,
        "frozen_fen": 0,
        "total_recharged": 1000000,
        "available_fen": 500000,
    }


@pytest.fixture
def sample_txn():
    return {
        "txn_id": 1,
        "agent_id": "a1",
        "txn_type": "recharge",
        "amount_fen": 500000,
        "balance_after": 500000,
        "fee_type": "",
        "reference_id": "",
        "idempotency_key": "wx_001",
        "detail": {},
        "created_at": None,
    }
