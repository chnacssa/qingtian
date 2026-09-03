"""
account_service.py 单元测试
SELECT FOR UPDATE 原子操作 + 哈希链
"""

from unittest.mock import patch

import pytest

from siku.account_service import (
    ensure_account,
    get_account,
    check_balance,
    recharge,
    deduct,
    admin_adjust,
    get_transactions,
    verify_chain,
    _compute_txn_hash,
)
from siku.config import GENESIS_HASH


class TestEnsureAccount:
    @pytest.mark.asyncio
    async def test_new_account(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1", "balance_fen": 0, "frozen_fen": 0,
            "total_recharged": 0, "created_at": None,
        }
        result = await ensure_account(mock_conn, "a1")
        assert result["agent_id"] == "a1"
        assert result["balance_fen"] == 0

    @pytest.mark.asyncio
    async def test_existing_account(self, mock_conn):
        """INSERT 返回空 → SELECT 已有记录"""
        mock_conn.fetchrow.side_effect = [
            None,
            {"agent_id": "a1", "balance_fen": 100, "frozen_fen": 0,
             "total_recharged": 100, "created_at": None},
        ]
        result = await ensure_account(mock_conn, "a1")
        assert result["balance_fen"] == 100


class TestGetAccount:
    @pytest.mark.asyncio
    async def test_found(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1", "balance_fen": 500, "frozen_fen": 50,
            "total_recharged": 1000, "created_at": None, "updated_at": None,
        }
        result = await get_account(mock_conn, "a1")
        assert result["available_fen"] == 450

    @pytest.mark.asyncio
    async def test_not_found(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await get_account(mock_conn, "nonexistent")
        assert result is None


class TestCheckBalance:
    @pytest.mark.asyncio
    async def test_sufficient(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1", "balance_fen": 500, "frozen_fen": 0,
            "total_recharged": 1000, "created_at": None, "updated_at": None,
        }
        result = await check_balance(mock_conn, "a1", 300)
        assert result["sufficient"] is True

    @pytest.mark.asyncio
    async def test_insufficient(self, mock_conn):
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1", "balance_fen": 100, "frozen_fen": 0,
            "total_recharged": 100, "created_at": None, "updated_at": None,
        }
        result = await check_balance(mock_conn, "a1", 500)
        assert result["sufficient"] is False

    @pytest.mark.asyncio
    async def test_no_account(self, mock_conn):
        mock_conn.fetchrow.return_value = None
        result = await check_balance(mock_conn, "nonexistent", 100)
        assert result["sufficient"] is False


class TestRecharge:
    @pytest.mark.asyncio
    async def test_basic(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            # FOR UPDATE (new order: lock first, then idempotency)
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0},
            # idempotency check
            None,
            # INSERT RETURNING
            {"txn_id": 1, "agent_id": "a1", "txn_type": "recharge", "amount_fen": 500,
             "balance_after": 1500, "fee_type": "", "idempotency_key": "k1",
             "created_at": None},
        ]
        mock_conn.fetchval.return_value = None  # prev_hash

        result = await recharge(mock_conn, "a1", 500, "k1", "test")
        assert result["status"] == "ok"
        assert result["amount_fen"] == 500

    @pytest.mark.asyncio
    async def test_idempotent(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            # FOR UPDATE first (new order)
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0},
            # idempotency check: already exists
            {"txn_id": 1, "agent_id": "a1", "txn_type": "recharge",
             "amount_fen": 500, "balance_after": 1500, "fee_type": "",
             "reference_id": "", "detail": {}, "idempotency_key": "k1",
             "created_at": None},
        ]
        result = await recharge(mock_conn, "a1", 500, "k1")
        assert result["already_processed"] is True

    @pytest.mark.asyncio
    async def test_agent_not_found(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            None,  # FOR UPDATE — 账户不存在（新顺序：先锁行）
        ]
        with pytest.raises(ValueError, match="not found"):
            await recharge(mock_conn, "a1", 500)


class TestDeduct:
    @pytest.mark.asyncio
    async def test_basic(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            # FOR UPDATE first (new order)
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0},
            # idempotency check
            None,
            # INSERT RETURNING
            {"txn_id": 2, "agent_id": "a1", "txn_type": "deduct", "amount_fen": 300,
             "balance_after": 700, "fee_type": "cert_upgrade", "reference_id": "C1",
             "idempotency_key": "k2", "created_at": None},
        ]
        mock_conn.fetchval.return_value = None  # prev_hash

        result = await deduct(mock_conn, "a1", 300, "cert_upgrade", "C1", "k2")
        assert result["status"] == "ok"
        assert result["balance_after"] == 700

    @pytest.mark.asyncio
    async def test_insufficient(self, mock_conn):
        mock_conn.fetchrow.return_value = \
            {"agent_id": "a1", "balance_fen": 100, "frozen_fen": 0}
        result = await deduct(mock_conn, "a1", 500)
        assert result["status"] == "error"
        assert result["error"] == "INSUFFICIENT_BALANCE"

    @pytest.mark.asyncio
    async def test_frozen_reduces_available(self, mock_conn):
        mock_conn.fetchrow.return_value = \
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 600}
        result = await deduct(mock_conn, "a1", 500)
        assert result["error"] == "INSUFFICIENT_BALANCE"

    @pytest.mark.asyncio
    async def test_idempotent(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            # FOR UPDATE first (new order)
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0},
            # idempotency check: already exists
            {"txn_id": 2, "agent_id": "a1", "txn_type": "deduct",
             "amount_fen": 300, "balance_after": 700, "fee_type": "cert_upgrade",
             "reference_id": "C1", "detail": {}, "idempotency_key": "k2",
             "created_at": None},
        ]
        result = await deduct(mock_conn, "a1", 300, "cert_upgrade", "C1", "k2")
        assert result["already_processed"] is True


