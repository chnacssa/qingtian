"""AIN 模块测试 — 明文 + IPC-014 加密格式"""
import base64
import os

import pytest

from huanyu.ain import (
    AIN_PATTERN,
    ENCRYPTED_AIN_PATTERN,
    VALID_ROLES,
    ain_to_base_segment,
    decrypt_segment,
    encrypt_segment,
    generate_ain,
    generate_identity_code,
    instance_from_ain,
    next_instance,
    org_from_ain,
    parse_ain,
    parse_ain_decrypt,
    role_from_ain,
    validate_ain,
    validate_ain_format,
)

# ── 测试数据 ────────────────────────────────────────

ORG = "acssa"
COUNTRY = "cn"
CITY = "hf"
BASE_NAME = "management"
ROLE = "biz:buyer"
INSTANCE = "001"

OLD_AIN = f"ain:1:{ORG}:{COUNTRY}-{CITY}-{BASE_NAME}:{ROLE}:{INSTANCE}"

# AES-256-GCM 密钥（32 字节）
LAYER2_KEY = os.urandom(32)
LAYER3_KEY = os.urandom(32)


# ── 基础函数 ────────────────────────────────────────

class TestGenerateIdentityCode:
    def test_basic(self):
        assert generate_identity_code("acssa") == "acssa/"

    def test_with_local(self):
        result = generate_identity_code("acssa", "ain:1:acssa:cn-hf:biz:buyer:001")
        assert result == "acssa/ain:1:acssa:cn-hf:biz:buyer:001"


class TestGenerateAIN:
    def test_old_format(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE)
        assert ain == OLD_AIN

    def test_match_ain_pattern(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE)
        assert AIN_PATTERN.match(ain) is not None

    def test_encrypted_format(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        # 加密格式以点分隔
        assert ain.startswith("ain:1:acssa.")
        parts = ain.split(".")
        assert len(parts) == 3
        # 层2和层3是 base64
        assert ENCRYPTED_AIN_PATTERN.match(ain) is not None

    def test_encrypted_without_key(self):
        """encrypt=True 但无 key 时回退到旧格式"""
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True)
        # 无 key 时不应加密，产生旧格式
        assert ain == OLD_AIN
        assert AIN_PATTERN.match(ain) is not None


