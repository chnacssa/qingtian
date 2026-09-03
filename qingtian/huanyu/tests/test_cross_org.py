"""跨企业通讯接线测试（2026-08-20 贪狼接线）。

覆盖：
- e2ee 信封：build/verify/tamper/wrong-key/schema
- NonceLRU 进程内去重、envelope_ts_valid 时间窗
- messaging._send_cross_org（mock Hub 目录 + hub_client_send）
- messaging.handle_hub_envelope（mock 目录 + 落库）

全部 mock 不碰 DB/Redis/网络。
"""

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from huanyu import crypto, e2ee
from huanyu.messaging import (
    _send_cross_org,
    _seen_envelope_nonce,
    handle_hub_envelope,
)


# ── e2ee 信封 ─────────────────────────────────────────

def _sign_keys():
    priv, pub = crypto.generate_ed25519_keypair()
    return priv, pub


def _make_envelope(priv: str) -> dict:
    return e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64body", priv)


class TestEnvelope:
    def test_roundtrip_verify(self):
        priv, pub = _sign_keys()
        env = _make_envelope(priv)
        assert env["v"] == "1"
        assert env["type"] == "msg"
        assert "sig" in env
        assert e2ee.verify_envelope(pub, env) is True

    def test_tamper_body_rejected(self):
        priv, pub = _sign_keys()
        env = _make_envelope(priv)
        env["body"] = "tampered"
        assert e2ee.verify_envelope(pub, env) is False

    def test_tamper_from_org_rejected(self):
        """篡改 from_org（来源伪造）被验签拒绝——P0-3 信封签名核心。"""
        priv, pub = _sign_keys()
        env = _make_envelope(priv)
        env["from_org"] = "orgEVIl"
        assert e2ee.verify_envelope(pub, env) is False

    def test_wrong_key_rejected(self):
        priv, pub = _sign_keys()
        _, other_pub = _sign_keys()
        env = _make_envelope(priv)
        assert e2ee.verify_envelope(other_pub, env) is False

    def test_missing_sig_rejected(self):
        priv, pub = _sign_keys()
        env = _make_envelope(priv)
        del env["sig"]  # 移除签名 → 验签拒绝
        assert e2ee.verify_envelope(pub, env) is False

    def test_schema_validation(self):
        priv, _ = _sign_keys()
        env = _make_envelope(priv)
        assert e2ee.verify_envelope_schema(env) is True
        # 缺字段 / 非法 type / 超长 org → False
        bad = {k: v for k, v in env.items() if k != "body"}
        assert e2ee.verify_envelope_schema(bad) is False
        assert e2ee.verify_envelope_schema({**env, "type": "evil"}) is False
        assert e2ee.verify_envelope_schema({**env, "from_org": "x" * 200}) is False
        assert e2ee.verify_envelope_schema({**env, "nonce": ""}) is False


class TestNonceLRU:
    def test_first_seen_passes_second_rejected(self):
        lru = e2ee.NonceLRU()
        assert lru.seen("abc123") is False
        assert lru.seen("abc123") is True
        assert lru.seen("def456") is False

    def test_expiry(self):
        lru = e2ee.NonceLRU(ttl_seconds=-1)  # 过期即失效
        assert lru.seen("x") is False
        assert lru.seen("x") is False  # 已过期 → 不再判重

    def test_bounded_capacity(self):
        lru = e2ee.NonceLRU(maxlen=10)
        for i in range(20):
            assert lru.seen(f"n{i}") is False
        # 容量有界：最旧的一半被丢弃，不无限增长
        assert len(lru._d) <= 10


class TestEnvelopeTs:
    def test_valid_now(self):
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        assert e2ee.envelope_ts_valid(ts) is True

    def test_too_old_rejected(self):
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        assert e2ee.envelope_ts_valid(ts) is False

    def test_garbage_rejected(self):
        assert e2ee.envelope_ts_valid("not-a-date") is False
        assert e2ee.envelope_ts_valid("") is False


# ── messaging._send_cross_org ─────────────────────────

def _offline_body(payload: dict, to_static_pub: bytes, from_org: str, to_org: str, nonce: str) -> str:
    """构造离线一次性密钥信封 body（模拟 _send_cross_org 加密结果）。"""
    enc = e2ee.encrypt_offline_message(
        to_static_pub, json.dumps(payload, ensure_ascii=False).encode(),
        from_org, to_org, nonce,
    )
    return base64.b64encode(json.dumps(enc, ensure_ascii=False).encode()).decode()


