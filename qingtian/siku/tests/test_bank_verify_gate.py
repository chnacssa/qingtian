"""司库 — P0 银联桩围栏回归测试（9-1 修复日）。

背景（review 2026-08-28-司库.md P0）：_verify_bank_transfer 桩恒 matched=True +
Path B 不验来账方 → 消息总线上任何 agent 发 payment_notify 即可自铸余额。
修复：bank_verify 模式开关（stub|manual|off，默认 manual）+ pending_recharges
待人审队列 + IM approve/reject 接线。
"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("QINGTIAN_ENV", "development")
os.environ.pop("SIKU_BANK_VERIFY", None)

from siku import config as cfg  # noqa: E402
from siku import finance_agent as fa  # noqa: E402
from siku.api import (  # noqa: E402
    _im_approve_pending_recharge, _im_reject_pending_recharge,
)
from siku.models import PaymentNotifyPayload  # noqa: E402


# ═══════════════════════════════════════════════════════
# 1. config — bank_verify 模式开关
# ═══════════════════════════════════════════════════════


def test_bank_verify_default_manual(monkeypatch):
    """未配置 → 默认 manual（fail-safe：宁可全人审不可自动入账）。"""
    monkeypatch.delenv("SIKU_BANK_VERIFY", raising=False)
    with patch("siku.config.get", return_value="manual"):
        assert cfg.get_bank_verify_mode() == "manual"


def test_bank_verify_env_override(monkeypatch):
    monkeypatch.setenv("SIKU_BANK_VERIFY", "stub")
    with patch("siku.config.get", return_value="manual"):
        assert cfg.get_bank_verify_mode() == "stub"


def test_bank_verify_illegal_falls_back_manual(monkeypatch):
    monkeypatch.setenv("SIKU_BANK_VERIFY", "auto_yes_please")
    assert cfg.get_bank_verify_mode() == "manual"


# ═══════════════════════════════════════════════════════
# 2. _verify_bank_transfer — 三模式分派
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bank_transfer_manual_mode_never_matches(monkeypatch):
    """manual（默认）：恒不自动通过 + 标记 pending_manual。"""
    monkeypatch.setenv("SIKU_BANK_VERIFY", "manual")
    r = await fa._verify_bank_transfer("公司A", 100, "bank", "V1")
    assert r["matched"] is False
    assert r["pending_manual"] is True


@pytest.mark.asyncio
async def test_bank_transfer_off_mode_disables(monkeypatch):
    monkeypatch.setenv("SIKU_BANK_VERIFY", "off")
    r = await fa._verify_bank_transfer("公司A", 100, "bank", "V1")
    assert r["matched"] is False
    assert "pending_manual" not in r


@pytest.mark.asyncio
async def test_bank_transfer_stub_only_when_explicit(monkeypatch):
    monkeypatch.setenv("SIKU_BANK_VERIFY", "stub")
    r = await fa._verify_bank_transfer("公司A", 100, "bank", "V1")
    assert r["matched"] is True
    assert r["verified_by"] == "unionpay_stub"


# ═══════════════════════════════════════════════════════
# 3. _process_incoming — manual 模式入待审队列
# ═══════════════════════════════════════════════════════


def _notify():
    return PaymentNotifyPayload(
        company_name="公司A", amount_fen=10000,
        payment_channel="bank", voucher_number="V123",
    )


def _conn():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="payer-1")
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _tx():
        yield

    conn.transaction = _tx
    return conn


@pytest.mark.asyncio
async def test_incoming_manual_queues_pending_not_recharge(monkeypatch):
    """manual：入 pending_recharges + mark_read 原消息，绝不调 acct.recharge。"""
    conn = _conn()
    monkeypatch.setenv("SIKU_BANK_VERIFY", "manual")
    with patch("siku.finance_agent.hmessaging.mark_read", new=AsyncMock()) as mark_read, \
         patch("siku.finance_agent.hmessaging.send_message", new=AsyncMock()), \
         patch("siku.finance_agent._maybe_notify", new=AsyncMock()), \
         patch("siku.finance_agent.acct.recharge", new=AsyncMock()) as recharge:
        result = await fa._process_incoming(conn, _notify(), "msg-1", "from-x")

    assert result["status"] == "pending_manual"
    insert_sql = conn.execute.await_args_list[0].args[0]
    assert "pending_recharges" in insert_sql
    assert "ON CONFLICT (message_id) DO NOTHING" in insert_sql
    mark_read.assert_awaited_once_with("msg-1")
    recharge.assert_not_awaited()  # 核心断言：未自动入账


@pytest.mark.asyncio
async def test_incoming_stub_mode_recharges(monkeypatch):
    """stub（显式）：保持自动入账旧行为（开发/测试拓扑）。"""
    conn = _conn()
    monkeypatch.setenv("SIKU_BANK_VERIFY", "stub")
    recharge_ret = {"txn_id": 9, "already_processed": False}
    with patch("siku.finance_agent.hmessaging.mark_read", new=AsyncMock()), \
         patch("siku.finance_agent.hmessaging.send_message", new=AsyncMock()), \
         patch("siku.finance_agent._maybe_notify", new=AsyncMock()), \
         patch("siku.finance_agent.acct.recharge",
               new=AsyncMock(return_value=recharge_ret)) as recharge:
        result = await fa._process_incoming(conn, _notify(), "msg-1", "from-x")

    assert result["status"] == "ok"
    assert result["txn_id"] == 9
    recharge.assert_awaited_once()
    idem = recharge.await_args.kwargs.get("idempotency_key")
    assert idem == "finance_agent:recharge:msg-1"


# ═══════════════════════════════════════════════════════
# 4. IM 人审接线 — approve / reject
# ═══════════════════════════════════════════════════════


def _pending_row(status="pending"):
    # dict 而非 SimpleNamespace：api 侧 row["status"] 下标访问（asyncpg Record 风格）
    return {
        "message_id": "msg-1", "company_name": "公司A", "payer_agent_id": "payer-1",
        "amount_fen": 10000, "payment_channel": "bank", "voucher_number": "V123",
        "status": status, "decided_by": "u_prev" if status != "pending" else "",
    }


@pytest.mark.asyncio
async def test_im_approve_recharges_with_path_b_idem_key(mock_conn, mock_pool):
    """approve：入账幂等键与 Path B 一致 + 待审单转 approved。"""
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _pending_row(),                                   # 查待审单
        SimpleNamespace(row_hash="x", id=1),              # write_finance_audit fetchrow
    ])
    with patch("siku.api.get_pool", return_value=mock_pool), \
         patch("siku.api.acct.recharge",
               new=AsyncMock(return_value={"txn_id": 9, "already_processed": False})) as recharge, \
         patch("huanyu.messaging.send_message", new=AsyncMock()), \
         patch("siku.finance_agent._maybe_notify", new=AsyncMock()):
        r = await _im_approve_pending_recharge("user-im", "msg-1")

    assert r["status"] == "ok"
    assert r["txn_id"] == 9
    idem = recharge.await_args.kwargs.get("idempotency_key")
    assert idem == "finance_agent:recharge:msg-1"
    status_sql = [c.args[0] for c in mock_conn.execute.await_args_list
                  if "pending_recharges" in c.args[0]][0]
    assert "status='approved'" in status_sql


@pytest.mark.asyncio
async def test_im_approve_rejected_ticket_refused(mock_conn, mock_pool):
    """已拒绝的单不可再通过。"""
    mock_conn.fetchrow = AsyncMock(return_value=_pending_row(status="rejected"))
    with patch("siku.api.get_pool", return_value=mock_pool), \
         patch("siku.api.acct.recharge", new=AsyncMock()) as recharge:
        r = await _im_approve_pending_recharge("user-im", "msg-1")
    assert r["status"] == "already_rejected"
    recharge.assert_not_awaited()


@pytest.mark.asyncio
async def test_im_reject_marks_rejected_no_recharge(mock_conn, mock_pool):
    """reject：仅状态翻转 + 审计，绝不入账。"""
    mock_conn.fetchrow = AsyncMock(side_effect=[
        _pending_row(),
        SimpleNamespace(row_hash="x", id=1),
    ])
    with patch("siku.api.get_pool", return_value=mock_pool), \
         patch("siku.api.acct.recharge", new=AsyncMock()) as recharge, \
         patch("siku.finance_agent._maybe_notify", new=AsyncMock()):
        r = await _im_reject_pending_recharge("user-im", "msg-1")
    assert r["status"] == "ok"
    recharge.assert_not_awaited()
    status_sql = [c.args[0] for c in mock_conn.execute.await_args_list
                  if "pending_recharges" in c.args[0]][0]
    assert "status='rejected'" in status_sql


@pytest.mark.asyncio
async def test_im_approve_unknown_ticket(mock_conn, mock_pool):
    mock_conn.fetchrow = AsyncMock(return_value=None)
    with patch("siku.api.get_pool", return_value=mock_pool):
        r = await _im_approve_pending_recharge("user-im", "nope")
    assert r["status"] == "not_found"
