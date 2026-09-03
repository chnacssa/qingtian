"""寰宇 — 证书管理单元测试 (无 DB 依赖)

覆盖: create_self_signed_cert / verify_cert / revoke_cert / is_revoked / get_crl
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from huanyu.certificate import (
    TIER_VALIDITY,
    create_self_signed_cert,
    verify_cert,
    revoke_cert,
    is_revoked,
    get_crl,
    _cert_fingerprint,
)
from huanyu.ed25519_utils import generate_keypair, sign_message, public_key_to_pem


class TestCertCreation:
    def test_create_returns_required_fields(self):
        priv, _pub = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv, tier="free")

        assert "ain" in cert
        assert "public_key" in cert
        assert "tier" in cert
        assert "issued_at" in cert
        assert "expires_at" in cert
        assert "signature" in cert
        assert "fingerprint" in cert
        assert cert["ain"] == "CN-BJ-A0001"
        assert cert["tier"] == "free"

    def test_create_different_ains_different_certs(self):
        priv1, _ = generate_keypair()
        priv2, _ = generate_keypair()

        cert1 = create_self_signed_cert("CN-BJ-A0001", priv1)
        cert2 = create_self_signed_cert("CN-BJ-A0002", priv2)

        # 不同 AIN 和不同私钥 → 签名必然不同
        assert cert1["signature"] != cert2["signature"]
        assert cert1["fingerprint"] != cert2["fingerprint"]

    def test_same_key_same_ain_different_timestamps(self):
        priv, _ = generate_keypair()
        cert1 = create_self_signed_cert("CN-BJ-A0001", priv)
        cert2 = create_self_signed_cert("CN-BJ-A0001", priv)

        # 同一私钥同一 AIN，但时间戳不同 → 指纹不同
        assert cert1["fingerprint"] != cert2["fingerprint"]

    def test_tier_validity_days(self):
        assert TIER_VALIDITY["free"] == 365
        assert TIER_VALIDITY["pro"] == 730
        assert TIER_VALIDITY["enterprise"] == 1095
        assert TIER_VALIDITY["alliance"] == 1825

    def test_unknown_tier_defaults_365(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv, tier="unknown")
        assert "expires_at" in cert  # 不崩


class TestCertVerify:
    def test_valid_cert_passes(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        assert verify_cert(cert) is True

    def test_tampered_ain_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        cert["ain"] = "CN-BJ-A9999"
        assert verify_cert(cert) is False

    def test_tampered_tier_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv, tier="free")
        cert["tier"] = "alliance"
        assert verify_cert(cert) is False

    def test_tampered_public_key_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        cert["public_key"] = cert["public_key"].replace("A", "B")
        assert verify_cert(cert) is False

    def test_missing_signature_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        del cert["signature"]
        assert verify_cert(cert) is False

    def test_empty_signature_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        cert["signature"] = ""
        assert verify_cert(cert) is False

    def test_expired_cert_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        # 手动把过期时间改成过去
        cert["expires_at"] = "2020-01-01T00:00:00+00:00"
        assert verify_cert(cert) is False

    def test_naive_expires_at_valid_not_invalid(self):
        """P2 (R11): naive expires_at（无时区后缀）按 UTC 处理，不再抛 TypeError 被吞 → 恒判无效。

        回归：naive 时间与 aware now 比较会抛 TypeError，被 except 吞掉 → 任何
        naive expires_at 的证书都判无效。现在 naive 视为 UTC，有效期判定正确。
        """
        priv, pub = generate_keypair()
        # 构造未来 naive 过期时间（去掉 tzinfo），并对该 body 正确签名
        body = {
            "ain": "CN-BJ-A0001",
            "public_key": public_key_to_pem(pub),
            "tier": "free",
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30))
                .replace(tzinfo=None).isoformat(),  # naive 未来时间
        }
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
        body["signature"] = sign_message(priv, payload)
        body["fingerprint"] = _cert_fingerprint(body)

        assert verify_cert(body) is True

    def test_naive_expired_cert_fails(self):
        """P2 (R11): naive 且已过期 → 判无效（时区统一后过期判定仍生效）。"""
        priv, pub = generate_keypair()
        body = {
            "ain": "CN-BJ-A0001",
            "public_key": public_key_to_pem(pub),
            "tier": "free",
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) - timedelta(days=30))
                .replace(tzinfo=None).isoformat(),  # naive 过去时间
        }
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
        body["signature"] = sign_message(priv, payload)
        body["fingerprint"] = _cert_fingerprint(body)

        assert verify_cert(body) is False

    def test_malformed_expires_at_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        cert["expires_at"] = "not-a-date"
        assert verify_cert(cert) is False

    def test_missing_public_key_fails(self):
        priv, _ = generate_keypair()
        cert = create_self_signed_cert("CN-BJ-A0001", priv)
        cert["public_key"] = ""
        assert verify_cert(cert) is False


class TestCertRevocation:
    def test_revoke_and_check(self):
        revoke_cert("crl_a_001")
        assert is_revoked("crl_a_001") is True
        assert is_revoked("crl_a_nonexist") is False

    def test_revoke_multiple(self):
        revoke_cert("crl_b_001")
        revoke_cert("crl_b_002")
        assert is_revoked("crl_b_001")
        assert is_revoked("crl_b_002")

    def test_get_crl_contains_revoked(self):
        revoke_cert("crl_c_001")
        revoke_cert("crl_c_002")
        crl = get_crl()
        assert "crl_c_001" in crl
        assert "crl_c_002" in crl
        assert crl == sorted(crl)  # CRL 应已排序


class TestCertFingerprint:
    def test_fingerprint_excludes_signature(self):
        priv, _ = generate_keypair()
        cert1 = create_self_signed_cert("CN-BJ-A0001", priv)
        fp1 = _cert_fingerprint(cert1)

        # 修改非签名字段 → 指纹应变化
        cert1["ain"] = "CN-BJ-A9999"
        fp2 = _cert_fingerprint(cert1)
        assert fp1 != fp2

    def test_fingerprint_deterministic(self):
        body = {"ain": "CN-BJ-A0001", "tier": "free", "public_key": "pem..."}
        assert _cert_fingerprint(body) == _cert_fingerprint(body)
