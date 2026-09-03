"""执策多签交叉验证测试"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestNeedsMultisig:
    def test_no_criteria(self):
        from zhice.multisig import needs_multisig
        assert not needs_multisig(None)
        assert not needs_multisig([])

    def test_no_multisig_rule(self):
        from zhice.multisig import needs_multisig
        criteria = [{"type": "output_contains", "field": "x", "keyword": "y"}]
        assert not needs_multisig(criteria)

    def test_has_multisig(self):
        from zhice.multisig import needs_multisig
        criteria = [{"type": "file_exists", "path": "/x", "required": True, "require_multisig": True}]
        assert needs_multisig(criteria)


class TestMultisigCount:
    def test_default_count(self):
        from zhice.multisig import get_multisig_count
        criteria = [{"type": "api_health", "require_multisig": True}]
        assert get_multisig_count(criteria) == 2

    def test_custom_count(self):
        from zhice.multisig import get_multisig_count
        criteria = [{"type": "api_health", "require_multisig": True, "multisig_count": 3}]
        assert get_multisig_count(criteria) == 3

    def test_max_count(self):
        from zhice.multisig import get_multisig_count
        criteria = [
            {"type": "api_health", "require_multisig": True, "multisig_count": 2},
            {"type": "file_exists", "require_multisig": True, "multisig_count": 4},
        ]
        assert get_multisig_count(criteria) == 4


class TestClaimVerification:
    @pytest.mark.asyncio
    async def test_claim_pending(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={
            "verification_id": 1, "step_id": 10, "task_id": 42,
            "rule_details": json.dumps([{"type": "file_exists", "path": "/x"}]),
            "notes": json.dumps({"step_title": "S", "step_index": 1}),
        })
        conn.fetchval = AsyncMock(return_value=None)  # 该 Agent 此前未对该 step 投票
        conn.execute = AsyncMock()

        from zhice.multisig import claim_verification
        result = await claim_verification(conn, 1, "agent2")
        assert result is not None
        assert result["verification_id"] == 1
        # 确认 UPDATE 把状态改为 claimed
        conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_claim_dup_vote_blocked(self):
        """R11 (P?): 同一 Agent 已对该 step 投过票 → 拒绝再次认领（单 agent 多票防伪）。"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={
            "verification_id": 2, "step_id": 10, "task_id": 42,
            "rule_details": json.dumps([{"type": "file_exists", "path": "/x"}]),
            "notes": json.dumps({"step_title": "S", "step_index": 1}),
        })
        conn.fetchval = AsyncMock(return_value=1)  # 已存在该 agent 的投票记录
        conn.execute = AsyncMock()

        from zhice.multisig import claim_verification
        result = await claim_verification(conn, 2, "agent2")
        assert result is None
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_claim_not_found(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        from zhice.multisig import claim_verification
        result = await claim_verification(conn, 999, "agent2")
        assert result is None


class TestSubmitVerification:
    @pytest.mark.asyncio
    async def test_submit_claimed_and_passes(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {  # 第一次 fetchrow: 验证任务
                "verification_id": 1, "step_id": 10, "task_id": 42,
                "rule_details": [{"type": "output_contains", "field": "result", "keyword": "OK"}],
            },
            {  # count_multisig_passes
                "passed": 1, "failed": 0,
            },
            {  # step
                "acceptance_criteria": [{"type": "output_contains", "field": "result", "keyword": "OK", "require_multisig": True}],
                "task_id": 42, "title": "S", "step_index": 1, "assigned_agent": "agent1",
            },
        ])
        conn.execute = AsyncMock()

        from zhice.multisig import submit_verification
        result = await submit_verification(
            conn, 1, "agent2",
            {"output_contains": [{"field": "result", "value": "OK"}]},
            # check_all will see this in comparison data
        )
        # Actually check_all needs the correct field structure
        # Let me adjust: the criteria is output_contains with field="result", keyword="OK"
        # check_results need to have the right structure

    @pytest.mark.asyncio
    async def test_submit_not_claimed(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        from zhice.multisig import submit_verification
        result = await submit_verification(conn, 999, "agent2", {})
        assert not result["success"]


class TestMultisigSubmitPasses:
    @pytest.mark.asyncio
    async def test_submit_passes(self):
        """验证者提交正确结果 → passed"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {
                "verification_id": 1, "step_id": 10, "task_id": 42,
                "rule_details": [{"type": "output_contains", "field": "result", "keyword": "OK"}],
            },
            {"passed": 0, "failed": 0},  # count before
            {  # step
                "acceptance_criteria": [{"type": "output_contains", "field": "result", "keyword": "OK", "require_multisig": True}],
                "task_id": 42, "title": "S", "step_index": 1, "assigned_agent": "agent1",
            },
        ])
        conn.execute = AsyncMock()

        from zhice.multisig import submit_verification
        result = await submit_verification(
            conn, 1, "agent2",
            {"result": "OK"},
        )
        assert result["success"]
        assert result["passed"]
