"""可靠投递：恰好一次幂等 + 重连续投 outbox（2026-08-21 贪狼 review 后补测）。

覆盖（与破军对齐方案一一对应，贪狼 board c85ebdf + 破军 6b277a0 实现）：
- P1-2 恰好一次：信封 msg_id 进验签原文 + 接收方幂等键 msg:{msg_id}，
  重发（新 nonce 同 msg_id）不重复；无 msg_id 回落 org:{nonce}
- 重连续投（破军 6b277a0 落地后激活）：
  - seq 单调生成（org 内 Redis INCR，非随机；无 Redis fallback 时间戳）
  - 信封带 seq 进验签原文；篡改 seq → verify_envelope False
  - B 侧 last_continuous 连续窗口推进（收到 3,5 → 只推进到 3，缺口不越；
    补收到 3 后窗口越到 5）
  - resync：B 重连后上报 {"type":"resync","org_id":X,"last_continuous":{...}}
  - 幂等联动：resync 补推 + A 侧重试双通道同 msg_id → 同幂等键（DB 层去重）
  - Hub 侧 _write_outbox/_handle_resync 在 manager 副本 test_cross_org_outbox.py

全部 mock 不碰 DB/Redis/网络。
"""

import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from huanyu import crypto, e2ee
from huanyu.hub_client import HubClient
from huanyu.messaging import (
    _get_all_last_continuous,
    _next_org_seq,
    _record_received_seq,
    _send_cross_org,
    handle_hub_envelope,
)


def _sign_keys():
    priv, pub = crypto.generate_ed25519_keypair()
    return priv, pub


def _offline_body(payload: dict, to_static_pub: bytes, from_org: str, to_org: str, nonce: str) -> str:
    enc = e2ee.encrypt_offline_message(
        to_static_pub, json.dumps(payload, ensure_ascii=False).encode(),
        from_org, to_org, nonce,
    )
    return base64.b64encode(json.dumps(enc, ensure_ascii=False).encode()).decode()


def _make_sent_envelope(from_sig_priv: str, to_static_pub: bytes, payload: dict,
                        msg_id: str = "", nonce: str | None = None) -> dict:
    """与 _send_cross_org 同规加密+签名信封（支持 msg_id / 指定 nonce）。"""
    nonce = nonce or crypto.generate_msg_nonce()
    body_b64 = _offline_body(payload, to_static_pub, "orgA", "orgB", nonce)
    return e2ee.build_envelope(
        "orgA", "orgB", "a1", "b1", body_b64, from_sig_priv,
        nonce=nonce, msg_id=msg_id,
    )


class TestEnvelopeMsgId:
    """P1-2：msg_id 进信封 + 进验签原文（防篡改去重键）。"""

    def test_build_envelope_carries_msg_id(self):
        priv, pub = _sign_keys()
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64", priv, msg_id="msg-42")
        assert env["msg_id"] == "msg-42"
        assert e2ee.verify_envelope(pub, env) is True

    def test_build_envelope_default_empty_msg_id(self):
        """不传 msg_id → 空串（老信封回落 org:{nonce} 兼容）。"""
        priv, _ = _sign_keys()
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64", priv)
        assert env["msg_id"] == ""

    def test_tamper_msg_id_rejected(self):
        """篡改 msg_id（改去重键/对齐点）→ 验签拒绝。"""
        priv, pub = _sign_keys()
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64", priv, msg_id="msg-42")
        env["msg_id"] = "msg-43"
        assert e2ee.verify_envelope(pub, env) is False


class TestSendCrossOrgMsgId:
    """P1-2：_send_cross_org 发出信封携带原始 message_id（重试保持不变）。"""

    @pytest.mark.asyncio
    async def test_envelope_carries_msg_id(self):
        _, target_static_pub = crypto.generate_x25519_keypair()
        org_sig_priv, _ = crypto.generate_ed25519_keypair()
        with patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                "x25519_static_pub": target_static_pub.hex(), "status": "active"})), \
             patch("huanyu.messaging.hub_client_send", AsyncMock(return_value=True)) as m_send, \
             patch("huanyu.messaging._wait_ack", AsyncMock(return_value=True)), \
             patch("huanyu.messaging.mark_delivery_status", AsyncMock(return_value=None)), \
             patch("huanyu.config.get_org_sign_key", return_value=org_sig_priv):
            await _send_cross_org("msg-7", "orgA", "orgB", "a1", "b1", {"q": 1})
        env = m_send.await_args.args[0]["envelope"]
        assert env["msg_id"] == "msg-7"


