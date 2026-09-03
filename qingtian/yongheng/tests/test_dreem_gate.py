"""
dreem_gate.py 单元测试
记忆整理触发器检查
"""

import pytest

from yongheng.dreem_gate import check_trigger


class TestCheckTrigger:
    @pytest.mark.asyncio
    async def test_no_records_no_trigger(self, mock_conn):
        mock_conn.fetch.return_value = []
        mock_conn.fetchrow.return_value = None

        should_run, reason = await check_trigger(mock_conn, "agent:test")
        assert should_run is False
        assert "conditions not met" in reason

    @pytest.mark.asyncio
    async def test_token_budget_exceeded(self, mock_conn):
        content = "x" * 5000
        mock_conn.fetch.return_value = [{"content": content}]
        mock_conn.fetchrow.return_value = None

        should_run, reason = await check_trigger(mock_conn, "agent:test")
        assert isinstance(should_run, bool)
        assert isinstance(reason, str)

    @pytest.mark.asyncio
    async def test_record_count_trigger(self, mock_conn):
        records = [{"content": "record " + str(i)} for i in range(501)]
        mock_conn.fetch.return_value = records
        mock_conn.fetchrow.return_value = None

        should_run, reason = await check_trigger(mock_conn, "agent:test")
        assert should_run is True
        assert "record_count" in reason

    @pytest.mark.asyncio
    async def test_below_threshold_no_trigger(self, mock_conn):
        records = [{"content": "short record"} for _ in range(10)]
        mock_conn.fetch.return_value = records
        mock_conn.fetchrow.return_value = None

        should_run, reason = await check_trigger(mock_conn, "agent:test")
        assert should_run is False
