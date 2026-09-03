"""
infra:finance — 财务对账 Agent 测试

测试 _check_balance、_auto_recharge、handle_billing_event 方法，
通过 mock get_pool / account_service 控制返回值。
"""

import pytest
from unittest.mock import AsyncMock, patch

from builtin.finance_agent import (
    _check_balance,
    _auto_recharge,
    handle_billing_event,
    _daily_reconciliation,
)


# ── Mock DB helpers (following huanyu/tests/conftest.py pattern) ──

class _ConnCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        pass


class _MockPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _ConnCtx(self._conn)

    async def close(self):
        pass


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.execute = AsyncMock()
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    return _MockPool(mock_conn)


def _account_row(balance_fen=0, frozen_fen=0):
    """account_service.get_account 返回行（单位：分）"""
    return {
        "agent_id": "biz:buyer-01",
        "balance_fen": balance_fen,
        "frozen_fen": frozen_fen,
        "total_recharged": 0,
        "created_at": None,
        "updated_at": None,
    }


# ══════════════════════════════════════════════════════════
# _check_balance
# ══════════════════════════════════════════════════════════

class TestCheckBalance:
    async def test_balance_found(self, mock_pool, mock_conn):
        mock_conn.fetchrow = AsyncMock(return_value=_account_row(balance_fen=20000, frozen_fen=5000))

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)):
            balance = await _check_balance("biz:buyer-01")

        assert balance == 15000  # available = balance_fen - frozen_fen
        mock_conn.fetchrow.assert_called_once()
        assert "FROM siku.accounts" in mock_conn.fetchrow.call_args[0][0]

    async def test_balance_not_found(self, mock_pool, mock_conn):
        mock_conn.fetchrow = AsyncMock(return_value=None)

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)):
            balance = await _check_balance("biz:nonexistent")

        assert balance is None

    async def test_db_error_returns_none(self, mock_pool, mock_conn):
        mock_conn.fetchrow = AsyncMock(side_effect=Exception("DB connection lost"))

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)):
            balance = await _check_balance("biz:buyer-01")

        assert balance is None

    async def test_negative_balance(self, mock_pool, mock_conn):
        mock_conn.fetchrow = AsyncMock(return_value=_account_row(balance_fen=-5000))

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)):
            balance = await _check_balance("biz:buyer-01")

        assert balance == -5000


# ══════════════════════════════════════════════════════════
# _auto_recharge
# ══════════════════════════════════════════════════════════

class TestAutoRecharge:
    async def test_recharge_success(self, mock_pool, mock_conn):
        mock_recharge = AsyncMock(return_value={
            "txn_id": 1, "agent_id": "biz:buyer-01",
            "status": "ok", "balance_after": 30000,
        })

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("builtin.finance_agent.siku_acct.recharge", mock_recharge):
            result = await _auto_recharge("biz:buyer-01", 15000)

        assert result["status"] == "ok"
        assert result["balance"] == 30000
        call = mock_recharge.call_args
        assert call[0][1] == "biz:buyer-01"
        assert call[0][2] == 15000

    async def test_recharge_account_not_found(self, mock_pool, mock_conn):
        mock_recharge = AsyncMock(side_effect=ValueError("agent biz:ghost not found"))

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("builtin.finance_agent.siku_acct.recharge", mock_recharge):
            result = await _auto_recharge("biz:ghost", 10000)

        assert result["status"] == "error"
        assert result["error"] == "account not found"

    async def test_recharge_db_error(self, mock_pool, mock_conn):
        mock_recharge = AsyncMock(side_effect=Exception("UPDATE failed"))

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("builtin.finance_agent.siku_acct.recharge", mock_recharge):
            result = await _auto_recharge("biz:buyer-01", 10000)

        assert result["status"] == "error"
        assert "UPDATE failed" in result["error"]


# ══════════════════════════════════════════════════════════
# handle_billing_event
# ══════════════════════════════════════════════════════════

class TestHandleBillingEvent:
    async def test_billing_alert_triggers_recharge_when_negative(self, mock_pool, mock_conn):
        # 余额 -50 元（-5000 分），充值金额 = 5000 + 10000 = 15000 分
        mock_conn.fetchrow = AsyncMock(return_value=_account_row(balance_fen=-5000))
        mock_recharge = AsyncMock(return_value={
            "txn_id": 1, "agent_id": "biz:buyer-01",
            "status": "ok", "balance_after": 10000,
        })

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("builtin.finance_agent.siku_acct.recharge", mock_recharge):
            await handle_billing_event({
                "type": "billing_alert",
                "payload": {"agent_id": "biz:buyer-01"},
            })

        mock_recharge.assert_called_once()
        call = mock_recharge.call_args
        assert call[0][1] == "biz:buyer-01"
        assert call[0][2] == 15000

    async def test_billing_alert_positive_balance_skips_recharge(self, mock_pool, mock_conn):
        mock_conn.fetchrow = AsyncMock(return_value=_account_row(balance_fen=20000))
        mock_recharge = AsyncMock()

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("builtin.finance_agent.siku_acct.recharge", mock_recharge):
            await handle_billing_event({
                "type": "billing_alert",
                "payload": {"agent_id": "biz:buyer-01"},
            })

        mock_recharge.assert_not_called()

    async def test_billing_alert_no_agent_id(self):
        with patch("builtin.finance_agent.get_pool") as mock_get_pool:
            await handle_billing_event({
                "type": "billing_alert",
                "payload": {},
            })
            mock_get_pool.assert_not_called()

    async def test_payment_received_logs(self):
        # Should not raise, should not call DB
        with patch("builtin.finance_agent.get_pool") as mock_get_pool:
            await handle_billing_event({
                "type": "payment_received",
                "payload": {"agent_id": "biz:buyer-01", "amount": 500},
            })
            mock_get_pool.assert_not_called()

    async def test_fee_due_soon_logs_warning(self):
        with patch("builtin.finance_agent.get_pool") as mock_get_pool:
            await handle_billing_event({
                "type": "fee_due",
                "payload": {"agent_id": "biz:buyer-01", "days_left": 2},
            })
            mock_get_pool.assert_not_called()

    async def test_fee_due_far_does_nothing(self):
        with patch("builtin.finance_agent.get_pool") as mock_get_pool:
            await handle_billing_event({
                "type": "fee_due",
                "payload": {"agent_id": "biz:buyer-01", "days_left": 7},
            })
            mock_get_pool.assert_not_called()


# ══════════════════════════════════════════════════════════
# _daily_reconciliation
# ══════════════════════════════════════════════════════════

class TestDailyReconciliation:
    async def test_reconciliation_runs_query(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(return_value=[
            {"agent_id": "biz:buyer-01", "total_fen": 150000},
            {"agent_id": "biz:seller-01", "total_fen": 80000},
        ])

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)):
            await _daily_reconciliation()

        mock_conn.fetch.assert_called_once()
        query = mock_conn.fetch.call_args[0][0]
        assert "siku.transactions" in query
        assert "SUM(amount_fen)" in query

    async def test_reconciliation_no_transactions(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(return_value=[])

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)):
            await _daily_reconciliation()

        mock_conn.fetch.assert_called_once()

    async def test_reconciliation_db_error(self, mock_pool, mock_conn):
        mock_conn.fetch = AsyncMock(side_effect=Exception("Query failed"))

        with patch("builtin.finance_agent.get_pool", AsyncMock(return_value=mock_pool)):
            # Should not raise
            await _daily_reconciliation()

        mock_conn.fetch.assert_called_once()
