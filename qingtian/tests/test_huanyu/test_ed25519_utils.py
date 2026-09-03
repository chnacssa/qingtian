"""寰宇 — Ed25519 工具单元测试 (无 DB 依赖)

覆盖: generate_keypair / sign / verify / PEM 往返 / 篡改检测
"""

import pytest

from huanyu.ed25519_utils import (
    generate_keypair,
    sign_message,
    verify_signature,
    public_key_to_pem,
    public_key_from_pem,
    private_key_to_pem,
)


class TestKeypairGeneration:
    def test_generate_returns_bytes(self):
        priv, pub = generate_keypair()
        assert isinstance(priv, bytes)
        assert isinstance(pub, bytes)
        assert len(priv) == 32  # Ed25519 私钥原始字节
        assert len(pub) == 32   # Ed25519 公钥原始字节

    def test_generate_unique_keys(self):
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        assert priv1 != priv2
        assert pub1 != pub2


class TestSignAndVerify:
    def test_sign_and_verify_roundtrip(self):
        priv, pub = generate_keypair()
        msg = "hello huanyu"
        sig = sign_message(priv, msg)
        assert verify_signature(pub, msg, sig) is True

    def test_verify_tampered_message_fails(self):
        priv, pub = generate_keypair()
        msg = "original message"
        sig = sign_message(priv, msg)
        assert verify_signature(pub, "tampered message", sig) is False

    def test_verify_wrong_public_key_fails(self):
        priv, pub = generate_keypair()
        _, pub2 = generate_keypair()
        msg = "test"
        sig = sign_message(priv, msg)
        assert verify_signature(pub2, msg, sig) is False

    def test_verify_tampered_signature_fails(self):
        priv, pub = generate_keypair()
        msg = "test"
        sig = sign_message(priv, msg)
        tampered = sig[:-4] + "AAAA"
        assert verify_signature(pub, msg, tampered) is False

    def test_verify_invalid_base64_fails(self):
        _, pub = generate_keypair()
        assert verify_signature(pub, "msg", "!!!not-base64!!!") is False

    def test_verify_empty_signature_fails(self):
        _, pub = generate_keypair()
        assert verify_signature(pub, "msg", "") is False

    def test_sign_bytes_input(self):
        priv, pub = generate_keypair()
        sig = sign_message(priv, b"binary message")
        assert verify_signature(pub, b"binary message", sig) is True

    def test_sign_deterministic(self):
        """同一私钥同一消息 → 签名相同（Ed25519 是确定性的）"""
        priv, _ = generate_keypair()
        sig1 = sign_message(priv, "deterministic test")
        sig2 = sign_message(priv, "deterministic test")
        assert sig1 == sig2

    def test_different_messages_different_signatures(self):
        priv, _ = generate_keypair()
        sig1 = sign_message(priv, "message A")
        sig2 = sign_message(priv, "message B")
        assert sig1 != sig2


class TestPEMRoundtrip:
    def test_public_key_pem_roundtrip(self):
        priv, pub = generate_keypair()
        pem = public_key_to_pem(pub)
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        pub2 = public_key_from_pem(pem)
        assert pub == pub2

    def test_private_key_pem_format(self):
        priv, _ = generate_keypair()
        pem = private_key_to_pem(priv)
        assert pem.startswith("-----BEGIN PRIVATE KEY-----")

    def test_sign_verify_after_pem_roundtrip(self):
        priv, pub = generate_keypair()
        pem = public_key_to_pem(pub)
        pub_restored = public_key_from_pem(pem)

        msg = "roundtrip test"
        sig = sign_message(priv, msg)
        assert verify_signature(pub_restored, msg, sig) is True


class TestCrossKeyIsolation:
    def test_key1_cannot_verify_key2_signature(self):
        priv1, pub1 = generate_keypair()
        priv2, _ = generate_keypair()

        msg = "cross key"
        sig = sign_message(priv1, msg)
        # 用 key2 的公钥验 key1 的签名 → 失败
        assert verify_signature(pub1, msg, sig) is True
        # 但不能用错误的公钥
        _, pub_other = generate_keypair()
        assert verify_signature(pub_other, msg, sig) is False
