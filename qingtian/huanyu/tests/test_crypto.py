"""crypto.py 单元测试 — Ed25519/X25519/HKDF/AES-GCM。"""

import pytest

from huanyu import crypto


def test_ed25519_sign_verify():
    priv, pub = crypto.generate_ed25519_keypair()
    sig = crypto.sign(priv, b"hello")
    assert crypto.verify(pub, b"hello", sig)


def test_ed25519_tamper_detected():
    priv, pub = crypto.generate_ed25519_keypair()
    sig = crypto.sign(priv, b"hello")
    assert not crypto.verify(pub, b"tampered", sig)


def test_x25519_dh_consistency():
    a_priv, a_pub = crypto.generate_x25519_keypair()
    b_priv, b_pub = crypto.generate_x25519_keypair()
    shared_a = crypto.x25519_derive(a_priv, b_pub)
    shared_b = crypto.x25519_derive(b_priv, a_pub)
    assert shared_a == shared_b
    assert len(shared_a) == 32


def test_hkdf_session_key_consistency():
    a_priv, _ = crypto.generate_x25519_keypair()
    _, b_pub = crypto.generate_x25519_keypair()
    shared = crypto.x25519_derive(a_priv, b_pub)
    ka = crypto.derive_session_key(shared, "orgA", "orgB", "na", "nb")
    kb = crypto.derive_session_key(shared, "orgA", "orgB", "na", "nb")
    assert ka == kb
    assert len(ka) == 32


def test_aes_gcm_roundtrip():
    key = crypto.hkdf_sha256(b"shared", crypto.PROTOCOL_LABEL, b"info")
    enc = crypto.aes_gcm_encrypt(key, b"secret", b"aad")
    pt = crypto.aes_gcm_decrypt(key, enc["iv"], enc["tag"], enc["cipher"], b"aad")
    assert pt == b"secret"


def test_aes_gcm_tamper_detected():
    key = crypto.hkdf_sha256(b"shared", crypto.PROTOCOL_LABEL, b"info")
    enc = crypto.aes_gcm_encrypt(key, b"secret", b"aad")
    with pytest.raises(Exception):
        crypto.aes_gcm_decrypt(key, enc["iv"], enc["tag"], enc["cipher"], b"wrong-aad")


def test_gcm_nonce_unique():
    key = crypto.hkdf_sha256(b"shared", crypto.PROTOCOL_LABEL, b"info")
    e1 = crypto.aes_gcm_encrypt(key, b"m1", b"aad")
    e2 = crypto.aes_gcm_encrypt(key, b"m2", b"aad")
    assert e1["iv"] != e2["iv"]
