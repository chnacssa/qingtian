"""
siku R11 P1 安全修复专项测试
  - verify-challenge Ed25519 预言机（领域前缀绑定）
  - annual_pay 幂等重放（already_processed 短路，防免费延期）
  - IM 回调验签（wechat/wecom/feishu fail-closed）
"""

import hashlib
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from siku.api import (
    finance_verify_challenge,
    annual_pay,
    im_feishu_callback,
    im_wechat_callback,
    im_wecom_callback,
)
from siku.models import AnnualPayRequest
from huanyu.ed25519_utils import generate_keypair, verify_signature

AUTH_ADMIN = {"agent_id": "admin", "role": "admin"}
AUTH_AGENT = {"agent_id": "a1", "role": "agent"}


async def _make_request(body: bytes, headers: dict | None = None,
                        query: dict | None = None) -> Request:
    """构造带 body/headers/query 的 Starlette Request（模拟平台回调）"""
    hdrs = [(k.encode(), str(v).encode()) for k, v in (headers or {}).items()]
    query_str = "&".join(f"{k}={v}" for k, v in (query or {}).items())
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/siku/im/callback",
        "headers": hdrs,
        "query_string": query_str.encode(),
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 9999),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=receive)


# ── P1 (R18): verify-challenge 领域前缀绑定 ─────────────────

class TestVerifyChallenge:
    @pytest.mark.asyncio
    async def test_signature_bound_to_domain_prefix(self, mock_conn, mock_pool):
        priv, pub = generate_keypair()
        nonce = "a" * 32

        # api.py 在 import 时 from .finance_agent import _agent_private_key
        # 直接 patch siku.api 侧的名字即可注入测试密钥
        with patch("siku.api._agent_private_key", priv), \
             patch("siku.api._agent_ain", "local_ain"), \
             patch("siku.api.get_pool", return_value=mock_pool):
            result = await finance_verify_challenge(
                {"ain": "remote_ain", "nonce": nonce}, AUTH_ADMIN,
            )

        assert result["ain"] == "local_ain"
        assert result["nonce"] == nonce
        # 签名必须绑定 finance-challenge:{ain}:{nonce}
        assert verify_signature(pub, f"finance-challenge:remote_ain:{nonce}", result["signature"]) is True
        # 裸 nonce 无法通过验证 —— 防止预言机
        assert verify_signature(pub, nonce, result["signature"]) is False

    @pytest.mark.asyncio
    async def test_short_nonce_rejected(self, mock_conn, mock_pool):
        with patch("siku.api.get_pool", return_value=mock_pool):
            with pytest.raises(HTTPException) as exc:
                await finance_verify_challenge(
                    {"ain": "x", "nonce": "short"}, AUTH_ADMIN,
                )
        assert exc.value.status_code == 400


# ── P1 (R19): annual_pay 幂等重放短路 ───────────────────────

class TestAnnualPayReplayGuard:
    @pytest.mark.asyncio
    async def test_already_processed_no_extension(self, mock_conn, mock_pool):
        """幂等命中时不得再延期 —— 否则同一次缴费可免费续多年"""
        mock_conn.fetchrow.side_effect = [
            {"agent_id": "a1", "category": "biz:seller", "status": "active"},
            {"agent_id": "a1", "free_months": 0, "first_paid_at": None,
             "expires_at": None, "is_expired": False},
        ]
        # deduct 返回幂等命中
        deduct_mock = AsyncMock(return_value={
            "already_processed": True, "txn_id": 99,
        })
        with patch("siku.api.get_pool", return_value=mock_pool), \
             patch("siku.api.acct.deduct", new=deduct_mock), \
             patch("siku.api.cfg.get_annual_fee_fen", return_value=99600):
            result = await annual_pay(
                AnnualPayRequest(agent_id="a1", request_id="req-1"),
                AUTH_AGENT,
            )

        assert result["status"] == "already_processed"
        assert result["txn_id"] == 99
        # 不得执行任何 UPDATE（延期/激活）与审计
        mock_conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_request_id_uses_period_key(self, mock_conn, mock_pool):
        """无 request_id 时退化为按计费周期幂等，且不重复延期"""
        mock_conn.fetchrow.side_effect = [
            {"agent_id": "a1", "category": "biz:seller", "status": "active"},
            {"agent_id": "a1", "free_months": 0, "first_paid_at": None,
             "expires_at": None, "is_expired": False},
        ]
        deduct_mock = AsyncMock(return_value={
            "already_processed": True, "txn_id": 100,
        })
        with patch("siku.api.get_pool", return_value=mock_pool), \
             patch("siku.api.acct.deduct", new=deduct_mock), \
             patch("siku.api.cfg.get_annual_fee_fen", return_value=99600):
            result = await annual_pay(
                AnnualPayRequest(agent_id="a1"),
                AUTH_AGENT,
            )

        assert result["status"] == "already_processed"
        mock_conn.execute.assert_not_awaited()


