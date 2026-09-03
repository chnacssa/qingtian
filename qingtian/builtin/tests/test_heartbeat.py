"""
infra:heartbeat-monitor — 心跳监控 Agent 测试

测试 check_stale_agents、check_suspended_agents、run_once 方法，
通过 mock get_pool 控制 DB 返回值。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from builtin.heartbeat_monitor_agent import (
    check_stale_agents,
    check_suspended_agents,
    run_once,
    get_agent_id,
    AGENT_ID,
)


# ── Mock DB helpers (following huanyu/tests/conftest.py pattern) ──

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


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.execute = AsyncMock()
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    return _MockPool(mock_conn)


# ══════════════════════════════════════════════════════════
# get_agent_id
# ══════════════════════════════════════════════════════════

class TestAgentId:
    def test_get_agent_id_returns_constant(self):
        assert get_agent_id() == AGENT_ID
        assert AGENT_ID == "infra:heartbeat-monitor-01"


# ══════════════════════════════════════════════════════════
# check_stale_agents
# ══════════════════════════════════════════════════════════

class TestCheckStaleAgents:
    async def test_marks_stale_agents_inactive(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(return_value=[
            {"agent_id": "biz:buyer-01"},
            {"agent_id": "biz:seller-01"},
        ])

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            result = await check_stale_agents()

        assert result == ["biz:buyer-01", "biz:seller-01"]
        mock_conn.fetch.assert_called_once()
        query = mock_conn.fetch.call_args[0][0]
        assert "UPDATE huanyu.agents" in query
        assert "status = 'inactive'" in query

    async def test_no_stale_agents(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            result = await check_stale_agents()

        assert result == []

    async def test_db_error_propagates(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(side_effect=Exception("DB error"))

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            with pytest.raises(Exception, match="DB error"):
                await check_stale_agents()


# ══════════════════════════════════════════════════════════
# check_suspended_agents
# ══════════════════════════════════════════════════════════

class TestCheckSuspendedAgents:
    async def test_marks_inactive_agents_suspended(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(return_value=[
            {"agent_id": "biz:zombie-01"},
        ])

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            result = await check_suspended_agents()

        assert result == ["biz:zombie-01"]
        mock_conn.fetch.assert_called_once()
        query = mock_conn.fetch.call_args[0][0]
        assert "UPDATE huanyu.agents" in query
        assert "status = 'suspended'" in query
        assert "INTERVAL '24 hours'" in query

    async def test_no_agents_to_suspend(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            result = await check_suspended_agents()

        assert result == []


# ══════════════════════════════════════════════════════════
# run_once
# ══════════════════════════════════════════════════════════

class TestRunOnce:
    async def test_run_once_returns_counts(self, mock_pool, mock_conn):
        # First fetch = check_stale_agents, second fetch = check_suspended_agents
        mock_conn.fetch = AsyncMock()
        mock_conn.fetch.side_effect = [
            [{"agent_id": "biz:stale-01"}, {"agent_id": "biz:stale-02"}],  # stale
            [{"agent_id": "biz:zombie-01"}],  # suspended
        ]

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            result = await run_once()

        assert result == {"stale_count": 2, "suspended_count": 1}
        assert mock_conn.fetch.call_count == 2

    async def test_run_once_no_agents(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            result = await run_once()

        assert result == {"stale_count": 0, "suspended_count": 0}
        assert mock_conn.fetch.call_count == 2

    async def test_run_once_stale_query_fails(self, mock_pool, mock_conn):
        # First query fails, but run_once doesn't catch (the error is at run() level)
        mock_conn.fetch = AsyncMock(side_effect=Exception("Stale check failed"))

        with patch("builtin.heartbeat_monitor_agent.get_pool", AsyncMock(return_value=mock_pool)):
            with pytest.raises(Exception, match="Stale check failed"):
                await run_once()
