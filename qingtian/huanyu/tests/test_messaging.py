"""
messaging.py 单元测试
使用 mock asyncpg 连接测试消息发送/收件箱/去重
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from huanyu.signing import sign_peer_message

from huanyu.messaging import (
    _make_idempotency_key,
    archive_message,
    batch_mark_read,
    get_conversation,
    get_inbox,
    get_pending_deliveries,
    get_unread_count,
    insert_incoming_peer_message,
    mark_delivery_status,
    mark_read,
    receive_peer_message,
    retry_delivery,
    send_message,
    verify_message_integrity,
)


# ── 测试签名密钥（B4: 移除 dev 回退密钥后显式配置） ─────────────

@pytest.fixture(autouse=True)
def _test_sign_key():
    """本文件消息签名相关测试需要有效签名密钥（默认密钥已移除）"""
    os.environ["HUANYU_SIGN_KEY"] = "test-messaging-key-for-b4"


# ── 幂等 Key 生成 ─────────────────────────────────────

class TestIdempotencyKey:
    def test_contains_random_uuid(self):
        """内容一致 → key 一致（基于内容去重，幂等重试可命中）"""
        k1 = _make_idempotency_key("a1", "a2", "info", "payload")
        k2 = _make_idempotency_key("a1", "a2", "info", "payload")
        assert k1 == k2

    def test_different_inputs(self):
        k1 = _make_idempotency_key("a1", "a2", "info", "payload")
        k2 = _make_idempotency_key("a1", "a3", "info", "payload")
        assert k1 != k2

    def test_hex_format(self):
        k = _make_idempotency_key("a", "b", "info", "{}")
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)


# ── 发送消息 ──────────────────────────────────────────

class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_new_message(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "message_id": "new-msg-id",
            "from_agent_id": "agent-1",
            "to_agent_id": "agent-2",
            "message_type": "info",
            "status": "unread",
            "delivery_status": "local",
            "created_at": None,
        }

        with patch("huanyu.messaging.get_pool", AsyncMock(return_value=MagicMock())) as mock_pool:
            mock_pool.return_value.acquire = mock_conn

            pool = mock_pool.return_value
            pool.acquire.return_value.__aenter__.return_value = mock_conn

            # 需要更精确的 mock...
            # 直接用 patch 替换 send_message 内部的 get_pool

    @pytest.mark.asyncio
    async def test_duplicate_idempotency(self, mock_conn, mock_pool):
        """幂等 Key 重复时返回已有记录"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        mock_conn.fetchrow.return_value = {
            "message_id": "existing-msg-id",
            "status": "unread",
            "delivery_status": "local",
            "created_at": now,
        }

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await send_message("a1", "a2", "info", {"k": "v"})
            assert result["status"] == "duplicate"
            assert result["message_id"] == "existing-msg-id"

    @pytest.mark.asyncio
    async def test_send_success(self, mock_conn, mock_pool):
        """正常发送返回消息详情"""
        mock_conn.fetchrow.side_effect = [
            None,                              # 幂等检查：不存在
            {"agent_id": "agent-1"},           # _resolve_agent_id: from_agent 匹配
            {"agent_id": "agent-2"},           # _resolve_agent_id: to_agent 匹配
            {                                  # INSERT RETURNING
                "message_id": "new-msg-id",
                "from_agent_id": "agent-1",
                "to_agent_id": "agent-2",
                "message_type": "info",
                "status": "unread",
                "delivery_status": "local",
                "created_at": None,
            },
        ]
        mock_conn.fetchval.return_value = None  # host 查询 → 本地投递

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await send_message("a1", "a2", "info", {"k": "v"})
            assert result["message_id"] == "new-msg-id"
            assert "idempotency_key" in result

    @pytest.mark.asyncio
    async def test_send_explicit_idempotency_key_used(self, mock_conn, mock_pool):
        """显式 idempotency_key 透传落库（2026-08-11：投标进度广播幂等去重）。

        调用方传显式键（按 agent/target/text 派生）→ INSERT 用显式键，同文本重复投递
        → 同键 → 唯一索引去重，不再重复进路由。
        """
        mock_conn.fetchrow.side_effect = [
            None,                              # 幂等检查：不存在
            {"agent_id": "agent-1"},           # _resolve_agent_id: from
            {"agent_id": "agent-2"},           # _resolve_agent_id: to
            {                                  # INSERT RETURNING
                "message_id": "new-msg-id",
                "from_agent_id": "agent-1",
                "to_agent_id": "agent-2",
                "message_type": "info",
                "status": "unread",
                "delivery_status": "local",
                "created_at": None,
            },
        ]
        mock_conn.fetchval.return_value = None  # host 查询 → 本地投递

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await send_message("a1", "a2", "info", {"text": "x"},
                                        idempotency_key="prog_explicit")
            assert result["idempotency_key"] == "prog_explicit"
        # INSERT（第 4 次 fetchrow）参数含显式幂等键（$9 位置）
        insert_args = mock_conn.fetchrow.call_args_list[3][0]
        assert insert_args[9] == "prog_explicit"