class TestHandleHubEnvelopeIdemKey:
    """P1-2：接收方幂等键 msg:{msg_id}（重发恰好一次）；无 msg_id 回落 org:{nonce}。"""

    def _call(self, env, m_insert):
        msg = {"type": "msg", "envelope": env}
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.config.get_org_static_priv", return_value=self._my_priv), \
             patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                    "ed25519_pubkey": self._from_pub, "status": "active"})), \
             patch("huanyu.messaging.insert_incoming_peer_message", AsyncMock()) as m_insert:
            handle_hub_envelope(msg)
        return m_insert

    @pytest.mark.asyncio
    async def test_idem_key_uses_msg_id(self):
        from_sig_priv, self._from_pub = _sign_keys()
        self._my_priv, my_static_pub = crypto.generate_x25519_keypair()
        env = _make_sent_envelope(from_sig_priv, my_static_pub, {"q": 85}, msg_id="msg-42")
        m_insert = AsyncMock()
        msg = {"type": "msg", "envelope": env}
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.config.get_org_static_priv", return_value=self._my_priv), \
             patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                    "ed25519_pubkey": self._from_pub, "status": "active"})), \
             patch("huanyu.messaging.insert_incoming_peer_message", m_insert):
            await handle_hub_envelope(msg)
        m_insert.assert_awaited_once()
        assert m_insert.await_args.kwargs["idempotency_key"] == "msg:msg-42"

    @pytest.mark.asyncio
    async def test_retry_new_nonce_same_msg_id_same_idem_key(self):
        """重发（新 nonce 同 msg_id）→ 幂等键不变 → DB 层去重（恰好一次）。"""
        from_sig_priv, self._from_pub = _sign_keys()
        self._my_priv, my_static_pub = crypto.generate_x25519_keypair()
        env1 = _make_sent_envelope(from_sig_priv, my_static_pub, {"q": 85}, msg_id="msg-9", nonce="nonce-a")
        env2 = _make_sent_envelope(from_sig_priv, my_static_pub, {"q": 85}, msg_id="msg-9", nonce="nonce-b")
        keys = []
        for env in (env1, env2):
            m_insert = AsyncMock()
            msg = {"type": "msg", "envelope": env}
            with patch("huanyu.config.get_org_id", return_value="orgB"), \
                 patch("huanyu.config.get_org_static_priv", return_value=self._my_priv), \
                 patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                        "ed25519_pubkey": self._from_pub, "status": "active"})), \
                 patch("huanyu.messaging.insert_incoming_peer_message", m_insert):
                await handle_hub_envelope(msg)
            m_insert.assert_awaited_once()
            keys.append(m_insert.await_args.kwargs["idempotency_key"])
        assert keys[0] == keys[1] == "msg:msg-9"
        assert env1["nonce"] != env2["nonce"]  # 确实验证的是"新 nonce 重发"场景

    @pytest.mark.asyncio
    async def test_no_msg_id_fallback_nonce(self):
        """老信封无 msg_id → 回落 org:{nonce}（兼容旧链路）。"""
        from_sig_priv, self._from_pub = _sign_keys()
        self._my_priv, my_static_pub = crypto.generate_x25519_keypair()
        env = _make_sent_envelope(from_sig_priv, my_static_pub, {"q": 85}, msg_id="")
        m_insert = AsyncMock()
        msg = {"type": "msg", "envelope": env}
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.config.get_org_static_priv", return_value=self._my_priv), \
             patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                    "ed25519_pubkey": self._from_pub, "status": "active"})), \
             patch("huanyu.messaging.insert_incoming_peer_message", m_insert):
            await handle_hub_envelope(msg)
        m_insert.assert_awaited_once()
        assert m_insert.await_args.kwargs["idempotency_key"] == f"org:{env['nonce']}"


# ── 重连续投：seq 信封（6b277a0）────────────────────────

class _FakeRedis:
    """内存版 Redis mock（seq/连续窗口/outbox 测试用，不碰真实 Redis）。"""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self._incr = 0

    async def incr(self, key: str) -> int:
        self._incr += 1
        return self._incr

    async def zadd(self, key: str, mapping: dict) -> int:
        self.zsets.setdefault(key, {}).update({str(m): float(s) for m, s in mapping.items()})
        return len(mapping)

    async def zscore(self, key: str, member: str):
        return self.zsets.get(key, {}).get(str(member))

    async def zrangebyscore(self, key: str, min=None, max=None, withscores=False):
        out = []
        for member, score in sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1]):
            if _in_range(min, max, score):
                out.append((member, score) if withscores else member)
        return out

    async def zrem(self, key: str, *members) -> int:
        zs = self.zsets.get(key, {})
        for m in members:
            zs.pop(str(m), None)
        return 0

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def get(self, key: str):
        return self.strings.get(key)

    async def set(self, key: str, value) -> None:
        self.strings[key] = str(value)

    async def keys(self, pattern: str):
        prefix = pattern.split("*")[0]
        return [k for k in self.strings if k.startswith(prefix)]


