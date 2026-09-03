"""
audit_service.py 单元测试
哈希链写入 / 验证 / 签名校验
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from zhenyue.audit_service import (
    write_audit,
    write_audit_from_middleware,
    cleanup_old_audit_logs,
    AuditVerificationError,
    verify_single_record,
    get_prev_hash,
    get_active_sign_key_id,
)


class TestAuditVerificationError:
    def test_creation(self):
        e = AuditVerificationError("uid-001", "hash_broken", "detail text")
        assert e.audit_uid == "uid-001"
        assert e.error_type == "hash_broken"
        assert "uid-001" in str(e)
        assert "hash_broken" in str(e)


class TestGetActiveSignKeyId:
    @pytest.mark.asyncio
    async def test_returns_key_id(self, mock_conn):
        mock_conn.fetchval.return_value = 42
        key_id = await get_active_sign_key_id(mock_conn)
        assert key_id == 42

    @pytest.mark.asyncio
    async def test_raises_when_no_key(self, mock_conn):
        mock_conn.fetchval.return_value = None
        with pytest.raises(RuntimeError, match="No active sign key"):
            await get_active_sign_key_id(mock_conn)


class TestGetPrevHash:
    @pytest.mark.asyncio
    async def test_returns_genesis_when_empty(self, mock_conn):
        mock_conn.fetchval.return_value = None
        h = await get_prev_hash(mock_conn)
        assert h is not None
        assert len(h) > 0

    @pytest.mark.asyncio
    async def test_returns_last_hash(self, mock_conn):
        mock_conn.fetchval.return_value = "abc123hash"
        h = await get_prev_hash(mock_conn)
        assert h == "abc123hash"


class TestWriteAudit:
    @pytest.mark.asyncio
    async def test_writes_and_returns_audit(self, mock_conn):
        # fetchval 顺序：active_key_id / private_key_enc(None→占位) / prev_hash(genesis) / audit_uid
        mock_conn.fetchval.side_effect = [42, None, None, "uid-test-001"]

        with patch("zhenyue.audit_service.encryptor") as mock_enc:
            mock_enc.encrypt.return_value = "encrypted_detail"
            result = await write_audit(mock_conn, {
                "agent_id": "agent-1",
                "action": "test_action",
                "target_type": "agent",
                "target_id": "target-1",
                "severity": "low",
            })
            assert result["audit_uid"] == "uid-test-001"
            assert result["agent_id"] == "agent-1"
            assert result["action"] == "test_action"
            assert result["severity"] == "low"
            assert "hash" in result

    @pytest.mark.asyncio
    async def test_default_values(self, mock_conn):
        mock_conn.fetchval.side_effect = [42, None, None, "uid-defaults"]

        with patch("zhenyue.audit_service.encryptor") as mock_enc:
            mock_enc.encrypt.return_value = "encrypted_detail"
            result = await write_audit(mock_conn, {
                "agent_id": "agent-1",
                "action": "minimal",
            })
            assert result["audit_uid"] == "uid-defaults"
            assert result["severity"] == "low"

    @pytest.mark.asyncio
    async def test_real_signature_when_key_available(self, mock_conn):
        """P1 (R11): 私钥可用 → 写入真实 Ed25519 签名（替代恒 '0'*128 占位符）"""
        mock_conn.fetchval.side_effect = [42, "encrypted_priv", None, "uid-test-002"]

        with patch("zhenyue.audit_service.encryptor") as mock_enc:
            mock_enc.encrypt.return_value = "encrypted_detail"
            mock_enc.decrypt.return_value = {"private_key": "ab" * 32}  # 有效 32-byte 私钥
            result = await write_audit(mock_conn, {
                "agent_id": "agent-1",
                "action": "signed_action",
            })

            assert result["audit_uid"] == "uid-test-002"
            # 提取 INSERT 第 12 个参数（signature），应为真实签名而非占位符
            args = mock_conn.fetchval.call_args.args
            signature = args[12]
            assert signature != "0" * 128
            assert len(signature) == 128

    @pytest.mark.asyncio
    async def test_serializes_writes_with_advisory_lock(self, mock_conn):
        """P1 (R11): get_prev_hash 无锁 → 并发写链分叉；事务内持 advisory xact lock"""
        mock_conn.fetchval.side_effect = [42, None, None, "uid-test-003"]

        with patch("zhenyue.audit_service.encryptor") as mock_enc:
            mock_enc.encrypt.return_value = "encrypted_detail"
            await write_audit(mock_conn, {"agent_id": "agent-1", "action": "lock_test"})

        # 事务必须被使用
        assert mock_conn.transaction.called
        # advisory lock 必须发出
        lock_sql = mock_conn.execute.call_args.args[0]
        assert "pg_advisory_xact_lock" in lock_sql


class TestVerifySingleRecord:
    def test_valid_hash_passes(self):
        import hashlib
        ts_iso = "2026-01-01T00:00:00.000000Z"
        prev = "genesis"
        agent_id = "agent-1"
        action = "test"
        detail = ""
        raw = f"{prev}:{agent_id}:{action}:{ts_iso}:{detail}"
        expected_hash = hashlib.sha256(raw.encode()).hexdigest()

        from datetime import datetime, timezone
        row = {
            "audit_uid": "uid-1",
            "agent_id": agent_id,
            "action": action,
            "detail_enc": detail,
            "hash": expected_hash,
            "signature": "0" * 128,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        result = verify_single_record(row, "00" * 32, prev)
        assert result == expected_hash

    def test_hash_mismatch_raises(self):
        from datetime import datetime, timezone
        row = {
            "audit_uid": "uid-1",
            "agent_id": "agent-1",
            "action": "test",
            "detail_enc": "",
            "hash": "wrong_hash_value_here_1234567890",
            "signature": "0" * 128,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        with pytest.raises(AuditVerificationError) as exc:
            verify_single_record(row, "00" * 32, "genesis")
        assert exc.value.error_type == "hash_broken"


class TestWriteAuditFromMiddleware:
    @pytest.mark.asyncio
    async def test_sets_source_layer(self, mock_conn):
        mock_conn.fetchval.side_effect = [42, None, None, "uid-mw-001"]

        with patch("zhenyue.audit_service.encryptor") as mock_enc:
            mock_enc.encrypt.return_value = "encrypted_detail"
            result = await write_audit_from_middleware(mock_conn, {
                "agent_id": "agent-1",
                "action": "mw_action",
                "detail": {"key": "value"},
            })
            assert result is not None
            assert result["agent_id"] == "agent-1"

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self, mock_conn):
        mock_conn.fetchval.side_effect = Exception("DB down")

        with patch("zhenyue.audit_service.encryptor"):
            result = await write_audit_from_middleware(mock_conn, {
                "agent_id": "agent-1",
                "action": "mw_action",
            })
            assert result is None


class TestCleanupOldAuditLogs:
    """P1 (R11): 整链截断后链校验不再永久 hash_broken —— 新首条 prev_hash 回 genesis 并重算"""

    @pytest.mark.asyncio
    async def test_reanchors_chain_after_truncation(self, mock_conn, mock_pool):
        # fetchval 调用序：① cutoff 边界=100 ② get_active_sign_key_id=None。
        # 无 active 密钥 → RuntimeError → 占位签名分支（4 参数 UPDATE）
        mock_conn.fetchval.side_effect = [100, None]
        # SET LOCAL / DELETE（返回删除条数） / 每条 UPDATE
        mock_conn.execute.side_effect = ["SET", "DELETE 50", "UPDATE", "UPDATE"]
        # 删除后剩余 2 条
        ts1 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 8, 2, 10, 0, 0, tzinfo=timezone.utc)
        mock_conn.fetch.return_value = [
            {"id": 101, "agent_id": "a1", "action": "act1", "created_at": ts1, "detail_enc": ""},
            {"id": 102, "agent_id": "a2", "action": "act2", "created_at": ts2, "detail_enc": ""},
        ]

        with patch("zhenyue.audit_service.get_pool", return_value=mock_pool):
            count = await cleanup_old_audit_logs(retention_days=30)

        assert count == 50

        # 第 1 条 UPDATE：prev_hash 必须回 genesis（而非已删记录的 hash）
        first_update = mock_conn.execute.call_args_list[2]
        sql, row_id, prev_hash, new_hash, sig = first_update.args
        assert row_id == 101
        from zhenyue import config as zcfg
        assert prev_hash == zcfg.get_audit_prev_hash_genesis()
        # 期望 hash = sha256(genesis:agent:action:ts_iso:detail)
        ts_iso1 = ts1.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        raw1 = f"{zcfg.get_audit_prev_hash_genesis()}:a1:act1:{ts_iso1}:"
        assert new_hash == hashlib.sha256(raw1.encode()).hexdigest()
        # #14: 无 active 密钥时签名退占位（verify 跳过占位，不再恒 signature_invalid）
        assert sig == "0" * 128

        # 第 2 条 UPDATE：prev_hash 必须是第 1 条的新 hash（链连续）
        second_update = mock_conn.execute.call_args_list[3]
        assert second_update.args[2] == new_hash

    @pytest.mark.asyncio
    async def test_reanchors_resigns_with_active_key(self, mock_conn, mock_pool):
        """P1 (#14): 有 active 密钥时重锚定必须同步重签 + 更新 sign_key_id，
        否则旧签名对新 hash 恒 signature_invalid，清理后链校验永久告警。"""
        from nacl.signing import SigningKey as _SK

        sk = _SK.generate()
        priv_hex = sk.encode().hex()

        # fetchval 调用序：① cutoff=100 ② get_active_sign_key_id=1
        mock_conn.fetchval.side_effect = [100, 1]
        mock_conn.execute.side_effect = ["SET", "DELETE 50", "UPDATE", "UPDATE"]
        ts1 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
        mock_conn.fetch.return_value = [
            {"id": 101, "agent_id": "a1", "action": "act1", "created_at": ts1, "detail_enc": ""},
        ]

        with patch("zhenyue.audit_service.get_pool", return_value=mock_pool), \
             patch("zhenyue.audit_service.get_sign_private_key", new=AsyncMock(return_value=priv_hex)):
            count = await cleanup_old_audit_logs(retention_days=30)

        assert count == 50
        # 5 参数 UPDATE：含新签名 + 当前 active sign_key_id
        sql, row_id, prev_hash, new_hash, sig, key_id = mock_conn.execute.call_args_list[2].args
        assert row_id == 101 and key_id == 1
        # 新签名必须能被对应公钥验证通过（hash 被重签覆盖）
        from nacl.signing import VerifyKey as _VK
        _VK(bytes.fromhex(sk.verify_key.encode().hex())).verify(new_hash.encode(), bytes.fromhex(sig))


# ── P2 (R11): report_event 不调 ensure_table → 建表前上报失败 ─

class TestReportEventEnsuresTable:
    """P2 (R11): report_event 上报前必须 ensure_table（幂等建表）"""

    @pytest.mark.asyncio
    async def test_report_event_calls_ensure_table(self, mock_conn, mock_pool):
        from datetime import datetime, timezone
        from zhenyue.audit_runtime import report_event

        mock_conn.fetchrow.return_value = {
            "id": 7, "created_at": datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc),
        }
        with patch("zhenyue.audit_runtime.ensure_table", new=AsyncMock()) as ensure, \
             patch("zhenyue.audit_runtime.get_pool", return_value=mock_pool):
            result = await report_event(
                agent_id="a1", skill_name="ls", event_type="egress_anomaly",
                severity="high", detail={"pid": 123},
            )

        ensure.assert_awaited_once()
        assert result["id"] == 7
        # detail 必须 JSON 序列化后落库（args[0] 为 SQL，参数从下标 1 起）
        inserted = mock_conn.fetchrow.await_args.args
        assert inserted[5] == '{"pid": 123}'
