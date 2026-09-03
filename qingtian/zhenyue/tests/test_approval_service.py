"""
approval_service.py 单元测试
审批创建 / 自动过期 / 通过执行 / 升级
"""

from unittest.mock import AsyncMock, patch

import pytest

from zhenyue.approval_service import (
    create_approval,
    auto_reject_expired,
    resolve_approval,
    check_approval_required,
)


class TestCheckApprovalRequired:
    def test_critical_requires_approval(self):
        result = check_approval_required("suspend_agent")
        assert result["severity"] == "critical"
        assert result["requires_approval"] is True

    def test_high_requires_approval(self):
        result = check_approval_required("delete_agent")
        assert result["severity"] == "high"
        assert result["requires_approval"] is True

    def test_medium_no_approval(self):
        result = check_approval_required("register_agent")
        assert result["severity"] == "medium"
        assert result["requires_approval"] is False

    def test_low_no_approval(self):
        result = check_approval_required("unknown_action_xyz")
        assert result["severity"] == "low"
        assert result["requires_approval"] is False

    def test_known_action_maps_correctly(self):
        for action, severity in [
            ("delete_agent", "high"),
            ("mass_suspend", "critical"),
            ("system_config", "critical"),
            ("reset_all_agents", "critical"),
        ]:
            result = check_approval_required(action)
            assert result["severity"] == severity


class TestCreateApproval:
    @pytest.mark.asyncio
    async def test_creates_approval_request(self, mock_conn):
        mock_conn.execute = AsyncMock()

        with patch("zhenyue.approval_service.write_audit", AsyncMock()), \
             patch("zhenyue.approval_service.alert_channel") as mock_alert:
            mock_alert.send_approval = AsyncMock()
            result = await create_approval(
                mock_conn, "agent-1", "delete_agent",
                target_type="agent", target_id="target-1", severity="high",
            )
            assert result["status"] == "pending"
            assert "request_id" in result
            assert "expires_at_seconds" in result

    @pytest.mark.asyncio
    async def test_default_severity_high(self, mock_conn):
        mock_conn.execute = AsyncMock()

        with patch("zhenyue.approval_service.write_audit", AsyncMock()), \
             patch("zhenyue.approval_service.alert_channel") as mock_alert:
            mock_alert.send_approval = AsyncMock()
            result = await create_approval(mock_conn, "agent-1", "some_action")
            assert result["status"] == "pending"


class TestAutoRejectExpired:
    @pytest.mark.asyncio
    async def test_returns_count(self, mock_conn):
        mock_conn.fetch.return_value = []

        with patch("zhenyue.approval_service.write_audit", AsyncMock()):
            count = await auto_reject_expired(mock_conn)
            assert count == 0

    @pytest.mark.asyncio
    async def test_rejects_expired(self, mock_conn):
        mock_conn.fetch.return_value = [
            {"request_id": "req-1", "agent_id": "a1", "action": "test", "severity": "high"},
            {"request_id": "req-2", "agent_id": "a2", "action": "test2", "severity": "high"},
        ]

        with patch("zhenyue.approval_service.write_audit", AsyncMock()):
            count = await auto_reject_expired(mock_conn)
            assert count == 2


class TestResolveApproval:
    @pytest.mark.asyncio
    async def test_not_found(self, mock_conn):
        mock_conn.fetchrow.return_value = None

        with patch("zhenyue.approval_service.write_audit", AsyncMock()):
            result = await resolve_approval(mock_conn, "nonexistent", "approved", "admin")
            assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_already_resolved(self, mock_conn):
        mock_conn.fetchrow.return_value = {"status": "approved", "request_id": "req-1"}

        with patch("zhenyue.approval_service.write_audit", AsyncMock()):
            result = await resolve_approval(mock_conn, "req-1", "approved", "admin")
            assert result["status"] == "approved"

    @pytest.mark.asyncio
    async def test_approve(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "request_id": "req-1", "agent_id": "a1", "action": "delete_agent",
            "target_type": "agent", "target_id": "t1", "severity": "high",
            "status": "pending",
        }

        with patch("zhenyue.approval_service.write_audit", AsyncMock()):
            result = await resolve_approval(mock_conn, "req-1", "approved", "admin")
            assert result["status"] == "approved"
            assert result["approver"] == "admin"

    @pytest.mark.asyncio
    async def test_reject(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "request_id": "req-1", "agent_id": "a1", "action": "delete_agent",
            "target_type": "agent", "target_id": "t1", "severity": "high",
            "status": "pending",
        }

        with patch("zhenyue.approval_service.write_audit", AsyncMock()):
            result = await resolve_approval(mock_conn, "req-1", "rejected", "admin")
            assert result["status"] == "rejected"


class TestExecuteScheduledApprovals:
    """P1 (#10, 2026-08-26): 定时执行不再空转——有 execute_func 真执行；
    无则明确审计告警且不标 executed_at（原实现解析 pending 后只 UPDATE executed_at，
    延迟执行的审批到期后全部静默空转）。"""

    @staticmethod
    def _row(pending='{"method": "POST", "path": "/v1/x"}'):
        return {
            "request_id": "r1", "agent_id": "a1", "action": "delete_agent",
            "severity": "high", "pending_request": pending,
        }

    @pytest.mark.asyncio
    async def test_executes_with_func(self, mock_conn):
        from zhenyue.approval_service import execute_scheduled_approvals
        mock_conn.fetch.return_value = [self._row()]
        func = AsyncMock(return_value={"status": "done"})
        with patch("zhenyue.approval_service.write_audit", new=AsyncMock()):
            executed = await execute_scheduled_approvals(mock_conn, execute_func=func)
        assert executed == 1
        func.assert_awaited_once_with({"method": "POST", "path": "/v1/x"})
        # 标记 executed_at（仅在真实执行成功后）
        sql = mock_conn.execute.call_args_list[0].args[0]
        assert "executed_at = NOW()" in sql

    @pytest.mark.asyncio
    async def test_no_func_does_not_mark_executed(self, mock_conn):
        from zhenyue.approval_service import execute_scheduled_approvals
        mock_conn.fetch.return_value = [self._row()]
        audit_mock = AsyncMock()
        with patch("zhenyue.approval_service.write_audit", new=audit_mock):
            executed = await execute_scheduled_approvals(mock_conn)
        assert executed == 0
        # 不标 executed_at（execute 无任何 SQL）
        mock_conn.execute.assert_not_awaited()
        # 写 missed 高严重度审计
        audit_call = audit_mock.call_args
        assert "approval_execution_missed" in audit_call.args[1]["action"]
        assert audit_call.args[1]["severity"] == "high"

    @pytest.mark.asyncio
    async def test_func_exception_does_not_mark_executed(self, mock_conn):
        from zhenyue.approval_service import execute_scheduled_approvals
        mock_conn.fetch.return_value = [self._row()]
        func = AsyncMock(side_effect=RuntimeError("re-issue boom"))
        audit_mock = AsyncMock()
        with patch("zhenyue.approval_service.write_audit", new=audit_mock):
            executed = await execute_scheduled_approvals(mock_conn, execute_func=func)
        assert executed == 0
        mock_conn.execute.assert_not_awaited()
        assert "approval_execution_failed" in audit_mock.call_args.args[1]["action"]
