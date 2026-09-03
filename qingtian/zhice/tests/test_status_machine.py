"""执策状态机 — 原子转换测试（mock DB）"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from zhice import status_machine as sm


@pytest.fixture
def mock_conn():
    """创建一个 mock 数据库连接"""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    return conn


def _row(**kwargs):
    """构造一个 mock row（模拟 asyncpg Record）"""
    defaults = {
        "step_id": 1, "task_id": 42, "step_index": 1,
        "title": "test", "instruction": "do something",
        "status": "pending", "status_reason": None,
        "assigned_agent": None, "assigned_at": None,
        "depends_on": None, "acceptance_criteria": None,
        "expected_outputs": None, "outputs": None, "summary": None,
        "auto_retry": 0, "timeout_minutes": 30,
        "idempotency_key": None, "last_heartbeat_at": None,
        "started_at": None, "completed_at": None,
        "created_at": None, "updated_at": None,
    }
    defaults.update(kwargs)
    m = MagicMock()
    m.__getitem__ = lambda self, k: defaults.get(k)
    m.get = defaults.get
    m.keys = lambda: defaults.keys()
    return m


class TestStepTransitions:
    async def test_assign_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="assigned", assigned_agent="agent1")
        result = await sm.step_assign(mock_conn, 1, "agent1")
        assert result is not None

    async def test_assign_wrong_status(self, mock_conn):
        mock_conn.fetchrow.return_value = None  # WHERE status='pending' 不匹配
        result = await sm.step_assign(mock_conn, 1, "agent1")
        assert result is None

    async def test_start_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="in_progress", assigned_agent="agent1")
        result = await sm.step_start(mock_conn, 1, "agent1")
        assert result is not None

    async def test_start_wrong_agent(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await sm.step_start(mock_conn, 1, "wrong_agent")
        assert result is None

    async def test_heartbeat_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="in_progress", status_reason="executing")
        result = await sm.step_heartbeat(mock_conn, 1, "agent1", "executing")
        assert result is not None

    async def test_heartbeat_wrong_status(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await sm.step_heartbeat(mock_conn, 1, "agent1")
        assert result is None

    async def test_complete_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="completed")
        result = await sm.step_complete(mock_conn, 1, "agent1", "done", {"result": "ok"})
        assert result is not None

    async def test_complete_wrong_status(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await sm.step_complete(mock_conn, 1, "agent1", "done", {})
        assert result is None

    async def test_fail_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="failed")
        result = await sm.step_fail(mock_conn, 1, "agent1", "error")
        assert result is not None

    async def test_reject_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="rejected")
        result = await sm.step_reject(mock_conn, 1)
        assert result is not None

    async def test_retry_with_remaining(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="in_progress", auto_retry=1)
        result = await sm.step_retry(mock_conn, 1)
        assert result is not None

    async def test_retry_exhausted(self, mock_conn):
        mock_conn.fetchrow.return_value = None  # auto_retry = 0, UPDATE 不匹配
        result = await sm.step_retry(mock_conn, 1)
        assert result is None

    async def test_timeout_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="timed_out")
        result = await sm.step_timeout(mock_conn, 1)
        assert result is not None

    async def test_timeout_wrong_status(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await sm.step_timeout(mock_conn, 1)
        assert result is None

    async def test_skip_success(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="skipped")
        result = await sm.step_skip(mock_conn, 1)
        assert result is not None

    async def test_cancel_from_pending(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="cancelled")
        result = await sm.step_cancel(mock_conn, 1)
        assert result is not None


class TestTaskTransitions:
    async def test_start(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="running")
        result = await sm.task_start(mock_conn, 42)
        assert result is not None

    async def test_start_already_running(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await sm.task_start(mock_conn, 42)
        assert result is None

    async def test_complete(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="completed")
        result = await sm.task_complete(mock_conn, 42)
        assert result is not None

    async def test_fail(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="failed")
        result = await sm.task_fail(mock_conn, 42, "reason")
        assert result is not None

    async def test_cancel(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(status="cancelled")
        result = await sm.task_cancel(mock_conn, 42)
        assert result is not None

    async def test_cancel_already_terminal(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await sm.task_cancel(mock_conn, 42)
        assert result is None


class TestQueries:
    async def test_get_step_found(self, mock_conn):
        mock_conn.fetchrow.return_value = _row()
        result = await sm.get_step(mock_conn, 1)
        assert result is not None

    async def test_get_step_not_found(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await sm.get_step(mock_conn, 999)
        assert result is None

    async def test_get_task_found(self, mock_conn):
        mock_conn.fetchrow.return_value = _row()
        result = await sm.get_task(mock_conn, 42)
        assert result is not None

    async def test_get_task_steps(self, mock_conn):
        mock_conn.fetch.return_value = [_row(step_index=1), _row(step_index=2)]
        results = await sm.get_task_steps(mock_conn, 42)
        assert len(results) == 2

    async def test_all_steps_terminal_yes(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(remaining=0)
        mock_conn.fetchrow.return_value.__getitem__ = lambda self, k: 0 if k == "remaining" else None
        # Simpler approach: mock the row dict
        row = {"remaining": 0}
        mock_conn.fetchrow.return_value = row
        result = await sm.all_steps_terminal(mock_conn, 42)
        assert result is True

    async def test_all_steps_terminal_no(self, mock_conn):
        row = {"remaining": 3}
        mock_conn.fetchrow.return_value = row
        result = await sm.all_steps_terminal(mock_conn, 42)
        assert result is False

    async def test_task_update_progress(self, mock_conn):
        mock_conn.fetchrow.return_value = _row(progress=75)
        result = await sm.task_update_progress(mock_conn, 42)
        assert result is not None


class TestValidStatusSets:
    def test_valid_task_status(self):
        assert "pending" in sm.VALID_TASK_STATUS
        assert "running" in sm.VALID_TASK_STATUS
        assert "completed" in sm.VALID_TASK_STATUS
        assert "cancelled" in sm.VALID_TASK_STATUS

    def test_valid_step_status(self):
        assert "pending" in sm.VALID_STEP_STATUS
        assert "assigned" in sm.VALID_STEP_STATUS
        assert "in_progress" in sm.VALID_STEP_STATUS
        assert "completed" in sm.VALID_STEP_STATUS
        assert "failed" in sm.VALID_STEP_STATUS
        assert "cancelled" in sm.VALID_STEP_STATUS

    def test_step_terminal(self):
        assert "completed" in sm.STEP_TERMINAL
        assert "failed" in sm.STEP_TERMINAL
        assert "skipped" in sm.STEP_TERMINAL
        assert "cancelled" in sm.STEP_TERMINAL
        assert "in_progress" not in sm.STEP_TERMINAL
