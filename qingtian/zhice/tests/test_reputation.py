"""执策信誉联动测试 — reverify + C-Level 自动调整"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import zhice.api  # ensure submodule is loaded for patching


class TestReverifyEndpoint:
    def _make_pool(self, mock_conn):
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx
        return pool

    def _step_row(self, **overrides):
        d = {
            "step_id": 1, "task_id": 42, "step_index": 1, "title": "S",
            "instruction": "Do", "status": "completed", "status_reason": None,
            "assigned_agent": "agent1", "assigned_at": None, "depends_on": [],
            "acceptance_criteria": [{"type": "file_exists", "path": "/x", "required": True}],
            "expected_outputs": None, "outputs": {}, "summary": "done",
            "auto_retry": 0, "timeout_minutes": 30, "idempotency_key": None,
            "last_heartbeat_at": None, "started_at": None, "completed_at": None,
            "created_at": None, "updated_at": None,
        }
        d.update(overrides)
        row = MagicMock()
        row.__getitem__ = lambda self, k: d.get(k)
        row.get = d.get
        row.keys = lambda: d.keys()
        return row

    @pytest.mark.asyncio
    async def test_reverify_completed_step(self):
        """抽查已完成的 Step → 成功"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[
            self._step_row(),  # sm.get_step
            self._step_row(outputs={"reverify_requested": True}),  # fresh get_step
        ])
        conn.execute = AsyncMock()

        with patch("zhice.api.get_pool", AsyncMock(return_value=self._make_pool(conn))):
            with patch("zhice.dispatcher.ws_notify", new_callable=AsyncMock) as mock_ws:
                from zhice.api import reverify_step
                result = await reverify_step(1)
                assert result["status"] == "reverify_requested"
                assert result["agent_notified"] is True
                mock_ws.assert_called_once()

    @pytest.mark.asyncio
    async def test_reverify_wrong_status(self):
        """抽查未完成的 Step → 409"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=self._step_row(status="in_progress"))

        with patch("zhice.api.get_pool", AsyncMock(return_value=self._make_pool(conn))):
            from zhice.api import reverify_step
            from zhice.models import AppError
            with pytest.raises(AppError) as exc:
                await reverify_step(1)
            assert exc.value.status == 409

    @pytest.mark.asyncio
    async def test_reverify_not_found(self):
        """抽查不存在 Step → 404"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        from fastapi import HTTPException

        with patch("zhice.api.get_pool", AsyncMock(return_value=self._make_pool(conn))):
            from zhice.api import reverify_step
            with pytest.raises(HTTPException) as exc:
                await reverify_step(999)
            assert exc.value.status_code == 404


class TestRecordReverifyResult:
    @pytest.mark.asyncio
    async def test_consecutive_fails_triggers_downgrade(self):
        """连续 3 次失败（含本次）→ 触发降级"""
        conn = AsyncMock()
        # INSERT 后查询会看到 3 条失败（本次 + 此前 2 次）
        conn.fetch = AsyncMock(return_value=[
            {"result": "failed", "verified_at": None},
            {"result": "failed", "verified_at": None},
            {"result": "failed", "verified_at": None},
        ])
        conn.execute = AsyncMock()

        with patch("zhice.reputation._call_zhenyue", new_callable=AsyncMock) as mock_zy:
            mock_zy.return_value = True
            from zhice.reputation import record_reverify_result
            rep = await record_reverify_result(conn, "agent1", 1, 42, passed=False)
            assert rep["action"] == "downgraded"
            assert rep["consecutive"] == 3
            mock_zy.assert_called_once_with("downgrade", "agent1",
                                            "抽查连续失败 3 次，自动降级")

    @pytest.mark.asyncio
    async def test_consecutive_passes_triggers_upgrade(self):
        """连续 5 次通过（含本次）→ 触发升级"""
        conn = AsyncMock()
        # INSERT 后查询会看到 5 条通过（本次 + 此前 4 次）
        conn.fetch = AsyncMock(return_value=[
            {"result": "passed", "verified_at": None},
            {"result": "passed", "verified_at": None},
            {"result": "passed", "verified_at": None},
            {"result": "passed", "verified_at": None},
            {"result": "passed", "verified_at": None},
        ])
        conn.execute = AsyncMock()

        with patch("zhice.reputation._call_zhenyue", new_callable=AsyncMock) as mock_zy:
            mock_zy.return_value = True
            from zhice.reputation import record_reverify_result
            rep = await record_reverify_result(conn, "agent1", 1, 42, passed=True)
            assert rep["action"] == "upgraded"
            assert rep["consecutive"] == 5

    @pytest.mark.asyncio
    async def test_single_fail_no_trigger(self):
        """只有 1 次失败 → 不触发降级"""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"result": "failed", "verified_at": None}])
        conn.execute = AsyncMock()

        with patch("zhice.reputation._call_zhenyue", new_callable=AsyncMock) as mock_zy:
            from zhice.reputation import record_reverify_result
            rep = await record_reverify_result(conn, "agent1", 1, 42, passed=False)
            assert rep["action"] == "none"
            assert rep["consecutive"] == 1
            mock_zy.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_results_break_streak(self):
        """失败→通过→失败 → 不连续，单独计"""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[
            {"result": "failed", "verified_at": None},
            {"result": "passed", "verified_at": None},
            {"result": "failed", "verified_at": None},
        ])
        conn.execute = AsyncMock()

        with patch("zhice.reputation._call_zhenyue", new_callable=AsyncMock) as mock_zy:
            from zhice.reputation import record_reverify_result
            rep = await record_reverify_result(conn, "agent1", 1, 42, passed=False)
            assert rep["action"] == "none"
            assert rep["consecutive"] == 1
            mock_zy.assert_not_called()

    @pytest.mark.asyncio
    async def test_zhenyue_unreachable_handled(self):
        """镇岳不可达 → 记录失败但不崩溃"""
        conn = AsyncMock()
        # INSERT 后查询会看到 3 条失败（触发 downgrade 调用）
        conn.fetch = AsyncMock(return_value=[
            {"result": "failed", "verified_at": None},
            {"result": "failed", "verified_at": None},
            {"result": "failed", "verified_at": None},
        ])
        conn.execute = AsyncMock()

        with patch("zhice.reputation._call_zhenyue", new_callable=AsyncMock) as mock_zy:
            mock_zy.return_value = False
            from zhice.reputation import record_reverify_result
            rep = await record_reverify_result(conn, "agent1", 1, 42, passed=False)
            assert rep["action"] == "downgrade_failed"
            assert rep["consecutive"] == 3