# ── 收件箱 ────────────────────────────────────────────

class TestInbox:
    @pytest.mark.asyncio
    async def test_get_inbox(self, mock_conn, mock_pool):
        mock_conn.fetch.side_effect = [
            [  # 收件箱查询
                {"message_id": "m1", "from_agent_id": "a1", "to_agent_id": "agent-x", "status": "unread"},
                {"message_id": "m2", "from_agent_id": "a2", "to_agent_id": "agent-x", "status": "read"},
            ],
            [  # _enrich_agent_names 批量名称查询
                {"agent_id": "a1", "name": "Alice"},
                {"agent_id": "a2", "name": "Bob"},
                {"agent_id": "agent-x", "name": "XAgent"},
            ],
        ]

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            msgs = await get_inbox("agent-x")
            assert len(msgs) == 2
            assert msgs[0]["from_agent_name"] == "Alice"
            assert msgs[1]["from_agent_name"] == "Bob"
            assert msgs[0]["to_agent_name"] == "XAgent"

    @pytest.mark.asyncio
    async def test_get_inbox_with_status_filter(self, mock_conn, mock_pool):
        mock_conn.fetch.side_effect = [
            [{"message_id": "m1", "from_agent_id": "a1", "to_agent_id": "agent-x", "status": "unread"}],
            [{"agent_id": "a1", "name": "Alice"}, {"agent_id": "agent-x", "name": "XAgent"}],
        ]

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            msgs = await get_inbox("agent-x", status="unread")
            assert len(msgs) == 1
            assert msgs[0]["from_agent_name"] == "Alice"

    @pytest.mark.asyncio
    async def test_get_unread_count(self, mock_conn, mock_pool):
        mock_conn.fetchval.return_value = 5

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            count = await get_unread_count("agent-x")
            assert count == 5

    @pytest.mark.asyncio
    async def test_get_conversation(self, mock_conn, mock_pool):
        mock_conn.fetch.side_effect = [
            [  # 对话查询
                {"message_id": "m1", "from_agent_id": "a1", "to_agent_id": "a2"},
                {"message_id": "m2", "from_agent_id": "a2", "to_agent_id": "a1"},
            ],
            [  # _enrich_agent_names 名称查询
                {"agent_id": "a1", "name": "Alice"},
                {"agent_id": "a2", "name": "Bob"},
            ],
        ]

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            msgs = await get_conversation("a1", "a2")
            assert len(msgs) == 2
            assert msgs[0]["from_agent_name"] == "Alice"
            assert msgs[0]["to_agent_name"] == "Bob"


# ── 消息操作 ──────────────────────────────────────────

class TestMessageOperations:
    @pytest.mark.asyncio
    async def test_mark_read(self, mock_conn, mock_pool):
        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await mark_read("msg-1")
            assert result["status"] == "ok"
            mock_conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_mark_read(self, mock_conn, mock_pool):
        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await batch_mark_read(["msg-1", "msg-2"])
            assert result["status"] == "ok"
            assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_archive_message(self, mock_conn, mock_pool):
        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await archive_message("msg-1")
            assert result["status"] == "ok"