class TestSendCrossOrg:
    @pytest.mark.asyncio
    async def test_send_success(self):
        _, target_static_pub = crypto.generate_x25519_keypair()
        org_sig_priv, _ = crypto.generate_ed25519_keypair()

        with patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                "x25519_static_pub": target_static_pub.hex(), "status": "active"})), \
             patch("huanyu.messaging.hub_client_send", AsyncMock(return_value=True)) as m_send, \
             patch("huanyu.messaging._wait_ack", AsyncMock(return_value=True)), \
             patch("huanyu.messaging.mark_delivery_status", AsyncMock(return_value=None)) as m_mark, \
             patch("huanyu.config.get_org_sign_key", return_value=org_sig_priv):
            await _send_cross_org(
                "msg-1", "orgA", "orgB", "a1", "b1", {"quote": 85})
            # 信封已签名发出，两层 ack 都收到，最终投递状态 delivered
            m_send.assert_awaited_once()
            m_mark.assert_awaited_with("msg-1", "delivered")

    @pytest.mark.asyncio
    async def test_send_no_static_pub_marks_failed(self):
        with patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                "x25519_static_pub": "", "status": "active"})), \
             patch("huanyu.messaging.mark_delivery_status", AsyncMock(return_value=None)) as m_mark:
            await _send_cross_org("msg-1", "orgA", "orgB", "a1", "b1", {})
            m_mark.assert_awaited_with("msg-1", "failed")

    @pytest.mark.asyncio
    async def test_send_hub_offline_marks_failed(self):
        _, target_static_pub = crypto.generate_x25519_keypair()
        with patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                "x25519_static_pub": target_static_pub.hex(), "status": "active"})), \
             patch("huanyu.messaging.hub_client_send", AsyncMock(return_value=False)), \
             patch("huanyu.messaging.mark_delivery_status", AsyncMock(return_value=None)) as m_mark, \
             patch("huanyu.config.get_org_sign_key", return_value="x" * 64):
            await _send_cross_org("msg-1", "orgA", "orgB", "a1", "b1", {})
            m_mark.assert_awaited_with("msg-1", "failed")


# ── messaging.handle_hub_envelope ─────────────────────

def _make_sent_envelope(from_sig_priv: str, to_static_pub: bytes, payload: dict) -> dict:
    """构造一封已按 _send_cross_org 同规加密+签名的信封（供接收端验签解密）。"""
    nonce = crypto.generate_msg_nonce()
    body_b64 = _offline_body(payload, to_static_pub, "orgA", "orgB", nonce)
    return e2ee.build_envelope("orgA", "orgB", "a1", "b1", body_b64, from_sig_priv, nonce=nonce)


class TestHandleHubEnvelope:
    @pytest.mark.asyncio
    async def test_receive_roundtrip(self):
        from_sig_priv, from_sig_pub = crypto.generate_ed25519_keypair()
        my_static_priv, my_static_pub = crypto.generate_x25519_keypair()

        payload = {"quote": 85, "items": ["A"]}
        env = _make_sent_envelope(from_sig_priv, my_static_pub, payload)
        msg = {"type": "msg", "envelope": env}

        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.config.get_org_static_priv", return_value=my_static_priv), \
             patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                    "ed25519_pubkey": from_sig_pub, "status": "active"})), \
             patch("huanyu.messaging.insert_incoming_peer_message", AsyncMock(return_value=None)) as m_insert:
            await handle_hub_envelope(msg)
            # 成功投递：落库 + 幂等键绑定信封 nonce
            m_insert.assert_awaited_once()
            kwargs = m_insert.await_args.kwargs
            assert kwargs["from_agent"] == "a1"
            assert kwargs["to_agent"] == "b1"
            assert kwargs["payload"] == payload
            assert kwargs["idempotency_key"] == f"org:{env['nonce']}"

    @pytest.mark.asyncio
    async def test_receive_replay_rejected(self):
        from_sig_priv, from_sig_pub = crypto.generate_ed25519_keypair()
        my_static_priv, my_static_pub = crypto.generate_x25519_keypair()
        env = _make_sent_envelope(from_sig_priv, my_static_pub, {"k": "v"})
        msg = {"type": "msg", "envelope": env}

        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.config.get_org_static_priv", return_value=my_static_priv), \
             patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                    "ed25519_pubkey": from_sig_pub, "status": "active"})), \
             patch("huanyu.messaging.insert_incoming_peer_message", AsyncMock()) as m_insert:
            await handle_hub_envelope(msg)   # 首次 → 投递
            await handle_hub_envelope(msg)   # 重放 → 拒绝
            assert m_insert.await_count == 1

    @pytest.mark.asyncio
    async def test_receive_bad_sig_rejected(self):
        from_sig_priv, from_sig_pub = crypto.generate_ed25519_keypair()
        my_static_priv, my_static_pub = crypto.generate_x25519_keypair()
        # 用另一把真实私钥签发 → 与目录登记的 from_sig_pub 不匹配 → 验签拒绝
        other_priv, _ = crypto.generate_ed25519_keypair()
        env = _make_sent_envelope(other_priv, my_static_pub, {"k": "v"})
        msg = {"type": "msg", "envelope": env}
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.config.get_org_static_priv", return_value=my_static_priv), \
             patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                    "ed25519_pubkey": from_sig_pub, "status": "active"})), \
             patch("huanyu.messaging.insert_incoming_peer_message", AsyncMock()) as m_insert:
            await handle_hub_envelope(msg)
            m_insert.assert_not_awaited()

    def test_seen_envelope_nonce_dedup(self):
        _seen_envelope_nonce.__globals__["_envelope_nonce_lru"] = None
        assert _seen_envelope_nonce("n1") is False
        assert _seen_envelope_nonce("n1") is True