class TestAdminAdjust:
    @pytest.mark.asyncio
    async def test_positive_adjust(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0},  # FOR UPDATE
            {"txn_id": 3, "agent_id": "a1", "txn_type": "admin_adjust",
             "amount_fen": 500, "balance_after": 1500, "fee_type": "",
             "idempotency_key": "", "created_at": None},  # INSERT RETURNING
        ]
        mock_conn.fetchval.return_value = None

        result = await admin_adjust(mock_conn, "a1", 500, "refund for C1 upgrade failure")
        assert result["status"] == "ok"
        assert result["balance_after"] == 1500

    @pytest.mark.asyncio
    async def test_negative_overdraft(self, mock_conn):
        mock_conn.fetchrow.return_value = \
            {"agent_id": "a1", "balance_fen": 100, "frozen_fen": 0}
        result = await admin_adjust(mock_conn, "a1", -500)
        assert result["error"] == "INSUFFICIENT_BALANCE"


class TestGetTransactions:
    @pytest.mark.asyncio
    async def test_basic(self, mock_conn):
        mock_conn.fetch.return_value = [
            {"txn_id": 2, "agent_id": "a1", "txn_type": "deduct", "amount_fen": 300,
             "balance_after": 700, "fee_type": "cert_upgrade", "reference_id": "C1",
             "idempotency_key": "k2", "detail": {}, "created_at": None},
            {"txn_id": 1, "agent_id": "a1", "txn_type": "recharge", "amount_fen": 1000,
             "balance_after": 1000, "fee_type": "", "reference_id": "",
             "idempotency_key": "k1", "detail": {}, "created_at": None},
        ]
        result = await get_transactions(mock_conn, "a1")
        assert len(result) == 2


class TestHashChain:
    def test_compute_txn_hash_deterministic(self):
        h1 = _compute_txn_hash(GENESIS_HASH, "a1", "recharge", "", 100, 200, "2026-01-01T00:00:00+00:00")
        h2 = _compute_txn_hash(GENESIS_HASH, "a1", "recharge", "", 100, 200, "2026-01-01T00:00:00+00:00")
        assert h1 == h2

    def test_compute_txn_hash_different_inputs(self):
        h1 = _compute_txn_hash(GENESIS_HASH, "a1", "recharge", "", 100, 200, "2026-01-01T00:00:00+00:00")
        h2 = _compute_txn_hash(GENESIS_HASH, "a1", "recharge", "", 101, 200, "2026-01-01T00:00:00+00:00")
        assert h1 != h2

    @pytest.mark.asyncio
    async def test_verify_chain_valid(self, mock_conn):
        h1 = _compute_txn_hash(GENESIS_HASH, "a1", "recharge", "", 1000, 1000, "2026-01-01T00:00:00+00:00")
        h2 = _compute_txn_hash(h1, "a1", "deduct", "cert_upgrade", 300, 700, "2026-01-02T00:00:00+00:00")

        from datetime import datetime, timezone
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        mock_conn.fetch.return_value = [
            {"txn_id": 1, "prev_hash": GENESIS_HASH, "row_hash": h1,
             "agent_id": "a1", "txn_type": "recharge", "fee_type": "",
             "amount_fen": 1000, "balance_after": 1000, "created_at": t1},
            {"txn_id": 2, "prev_hash": h1, "row_hash": h2,
             "agent_id": "a1", "txn_type": "deduct", "fee_type": "cert_upgrade",
             "amount_fen": 300, "balance_after": 700, "created_at": t2},
        ]
        result = await verify_chain(mock_conn, "a1")
        assert result["valid"] is True
        assert result["total_txns"] == 2

    @pytest.mark.asyncio
    async def test_verify_chain_broken_prev(self, mock_conn):
        h1 = _compute_txn_hash(GENESIS_HASH, "a1", "recharge", "", 1000, 1000, "2026-01-01T00:00:00+00:00")
        bad_prev = "aaaabbbbccccddddeeeeffff000011112222333344445555666677778888"

        t1 = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc)
        t2 = __import__("datetime").datetime(2026, 1, 2, tzinfo=__import__("datetime").timezone.utc)

        mock_conn.fetch.return_value = [
            {"txn_id": 1, "prev_hash": GENESIS_HASH, "row_hash": h1,
             "agent_id": "a1", "txn_type": "recharge", "fee_type": "",
             "amount_fen": 1000, "balance_after": 1000, "created_at": t1},
            {"txn_id": 2, "prev_hash": bad_prev, "row_hash": "xxx",
             "agent_id": "a1", "txn_type": "deduct", "fee_type": "cert_upgrade",
             "amount_fen": 300, "balance_after": 700, "created_at": t2},
        ]
        result = await verify_chain(mock_conn, "a1")
        assert result["valid"] is False
        assert result["broken_at_txn_id"] == 2

    @pytest.mark.asyncio
    async def test_verify_chain_empty(self, mock_conn):
        mock_conn.fetch.return_value = []
        result = await verify_chain(mock_conn, "a1")
        assert result["valid"] is True
        assert result["total_txns"] == 0
