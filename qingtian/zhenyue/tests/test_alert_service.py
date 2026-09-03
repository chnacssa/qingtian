"""
alert_service.py 单元测试
告警去重 / 限流 / 静默时段 / 飞书卡片
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from zhenyue.alert_service import (
    AlertChannel,
    alert_channel,
    _build_approval_card,
)


class TestBuildApprovalCard:
    def test_card_structure(self):
        card = _build_approval_card({
            "request_id": "req-001",
            "agent_id": "agent-1",
            "action": "delete_agent",
            "severity": "critical",
            "target": "agent-bad",
        })
        assert card["msg_type"] == "interactive"
        assert "card" in card
        assert card["card"]["header"]["template"] == "red"

    def test_high_severity_orange(self):
        card = _build_approval_card({
            "request_id": "req-002",
            "agent_id": "agent-2",
            "action": "transition_nego",
            "severity": "high",
            "target": "",
        })
        assert card["card"]["header"]["template"] == "red"  # high also red

    def test_medium_severity_orange(self):
        card = _build_approval_card({
            "request_id": "req-003",
            "agent_id": "agent-3",
            "action": "create_agreement",
            "severity": "medium",
            "target": "",
        })
        assert card["card"]["header"]["template"] == "orange"

    def test_card_contains_fields(self):
        card = _build_approval_card({
            "request_id": "req-004",
            "agent_id": "agent-4",
            "action": "test_action",
            "severity": "low",
            "target": "target-x",
        })
        elements = card["card"]["elements"]
        fields = elements[0]["fields"]
        assert len(fields) == 4


class TestAlertChannel:
    def test_singleton_exists(self):
        assert alert_channel is not None
        assert isinstance(alert_channel, AlertChannel)

    @pytest.mark.asyncio
    @patch.object(AlertChannel, "_is_silent_hours", return_value=False)
    async def test_dedup_blocks_duplicate(self, _mock_silent):
        channel = AlertChannel()
        channel._do_send = AsyncMock(return_value=True)

        result1 = await channel.send("test_alert", {"msg": "hello"}, "high")
        result2 = await channel.send("test_alert", {"msg": "hello again"}, "high")

        # First should be sent, second deduped
        assert result1 is True
        assert result2 is False
        assert channel._do_send.call_count == 1

    @pytest.mark.asyncio
    async def test_critical_bypasses_dedup(self):
        channel = AlertChannel()
        channel._do_send = AsyncMock(return_value=True)

        await channel.send_critical("critical_alert", {"msg": "first"})
        await channel.send_critical("critical_alert", {"msg": "second"})

        # Critical always sends
        assert channel._do_send.call_count == 2

    @pytest.mark.asyncio
    @patch.object(AlertChannel, "_is_silent_hours", return_value=False)
    async def test_different_alert_types_not_deduped(self, _mock_silent):
        channel = AlertChannel()
        channel._do_send = AsyncMock(return_value=True)

        await channel.send("alert_a", {"msg": "a"}, "high")
        await channel.send("alert_b", {"msg": "b"}, "high")

        assert channel._do_send.call_count == 2

    @pytest.mark.asyncio
    async def test_send_approval_delegates(self):
        channel = AlertChannel()
        channel._do_send = AsyncMock(return_value=True)

        result = await channel.send_approval({
            "request_id": "req-010",
            "agent_id": "agent-1",
            "action": "delete_agent",
            "severity": "critical",
        })
        assert result is True
        channel._do_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_silent_queue_flush(self):
        channel = AlertChannel()
        channel._do_send = AsyncMock(return_value=True)
        channel._silent_queue = [
            {"type": "alert_1", "content": {"msg": "queued"}, "severity": "high"},
        ]

        await channel.flush_silent_queue()
        assert len(channel._silent_queue) == 0
        assert channel._do_send.call_count == 1
