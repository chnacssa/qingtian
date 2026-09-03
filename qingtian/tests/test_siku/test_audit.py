"""司库 — 审计模块单元测试 (无 DB 依赖)

覆盖: _now_iso / _compute_audit_hash / write_finance_audit mock /
      verify_finance_audit_chain mock / hash chain integrity
"""

import hashlib
import json
import pytest
from datetime import datetime, timezone

from siku.audit import (
    _now_iso,
    _compute_audit_hash,
    write_finance_audit,
    verify_finance_audit_chain,
    GENESIS,
)


class TestNowIso:
    def test_returns_iso_with_z_suffix(self):
        result = _now_iso()
        assert "T" in result
        assert result.endswith("Z")
        assert "+" not in result  # strftime Z 格式不含 +00:00

    def test_two_calls_monotonic(self):
        t1 = _now_iso()
        t2 = _now_iso()
        assert t1 <= t2


class TestComputeAuditHash:
    def test_deterministic(self):
        h1 = _compute_audit_hash(
            GENESIS, "agent-1", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z",
            '{"amount": 1000}',
        )
        h2 = _compute_audit_hash(
            GENESIS, "agent-1", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z",
            '{"amount": 1000}',
        )
        assert h1 == h2

    def test_different_input_different_hash(self):
        h1 = _compute_audit_hash(
            GENESIS, "agent-1", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z", "{}",
        )
        h2 = _compute_audit_hash(
            GENESIS, "agent-2", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z", "{}",
        )
        assert h1 != h2

    def test_prev_hash_changes_output(self):
        h1 = _compute_audit_hash(
            GENESIS, "agent-1", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z", "{}",
        )
        h2 = _compute_audit_hash(
            h1, "agent-1", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z", "{}",
        )
        assert h1 != h2

    def test_amount_changes_hash(self):
        h1 = _compute_audit_hash(
            GENESIS, "agent-1", "deduct", "fee",
            "2026-06-04T10:00:00.000000Z", '{"amount": 100}',
        )
        h2 = _compute_audit_hash(
            GENESIS, "agent-1", "deduct", "fee",
            "2026-06-04T10:00:00.000000Z", '{"amount": 200}',
        )
        assert h1 != h2

    def test_action_changes_hash(self):
        h1 = _compute_audit_hash(
            GENESIS, "agent-1", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z", "{}",
        )
        h2 = _compute_audit_hash(
            GENESIS, "agent-1", "deduct", "payment",
            "2026-06-04T10:00:00.000000Z", "{}",
        )
        assert h1 != h2

    def test_hash_is_64_hex_chars(self):
        h = _compute_audit_hash(
            GENESIS, "agent-1", "recharge", "payment",
            "2026-06-04T10:00:00.000000Z", "{}",
        )
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_genesis_is_64_zeros(self):
        assert GENESIS == "0" * 64


class TestWriteFinanceAuditNonFatal:
    """write_finance_audit 失败不应抛异常（容错设计）"""

    @pytest.mark.asyncio
    async def test_write_returns_error_dict_on_failure(self):
        """Mock conn 抛异常 → 返回 error dict 而非 raise"""
        class FailingConn:
            async def fetchval(self, *args, **kw):
                raise RuntimeError("DB unavailable")
            async def fetchrow(self, *args, **kw):
                raise RuntimeError("DB unavailable")

        result = await write_finance_audit(FailingConn(), {
            "agent_id": "test-agent",
            "action": "recharge",
            "event_type": "payment",
            "amount_fen": 10000,
            "severity": "high",
        })
        assert "error" in result
        assert result["status"] == "logged_only"


class TestVerifyFinanceAuditChain:
    @pytest.mark.asyncio
    async def test_empty_chain_valid(self):
        class MockConn:
            async def fetch(self, *args, **kw):
                return []

        result = await verify_finance_audit_chain(MockConn())
        assert result["valid"] is True
        assert result["total_records"] == 0

    @pytest.mark.asyncio
    async def test_single_record_chain_valid(self):
        """单条记录的哈希链 — prev_hash = GENESIS"""
        agent_id = "test-agent"
        detail_json = "{}"
        created_at = datetime.now(timezone.utc)

        h1 = _compute_audit_hash(
            GENESIS, agent_id, "recharge", "payment",
            created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
            detail_json,
        )

        class MockConn:
            async def fetch(self, *args, **kw):
                return [{
                    "id": 1, "agent_id": agent_id,
                    "action": "recharge", "event_type": "payment",
                    "target_id": "", "amount_fen": 1000,
                    "severity": "high", "detail": detail_json,
                    "prev_hash": GENESIS, "row_hash": h1,
                    "created_at": created_at,
                }]

        result = await verify_finance_audit_chain(MockConn())
        assert result["valid"] is True
        assert result["total_records"] == 1

    @pytest.mark.asyncio
    async def test_broken_prev_hash_detected(self):
        """prev_hash 不连续 → 检测到断裂"""
        created_at = datetime.now(timezone.utc)
        detail_json = "{}"

        class MockConn:
            async def fetch(self, *args, **kw):
                return [
                    {"id": 1, "agent_id": "a", "action": "recharge",
                     "event_type": "p", "target_id": "", "amount_fen": 100,
                     "severity": "info", "detail": detail_json,
                     "prev_hash": GENESIS,
                     "row_hash": _compute_audit_hash(GENESIS, "a", "recharge", "p",
                                                       created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                                                       detail_json),
                     "created_at": created_at},
                    {"id": 2, "agent_id": "a", "action": "deduct",
                     "event_type": "p", "target_id": "", "amount_fen": 50,
                     "severity": "info", "detail": detail_json,
                     "prev_hash": "0" * 64,  # ❌ 断链 — 应该是第1条的 row_hash
                     "row_hash": "0" * 64,
                     "created_at": created_at},
                ]

        result = await verify_finance_audit_chain(MockConn())
        assert result["valid"] is False
        assert "broken_at_id" in result


class TestGenesisConstant:
    def test_genesis_in_account_service(self):
        from siku.account_service import GENESIS as ACCT_GENESIS
        assert ACCT_GENESIS == "0" * 64