def _in_range(min_spec, max_spec, score: float) -> bool:
    """近似 Redis 区间语义：min 可带 "(" 开区间，支持 ±inf。"""
    if min_spec is not None and min_spec != "-inf":
        if isinstance(min_spec, str) and min_spec.startswith("("):
            if score <= float(min_spec[1:]):
                return False
        elif score < float(min_spec):
            return False
    if max_spec is not None and max_spec != "+inf":
        if isinstance(max_spec, str) and max_spec.startswith("("):
            if score >= float(max_spec[1:]):
                return False
        elif score > float(max_spec):
            return False
    return True


class TestEnvelopeSeq:
    """6b277a0：信封带 seq 进验签原文（resync 对齐点防篡改）。"""

    def test_build_envelope_carries_seq(self):
        priv, pub = _sign_keys()
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64", priv, seq=42)
        assert env["seq"] == "42"
        assert e2ee.verify_envelope(pub, env) is True

    def test_default_seq_zero(self):
        priv, _ = _sign_keys()
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64", priv)
        assert env["seq"] == "0"

    def test_tamper_seq_rejected(self):
        """篡改 seq（改 resync 对齐点）→ 验签拒绝。"""
        priv, pub = _sign_keys()
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64", priv, seq=42)
        env["seq"] = "43"
        assert e2ee.verify_envelope(pub, env) is False

    def test_seq_length_guard(self):
        """seq 超长（>20）→ schema 拒绝。"""
        priv, _ = _sign_keys()
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", "b64", priv, seq=42)
        env["seq"] = "9" * 21
        assert e2ee.verify_envelope_schema(env) is False


class TestNextOrgSeq:
    """6b277a0：seq 单调生成（org 内自增），非随机。"""

    @pytest.mark.asyncio
    async def test_redis_incr_monotonic(self):
        fake = _FakeRedis()
        with patch("huanyu.config.get_org_id", return_value="orgA"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
            s1, s2, s3 = await _next_org_seq(), await _next_org_seq(), await _next_org_seq()
        assert (s1, s2, s3) == (1, 2, 3)  # 单调自增，非随机

    @pytest.mark.asyncio
    async def test_no_redis_fallback_timestamp(self):
        with patch("huanyu.config.get_org_id", return_value="orgA"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=None)):
            s = await _next_org_seq()
        assert s > 0  # 无 Redis 时 fallback 微秒时间戳（可用但非严格单调）


class TestRecordReceivedSeq:
    """6b277a0：B 侧 last_continuous 连续窗口推进（非简单 max，防缺口漏投）。"""

    @pytest.mark.asyncio
    async def test_consecutive_advances(self):
        fake = _FakeRedis()
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
            for seq in (1, 2, 3):
                await _record_received_seq("orgA", seq)
        assert fake.strings.get("hub:lastc:orgB:orgA") == "3"

    @pytest.mark.asyncio
    async def test_gap_does_not_advance(self):
        """收到 1,2,4（3 丢）→ 连续窗口停在 2，不越缺口（非 max）。"""
        fake = _FakeRedis()
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
            for seq in (1, 2, 4):
                await _record_received_seq("orgA", seq)
        assert fake.strings.get("hub:lastc:orgB:orgA") == "2"

    @pytest.mark.asyncio
    async def test_gap_filled_then_advances(self):
        """先收 1,2,4（停在 2），补收 3 → 窗口越过 3 直抵 4。"""
        fake = _FakeRedis()
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
            for seq in (1, 2, 4):
                await _record_received_seq("orgA", seq)
            await _record_received_seq("orgA", 3)
        assert fake.strings.get("hub:lastc:orgB:orgA") == "4"

    @pytest.mark.asyncio
    async def test_seq_le_zero_ignored(self):
        fake = _FakeRedis()
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
            await _record_received_seq("orgA", 0)
            await _record_received_seq("orgA", -5)
        assert fake.zsets == {} and fake.strings == {}


class TestGetAllLastContinuous:
    """6b277a0：resync 上报前收集本企业全部连续窗口 {from_org: X}。"""

    @pytest.mark.asyncio
    async def test_collects_all_from_orgs(self):
        fake = _FakeRedis()
        fake.strings["hub:lastc:orgB:orgA"] = "3"
        fake.strings["hub:lastc:orgB:orgC"] = "7"
        fake.strings["hub:lastc:orgB:orgA"] = "5"  # 覆盖写入
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
            result = await _get_all_last_continuous()
        assert result == {"orgA": 5, "orgC": 7}

    @pytest.mark.asyncio
    async def test_no_redis_empty(self):
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=None)):
            assert await _get_all_last_continuous() == {}


