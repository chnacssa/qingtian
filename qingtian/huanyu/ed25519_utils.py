"""
Ed25519 密钥工具 — QACP 证书体系基础
密钥生成、签名、验签、PEM 导出
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def generate_keypair() -> tuple[bytes, bytes]:
    """生成 Ed25519 密钥对 → (private_key_bytes, public_key_bytes)"""
    sk = ed25519.Ed25519PrivateKey.generate()
    pk = sk.public_key()
    raw_private = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw_private, raw_public


def sign_data(private_key_hex: str, message: str | bytes) -> str:
    """Ed25519 签名 — hex 编码私钥 → base64 签名（agent_credential 使用）"""
    private_key_bytes = bytes.fromhex(private_key_hex)
    return sign_message(private_key_bytes, message)


def sign_message(private_key_bytes: bytes, message: str | bytes) -> str:
    """Ed25519 签名 → base64 字符串"""
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    if isinstance(message, str):
        message = message.encode()
    sig = sk.sign(message)
    return base64.b64encode(sig).decode()


def verify_signature(public_key_bytes: bytes, message: str | bytes, signature: str) -> bool:
    """验签 → bool"""
    try:
        pk = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
        if isinstance(message, str):
            message = message.encode()
        sig_bytes = base64.b64decode(signature)
        pk.verify(sig_bytes, message)
        return True
    except Exception:
        return False


def public_key_to_pem(public_key_bytes: bytes) -> str:
    """Ed25519 公钥 → SubjectPublicKeyInfo PEM"""
    pk = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
    return pk.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def public_key_from_pem(pem: str) -> bytes:
    """PEM → Ed25519 公钥原始字节"""
    pk = serialization.load_pem_public_key(pem.encode())
    return pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def private_key_to_pem(private_key_bytes: bytes) -> str:
    """Ed25519 私钥 → PKCS8 PEM"""
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    return sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def private_key_from_pem(pem: str) -> bytes:
    """PKCS8 PEM → Ed25519 私钥原始字节（C7/R11: 私钥持久化回读）"""
    sk = serialization.load_pem_private_key(pem.encode(), password=None)
    return sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