# ── 投递状态 ──────────────────────────────────────────

class TestDeliveryStatus:
    @pytest.mark.asyncio
    async def test_mark_delivered(self, mock_conn, mock_pool):
        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await mark_delivery_status("msg-1", "delivered")
            assert result["delivery_status"] == "delivered"

    @pytest.mark.asyncio
    async def test_mark_failed(self, mock_conn, mock_pool):
        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await mark_delivery_status("msg-1", "failed")
            assert result["delivery_status"] == "failed"

    @pytest.mark.asyncio
    async def test_get_pending_deliveries(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"message_id": "p1", "to_agent_id": "a2"},
            {"message_id": "p2", "to_agent_id": "a3"},
        ]

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            pending = await get_pending_deliveries()
            assert len(pending) == 2


# ── 重试投递 ─────────────────────────────────────────

class TestRetryDelivery:
    @pytest.mark.asyncio
    async def test_retry_delivery_marks_failed_when_no_target(self, mock_conn, mock_pool):
        """P2 (R11): 无投递目标（agent 无 server_host）→ 标记 failed 交日清任务清理。

        回归：此前返回 skipped 且不改状态，消息永远卡 pending 无限累积。
        """
        msg = {
            "message_id": "m1", "from_agent_id": "a1", "to_agent_id": "a2",
            "message_type": "info", "payload": {"k": "v"}, "priority": "normal",
            "signature": "sig", "idempotency_key": "ik", "reply_to": None,
            "negotiation_id": None,
        }
        mock_conn.fetchrow.return_value = msg
        mock_conn.fetchval.return_value = None  # server_host 查不到 → 无目标

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await retry_delivery("m1")

        assert result["status"] == "failed"
        assert "no_server_host" in result["reason"]
        # 状态已标记 failed（DB 有 UPDATE delivery_status='failed'）
        executed_sqls = [c.args[0] for c in mock_conn.execute.call_args_list]
        assert any("delivery_status = 'failed'" in s for s in executed_sqls)

    @pytest.mark.asyncio
    async def test_retry_delivery_marks_failed_when_peer_not_found(self, mock_conn, mock_pool):
        """P2 (R11): 目标底座未注册（peer 不可解析）→ 同样标记 failed。"""
        msg = {
            "message_id": "m1", "from_agent_id": "a1", "to_agent_id": "a2",
            "message_type": "info", "payload": {"k": "v"}, "priority": "normal",
            "signature": "sig", "idempotency_key": "ik", "reply_to": None,
            "negotiation_id": None,
        }
        # fetchrow: [消息行, peers 查无(active), peers 降级查无]；fetchval: server_host 有值, server_ip 查无
        mock_conn.fetchrow.side_effect = [msg, None, None]
        mock_conn.fetchval.side_effect = ["target-host", None]

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await retry_delivery("m1")

        assert result["status"] == "failed"
        assert "peer_not_found" in result["reason"]


# ── 跨底座消息入库 ────────────────────────────────────

class TestIncomingPeerMessage:
    @pytest.mark.asyncio
    async def test_new_peer_message(self, mock_conn, mock_pool):
        mock_conn.fetchrow.side_effect = [
            None,  # 幂等检查：不存在
            {"message_id": "peer-msg-1", "status": "unread"},  # INSERT 返回
        ]

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await insert_incoming_peer_message(
                "peer-msg-1", "from-a", "to-b", "info",
                {"k": "v"}, "normal", "sig", "idem-key",
            )
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_duplicate_peer_message(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"message_id": "existing-msg"}

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await insert_incoming_peer_message(
                "peer-msg-1", "from-a", "to-b", "info",
                {"k": "v"}, "normal", "sig", "idem-key",
            )
            assert result["status"] == "duplicate"


# ── 防重放（P2 (R11): replay_guard 首次接入消息入口） ─────────

