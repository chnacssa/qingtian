"""License 验证单元测试 — Ed25519 签名/时钟篡改/离线计数器"""

import json
import os
import tempfile
import time

import pytest

from common.crypto import generate_keypair, sign, sha256

# SkillLicenseManager 已移至闭源包 osskill_acssa（osskill/__init__.py 同款 try/except 模式）
try:
    from osskill_acssa.license_manager import SkillLicenseManager as _SLM  # type: ignore
    _HAS_CLOSED_SOURCE = True
except ImportError:
    _SLM = None  # type: ignore
    _HAS_CLOSED_SOURCE = False


class TestEd25519Crypto:
    """Ed25519 签名/验证基础测试"""

    def test_generate_keypair(self):
        priv, pub = generate_keypair()
        assert len(priv) == 32
        assert len(pub) == 32

    def test_sign_and_verify(self):
        priv, pub = generate_keypair()
        msg = b"hello world"
        sig = sign(priv, msg)
        assert len(sig) == 64

        from common.crypto import verify
        assert verify(pub, msg, sig)
        assert not verify(pub, b"tampered", sig)

    def test_verify_different_key(self):
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        msg = b"test"
        sig = sign(priv1, msg)

        from common.crypto import verify
        assert not verify(pub2, msg, sig)

    def test_platform_key(self):
        from common.crypto import platform_key
        pk1 = platform_key("machine_01", "uuid_abc")
        pk2 = platform_key("machine_01", "uuid_abc")
        pk3 = platform_key("machine_02", "uuid_abc")
        assert pk1 == pk2
        assert pk1 != pk3
        assert len(pk1) == 16

    def test_sha256(self):
        h = sha256(b"test")
        assert len(h) == 64
        assert h == sha256(b"test")
        assert h != sha256(b"different")


@pytest.mark.skipif(not _HAS_CLOSED_SOURCE,
                    reason="osskill_acssa 闭源包未安装，跳过 License 集成测试")
class TestSkillLicenseManager:
    """SkillLicenseManager 完整验证流程测试"""

    @pytest.fixture
    def keypair(self):
        return generate_keypair()

    def _create_license(self, keypair, overrides: dict = None) -> dict:
        """创建测试用 License 数据"""
        priv, pub = keypair
        data = {
            "skill_name": "test_skill",
            "version": "2.0.0",
            "licensee": "TestCorp",
            "level": 2,
            "issued_at": int(time.time()) - 1000,
            "expires_at": int(time.time()) + 86400 * 365,
            "features": ["feature_a", "feature_b"],
            "platform_key": "",
            "nonce": sha256(str(time.time()).encode())[:16],
        }
        if overrides:
            data.update(overrides)

        # 签名
        canonical = json.dumps(data, sort_keys=True).encode("utf-8")
        data["signature"] = sign(priv, canonical).hex()
        return data, pub

    def test_verify_valid_license(self, keypair):
        """有效 License 验证通过"""
        license_data, pub_key = self._create_license(keypair)

        with tempfile.TemporaryDirectory() as tmpdir:
            # 写公钥
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="test_skill",
                public_key_path=pub_path,
                data_dir=tmpdir,
            )
            result = mgr.verify(license_data)
            assert result.valid
            assert result.level == 2
            assert result.license_type == "paid"
            assert "feature_a" in result.features

    def test_verify_no_license_is_free(self, keypair):
        """无 License = 免费（Level 0）"""
        from osskill.license_manager import SkillLicenseManager
        mgr = SkillLicenseManager(skill_name="test_skill")
        result = mgr.verify(None)
        assert result.valid
        assert result.level == 0
        assert result.license_type == "free"

    def test_verify_tampered_signature(self, keypair):
        """篡改签名导致验证失败"""
        license_data, pub_key = self._create_license(keypair)
        license_data["signature"] = "0" * 128  # 篡改

        with tempfile.TemporaryDirectory() as tmpdir:
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="test_skill",
                public_key_path=pub_path,
            )
            result = mgr.verify(license_data)
            assert not result.valid

    def test_verify_expired_license(self, keypair):
        """过期 License 验证失败"""
        license_data, pub_key = self._create_license(keypair, {
            "expires_at": int(time.time()) - 1000,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="test_skill",
                public_key_path=pub_path,
            )
            result = mgr.verify(license_data)
            assert not result.valid

    def test_verify_clock_tamper(self, keypair):
        """时钟篡改检测：issued_at 在未来 24h 以上"""
        license_data, pub_key = self._create_license(keypair, {
            "issued_at": int(time.time()) + 90000,  # 25h in future
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="test_skill",
                public_key_path=pub_path,
            )
            result = mgr.verify(license_data)
            assert not result.valid
            assert "clock" in result.error.lower()

    def test_verify_platform_key_mismatch(self, keypair):
        """platform_key 不匹配拒绝"""
        license_data, pub_key = self._create_license(keypair, {
            "platform_key": "wrong_key_12345678",
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="test_skill",
                public_key_path=pub_path,
                machine_id="machine_01",
                install_uuid="uuid_abc",
            )
            result = mgr.verify(license_data)
            assert not result.valid
            assert "platform key" in result.error.lower()

    def test_verify_trial_offline_counter(self, keypair):
        """试用 License 离线计数器检查"""
        license_data, pub_key = self._create_license(keypair, {
            "level": 1,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="test_skill",
                public_key_path=pub_path,
                data_dir=tmpdir,
            )

            # 多次离线验证（每次使用不同 nonce）
            for i in range(7):
                ld = self._create_license(keypair, {
                    "level": 1,
                    "nonce": sha256(str(time.time() + i).encode())[:16],
                })[0]
                result = mgr.verify(ld)
                assert result.valid, f"Failed on attempt {i+1}"

            # 第 8 次应该失败
            result = mgr.verify(license_data.copy())
            assert not result.valid
            assert "offline" in result.error.lower()

    def test_verify_unknown_level(self, keypair):
        """未知 level 拒绝"""
        license_data, pub_key = self._create_license(keypair, {
            "level": 99,
        })

        with tempfile.TemporaryDirectory() as tmpdir:
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="test_skill",
                public_key_path=pub_path,
            )
            result = mgr.verify(license_data)
            assert not result.valid

    def test_verify_skill_name_mismatch(self, keypair):
        """Skill 名称不匹配不影响签名校验（签名已覆盖）"""
        license_data, pub_key = self._create_license(keypair)

        with tempfile.TemporaryDirectory() as tmpdir:
            pub_path = os.path.join(tmpdir, "test.pub")
            with open(pub_path, "wb") as f:
                f.write(pub_key.hex().encode())

            from osskill.license_manager import SkillLicenseManager
            mgr = SkillLicenseManager(
                skill_name="other_skill",
                public_key_path=pub_path,
            )
            result = mgr.verify(license_data)
            # signature covers skill_name, so it would fail
            assert not result.valid
