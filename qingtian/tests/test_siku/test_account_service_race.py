"""司库 — 账户服务幂等 + 并发竞态测试 (无 DB 依赖)

覆盖逻辑路径: 幂等键重复 / 余额不足 / 调账负数保护 /
             _compute_txn_hash / ensure_account mock
"""

import hashlib
import json
import pytest

from siku.account_service import (
    _compute_txn_hash,
    GENESIS,
)


class TestComputeTxnHash:
    def test_deterministic(self):
        h1 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        h2 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        assert h1 == h2

    def test_amount_changes_hash(self):
        h1 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        h2 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               4999, 14999, "2026-06-04T10:00:00+00:00")
        assert h1 != h2

    def test_balance_after_changes_hash(self):
        h1 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        h2 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               5000, 15001, "2026-06-04T10:00:00+00:00")
        assert h1 != h2

    def test_agent_id_changes_hash(self):
        h1 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        h2 = _compute_txn_hash(GENESIS, "agent-2", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        assert h1 != h2

    def test_prev_hash_changes_output(self):
        h1 = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        h2 = _compute_txn_hash(h1, "agent-1", "recharge", "",
                               5000, 15000, "2026-06-04T10:00:00+00:00")
        assert h1 != h2

    def test_hash_is_64_hex(self):
        h = _compute_txn_hash(GENESIS, "agent-1", "recharge", "",
                              5000, 15000, "2026-06-04T10:00:00+00:00")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


class TestIdempotencyLogic:
    """幂等键重复检测 — 验证逻辑路径（mock DB）"""

    @pytest.mark.asyncio
    async def test_recharge_idempotent_returns_existing(self):
        """重复幂等键 → 返回已有交易，不二次入账"""
        from siku.account_service import recharge

        class MockConn:
            async def fetchrow(self, query, *params):
                if "FOR UPDATE" in query:
                    return {"agent_id": "test", "balance_fen": 10000, "frozen_fen": 0}
                if "idempotency_key" in query:
                    return {
                        "txn_id": 1, "agent_id": "test",
                        "txn_type": "recharge", "amount_fen": 5000,
                        "balance_after": 15000, "fee_type": "",
                        "reference_id": "", "detail": "{}",
                        "idempotency_key": "dup-key-001",
                        "created_at": None,
                    }
                return None

            async def fetchval(self, query, *params):
                return "a" * 64

            async def execute(self, query, *params):
                return "UPDATE 1"

        result = await recharge(MockConn(), "test", 5000, idempotency_key="dup-key-001")
        assert result.get("already_processed") is True

    @pytest.mark.asyncio
    async def test_deduct_insufficient_balance_returns_error(self):
        """余额不足 → 返回 error，不扣款"""
        from siku.account_service import deduct

        account_called = False

        class MockConn:
            async def fetchrow(self, query, *params):
                nonlocal account_called
                if "FOR UPDATE" in query:
                    account_called = True
                    return {"agent_id": "test", "balance_fen": 100, "frozen_fen": 0}
                if "idempotency_key" in query:
                    return None
                return None

            async def fetchval(self, query, *params):
                return "a" * 64

            async def execute(self, query, *params):
                return "UPDATE 0"

        result = await deduct(MockConn(), "test", 5000)  # 只有 100，要扣 5000
        assert result["status"] == "error"
        assert result["error"] == "INSUFFICIENT_BALANCE"
        assert result["available_fen"] == 100

    @pytest.mark.asyncio
    async def test_admin_adjust_negative_cannot_exceed_balance(self):
        """调账扣回超过余额 → 拒绝"""
        from siku.account_service import admin_adjust

        class MockConn:
            async def fetchrow(self, query, *params):
                if "FOR UPDATE" in query:
                    return {"agent_id": "test", "balance_fen": 1000, "frozen_fen": 0}
                if "idempotency_key" in query:
                    return None
                return None

            async def fetchval(self, query, *params):
                return "a" * 64

            async def execute(self, query, *params):
                return "UPDATE 0"

        result = await admin_adjust(MockConn(), "test", -2000)  # 扣回 2000 > 余额 1000
        assert result["status"] == "error"
        assert result["error"] == "INSUFFICIENT_BALANCE"


class TestRechargeDeductRace:
    """并发竞态：两个请求同时扣款，总额不应超过余额"""

    @pytest.mark.asyncio
    async def test_concurrent_deduct_second_sees_updated_balance(self):
        """模拟 A 扣款后 B 再读 → B 看到扣后余额（FOR UPDATE 序列化）"""
        balance_state = {"value": 10000}

        txn_counter = [0]

        class MockConn:
            async def fetchrow(self, query, *params):
                if "FOR UPDATE" in query:
                    return {"agent_id": "test", "balance_fen": balance_state["value"],
                            "frozen_fen": 0}
                if "transactions" in query and "INSERT INTO" in query and "RETURNING" in query:
                    txn_counter[0] += 1
                    return {
                        "txn_id": txn_counter[0],
                        "agent_id": "test",
                        "txn_type": "deduct",
                        "amount_fen": params[1] if len(params) > 1 else 0,
                        "balance_after": params[2] if len(params) > 2 else 0,
                        "fee_type": "",
                        "reference_id": "",
                        "idempotency_key": "",
                        "created_at": None,
                    }
                if "idempotency_key" in query:
                    return None
                return None

            async def fetchval(self, query, *params):
                return "a" * 64

            async def execute(self, query, *params):
                if "UPDATE" in query and "balance_fen" in query:
                    new_balance = params[0]
                    balance_state["value"] = new_balance
                    return "UPDATE 1"
                return "INSERT 1"

        from siku.account_service import deduct

        # A 先扣 6000
        r1 = await deduct(MockConn(), "test", 6000)
        assert r1["status"] == "ok", f"A failed: {r1}"

        # B 再扣 6000（余额只剩 4000，应拒绝）
        r2 = await deduct(MockConn(), "test", 6000)
        assert r2["status"] == "error"
        assert r2["error"] == "INSUFFICIENT_BALANCE"


class TestGenesisCrossModule:
    def test_audit_and_account_share_genesis(self):
        from siku.audit import GENESIS as AUDIT_GENESIS
        from siku.account_service import GENESIS as ACCT_GENESIS
        assert AUDIT_GENESIS == ACCT_GENESIS
        assert len(AUDIT_GENESIS) == 64
