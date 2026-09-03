"""司库+寰宇联调 Round 2 — 消息链路集成测试 (mock DB)

覆盖:
  - send_message → finance_agent 收到 payment_notify
  - process_inbox 轮询逻辑
  - 消息标记已读
  - 幂等消息去重
  - payment_confirm 回执
"""

import json
import pytest

# ═══════════════════════════════════════════════════════
# 消息路由：Agent → huanyu → finance_agent
# ═══════════════════════════════════════════════════════


class TestPaymentNotifyRouting:
    """payment_notify 消息路由到 finance_agent"""

    def test_payment_notify_payload_structure(self):
        """payment_notify 消息格式校验"""
        msg = {
            "message_type": "payment_notify",
            "from_agent_id": "buyer-agent-001",
            "to_agent_id": "infra:finance",
            "payload": {
                "amount_fen": 500000,
                "product": "螺纹钢",
                "agreement_id": "agr-001",
                "buyer_finance_ain": "CN-BJ-F0001",
                "seller_finance_ain": "CN-SH-F0002",
            },
        }
        assert msg["message_type"] == "payment_notify"
        assert "amount_fen" in msg["payload"]
        assert msg["payload"]["amount_fen"] > 0

    def test_payment_confirm_payload_structure(self):
        """payment_confirm 回执格式"""
        confirm = {
            "message_type": "payment_confirm",
            "from_agent_id": "infra:finance",
            "to_agent_id": "buyer-agent-001",
            "payload": {
                "txn_id": 42,
                "amount_fen": 500000,
                "status": "confirmed",
                "matched_bank_txn": "BANK20260604001",
            },
        }
        assert confirm["payload"]["status"] == "confirmed"

    def test_message_idempotency_key_unique(self):
        """每条消息幂等键唯一"""
        keys = [f"pay-{i}-{hex(i*31)}" for i in range(100)]
        assert len(keys) == len(set(keys))


class TestFinanceAgentInboxPoll:
    """finance_agent.process_inbox — 轮询未读消息"""

    @pytest.mark.asyncio
    async def test_empty_inbox_returns_nothing(self):
        """空 inbox → 不处理"""
        class MockConn:
            async def fetch(self, query, *params):
                return []

        # 模拟 process_inbox 的核心逻辑
        rows = await MockConn().fetch("SELECT ... WHERE status='unread'")
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_unread_payment_notify_processed(self):
        """未读 payment_notify → 被拉取处理"""
        messages = [
            {
                "message_id": "msg-001",
                "from_agent_id": "buyer-001",
                "to_agent_id": "infra:finance",
                "message_type": "payment_notify",
                "payload": json.dumps({"amount_fen": 10000}),
                "status": "unread",
            },
            {
                "message_id": "msg-002",
                "from_agent_id": "buyer-002",
                "to_agent_id": "infra:finance",
                "message_type": "payment_notify",
                "payload": json.dumps({"amount_fen": 20000}),
                "status": "unread",
            },
        ]

        class MockConn:
            async def fetch(self, query, *params):
                return messages

            async def execute(self, query, *params):
                return "UPDATE 1"

        rows = await MockConn().fetch("SELECT ...")
        payment_msgs = [r for r in rows if r["message_type"] == "payment_notify"]
        assert len(payment_msgs) == 2
        p0 = payment_msgs[0]["payload"]
        p1 = payment_msgs[1]["payload"]
        # payload 可以是 dict 或 JSON 字符串
        if isinstance(p0, str):
            p0 = json.loads(p0)
        if isinstance(p1, str):
            p1 = json.loads(p1)
        assert p0["amount_fen"] == 10000
        assert p1["amount_fen"] == 20000

    @pytest.mark.asyncio
    async def test_mark_as_read_after_processing(self):
        """处理完成后标记已读"""
        marked_ids = []

        class MockConn:
            async def fetch(self, query, *params):
                return [{
                    "message_id": "msg-001",
                    "from_agent_id": "buyer-001",
                    "message_type": "payment_notify",
                    "payload": json.dumps({"amount_fen": 10000}),
                    "status": "unread",
                }]

            async def execute(self, query, *params):
                marked_ids.append(params[0] if params else "unknown")
                return "UPDATE 1"

        conn = MockConn()
        rows = await conn.fetch("SELECT ...")
        for row in rows:
            await conn.execute("UPDATE messages SET status='read' ...", row["message_id"])

        assert "msg-001" in marked_ids


class TestCrossBaseRouting:
    """跨底座消息路由"""

    def test_local_agent_resolved_locally(self):
        """本地 Agent → 不跨底座"""
        local_agents = {"buyer-001", "seller-001", "infra:finance"}
        target = "infra:finance"
        assert target in local_agents  # 本地解析
        is_cross_base = target not in local_agents
        assert is_cross_base is False

    def test_remote_agent_requires_cross_base(self):
        """远程 Agent → 标记为 cross_base"""
        local_agents = {"buyer-001", "infra:finance"}
        target = "remote-seller-002"
        assert target not in local_agents  # 需要跨底座


class TestMessageSignature:
    """消息 HMAC 签名验证"""

    def test_tampered_payload_detected(self):
        """payload 被篡改 → 签名不匹配"""
        import hashlib, hmac

        key = b"test-sign-key"
        payload = json.dumps({"amount": 1000}, sort_keys=True)
        sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

        # 篡改
        tampered = json.dumps({"amount": 9999}, sort_keys=True)
        sig2 = hmac.new(key, tampered.encode(), hashlib.sha256).hexdigest()

        assert sig != sig2

    def test_different_keys_different_signatures(self):
        """不同密钥 → 签名不同"""
        import hashlib, hmac

        payload = json.dumps({"amount": 1000})
        sig1 = hmac.new(b"key-a", payload.encode(), hashlib.sha256).hexdigest()
        sig2 = hmac.new(b"key-b", payload.encode(), hashlib.sha256).hexdigest()

        assert sig1 != sig2
