"""
chat_channel.py 单元测试
飞书/企业微信/微信 三通道通知 + 去重/限流/静默
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from siku.models import ChatPayload, IMCallbackPayload
from siku.chat_channel import (
    _build_feishu_card, _build_wecom_markdown,
    FeishuChannel, WeComChannel, WeChatChannel, ChatNotifier,
)
from siku.api import _parse_im_text, _handle_im_action


class TestChatPayload:
    def test_minimal(self):
        p = ChatPayload(title="测试", content="内容")
        assert p.title == "测试"
        assert p.severity == "info"
        assert p.action_buttons == []
        assert p.metadata == {}

    def test_full(self):
        p = ChatPayload(
            title="大额转款通知",
            content="100万元已到账",
            severity="critical",
            action_buttons=[{"label": "确认", "action": "approve", "value": "txn_001"}],
            metadata={"txn_id": "123", "amount_fen": "10000000"},
        )
        assert p.severity == "critical"
        assert len(p.action_buttons) == 1
        assert p.metadata["txn_id"] == "123"


class TestFeishuCard:
    def test_basic_card(self):
        p = ChatPayload(title="测试", content="消息内容")
        card = _build_feishu_card(p)
        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["title"]["content"] == "测试"

    def test_card_with_metadata(self):
        p = ChatPayload(
            title="到账通知", content="已到账",
            metadata={"企业": "XX科技", "金额": "1000.00元"},
        )
        card = _build_feishu_card(p)
        elements = card["card"]["elements"]
        assert any("XX科技" in str(e) for e in elements)

    def test_critical_card_red(self):
        p = ChatPayload(title="紧急", content="!!", severity="critical")
        card = _build_feishu_card(p)
        assert card["card"]["header"]["template"] == "red"

    def test_card_with_buttons(self):
        p = ChatPayload(
            title="审批", content="请审批",
            action_buttons=[{"label": "通过", "action": "approve", "value": "123"}],
        )
        card = _build_feishu_card(p)
        elements = card["card"]["elements"]
        assert any("通过" in str(e) for e in elements)


class TestWeComMarkdown:
    def test_basic(self):
        p = ChatPayload(title="测试", content="消息内容")
        md = _build_wecom_markdown(p)
        assert "测试" in md
        assert "消息内容" in md
        assert "司库会计" in md

    def test_critical_icon(self):
        p = ChatPayload(title="紧急", content="!", severity="critical")
        md = _build_wecom_markdown(p)
        assert "🔴" in md

    def test_with_metadata(self):
        p = ChatPayload(title="通知", content="内容", metadata={"企业": "XX科技"})
        md = _build_wecom_markdown(p)
        assert "XX科技" in md


class TestFeishuChannel:
    @pytest.mark.asyncio
    async def test_disabled_when_no_webhook(self):
        ch = FeishuChannel({"webhook_url": ""})
        assert not ch.enabled
        ok = await ch.send(ChatPayload(title="x", content="y"))
        assert not ok

    @pytest.mark.asyncio
    async def test_send_success(self):
        ch = FeishuChannel({"webhook_url": "https://feishu.example.com/webhook"})
        assert ch.enabled
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
            ok = await ch.send(ChatPayload(title="测试", content="内容"))
        assert ok

    @pytest.mark.asyncio
    async def test_send_http_error(self):
        ch = FeishuChannel({"webhook_url": "https://feishu.example.com/webhook"})
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Error"
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
            ok = await ch.send(ChatPayload(title="测试", content="内容"))
        assert not ok


class TestWeComChannel:
    @pytest.mark.asyncio
    async def test_disabled_when_no_webhook(self):
        ch = WeComChannel({"webhook_url": ""})
        assert not ch.enabled
        ok = await ch.send(ChatPayload(title="x", content="y"))
        assert not ok

    @pytest.mark.asyncio
    async def test_send_success(self):
        ch = WeComChannel({"webhook_url": "https://qyapi.weixin.qq.com/webhook"})
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
            ok = await ch.send(ChatPayload(title="测试", content="内容"))
        assert ok


class TestWeChatChannel:
    @pytest.mark.asyncio
    async def test_disabled_when_no_creds(self):
        ch = WeChatChannel({"mode": "wecom_bridge", "wecom_corp_id": "", "wecom_secret": ""})
        assert not ch.enabled

    @pytest.mark.asyncio
    async def test_enabled_with_wecom_bridge_creds(self):
        ch = WeChatChannel({"mode": "wecom_bridge", "wecom_corp_id": "corp", "wecom_secret": "sec"})
        assert ch.enabled

    @pytest.mark.asyncio
    async def test_skip_when_no_wechat_user_id(self):
        ch = WeChatChannel({"mode": "wecom_bridge", "wecom_corp_id": "corp", "wecom_secret": "sec"})
        ch._access_token = "fake-token"
        ch._token_expiry = 9999999999
        ok = await ch.send(ChatPayload(title="x", content="y"))
        assert not ok

    @pytest.mark.asyncio
    async def test_get_access_token(self):
        ch = WeChatChannel({"mode": "wecom_bridge", "wecom_corp_id": "corp", "wecom_secret": "sec"})
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errcode": 0, "access_token": "token123", "expires_in": 7200}
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
            token = await ch._get_access_token()
        assert token == "token123"


class TestChatNotifier:
    @pytest.mark.asyncio
    async def test_no_channels_when_none_enabled(self):
        with patch("siku.chat_channel.cfg.im_channel_enabled", return_value=False):
            notifier = ChatNotifier()
            results = await notifier.notify(ChatPayload(title="x", content="y"))
            assert results == {}

    @pytest.mark.asyncio
    async def test_dedup_blocks_duplicate(self):
        notifier = ChatNotifier()
        payload = ChatPayload(title="测试", content="内容", severity="info")
        assert not notifier._dedup("测试")  # 第一次不是重复
        assert notifier._dedup("测试")       # 第二次是重复
        results = await notifier.notify(payload)
        assert results == {}

    @pytest.mark.asyncio
    async def test_critical_bypasses_dedup(self):
        with patch("siku.chat_channel.cfg.im_channel_enabled", return_value=False):
            notifier = ChatNotifier()
            payload = ChatPayload(title="紧急", content="!", severity="critical")
            results = await notifier.notify(payload)
            assert results == {}

    @pytest.mark.asyncio
    async def test_flush_silent_queue(self):
        with patch("siku.chat_channel.cfg.im_channel_enabled", return_value=False):
            notifier = ChatNotifier()
            notifier._silent_queue = [(ChatPayload(title="q", content="x"), 0)]
            sent = await notifier.flush_silent_queue()
            assert sent == 1
            assert len(notifier._silent_queue) == 0

    @pytest.mark.asyncio
    async def test_notify_flushes_silent_queue_when_not_silent(self):
        """P2 (R11): 非静默时段收到通知 → 自动补发静默积压队列（此前无生产调用点）"""
        with patch("siku.chat_channel.cfg.im_channel_enabled", return_value=False):
            notifier = ChatNotifier()
            notifier._silent_queue = [
                (ChatPayload(title="q1", content="x1"), 0),
                (ChatPayload(title="q2", content="x2"), 0),
            ]
            with patch.object(notifier, "_is_silent_hours", return_value=False):
                await notifier.notify(ChatPayload(title="now", content="y"))
            assert len(notifier._silent_queue) == 0

    @pytest.mark.asyncio
    async def test_notify_still_queues_during_silent_hours(self):
        """静默时段仍入队、不直接发送"""
        with patch("siku.chat_channel.cfg.im_channel_enabled", return_value=False):
            notifier = ChatNotifier()
            with patch.object(notifier, "_is_silent_hours", return_value=True):
                results = await notifier.notify(ChatPayload(title="q", content="x"))
            assert results == {}
            assert len(notifier._silent_queue) == 1

    @pytest.mark.asyncio
    async def test_flush_silent_queue_isolates_send_failures(self):
        """P2 (R11): 单条发送异常不阻塞其余条目，且队列清空不残留"""
        with patch("siku.chat_channel.cfg.im_channel_enabled", return_value=False):
            notifier = ChatNotifier()
            notifier._silent_queue = [
                (ChatPayload(title="bad", content="x"), 0),
                (ChatPayload(title="ok", content="y"), 0),
            ]
            real = notifier._do_notify

            async def flaky(payload):
                if payload.title == "bad":
                    raise RuntimeError("boom")
                return await real(payload)

            with patch.object(notifier, "_do_notify", new=flaky):
                sent = await notifier.flush_silent_queue()
            assert sent == 1
            assert len(notifier._silent_queue) == 0


class TestIMTextParsing:
    def test_balance(self):
        action, query = _parse_im_text("余额 XX科技")
        assert action == "balance"
        assert query == "XX科技"

    def test_balance_no_args(self):
        action, query = _parse_im_text("余额")
        assert action == "balance"
        assert query == ""

    def test_transactions(self):
        action, query = _parse_im_text("流水 某公司")
        assert action == "transactions"

    def test_approve(self):
        action, query = _parse_im_text("通过 txn_001")
        assert action == "approve"
        assert query == "txn_001"

    def test_reject(self):
        action, query = _parse_im_text("拒绝 txn_002")
        assert action == "reject"

    def test_invoice(self):
        action, query = _parse_im_text("发票 XX公司 100000")
        assert action == "invoice"

    def test_unknown(self):
        action, query = _parse_im_text("你好")
        assert action == "unknown"


class TestIMCallback:
    @pytest.mark.asyncio
    async def test_handle_balance_action(self):
        result = await _handle_im_action("wecom", "balance", "user1", "", "XX科技")
        assert result["action"] == "balance"

    @pytest.mark.asyncio
    async def test_handle_approve_action(self):
        """P0 接线（9-1）：approve 分支真正路由到待审单入账逻辑（不再仅 acknowledged）。"""
        with patch("siku.api._im_approve_pending_recharge",
                   new=AsyncMock(return_value={"status": "ok", "txn_id": 7, "note": "已入账"})) as m:
            result = await _handle_im_action("feishu", "approve", "user1", "", "txn_001")
        assert result["action"] == "approve"
        assert result["status"] == "ok"
        assert result["txn_id"] == 7
        m.assert_awaited_once_with("user1", "txn_001")

    @pytest.mark.asyncio
    async def test_handle_unknown_action(self):
        result = await _handle_im_action("feishu", "unknown", "user1", "", "???")
        assert result["action"] == "unknown"


class TestIMCallbackPayload:
    def test_minimal(self):
        p = IMCallbackPayload(channel="feishu", action="approve", user_id="u1")
        assert p.channel == "feishu"
        assert p.action == "approve"
        assert p.user_id == "u1"
