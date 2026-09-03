"""
cron.py 单元测试 — P2 (R11): pending 消息重试上限

覆盖：
- 单条投递成功 → 计数清除；
- 反复失败超 _MAX_RETRY_ROUNDS 轮 → 本进程内暂停重试（保持 failed，由日清任务 7 天后清理），
  不再无限打同一批无法投递的消息。
纯 mock 测试，不依赖数据库。
"""

from unittest.mock import patch

import pytest

import huanyu.cron as cron


class TestRetryPendingDeliveriesJob:
    @pytest.mark.asyncio
    async def test_delivered_clears_round_counter(self):
        """投递成功 → 成功计数 + 清除该消息的重试计数。"""
        cron._retry_rounds.clear()
        pending = [{"message_id": "m1"}]

        async def _get(limit=100):
            return pending

        async def _retry(mid):
            return {"status": "delivered", "message_id": mid}

        with patch("huanyu.messaging.get_pending_deliveries", side_effect=_get), \
             patch("huanyu.messaging.retry_delivery", side_effect=_retry) as mock_retry:
            await cron._retry_pending_deliveries_job()

        assert mock_retry.call_count == 1
        assert "m1" not in cron._retry_rounds  # 投递成功 → 清除计数

    @pytest.mark.asyncio
    async def test_pauses_after_max_retry_rounds(self):
        """反复失败超 _MAX_RETRY_ROUNDS 轮 → 暂停重试（保持 failed 待日清清理）。"""
        cron._retry_rounds.clear()
        pending = [{"message_id": "m1"}, {"message_id": "m2"}]

        async def _get(limit=100):
            return pending

        async def _retry(mid):
            return {"status": "failed", "error": "boom"}

        with patch("huanyu.messaging.get_pending_deliveries", side_effect=_get), \
             patch("huanyu.messaging.retry_delivery", side_effect=_retry) as mock_retry:
            # 跑 MAX+1 轮：前 MAX 轮逐条重试，第 MAX+1 轮起全部暂停
            for _ in range(cron._MAX_RETRY_ROUNDS + 1):
                await cron._retry_pending_deliveries_job()

        # 每条消息最多被重试 _MAX_RETRY_ROUNDS 次，之后不再空转
        assert mock_retry.call_count == len(pending) * cron._MAX_RETRY_ROUNDS
