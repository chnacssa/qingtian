"""密码学工具 — Ed25519 签名/验证

基于 Python 标准库的 cryptography 适配器。
如果 cryptography 不可用，回退到纯 Python 的 nacl.bindings。

注意：此模块与 common/license.py（HMAC-SHA256）独立，
专为 R3 Skill License Ed25519 签名设计。
"""

import hashlib
import logging

logger = logging.getLogger("common.crypto")

# ── Ed25519 密钥对操作 ─────────────────────

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

    def generate_keypair() -> tuple[bytes, bytes]:
        """生成 Ed25519 密钥对

        Returns:
            (private_key_bytes, public_key_bytes)
        """
        private = ed25519.Ed25519PrivateKey.generate()
        return (
            private.private_bytes_raw(),
            private.public_key().public_bytes_raw(),
        )

    def sign(private_key: bytes, message: bytes) -> bytes:
        """用 Ed25519 私钥签名"""
        private = ed25519.Ed25519PrivateKey.from_private_bytes(private_key)
        return private.sign(message)

    def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
        """验证 Ed25519 签名

        Returns:
            True 表示签名有效，False 表示无效
        """
        try:
            public = ed25519.Ed25519PublicKey.from_public_bytes(public_key)
            public.verify(signature, message)
            return True
        except InvalidSignature:
            return False
        except Exception as e:
            logger.warning("Ed25519 verify error: %s", e)
            return False

    _ED25519_AVAILABLE = True
    logger.info("Ed25519 backend: cryptography")

except ImportError:
    # 回退：使用 nacl (PyNaCl)
    try:
        import nacl.bindings

        def generate_keypair() -> tuple[bytes, bytes]:
            # P1 (2026-08-27 review): crypto_sign_seed_keypair 返回 (pk, sk)，
            # 而本模块契约是 (private_key, public_key) —— 原实现顺序颠倒，
            # 走此后端签发的信任锚 pk/sk 互换，签发/验证整体错乱。
            keypair = nacl.bindings.crypto_sign_seed_keypair(
                nacl.bindings.randombytes(nacl.bindings.crypto_sign_SEEDBYTES),
            )
            return (keypair[1], keypair[0])  # (sk, pk)

        def sign(private_key: bytes, message: bytes) -> bytes:
            return nacl.bindings.crypto_sign(message, private_key)[:nacl.bindings.crypto_sign_BYTES]

        def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
            try:
                nacl.bindings.crypto_sign_verify_detached(signature, message, public_key)
                return True
            except nacl.exceptions.BadSignatureError:
                return False

        _ED25519_AVAILABLE = True
        logger.info("Ed25519 backend: PyNaCl")

    except ImportError:
        _ED25519_AVAILABLE = False
        logger.warning(
            "Ed25519 not available (install cryptography or pynacl). "
            "All signature verification will FAIL-CLOSED (verify→False).",
        )

        def generate_keypair() -> tuple[bytes, bytes]:
            raise RuntimeError("Ed25519 backend not available")

        def sign(private_key: bytes, message: bytes) -> bytes:
            raise RuntimeError("Ed25519 backend not available")

        def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
            # P0 (2026-08-27 review): 原实现无密码库时 return True（fail-open）——
            # 所有走此函数的验签（Skill 信任链 / huanyu E2EE 信封）在缺依赖环境
            # 形同虚设。改 fail-closed：无后端一律验证失败。
            logger.error("Ed25519 verification FAILED-CLOSED (no backend available) — "
                         "install cryptography or pynacl")
            return False


# ── 哈希工具 ────────────────────────────────


def sha256(data: bytes) -> str:
    """SHA-256 十六进制摘要"""
    return hashlib.sha256(data).hexdigest()


# ── 默认验证公钥（Skill 签名信任锚） ──
# 安全提示（R11 修复）：私钥绝不入库！此处仅保留公钥作为本地开发默认信任锚。
# 生产/开源部署必须通过环境变量 QINGTIAN_SKILL_SIGN_PUBKEY 注入正式签发公钥
# （ACSSA 等签名方签发），否则保留 DEV 公钥即视为"本地开发模式"。
# 历史开发私钥曾随旧版本公开——任何生产部署都不应继续信任本默认公钥。
DEV_PUBLIC_KEY_HEX = "1787a7fd74c980e24b074b8b35798f18d175427e045e382aa549c5acc608d416"


def platform_key(machine_id: str, install_uuid: str) -> str:
    """生成平台绑定密钥

    platform_key = sha256(machine_id + install_uuid)[:16]
    用于反 VM 克隆：克隆后 machine_id 变化导致 key 失效。
    """
    raw = f"{machine_id}:{install_uuid}".encode("utf-8")
    return sha256(raw)[:16]