# ── P2 (R11): annual_pay 闰日 +1 年时间炸弹 ─────────────────

class TestAnnualPayLeapDay:
    """P2 (R11): 闰日（2/29）续费 +1 年不再抛 ValueError → 500"""

    def test_add_one_year_leap_day_rolls_back(self):
        from datetime import datetime, timezone
        from siku.api import _add_one_year
        leap = datetime(2028, 2, 29, tzinfo=timezone.utc)
        result = _add_one_year(leap)
        assert (result.year, result.month, result.day) == (2029, 2, 28)

    def test_add_one_year_normal_day(self):
        from datetime import datetime, timezone
        from siku.api import _add_one_year
        dt = datetime(2025, 1, 15, tzinfo=timezone.utc)
        result = _add_one_year(dt)
        assert (result.year, result.month, result.day) == (2026, 1, 15)

    @pytest.mark.asyncio
    async def test_annual_pay_leap_day_no_500(self, mock_conn, mock_pool):
        from datetime import datetime, timezone
        # expires_at = 2028-02-29（闰日）> now → 走续费延期；目标年 2029 无 2/29 → 回落 2/28
        mock_conn.fetchrow.side_effect = [
            {"agent_id": "a1", "category": "biz:seller", "status": "active"},
            {"agent_id": "a1", "free_months": 0,
             "first_paid_at": datetime(2028, 2, 29, tzinfo=timezone.utc),
             "expires_at": datetime(2028, 2, 29, tzinfo=timezone.utc),
             "is_expired": False},
        ]
        with patch("siku.api.get_pool", return_value=mock_pool), \
             patch("siku.api.acct.deduct", new=AsyncMock(return_value={"txn_id": 1})), \
             patch("siku.api.cfg.get_annual_fee_fen", return_value=99600), \
             patch("siku.api.write_finance_audit", new=AsyncMock()) as wfa:
            result = await annual_pay(
                AnnualPayRequest(agent_id="a1"),
                AUTH_AGENT,
            )

        assert result["status"] == "ok"
        assert result["expires_at"].startswith("2029-02-28")
        audit = wfa.call_args.args[1]
        assert audit["detail"]["new_expires_at"].startswith("2029-02-28")


# ── P1 (R17): IM 回调 fail-closed 验签 ─────────────────────

