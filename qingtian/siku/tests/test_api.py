"""
api.py 单元测试
余额 / 充值 / 扣款 / 年费 / 发票 / 定价端点
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from siku.api import (
    get_balance, recharge, deduct, check_balance,
    get_transactions, verify_chain,
    annual_status, annual_pay,
    pricing, payment_info,
    request_invoice, list_invoices, get_invoice,
    issue_invoice, reject_invoice, void_invoice,
)


AUTH_ADMIN = {"agent_id": "admin", "role": "admin"}
AUTH_AGENT = {"agent_id": "a1", "role": "agent"}


def _mock_pool(conn):
    class Ctx:
        def __init__(self, c):
            self.c = c
        async def __aenter__(self):
            return self.c
        async def __aexit__(self, *args):
            pass
    class Pool:
        def acquire(self):
            return Ctx(conn)
    return Pool()


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_admin_can_read(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1", "balance_fen": 500, "frozen_fen": 0,
            "total_recharged": 1000, "available_fen": 500,
        }
        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await get_balance("a1", AUTH_ADMIN)
            assert result["available_fen"] == 500

    @pytest.mark.asyncio
    async def test_agent_can_read_own(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1", "balance_fen": 500, "frozen_fen": 0,
            "total_recharged": 1000, "available_fen": 500,
        }
        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await get_balance("a1", AUTH_AGENT)
            assert result["available_fen"] == 500

    @pytest.mark.asyncio
    async def test_agent_cannot_read_others(self, mock_conn, mock_pool):
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await get_balance("a2", AUTH_AGENT)
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await get_balance("a1", AUTH_ADMIN)
            assert exc.value.status_code == 404


class TestRechargeAPI:
    @pytest.mark.asyncio
    async def test_admin_can_recharge(self, mock_conn, mock_pool):
        from siku.models import RechargeRequest
        mock_conn.fetchrow.side_effect = [
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0},
            None,
            {"txn_id": 1, "agent_id": "a1", "txn_type": "recharge",
             "amount_fen": 500, "balance_after": 1500, "fee_type": "",
             "idempotency_key": "k1", "created_at": None},
        ]
        mock_conn.fetchval.return_value = None

        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await recharge(
                RechargeRequest(agent_id="a1", amount_fen=500, idempotency_key="k1"),
                AUTH_ADMIN,
            )
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_agent_cannot_recharge(self):
        from siku.models import RechargeRequest
        with pytest.raises(HTTPException) as exc:
            await recharge(RechargeRequest(agent_id="a1", amount_fen=500, idempotency_key="k1"),
                         AUTH_AGENT)
        assert exc.value.status_code == 403


class TestDeductAPI:
    @pytest.mark.asyncio
    async def test_success(self, mock_conn, mock_pool):
        from siku.models import DeductRequest
        mock_conn.fetchrow.side_effect = [
            {"agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0},
            None,
            {"txn_id": 2, "agent_id": "a1", "txn_type": "deduct",
             "amount_fen": 300, "balance_after": 700, "fee_type": "cert_upgrade",
             "reference_id": "C1", "idempotency_key": "k2", "created_at": None},
        ]
        mock_conn.fetchval.return_value = None

        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await deduct(
                DeductRequest(agent_id="a1", amount_fen=300, fee_type="cert_upgrade",
                            reference_id="C1", idempotency_key="k2"),
                AUTH_AGENT,
            )
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_agent_cannot_deduct_others(self, mock_conn, mock_pool):
        from siku.models import DeductRequest
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await deduct(
                    DeductRequest(agent_id="a2", amount_fen=300, fee_type="cert_upgrade"),
                    AUTH_AGENT,
                )
            assert exc.value.status_code == 403


class TestCheckBalanceAPI:
    @pytest.mark.asyncio
    async def test_sufficient(self, mock_conn, mock_pool):
        from siku.models import CheckBalanceRequest
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1", "balance_fen": 1000, "frozen_fen": 0,
            "total_recharged": 2000, "created_at": None, "updated_at": None,
        }
        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await check_balance(
                CheckBalanceRequest(agent_id="a1", required_fen=300),
                AUTH_AGENT,
            )
            assert result["sufficient"] is True


class TestGetTransactionsAPI:
    @pytest.mark.asyncio
    async def test_basic(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = []
        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await get_transactions("a1", 20, 0, AUTH_AGENT)
            assert result["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_agent_cannot_read_others(self, mock_conn, mock_pool):
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await get_transactions("a2", 20, 0, AUTH_AGENT)
            assert exc.value.status_code == 403


class TestVerifyChainAPI:
    @pytest.mark.asyncio
    async def test_requires_admin(self, mock_conn, mock_pool):
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await verify_chain("a1", AUTH_AGENT)
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_valid(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = []
        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await verify_chain("a1", AUTH_ADMIN)
            assert result["valid"] is True


class TestPricing:
    @pytest.mark.asyncio
    async def test_returns_pricing(self):
        result = await pricing()
        assert "C1" in result["cert"]
        assert result["annual_fee"] == 99600


class TestPaymentInfo:
    @pytest.mark.asyncio
    async def test_requires_auth(self):
        with pytest.raises(HTTPException) as exc:
            await payment_info({"agent_id": "guest", "role": "guest"})
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_returns_info(self):
        with patch("siku.api.cfg.get_payment_info", return_value={"corporate": {}}):
            result = await payment_info(AUTH_AGENT)
            assert "note" in result


class TestInvoices:
    @pytest.mark.asyncio
    async def test_list_own(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = []
        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await list_invoices("a1", None, 20, 0, AUTH_AGENT)
            assert result["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_list_other_agent(self, mock_conn, mock_pool):
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await list_invoices("a2", None, 20, 0, AUTH_AGENT)
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_invoice_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await get_invoice(999, AUTH_ADMIN)
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_invoice_ownership_check(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "invoice_id": 1, "agent_id": "a2",  # 属于 a2
            "invoice_type": "electronic", "title": "test",
            "tax_number": "", "amount_fen": 100,
            "related_txn_ids": [], "status": "pending",
            "file_url": "", "file_hash": "", "issuer": "",
            "issued_at": None, "reject_reason": "", "remark": "",
            "created_at": None, "updated_at": None,
        }
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await get_invoice(1, AUTH_AGENT)  # agent "a1" tries to read "a2"'s invoice
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_request_invoice(self, mock_conn, mock_pool):
        from siku.models import InvoiceRequest
        mock_conn.fetchrow.return_value = {
            "invoice_id": 1, "agent_id": "a1", "status": "pending",
            "title": "test corp", "amount_fen": 100, "created_at": None,
        }
        with patch("siku.api.get_pool", return_value=mock_pool):
            result = await request_invoice(
                InvoiceRequest(agent_id="a1", title="test corp", amount_fen=100),
                AUTH_AGENT,
            )
            assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_issue_invoice_admin_only(self):
        from siku.models import InvoiceIssueRequest
        with pytest.raises(HTTPException) as exc:
            await issue_invoice(
                InvoiceIssueRequest(invoice_id=1, file_url="/tmp/1.pdf", file_hash="sha"),
                AUTH_AGENT,
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_void_invoice_admin_only(self):
        from siku.models import InvoiceVoidRequest
        with pytest.raises(HTTPException) as exc:
            await void_invoice(
                InvoiceVoidRequest(invoice_id=1, reason="error"),
                AUTH_AGENT,
            )
        assert exc.value.status_code == 403
