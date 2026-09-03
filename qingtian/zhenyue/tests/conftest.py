"""
镇岳测试 — 共享 fixtures 和 mock 对象
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


# ── Mock asyncpg 连接 ──────────────────────────────────

class _ConnCtx:
    """模拟 async with conn.acquire() as conn 的上下文管理器"""
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


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
    # transaction() 支持（write_audit / cleanup 用 advisory lock + 事务）
    tctx = MagicMock()
    tctx.__aenter__ = AsyncMock()
    tctx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tctx)
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    """模拟 asyncpg pool，返回正确的 async context manager"""
    return _MockPool(mock_conn)


@pytest.fixture(autouse=True)
def clean_env():
    """每个测试前清除环境变量"""
    old_admin = os.environ.pop("ZHENYUE_ADMIN_TOKEN", None)
    old_config = os.environ.pop("QINGTIAN_CONFIG", None)
    yield
    if old_admin:
        os.environ["ZHENYUE_ADMIN_TOKEN"] = old_admin
    if old_config:
        os.environ["QINGTIAN_CONFIG"] = old_config