class TestHandleHubEnvelopeRecordsSeq:
    """6b277a0：收信封落库后记录 seq 进连续窗口。"""

    @pytest.mark.asyncio
    async def test_records_seq_on_receive(self):
        """收 1..5 连续信封 → 连续窗口推进到 5。"""
        from_sig_priv, from_pub = _sign_keys()
        my_priv, my_static_pub = crypto.generate_x25519_keypair()
        fake = _FakeRedis()
        for seq in range(1, 6):
            nonce = crypto.generate_msg_nonce()
            body_b64 = _offline_body({"q": seq}, my_static_pub, "orgA", "orgB", nonce)
            env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", body_b64, from_sig_priv,
                                      nonce=nonce, msg_id=f"msg-{seq}", seq=seq)
            with patch("huanyu.config.get_org_id", return_value="orgB"), \
                 patch("huanyu.config.get_org_static_priv", return_value=my_priv), \
                 patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                        "ed25519_pubkey": from_pub, "status": "active"})), \
                 patch("huanyu.messaging.insert_incoming_peer_message", AsyncMock()), \
                 patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
                await handle_hub_envelope({"type": "msg", "envelope": env})
        assert fake.strings.get("hub:lastc:orgB:orgA") == "5"
        assert set(fake.zsets.get("hub:recv:orgB:orgA", {})) == {"1", "2", "3", "4", "5"}

    @pytest.mark.asyncio
    async def test_single_seq_gap_keeps_window(self):
        """只收 seq=5（1-4 缺）→ 连续窗口不越缺口，停在 0（防漏投）。"""
        from_sig_priv, from_pub = _sign_keys()
        my_priv, my_static_pub = crypto.generate_x25519_keypair()
        nonce = crypto.generate_msg_nonce()
        body_b64 = _offline_body({"q": 1}, my_static_pub, "orgA", "orgB", nonce)
        env = e2ee.build_envelope("orgA", "orgB", "a1", "b1", body_b64, from_sig_priv,
                                  nonce=nonce, msg_id="msg-1", seq=5)
        fake = _FakeRedis()
        with patch("huanyu.config.get_org_id", return_value="orgB"), \
             patch("huanyu.config.get_org_static_priv", return_value=my_priv), \
             patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                    "ed25519_pubkey": from_pub, "status": "active"})), \
             patch("huanyu.messaging.insert_incoming_peer_message", AsyncMock()), \
             patch("huanyu.pubsub._get_redis", AsyncMock(return_value=fake)):
            await handle_hub_envelope({"type": "msg", "envelope": env})
        # 只记 zset，不推进 lastc
        assert "5" in fake.zsets.get("hub:recv:orgB:orgA", {})
        assert "hub:lastc:orgB:orgA" not in fake.strings


class TestResyncSend:
    """6b277a0：企业端重连后发 resync（对齐点续投请求）。"""

    @pytest.mark.asyncio
    async def test_send_resync_on_reconnect(self):
        ws = AsyncMock()
        ws.send = AsyncMock()
        with patch("huanyu.messaging._get_all_last_continuous",
                   AsyncMock(return_value={"orgA": 3})):
            await HubClient("orgB", "tok", "https://hub.example")._send_resync(ws)
        ws.send.assert_awaited_once()
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["type"] == "resync"
        assert msg["org_id"] == "orgB"
        assert msg["last_continuous"] == {"orgA": 3}


class TestIdemLinkageResyncAndRetry:
    """幂等联动：resync 补推 + A 侧重试双通道同 msg_id → 同幂等键（DB 层去重）。"""

    @pytest.mark.asyncio
    async def test_both_channels_same_idem_key(self):
        """同一 msg_id 的信封，无论经 Hub resync 补推还是 A 侧重试，幂等键都是 msg:{msg_id}。"""
        from_sig_priv, from_pub = _sign_keys()
        my_priv, my_static_pub = crypto.generate_x25519_keypair()
        keys = []
        for nonce in ("resync-push-nonce", "retry-nonce"):
            env = _make_sent_envelope(from_sig_priv, my_static_pub, {"q": 85},
                                      msg_id="msg-77", nonce=nonce)
            m_insert = AsyncMock()
            with patch("huanyu.config.get_org_id", return_value="orgB"), \
                 patch("huanyu.config.get_org_static_priv", return_value=my_priv), \
                 patch("huanyu.messaging._get_org_pubkeys", AsyncMock(return_value={
                        "ed25519_pubkey": from_pub, "status": "active"})), \
                 patch("huanyu.messaging.insert_incoming_peer_message", m_insert), \
                 patch("huanyu.pubsub._get_redis", AsyncMock(return_value=_FakeRedis())):
                await handle_hub_envelope({"type": "msg", "envelope": env})
            keys.append(m_insert.await_args.kwargs["idempotency_key"])
        assert keys[0] == keys[1] == "msg:msg-77"