class TestPeerMessageReplayGuard:
    @pytest.mark.asyncio
    async def test_replayed_nonce_rejected(self):
        """重复/过期 nonce → 判定为重放，拒绝返回 error。

        nonce 检查位于 peer_sig 验签之后（只记录已认证消息的 nonce），
        测试需先带有效 peer_sig 通过验签再触发防重放。
        """
        payload = {"k": "v"}
        sig = sign_peer_message(json.dumps(payload, ensure_ascii=False, sort_keys=True), "")
        body = {"msg_id": "m1", "from": "a1", "to": "a2", "message_type": "info",
                "payload": payload, "nonce": "dup-nonce", "peer_sig": sig,
                "idempotency_key": "ik1"}

        with patch("huanyu.replay_guard.get_replay_guard") as mock_guard:
            mock_guard.return_value.check_and_record.return_value = False
            result = await receive_peer_message(body)

        assert result["status"] == "error"
        assert "replay" in result["error"]
        mock_guard.return_value.check_and_record.assert_called_once_with("dup-nonce")

    @pytest.mark.asyncio
    async def test_fresh_nonce_recorded_then_inserted(self, mock_conn, mock_pool):
        """新 nonce 通过防重放并记录，随后正常入库（peer_sig 校验通过）。"""
        payload = {"k": "v"}
        nonce = "fresh-nonce-1"
        sig = sign_peer_message(json.dumps(payload, ensure_ascii=False, sort_keys=True), "")
        body = {"msg_id": "m1", "from": "a1", "to": "a2", "message_type": "info",
                "payload": payload, "nonce": nonce, "peer_sig": sig, "idempotency_key": "ik1"}

        with patch("huanyu.replay_guard.get_replay_guard") as mock_guard, \
             patch("huanyu.messaging.insert_incoming_peer_message", new_callable=AsyncMock) as mock_insert:
            mock_guard.return_value.check_and_record.return_value = True
            mock_insert.return_value = {"status": "ok", "message_id": "m1"}
            result = await receive_peer_message(body)

        mock_guard.return_value.check_and_record.assert_called_once_with(nonce)
        mock_insert.assert_called_once()
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_missing_nonce_skips_guard(self, mock_conn, mock_pool):
        """无 nonce（国标等旧格式）→ 跳过防重放，不阻断入库。"""
        payload = {"k": "v"}
        sig = sign_peer_message(json.dumps(payload, ensure_ascii=False, sort_keys=True), "")
        body = {"msg_id": "m1", "from": "a1", "to": "a2", "message_type": "info",
                "payload": payload, "peer_sig": sig, "idempotency_key": "ik1"}

        with patch("huanyu.replay_guard.get_replay_guard") as mock_guard, \
             patch("huanyu.messaging.insert_incoming_peer_message", new_callable=AsyncMock) as mock_insert:
            mock_guard.return_value.check_and_record.return_value = True
            mock_insert.return_value = {"status": "ok", "message_id": "m1"}
            result = await receive_peer_message(body)

        mock_guard.return_value.check_and_record.assert_not_called()
        mock_insert.assert_called_once()
        assert result["status"] == "ok"


# ── 消息验证 ──────────────────────────────────────────

class TestVerifyMessageIntegrity:
    @pytest.mark.asyncio
    async def test_valid_message(self, mock_conn, mock_pool):
        from huanyu.signing import sign_message

        payload = {"test": "data"}
        sig = sign_message("a1", "a2", "info", json.dumps(payload, ensure_ascii=False, sort_keys=True))

        mock_conn.fetchrow.return_value = {
            "from_agent_id": "a1",
            "to_agent_id": "a2",
            "message_type": "info",
            "payload": payload,
            "signature": sig,
        }

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await verify_message_integrity("msg-1")
            assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_tampered_message(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "from_agent_id": "a1",
            "to_agent_id": "a2",
            "message_type": "info",
            "payload": {"test": "data"},
            "signature": "wrong_sig",
        }

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await verify_message_integrity("msg-1")
            assert result["valid"] is False

    @pytest.mark.asyncio
    async def test_message_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.messaging.get_pool", return_value=mock_pool):
            result = await verify_message_integrity("nonexistent")
            assert result["valid"] is False
            assert "不存在" in result["error"]
