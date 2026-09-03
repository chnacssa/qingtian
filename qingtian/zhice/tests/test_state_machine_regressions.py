"""R11 P1 状态机修复回归测试 — 依赖门 / reverify / rejected / manual_review / cancel_task / feishu 前缀

覆盖 R11 review 的 zhice 状态机正确性修复：
  1. _effective_executor / _normalize_agent_id — feishu 前缀归一化等效通过 → DB 用存库原始值
  2. step_retry — rejected/timed_out → pending（认领循环只认 pending，原 assigned NULL 孤儿卡死）
  3. reverify_step — 重置 in_progress + started_at（防重交 409 / 看门狗按旧窗口秒超时）
  4. review_step — manual_review 全通过 → completed；任一失败 → reject/retry
  5. cancel_task — 依赖跳过用 fresh 快照计算 cancelled_indices（原实现恒空不触发）
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from zhice import status_machine as sm
import zhice.api  # ensure submodule is loaded for patching


def _make_pool(conn):
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire.return_value = ctx
    return pool


def _step(**overrides):
    d = {
        "step_id": 1, "task_id": 42, "step_index": 1, "title": "S",
        "instruction": "Do", "status": "in_progress", "status_reason": None,
        "assigned_agent": "agent1", "assigned_at": None, "depends_on": [],
        "acceptance_criteria": [], "expected_outputs": None, "outputs": None,
        "summary": None, "auto_retry": 1, "timeout_minutes": 30,
        "idempotency_key": None, "last_heartbeat_at": None,
        "started_at": None, "completed_at": None, "created_at": None, "updated_at": None,
    }
    d.update(overrides)
    return d


class TestEffectiveExecutor:
    """feishu 前缀归一化等效通过 → DB 精确匹配用存库原始值（R11 P?）"""

    def test_stored_value_used_when_assigned(self):
        from zhice.api import _effective_executor
        step = {"assigned_agent": "feishu:ou_abc"}
        assert _effective_executor(step, "ou_abc") == "feishu:ou_abc"

    def test_falls_back_to_caller_when_unassigned(self):
        from zhice.api import _effective_executor
        assert _effective_executor({"assigned_agent": None}, "agent1") == "agent1"
        assert _effective_executor({"assigned_agent": ""}, "agent1") == "agent1"

    def test_normalize_agent_id_strips_platform_prefix(self):
        from zhice.api import _normalize_agent_id
        assert _normalize_agent_id("feishu:ou_abc") == "ou_abc"
        assert _normalize_agent_id("ou_abc") == "ou_abc"
        assert _normalize_agent_id("  DINGTALK:AbC ") == "abc"


class TestStepRetryRejectedToPending:
    """rejected → pending（原 assigned NULL 孤儿，认领循环只认 pending → 卡死任务）"""

    @pytest.mark.asyncio
    async def test_rejected_maps_to_pending_in_sql(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_step(status="pending"))
        await sm.step_retry(conn, 1)
        sql = conn.fetchrow.call_args[0][0]
        assert "WHEN 'rejected' THEN 'pending'" in sql
        assert "WHEN 'timed_out' THEN 'pending'" in sql
        assert "WHEN 'failed' THEN 'in_progress'" in sql

    @pytest.mark.asyncio
    async def test_retry_clears_assignment_for_rejected(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_step(status="pending"))
        await sm.step_retry(conn, 1)
        sql = conn.fetchrow.call_args[0][0]
        # rejected/timed_out 回收后 assigned_agent 置空 → 重新进认领池
        assert "assigned_agent = CASE WHEN status != 'failed' THEN NULL" in sql


class TestReverifyStep:
    """reverify 409 修复：completed → in_progress + 重置 started_at（R11 P?）"""

    @pytest.mark.asyncio
    async def test_reverify_resets_status_and_started_at(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_step(status="completed"))
        conn.execute = AsyncMock()
        pool = _make_pool(conn)

        with (
            patch("zhice.api.get_pool", AsyncMock(return_value=pool)),
            patch("zhice.api.dispatcher.ws_notify", new_callable=AsyncMock),
            patch("zhice.api.runner.step_hooks", new_callable=AsyncMock),
        ):
            from zhice.api import reverify_step
            result = await reverify_step(1)

        assert result["status"] == "reverify_requested"
        sql = conn.execute.call_args[0][0]
        assert "status = 'in_progress'" in sql
        assert "status_reason = 'reverifying'" in sql
        assert "started_at = NOW()" in sql
        # outputs 保留并标记抽查请求
        assert "reverify_requested" in sql

    @pytest.mark.asyncio
    async def test_reverify_rejects_non_completed(self):
        from zhice.models import AppError
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_step(status="in_progress"))
        pool = _make_pool(conn)

        with patch("zhice.api.get_pool", AsyncMock(return_value=pool)):
            from zhice.api import reverify_step
            with pytest.raises(AppError) as exc:
                await reverify_step(1)
        assert exc.value.status == 409


class TestReviewStep:
    """manual_review 卡死修复：全通过 → completed；任一失败 → reject/retry（R11 P?）"""

    @pytest.mark.asyncio
    async def test_all_approved_completes_step(self):
        """所有人工审核通过 → step_complete（原实现只改 status_reason，Step 永不终态）"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"verification_id": 1, "step_id": 1, "rule_type": "manual_review"},  # verification
            _step(status="in_progress"),                                        # get_step
        ])
        conn.fetchval = AsyncMock(return_value=0)  # 无待处理人工审核
        conn.fetch = AsyncMock(return_value=[
            {"rule_type": "output_contains", "check_mode": "engine", "result": "passed"},
            {"rule_type": "manual_review", "check_mode": "multisig_pending", "result": "passed"},
        ])
        pool = _make_pool(conn)

        with (
            patch("zhice.api.get_pool", AsyncMock(return_value=pool)),
            patch("zhice.api.sm.step_complete", new_callable=AsyncMock,
                  return_value=_step(status="completed")) as mock_complete,
            patch("zhice.api.sm.step_reject", new_callable=AsyncMock),
            patch("zhice.api.runner.try_complete_task", new_callable=AsyncMock) as mock_try,
            patch("zhice.api.runner.step_hooks", new_callable=AsyncMock),
        ):
            from zhice.api import review_step
            from zhice.models import ReviewRequest
            result = await review_step(1, ReviewRequest(verification_id=1, decision="approved"))

        mock_complete.assert_awaited_once()
        # _effective_executor 用存库原始值（assigned_agent=agent1）
        assert mock_complete.call_args.args[2] == "agent1"
        mock_try.assert_awaited_once()
        assert result["result"] == "passed"

    @pytest.mark.asyncio
    async def test_any_failed_rejects_and_retries(self):
        """任一人工审核失败 → reject + 消耗 auto_retry 重试（不卡在 in_progress）"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"verification_id": 1, "step_id": 1, "rule_type": "manual_review"},
            _step(status="in_progress"),
        ])
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[
            {"rule_type": "output_contains", "check_mode": "engine", "result": "failed"},
        ])
        pool = _make_pool(conn)

        with (
            patch("zhice.api.get_pool", AsyncMock(return_value=pool)),
            patch("zhice.api.sm.step_complete", new_callable=AsyncMock) as mock_complete,
            patch("zhice.api.sm.step_reject", new_callable=AsyncMock,
                  return_value=_step(status="rejected", auto_retry=1)) as mock_reject,
            patch("zhice.api.sm.step_retry", new_callable=AsyncMock,
                  return_value=_step(status="pending")) as mock_retry,
            patch("zhice.api.runner.try_complete_task", new_callable=AsyncMock),
            patch("zhice.api.runner.step_hooks", new_callable=AsyncMock),
        ):
            from zhice.api import review_step
            from zhice.models import ReviewRequest
            result = await review_step(1, ReviewRequest(verification_id=1, decision="rejected"))

        mock_reject.assert_awaited_once()
        mock_retry.assert_awaited_once()
        mock_complete.assert_not_awaited()
        assert result["result"] == "failed"


class TestCancelTaskDependencySkip:
    """cancel_task 依赖跳过 — 用 fresh 快照计算 cancelled_indices（原实现恒空不触发）"""

    @pytest.mark.asyncio
    async def test_dependent_pending_step_skipped_via_fresh_snapshot(self):
        conn = AsyncMock()
        # cancel_task 走 conn.transaction()
        conn.transaction = MagicMock()
        tctx = MagicMock()
        tctx.__aenter__ = AsyncMock()
        tctx.__aexit__ = AsyncMock(return_value=None)
        conn.transaction.return_value = tctx
        pool = _make_pool(conn)
        # 旧快照：step1 取消前还是 in_progress → 若用旧快照 cancelled_indices 恒空
        old_steps = [
            _step(step_id=1, step_index=1, status="in_progress", assigned_agent="agent1"),
            _step(step_id=2, step_index=2, status="pending", depends_on=[1], assigned_agent=None),
        ]
        # fresh 快照：step1 已 cancelled
        fresh_steps = [
            _step(step_id=1, step_index=1, status="cancelled", assigned_agent="agent1"),
            _step(step_id=2, step_index=2, status="pending", depends_on=[1], assigned_agent=None),
        ]

        with (
            patch("zhice.api.get_pool", AsyncMock(return_value=pool)),
            patch("zhice.api.sm.task_cancel", new_callable=AsyncMock,
                  return_value={"task_id": 42, "title": "T"}),
            patch("zhice.api.sm.get_task_steps", new_callable=AsyncMock,
                  side_effect=[old_steps, fresh_steps]),
            patch("zhice.api.sm.step_cancel", new_callable=AsyncMock,
                  return_value=_step(status="cancelled")),
            patch("zhice.api.sm.get_step", new_callable=AsyncMock,
                  return_value=_step(status="cancelled")),
            patch("zhice.api.sm.step_skip", new_callable=AsyncMock,
                  return_value=_step(status="skipped")) as mock_skip,
            patch("zhice.api.runner.step_hooks", new_callable=AsyncMock),
            patch("zhice.api.dispatcher.ws_notify", new_callable=AsyncMock),
        ):
            from zhice.api import cancel_task
            result = await cancel_task(42)

        assert result["status"] == "cancelled"
        # step2 依赖被取消的 step1 → fresh 快照计算 cancelled_indices=[1] → skip step2
        mock_skip.assert_awaited_once()
        assert mock_skip.call_args.args[1] == 2