class TestParseAIN:
    def test_old_format(self):
        parsed = parse_ain(OLD_AIN)
        assert parsed is not None
        assert parsed["encrypted"] is False
        assert parsed["org"] == ORG
        assert parsed["country"] == COUNTRY
        assert parsed["city"] == CITY
        assert parsed["base_name"] == BASE_NAME
        assert parsed["role"] == ROLE
        assert parsed["domain"] == "biz"
        assert parsed["role_code"] == "buyer"
        assert parsed["instance"] == INSTANCE
        assert parsed["version"] == 1

    def test_encrypted_format(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        parsed = parse_ain(ain)
        assert parsed is not None
        assert parsed["encrypted"] is True
        assert parsed["org"] == ORG
        assert parsed["version"] == 1
        # 加密字段非 None
        assert parsed["encrypted_layer2"] is not None
        assert parsed["encrypted_layer3"] is not None
        # 字段不可见
        assert parsed["country"] is None
        assert parsed["instance"] is None

    def test_decrypt_roundtrip(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        parsed = parse_ain_decrypt(ain, LAYER2_KEY, LAYER3_KEY)
        assert parsed is not None
        assert parsed["encrypted"] is False
        assert parsed["org"] == ORG
        assert parsed["country"] == COUNTRY
        assert parsed["city"] == CITY
        assert parsed["base_name"] == BASE_NAME
        assert parsed["role"] == ROLE
        assert parsed["domain"] == "biz"
        assert parsed["role_code"] == "buyer"
        assert parsed["instance"] == INSTANCE

    def test_decrypt_old_format_passthrough(self):
        """旧格式不需要解密"""
        parsed = parse_ain_decrypt(OLD_AIN, LAYER2_KEY, LAYER3_KEY)
        assert parsed is not None
        assert parsed["encrypted"] is False
        assert parsed["instance"] == INSTANCE

    def test_decrypt_wrong_key(self):
        """错误密钥返回 None"""
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        wrong_key = os.urandom(32)
        parsed = parse_ain_decrypt(ain, wrong_key, LAYER3_KEY)
        assert parsed is None

        parsed = parse_ain_decrypt(ain, LAYER2_KEY, wrong_key)
        assert parsed is None

    def test_invalid_format(self):
        assert parse_ain("invalid") is None
        assert parse_ain("") is None
        assert parse_ain("ain:bad") is None

    def test_encrypted_no_key_but_old_fields(self):
        """加密 AIN 不提供密钥时，向后兼容字段为 None"""
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        parsed = parse_ain(ain)
        assert parsed["country"] is None
        assert parsed["city"] is None
        assert parsed["base_name"] is None
        assert parsed["role"] is None
        assert parsed["instance"] is None


class TestValidateAIN:
    def test_valid(self):
        assert validate_ain(OLD_AIN) is True

    def test_invalid_role(self):
        bad = "ain:1:acssa:cn-hf-management:biz:nonexistent:001"
        assert validate_ain(bad) is False

    def test_invalid_format(self):
        assert validate_ain("not-an-ain") is False

    def test_encrypted_returns_false(self):
        """加密 AIN 无法校验角色"""
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        assert validate_ain(ain) is False


class TestValidateAINFormat:
    """validate_ain_format — 只验格式不验角色"""

    def test_old_format(self):
        assert validate_ain_format(OLD_AIN) is True

    def test_encrypted_format(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        assert validate_ain_format(ain) is True

    def test_invalid_format(self):
        assert validate_ain_format("not-an-ain") is False
        assert validate_ain_format("") is False


class TestGenerateAINErrors:
    """generate_ain role 校验"""

    def test_role_without_domain_raises(self):
        with pytest.raises(ValueError, match="domain prefix"):
            generate_ain(ORG, COUNTRY, CITY, BASE_NAME, "buyer", INSTANCE)

    def test_role_with_domain_ok(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE)
        assert AIN_PATTERN.match(ain) is not None


class TestExtractHelpers:
    def test_ain_to_base_segment(self):
        assert ain_to_base_segment(OLD_AIN) == "cn-hf-management"

    def test_ain_to_base_segment_encrypted(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        assert ain_to_base_segment(ain) is None

    def test_instance_from_ain(self):
        assert instance_from_ain(OLD_AIN) == "001"

    def test_instance_from_ain_encrypted(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        assert instance_from_ain(ain) is None

    def test_role_from_ain(self):
        assert role_from_ain(OLD_AIN) == ROLE

    def test_role_from_ain_encrypted(self):
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        assert role_from_ain(ain) is None

    def test_org_from_ain_old(self):
        assert org_from_ain(OLD_AIN) == ORG

    def test_org_from_ain_encrypted(self):
        """org 在公开层，加密格式也能提取"""
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        assert org_from_ain(ain) == ORG

    def test_invalid_ain_returns_none(self):
        assert ain_to_base_segment("invalid") is None
        assert instance_from_ain("invalid") is None
        assert role_from_ain("invalid") is None
        assert org_from_ain("invalid") is None


# ── 加密函数 ────────────────────────────────────────

class TestEncryptDecrypt:
    def test_roundtrip(self):
        plain = "cn-hf-management:biz"
        encrypted = encrypt_segment(plain, LAYER2_KEY)
        assert encrypted != plain
        decrypted = decrypt_segment(encrypted, LAYER2_KEY)
        assert decrypted == plain

    def test_different_ciphertexts(self):
        """每次加密结果不同（nonce 随机）"""
        plain = "same-text"
        e1 = encrypt_segment(plain, LAYER2_KEY)
        e2 = encrypt_segment(plain, LAYER2_KEY)
        assert e1 != e2

    def test_decrypt_wrong_key(self):
        plain = "secret-data"
        encrypted = encrypt_segment(plain, LAYER2_KEY)
        wrong_key = os.urandom(32)
        assert decrypt_segment(encrypted, wrong_key) is None

    def test_decrypt_invalid_base64(self):
        assert decrypt_segment("!!!invalid!!!", LAYER2_KEY) is None

    def test_decrypt_empty(self):
        assert decrypt_segment("", LAYER2_KEY) is None

    def test_layer2_encrypt(self):
        plain = "cn-hf-management:biz"
        encrypted = encrypt_segment(plain, LAYER2_KEY)
        # base64 编码的 nonce(12) + ciphertext
        data = base64.urlsafe_b64decode(encrypted)
        assert len(data) > 12  # nonce + ct
        nonce = data[:12]
        assert len(nonce) == 12


# ── 加密 AIN 边界案例 ───────────────────────────────

class TestEncryptedAINEdgeCases:
    def test_various_roles(self):
        for role in list(VALID_ROLES)[:5]:  # 取前 5 个角色测试
            ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, role, INSTANCE,
                               encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
            parsed = parse_ain_decrypt(ain, LAYER2_KEY, LAYER3_KEY)
            assert parsed is not None
            assert parsed["role"] == role
            assert parsed["domain"] == role.split(":")[0]
            assert parsed["role_code"] == role.split(":")[1]

    def test_different_instances(self):
        for inst in ["001", "042", "999"]:
            ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, inst,
                               encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
            parsed = parse_ain_decrypt(ain, LAYER2_KEY, LAYER3_KEY)
            assert parsed is not None
            assert parsed["instance"] == inst

    def test_base_name_with_hyphen(self):
        """底座名含连字符"""
        ain = generate_ain(ORG, COUNTRY, CITY, "my-resolver", ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        parsed = parse_ain_decrypt(ain, LAYER2_KEY, LAYER3_KEY)
        assert parsed is not None
        assert parsed["base_name"] == "my-resolver"

    def test_backward_compat_parse(self):
        """旧格式和新格式都能被 parse_ain 识别"""
        old = parse_ain(OLD_AIN)
        assert old is not None
        assert old["encrypted"] is False

        enc = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        new = parse_ain(enc)
        assert new is not None
        assert new["encrypted"] is True

    def test_multichar_country_code(self):
        """多字符国家码（如 ae-dubai）正确解析"""
        ain = generate_ain(ORG, "ae", "dubai", BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        parsed = parse_ain_decrypt(ain, LAYER2_KEY, LAYER3_KEY)
        assert parsed is not None
        assert parsed["country"] == "ae"
        assert parsed["city"] == "dubai"
        assert parsed["base_name"] == BASE_NAME
        assert parsed["role"] == ROLE


# ── 正则匹配 ────────────────────────────────────────

class TestPatterns:
    def test_ain_pattern_full(self):
        m = AIN_PATTERN.match("ain:1:acssa:cn-hf-management:biz:buyer:001")
        assert m is not None
        assert m.group(1) == "1"
        assert m.group(2) == "acssa"
        assert m.group(3) == "cn"
        assert m.group(4) == "hf"
        assert m.group(5) == "management"
        assert m.group(6) == "biz:buyer"
        assert m.group(7) == "001"

    def test_encrypted_pattern(self):
        """加密 AIN 格式匹配"""
        enc_layer2 = encrypt_segment("cn-hf-management:biz", LAYER2_KEY)
        enc_layer3 = encrypt_segment("buyer:001", LAYER3_KEY)
        encrypted_ain = f"ain:1:acssa.{enc_layer2}.{enc_layer3}"
        m = ENCRYPTED_AIN_PATTERN.match(encrypted_ain)
        assert m is not None
        assert m.group(1) == "1"
        assert m.group(2) == "acssa"
        assert m.group(3) == enc_layer2
        assert m.group(4) == enc_layer3

    def test_encrypted_not_match_old_pattern(self):
        """加密 AIN 不应被旧格式匹配到"""
        ain = generate_ain(ORG, COUNTRY, CITY, BASE_NAME, ROLE, INSTANCE,
                           encrypt=True, layer2_key=LAYER2_KEY, layer3_key=LAYER3_KEY)
        assert AIN_PATTERN.match(ain) is None