class TestIMCallbackVerification:
    @pytest.mark.asyncio
    async def test_wechat_valid_signature(self, mock_conn, mock_pool):
        token, ts, nonce = "t0k3n", str(int(time.time())), "n1"
        sig = hashlib.sha1("".join(sorted([token, ts, nonce])).encode()).hexdigest()
        body = '{"FromUserName":"user1","Content":"余额查询"}'.encode("utf-8")

        with patch("siku.api.cfg.get_im_channel_config", return_value={"token": token}), \
             patch("siku.api.get_pool", return_value=mock_pool):
            req = await _make_request(body, query={"signature": sig, "timestamp": ts, "nonce": nonce})
            resp = await im_wechat_callback(req)

        assert resp["status"] == "ok"

    @pytest.mark.asyncio
    async def test_wechat_bad_signature_rejected(self, mock_conn, mock_pool):
        token, ts, nonce = "t0k3n", str(int(time.time())), "n1"
        bad_sig = hashlib.sha1("".join(sorted(["wrong", ts, nonce])).encode()).hexdigest()
        body = b'{"Content":"hello"}'

        with patch("siku.api.cfg.get_im_channel_config", return_value={"token": token}):
            req = await _make_request(body, query={"signature": bad_sig, "timestamp": ts, "nonce": nonce})
            with pytest.raises(HTTPException) as exc:
                await im_wechat_callback(req)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wechat_token_unconfigured_fails_closed(self, mock_conn, mock_pool):
        with patch("siku.api.cfg.get_im_channel_config", return_value={}):
            req = await _make_request(b'{"Content":"hello"}')
            with pytest.raises(HTTPException) as exc:
                await im_wechat_callback(req)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_wecom_signature_includes_body(self, mock_conn, mock_pool):
        token, ts, nonce = "w1", str(int(time.time())), "n2"
        body = '{"FromUserName":"user2","Content":"通过123"}'.encode("utf-8")
        sig = hashlib.sha1("".join(sorted([token, ts, nonce, body.decode()])).encode()).hexdigest()

        with patch("siku.api.cfg.get_im_channel_config", return_value={"token": token}), \
             patch("siku.api.get_pool", return_value=mock_pool), \
             patch("siku.api._im_approve_pending_recharge",
                   new=AsyncMock(return_value={"status": "ok"})):
            req = await _make_request(body, query={"msg_signature": sig, "timestamp": ts, "nonce": nonce})
            resp = await im_wecom_callback(req)

        assert resp["status"] == "ok"

    @pytest.mark.asyncio
    async def test_wecom_missing_signature_rejected(self, mock_conn, mock_pool):
        with patch("siku.api.cfg.get_im_channel_config", return_value={"token": "w1"}):
            req = await _make_request(b'{"Content":"hi"}', query={"timestamp": "1", "nonce": "2"})
            with pytest.raises(HTTPException) as exc:
                await im_wecom_callback(req)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_feishu_header_token_valid(self, mock_conn, mock_pool):
        body = b'{"header":{"token":"fei_tok"},"action":{"value":"{\\"action\\":\\"balance\\"}"},"open_id":"u3"}'

        with patch("siku.api.cfg.get_im_channel_config", return_value={"verify_token": "fei_tok"}), \
             patch("siku.api.get_pool", return_value=mock_pool):
            req = await _make_request(body)
            resp = await im_feishu_callback(req)

        assert resp["status"] == "ok"

    @pytest.mark.asyncio
    async def test_feishu_unconfigured_fails_closed(self, mock_conn, mock_pool):
        with patch("siku.api.cfg.get_im_channel_config", return_value={}):
            req = await _make_request(b'{"action":{"value":"{}"}}')
            with pytest.raises(HTTPException) as exc:
                await im_feishu_callback(req)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_feishu_challenge_echo(self, mock_conn, mock_pool):
        body = b'{"challenge":"ch123","token":"fei_tok"}'
        with patch("siku.api.cfg.get_im_channel_config", return_value={"verify_token": "fei_tok"}):
            req = await _make_request(body)
            resp = await im_feishu_callback(req)
        assert resp == {"challenge": "ch123"}

    @pytest.mark.asyncio
    async def test_feishu_challenge_token_mismatch_rejected(self, mock_conn, mock_pool):
        body = b'{"challenge":"ch123","token":"wrong"}'
        with patch("siku.api.cfg.get_im_channel_config", return_value={"verify_token": "fei_tok"}):
            req = await _make_request(body)
            with pytest.raises(HTTPException) as exc:
                await im_feishu_callback(req)
        assert exc.value.status_code == 401
