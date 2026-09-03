"""
infra:notifier — 通知推送 Agent 测试

测试 send_notification 和 handle_notification_request 方法，
通过 mock httpx.AsyncClient 模拟 IM 通道 HTTP 回调。
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from builtin.notifier_agent import (
    send_notification,
    handle_notification_request,
    _send_via_channel,
    _get_agent_channels,
    SUPPORTED_CHANNELS,
)


# ══════════════════════════════════════════════════════════
# _get_agent_channels
# ══════════════════════════════════════════════════════════

class TestGetAgentChannels:
    def test_infra_agent_returns_feishu(self):
        channels = _get_agent_channels("infra:monitor-01")
        assert channels == ["feishu"]

    def test_biz_agent_returns_feishu_and_wecom(self):
        channels = _get_agent_channels("biz:buyer-01")
        assert channels == ["feishu", "wecom"]


# ══════════════════════════════════════════════════════════
# _send_via_channel
# ══════════════════════════════════════════════════════════

class TestSendViaChannel:
    @patch("builtin.notifier_agent.httpx.AsyncClient")
    async def test_send_feishu_success(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client_cls.return_value = mock_client

        with patch("builtin.notifier_agent._FEISHU_WEBHOOK", "https://feishu.webhook/test"):
            result = await _send_via_channel("feishu", "Test", "Hello", "normal")

        assert result is True
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["json"]["msg_type"] == "interactive"

    @patch("builtin.notifier_agent.httpx.AsyncClient")
    async def test_send_wecom_success(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client_cls.return_value = mock_client

        with patch("builtin.notifier_agent._WECOM_WEBHOOK", "https://wecom.webhook/test"):
            result = await _send_via_channel("wecom", "Test", "Hello", "normal")

        assert result is True
        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["json"]["msgtype"] == "markdown"

    @patch("builtin.notifier_agent.httpx.AsyncClient")
    async def test_send_wechat_success(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client_cls.return_value = mock_client

        with patch("builtin.notifier_agent._WECHAT_WEBHOOK", "https://wechat.webhook/test"):
            result = await _send_via_channel("wechat", "Test", "Hello", "normal")

        assert result is True
        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["json"]["msgtype"] == "text"

    async def test_send_no_webhook_returns_false(self):
        with patch("builtin.notifier_agent._FEISHU_WEBHOOK", ""):
            result = await _send_via_channel("feishu", "Test", "Hello", "normal")
        assert result is False

    async def test_send_unsupported_channel_returns_false(self):
        result = await _send_via_channel("slack", "Test", "Hello", "normal")
        assert result is False


# ══════════════════════════════════════════════════════════
# send_notification
# ══════════════════════════════════════════════════════════

class TestSendNotification:
    @patch("builtin.notifier_agent._send_via_channel", new_callable=AsyncMock)
    async def test_deliver_first_channel_success(self, mock_send):
        mock_send.return_value = True

        result = await send_notification(
            agent_id="biz:buyer-01",
            title="Test",
            content="Hello",
            channels=["feishu", "wecom"],
        )

        assert result["delivered"] is True
        assert result["channels_used"] == ["feishu"]
        assert result["failed_channels"] == []
        mock_send.assert_called_once_with("feishu", "Test", "Hello", "normal")

    @patch("builtin.notifier_agent._send_via_channel", new_callable=AsyncMock)
    async def test_fallback_on_first_failure(self, mock_send):
        # First channel fails, second succeeds
        mock_send.side_effect = [False, True]

        result = await send_notification(
            agent_id="biz:buyer-01",
            title="Test",
            content="Hello",
            channels=["feishu", "wecom"],
        )

        assert result["delivered"] is True
        assert result["channels_used"] == ["wecom"]
        assert result["failed_channels"] == ["feishu"]

    @patch("builtin.notifier_agent._send_via_channel", new_callable=AsyncMock)
    async def test_all_channels_fail(self, mock_send):
        mock_send.return_value = False

        result = await send_notification(
            agent_id="biz:buyer-01",
            title="Test",
            content="Hello",
            channels=["feishu", "wecom"],
        )

        assert result["delivered"] is False
        assert result["channels_used"] == []
        assert result["failed_channels"] == ["feishu", "wecom"]

    @patch("builtin.notifier_agent._send_via_channel", new_callable=AsyncMock)
    async def test_channel_exception_is_caught(self, mock_send):
        mock_send.side_effect = RuntimeError("Connection failed")

        result = await send_notification(
            agent_id="biz:buyer-01",
            title="Test",
            content="Hello",
            channels=["feishu"],
        )

        assert result["delivered"] is False
        assert result["failed_channels"] == ["feishu"]

    @patch("builtin.notifier_agent._send_via_channel", new_callable=AsyncMock)
    async def test_high_priority_passed_through(self, mock_send):
        mock_send.return_value = True

        result = await send_notification(
            agent_id="infra:monitor-01",
            title="Alert",
            content="CPU 99%",
            priority="high",
        )

        assert result["delivered"] is True
        mock_send.assert_called_once_with("feishu", "Alert", "CPU 99%", "high")

    @patch("builtin.notifier_agent._get_agent_channels")
    @patch("builtin.notifier_agent._send_via_channel", new_callable=AsyncMock)
    async def test_default_channels_from_agent_id(self, mock_send, mock_get_channels):
        mock_get_channels.return_value = ["wecom"]
        mock_send.return_value = True

        result = await send_notification(
            agent_id="biz:seller-01",
            title="Test",
            content="Msg",
        )

        assert result["delivered"] is True
        mock_get_channels.assert_called_once_with("biz:seller-01")


# ══════════════════════════════════════════════════════════
# handle_notification_request
# ══════════════════════════════════════════════════════════

class TestHandleNotificationRequest:
    @patch("builtin.notifier_agent.send_notification", new_callable=AsyncMock)
    async def test_extracts_payload_fields(self, mock_send):
        mock_send.return_value = {"delivered": True, "channels_used": ["feishu"], "failed_channels": []}

        event = {
            "type": "notify",
            "payload": {
                "agent_id": "biz:buyer-01",
                "title": "New Task",
                "content": "You have a new task assigned",
                "channels": ["feishu"],
                "priority": "high",
            },
        }

        result = await handle_notification_request(event)

        assert result["delivered"] is True
        mock_send.assert_called_once_with("biz:buyer-01", "New Task", "You have a new task assigned", ["feishu"], "high")

    @patch("builtin.notifier_agent.send_notification", new_callable=AsyncMock)
    async def test_uses_defaults_for_missing_fields(self, mock_send):
        mock_send.return_value = {"delivered": False, "channels_used": [], "failed_channels": []}

        event = {"type": "notify", "payload": {}}

        result = await handle_notification_request(event)

        mock_send.assert_called_once_with("", "通知", "", None, "normal")
