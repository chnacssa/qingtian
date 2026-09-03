"""
message_signing.py 单元测试
HMAC 签名 / 重放检测（P1 #4 2026-08-26: nonce Redis 化后的两级路径）
"""

import time
from unittest.mock import patch

import pytest

from zhenyue.message_signing import (
    sign_message,
    verify_message,
    check_and_record_nonce,
)


def _msg(message_id="m-001", timestamp=None, payload=None):
    payload = payload or {"k": "v"}
    timestamp = timestamp if timestamp is not None else int(time.time())
    sig = sign_message("from-a", "to-b", message_id, timestamp, payload)
    return message_id, timestamp, payload, sig


class TestVerifyMessage:
    """每例强制内存回退路径（Redis 分支单独立测），隔离模块级 Redis 状态。"""

    @pytest.fixture(autouse=True)
    def no_redis(self):
        with patch("zhenyue.message_signing._get_redis", return_value=None):
            yield

    def test_roundtrip_ok(self):
        mid, ts, payload, sig = _msg()
        assert verify_message("from-a", "to-b", mid, ts, payload, sig) is True

    def test_replay_rejected(self):
        mid, ts, payload, sig = _msg(message_id="m-replay")
        assert verify_message("from-a", "to-b", mid, ts, payload, sig) is True
        # 同一 message_id 二次验证（重放）→ nonce 命中拒绝
        assert verify_message("from-a", "to-b", mid, ts, payload, sig) is False

    def test_expired_timestamp_rejected(self):
        old_ts = int(time.time()) - 3600
        mid, ts, payload, sig = _msg(message_id="m-old", timestamp=old_ts)
        assert verify_message("from-a", "to-b", mid, ts, payload, sig) is False

    def test_bad_signature_rejected(self):
        mid, ts, payload, _ = _msg(message_id="m-bad")
        assert verify_message("from-a", "to-b", mid, ts, payload, "0" * 64) is False


class TestNonceRedisPath:
    """#4: Redis 可用时走 SETNX+TTL（多 worker 全局一份），语义与内存一致。"""

    def test_redis_setnx_semantics(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.set.side_effect = [True, None]  # 首次占位成功 / 二次已被占
        with patch("zhenyue.message_signing._get_redis", return_value=client):
            assert check_and_record_nonce("m-redis-1") is True
            assert check_and_record_nonce("m-redis-1") is False
        # SETNX 带 nx=True + ex=TTL
        kwargs = client.set.call_args.kwargs
        assert kwargs.get("nx") is True and kwargs.get("ex", 0) >= 60

    def test_redis_error_falls_back_to_memory(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.set.side_effect = ConnectionError("redis down")
        with patch("zhenyue.message_signing._get_redis", return_value=client), \
             patch("zhenyue.message_signing._nonce_set", new=set()):
            # Redis 写失败回退内存：首次 True、重放 False（窗口语义保持）
            assert check_and_record_nonce("m-fb-1") is True
            assert check_and_record_nonce("m-fb-1") is False
