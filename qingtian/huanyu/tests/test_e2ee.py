"""e2ee.py 单元测试 — 在线消息 + 离线消息。"""

import pytest

from huanyu import crypto, e2ee


def _session_key():
    a_priv, a_pub = crypto.generate_x25519_keypair()
    _, b_pub = crypto.generate_x25519_keypair()
    shared = crypto.x25519_derive(a_priv, b_pub)
    return crypto.derive_session_key(shared, "orgA", "orgB", "na", "nb")


def test_online_message_roundtrip():
    sk = _session_key()
    enc = e2ee.encrypt_message(sk, b"quote 85", "orgA", "orgB", "biz:buyer-01", "biz:seller-02", "ts1")
    pt = e2ee.decrypt_message(sk, enc, "orgA", "orgB", "biz:buyer-01", "biz:seller-02", "ts1")
    assert pt == b"quote 85"


def test_online_aad_tamper_detected():
    sk = _session_key()
    enc = e2ee.encrypt_message(sk, b"quote 85", "orgA", "orgB", "biz:buyer-01", "biz:seller-02", "ts1")
    # ts 变了 → AAD 不匹配 → 解密失败
    with pytest.raises(Exception):
        e2ee.decrypt_message(sk, enc, "orgA", "orgB", "biz:buyer-01", "biz:seller-02", "ts2")


def test_offline_message_roundtrip():
    b_static_priv, b_static_pub = crypto.generate_x25519_keypair()
    off = e2ee.encrypt_offline_message(b_static_pub, b"offline inquiry", "orgA", "orgB", "nonce1")
    pt = e2ee.decrypt_offline_message(b_static_priv, off["o_pub"], off, "orgA", "orgB", "nonce1")
    assert pt == b"offline inquiry"


def test_offline_per_message_key_isolation():
    """每条离线消息独立密钥：nonce 不匹配则解不出。"""
    b_static_priv, b_static_pub = crypto.generate_x25519_keypair()
    off = e2ee.encrypt_offline_message(b_static_pub, b"another", "orgA", "orgB", "nonce2")
    with pytest.raises(Exception):
        e2ee.decrypt_offline_message(b_static_priv, off["o_pub"], off, "orgA", "orgB", "nonce1")
