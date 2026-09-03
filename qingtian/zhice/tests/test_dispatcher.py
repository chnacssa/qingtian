"""执策分派器 — Phase 2 测试（mock DB + mock WS）"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_ctx(return_value=None):
    """构造 async context manager mock"""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=return_value)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    # transaction() 返回 async context manager（不能用 AsyncMock 默认的 coroutine）
    conn.transaction = MagicMock(return_value=_make_ctx())
    return conn


def _setup_pool_patch(mock_conn):
    """为 dispatcher 中 `pool = await get_pool(); async with pool.acquire()` 模式设置 mock"""
    mock_pool = MagicMock()

    # async with pool.acquire() as conn → mock_conn
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire.return_value = ctx

    # await get_pool() → mock_pool
    return patch("zhice.dispatcher.get_pool", AsyncMock(return_value=mock_pool))


def _agent_row(agent_id="agent1", name="小运", status="active"):
    m = MagicMock()
    d = {"agent_id": agent_id, "name": name, "status": status}
    m.__getitem__ = lambda self, k: d.get(k)
    m.get = lambda k, default=None: d.get(k, default)
    return m


def _step_row(**kwargs):
    defaults = {
        "step_id": 1, "task_id": 42, "step_index": 3,
        "title": "test_step", "instruction": "do it",
        "status": "pending", "status_reason": None,
        "assigned_agent": None, "assigned_at": None,
        "acceptance_criteria": None, "auto_retry": 0,
        "timeout_minutes": 30, "started_at": None,
        "last_heartbeat_at": None, "depends_on": None,
    }
    defaults.update(kwargs)
    m = MagicMock()
    m.__getitem__ = lambda self, k: defaults.get(k)
    m.get = defaults.get
    m.keys = lambda: defaults.keys()
    return m


class TestAssignStep:
    async def test_assign_success(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            _agent_row(),
            _step_row(),
            _step_row(status="assigned", assigned_agent="agent1"),
        ]

        with _setup_pool_patch(mock_conn):
            with patch("zhice.dispatcher.ws_notify", new_callable=AsyncMock) as mock_ws:
                from zhice import dispatcher
                result = await dispatcher.assign_step(42, 3, "小运", "master")

        assert result["success"]
        assert result["assigned_agent"] == "agent1"
        mock_ws.assert_called_once()
        assert mock_ws.call_args[0][0] == "agent1"
        assert mock_ws.call_args[0][1] == "assigned"

    async def test_assign_agent_not_found(self, mock_conn):
        mock_conn.fetchrow.return_value = None

        with _setup_pool_patch(mock_conn):
            from zhice import dispatcher
            result = await dispatcher.assign_step(42, 3, "nonexistent", "master")

        assert not result["success"]
        assert "不存在" in result["error"]

    async def test_assign_agent_inactive(self, mock_conn):
        mock_conn.fetchrow.return_value = _agent_row(status="suspended")

        with _setup_pool_patch(mock_conn):
            from zhice import dispatcher
            result = await dispatcher.assign_step(42, 3, "小运", "master")

        assert not result["success"]
        assert "suspended" in result["error"]

    async def test_assign_step_not_pending(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            _agent_row(),
            _step_row(status="in_progress"),
        ]

        with _setup_pool_patch(mock_conn):
            from zhice import dispatcher
            result = await dispatcher.assign_step(42, 3, "小运", "master")

        assert not result["success"]
        assert "in_progress" in result["error"]

    async def test_assign_step_not_found(self, mock_conn):
        mock_conn.fetchrow.side_effect = [_agent_row(), None]

        with _setup_pool_patch(mock_conn):
            from zhice import dispatcher
            result = await dispatcher.assign_step(42, 99, "小运", "master")

        assert not result["success"]

    async def test_assign_concurrent_modification(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            _agent_row(), _step_row(), None,
        ]

        with _setup_pool_patch(mock_conn):
            from zhice import dispatcher
            result = await dispatcher.assign_step(42, 3, "小运", "master")

        assert not result["success"]
        assert "并发" in result["error"]


class TestRecoveryState:
    async def test_recovery_with_unfinished(self, mock_conn):
        mock_conn.fetch.side_effect = [
            [_step_row(step_id=1, task_id=42, step_index=1, status="in_progress",
                       assigned_agent="agent1", auto_retry=2)],
            [_step_row(step_id=2, task_id=42, step_index=2, status="pending", depends_on=[1])],
        ]

        with _setup_pool_patch(mock_conn):
            from zhice import dispatcher
            result = await dispatcher.get_recovery_state("agent1")

        assert result["agent_id"] == "agent1"
        assert len(result["unfinished"]) == 1
        assert result["unfinished"][0]["status"] == "in_progress"
        assert result["unfinished"][0]["retries_left"] == 2
        assert len(result["pending"]) == 1

    async def test_recovery_no_unfinished(self, mock_conn):
        mock_conn.fetch.return_value = []

        with _setup_pool_patch(mock_conn):
            from zhice import dispatcher
            result = await dispatcher.get_recovery_state("agent1")

        assert result["agent_id"] == "agent1"
        assert len(result["unfinished"]) == 0
        assert len(result["pending"]) == 0
