"""
通用密码哈希工具 — PBKDF2-SHA256

用法:
  from common.password import hash_password, verify_password, validate_password_strength

  pw_hash = hash_password("mypassword")        # 返回 "$pbkdf2$<salt_hex>$<hash_hex>"
  ok = verify_password("mypassword", pw_hash)  # True / False
  ok, err = validate_password_strength("abc")  # False, "密码至少 8 位"
"""

import hashlib
import os
import re
import secrets


_HASH_ITERATIONS = 600_000
_HASH_LENGTH = 32  # bytes
_SALT_LENGTH = 16  # bytes


def validate_password_strength(password: str) -> tuple[bool, str | None]:
    """验证密码强度。返回 (是否通过, 错误信息)。

    规则：
    - 长度 ≥ 8 位
    - 必须包含至少一个字母
    - 必须包含至少一个数字
    """
    if len(password) < 8:
        return False, "密码至少 8 位"
    if not re.search(r"[a-zA-Z]", password):
        return False, "密码必须包含至少一个字母"
    if not re.search(r"\d", password):
        return False, "密码必须包含至少一个数字"
    return True, None


def hash_password(password: str) -> str:
    """对密码进行 PBKDF2-SHA256 哈希，返回可存储的字符串。"""
    salt = os.urandom(_SALT_LENGTH)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS, dklen=_HASH_LENGTH)
    return f"$pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """验证密码是否匹配已存储的哈希字符串。"""
    if stored is None:
        return False
    try:
        _, algo, salt_hex, hash_hex = stored.split("$", 3)
        if algo != "pbkdf2":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _HASH_ITERATIONS, dklen=len(expected))
        return secrets.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False
