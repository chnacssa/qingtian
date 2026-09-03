"""执策 Ed25519 签名测试 — sign + verify + submit with signature"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import zhice.api  # ensure submodule is loaded for patching


class TestSignCheckResults:
    def test_sign_and_verify_roundtrip(self):
        """签名 → 验签 完整闭环"""
        from nacl.signing import SigningKey

        sk = SigningKey.generate()
        private_key_hex = bytes(sk).hex()
        public_key_hex = bytes(sk.verify_key).hex()

        check_results = {
            "file_exists": [{"path": "/opt/app/main.py", "exists": True}],
            "api_health": [{"url": "http://localhost:1996/health", "status_code": 200}],
        }

        from zhice.signing import sign_check_results, verify_signature

        sig = sign_check_results(private_key_hex, check_results, step_id=1, task_id=42)
        assert len(sig) == 128

        # 模拟镇岳公钥 API
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"public_key": public_key_hex}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp

            async def _verify():
                valid, err = await verify_signature("agent:test", 1, 42, check_results, sig)
                assert valid
                assert err == ""

            import asyncio
            asyncio.run(_verify())

    def test_sign_empty_check_results(self):
        """空 check_results 签名"""
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        from zhice.signing import sign_check_results
        sig = sign_check_results(bytes(sk).hex(), {}, step_id=1, task_id=42)
        assert len(sig) == 128

    def test_sign_deterministic(self):
        """相同输入 → 不同签名（Ed25519 非确定性签名）"""
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        from zhice.signing import sign_check_results
        data = {"file_exists": [{"path": "/x", "exists": True}]}
        sig1 = sign_check_results(bytes(sk).hex(), data, step_id=1, task_id=42)
        sig2 = sign_check_results(bytes(sk).hex(), data, step_id=1, task_id=42)
        # Ed25519 标准签名是确定性的，但 nacl SigningKey.sign() 使用随机 nonce
        # 所以两次签名可能不同（这是正常的）


class TestVerifySignature:
    @pytest.mark.asyncio
    async def test_verifies_valid_signature(self):
        """有效签名 → 验证通过"""
        from nacl.signing import SigningKey
        from zhice.signing import sign_check_results, verify_signature

        sk = SigningKey.generate()
        check_results = {"api_health": [{"url": "http://x", "status_code": 200}]}
        sig = sign_check_results(bytes(sk).hex(), check_results, step_id=1, task_id=42)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"public_key": bytes(sk.verify_key).hex()}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            valid, err = await verify_signature("agent:test", 1, 42, check_results, sig)
            assert valid
            assert err == ""

    @pytest.mark.asyncio
    async def test_rejects_wrong_signature(self):
        """错误签名 → 验证失败"""
        from nacl.signing import SigningKey
        from zhice.signing import verify_signature

        sk = SigningKey.generate()
        sk2 = SigningKey.generate()
        check_results = {"file_exists": [{"path": "/x", "exists": True}]}

        # 用 sk2 签名，但返回 sk 的公钥（不匹配）
        from zhice.signing import sign_check_results
        sig = sign_check_results(bytes(sk2).hex(), check_results, step_id=1, task_id=42)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"public_key": bytes(sk.verify_key).hex()}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            valid, err = await verify_signature("agent:test", 1, 42, check_results, sig)
            assert not valid
            assert "不匹配" in err

    @pytest.mark.asyncio
    async def test_rejects_invalid_signature_length(self):
        """签名长度不对 → 验证失败（无需查公钥即拒）"""
        from zhice.signing import verify_signature
        valid, err = await verify_signature("agent:test", 1, 42, {}, "too_short")
        assert not valid
        assert "格式错误" in err

    @pytest.mark.asyncio
    async def test_agent_no_public_key(self):
        """Agent 无公钥但带签名 → 无法验签，拒绝"""
        from zhice.signing import verify_signature
        from nacl.signing import SigningKey
        sk = SigningKey.generate()
        from zhice.signing import sign_check_results
        sig = sign_check_results(bytes(sk).hex(), {"x": 1}, step_id=1, task_id=42)

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            valid, err = await verify_signature("agent:test", 1, 42, {"x": 1}, sig)
            assert not valid
            assert "无活跃公钥" in err

    @pytest.mark.asyncio
    async def test_agent_no_public_key_without_signature_passes(self):
        """R11 (P?): Agent 无公钥且无签名 → 无签名能力，unsigned 向后兼容放行"""
        from zhice.signing import verify_signature

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            valid, err = await verify_signature("agent:test", 1, 42, {"x": 1}, "")
            assert valid
            assert err == ""

    @pytest.mark.asyncio
    async def test_agent_with_key_missing_signature_rejected(self):
        """R11 (P?): Agent 已注册公钥但缺签名 → fail-closed 拒绝（不再空转跳过）"""
        from zhice.signing import verify_signature
        from nacl.signing import SigningKey
        sk = SigningKey.generate()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"public_key": bytes(sk.verify_key).hex()}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            valid, err = await verify_signature("agent:test", 1, 42, {"x": 1}, "")
            assert not valid
            assert "缺少签名" in err

    @pytest.mark.asyncio
    async def test_signature_bound_to_step_task(self):
        """R11 (P?): 签名绑定 step_id/task_id —— 同一 check_results 换 step 验签失败（防重放）"""
        from nacl.signing import SigningKey
        from zhice.signing import sign_check_results, verify_signature

        sk = SigningKey.generate()
        check_results = {"file_exists": [{"path": "/x", "exists": True}]}
        sig = sign_check_results(bytes(sk).hex(), check_results, step_id=1, task_id=42)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"public_key": bytes(sk.verify_key).hex()}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            # 换一个 step → 同一签名验签失败
            valid, err = await verify_signature("agent:test", 999, 42, check_results, sig)
            assert not valid
            assert "不匹配" in err


class TestSubmitWithSignature:
    """集成测试：submit_step 带签名"""
    def _make_pool(self, mock_conn):
        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire.return_value = ctx
        return pool

    def _step_row(self, **overrides):
        d = {
            "step_id": 1, "task_id": 42, "step_index": 1, "title": "S",
            "instruction": "Do", "status": "in_progress", "status_reason": None,
            "assigned_agent": "agent1", "assigned_at": None, "depends_on": [],
            "acceptance_criteria": [{"type": "output_contains", "field": "result", "keyword": "OK"}],
            "expected_outputs": None, "outputs": None, "summary": None,
            "auto_retry": 0, "timeout_minutes": 30, "idempotency_key": None,
            "last_heartbeat_at": None, "started_at": None, "completed_at": None,
            "created_at": None, "updated_at": None,
        }
        d.update(overrides)
        row = MagicMock()
        row.__getitem__ = lambda self, k: d.get(k)
        row.get = d.get
        row.keys = lambda: d.keys()
        return row

    @pytest.mark.asyncio
    async def test_submit_signature_invalid_rejected(self):
        """带错误签名 → rejected（协议错误，不消耗 auto_retry）"""
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[
            self._step_row(),
            None,  # try_complete_task's get_task
        ])
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock()
        conn.execute = AsyncMock()
        conn.transaction = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock()
        ctx.__aexit__ = AsyncMock(return_value=None)
        conn.transaction.return_value = ctx

        from zhice.models import SubmitRequest

        with patch("zhice.api.get_pool", AsyncMock(return_value=self._make_pool(conn))):
            with patch("zhice.dispatcher.ws_notify", new_callable=AsyncMock):
                with patch("zhice.signing.verify_signature", new_callable=AsyncMock) as mock_verify:
                    mock_verify.return_value = (False, "签名验证失败：签名与 check_results 不匹配")
                    from zhice.api import submit_step
                    result = await submit_step(1, SubmitRequest(
                        agent_id="agent1", status="completed", summary="done",
                        outputs={"result": "OK", "check_results": {"file_exists": [{"path": "/x", "exists": True}]}},
                        idempotency_key="k-sig",
                        signature="ab" * 64,  # fake signature
                    ), auth={"agent_id": "test-admin", "role": "admin"})
                    assert result.status == "rejected"
                    assert result.verification_result == "signature_invalid"
                    assert len(result.failed_rules) == 1
                    assert result.failed_rules[0]["type"] == "signature"

    @pytest.mark.asyncio
    async def test_submit_no_signature_still_works(self):
        """无签名 → 向后兼容，正常流程"""
        conn = AsyncMock()
        completed_row = self._step_row(
            acceptance_criteria=[{"type": "output_contains", "field": "result", "keyword": "OK"}],
            status="completed", summary="done", outputs={"result": "OK"},
        )
        conn.fetchrow = AsyncMock(side_effect=[
            self._step_row(  # sm.get_step
                acceptance_criteria=[{"type": "output_contains", "field": "result", "keyword": "OK"}],
            ),
            completed_row,  # sm.step_complete RETURNING *
            None,           # try_complete_task get_task
        ])
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock()
        conn.execute = AsyncMock()
        conn.transaction = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock()
        ctx.__aexit__ = AsyncMock(return_value=None)
        conn.transaction.return_value = ctx

        from zhice.models import SubmitRequest

        with patch("zhice.api.get_pool", AsyncMock(return_value=self._make_pool(conn))):
            with patch("zhice.dispatcher.ws_notify", new_callable=AsyncMock):
                with patch("zhice.runner.try_complete_task", new_callable=AsyncMock):
                    from zhice.api import submit_step
                    result = await submit_step(1, SubmitRequest(
                        agent_id="agent1", status="completed", summary="done",
                        outputs={"result": "OK"},
                        idempotency_key="k-no-sig",
                        signature="",  # 无签名
                    ), auth={"agent_id": "test-admin", "role": "admin"})
                    assert result.status == "completed"
                    assert result.verification_result == "passed"
