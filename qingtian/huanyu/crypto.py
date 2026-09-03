"""寰宇 — 跨企业通讯加密基础（E2EE 地基）。

提供跨企业通讯所需的密码学原语，全部基于 `cryptography`（与 ed25519_utils 一致）：

- Ed25519 密钥对 / 签名 / 验签（复用 ed25519_utils）
- X25519 密钥对 + DH 密钥协商
- HKDF-SHA256 密钥派生
- AES-256-GCM 加解密

协议依据：docs/寰宇跨企业通讯-技术落地实施文档.md §4.1 握手协议。

安全约定：
- GCM nonce（iv）每条消息唯一，随机 96-bit（12 字节），绝不重用。
- HKDF 派生会话密钥，DH 共享秘密不直接用作密钥。
"""

import base64
import logging
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import ed25519_utils as _ed

logger = logging.getLogger("huanyu.crypto")

# 协议常量（与握手协议钉死一致）
PROTOCOL_LABEL = b"huanyu-e2ee-v1"
GCM_NONCE_SIZE = 12   # 96-bit
GCM_TAG_SIZE = 16
X25519_KEY_SIZE = 32
SESSION_KEY_SIZE = 32


# ── Ed25519（复用）─────────────────────────────────────

def generate_ed25519_keypair() -> tuple[str, str]:
    """生成企业签名密钥对 → (private_hex, public_hex)。"""
    priv, pub = _ed.generate_keypair()
    return priv.hex(), pub.hex()


def sign(priv_hex: str, message: bytes) -> str:
    """Ed25519 签名（hex 私钥，字节消息）→ base64 签名。"""
    return _ed.sign_message(bytes.fromhex(priv_hex), message)


def verify(pub_hex: str, message: bytes, signature_b64: str) -> bool:
    """Ed25519 验签（hex 公钥）→ bool。"""
    return _ed.verify_signature(bytes.fromhex(pub_hex), message, signature_b64)


def public_key_from_private(priv_hex: str) -> str:
    """由 Ed25519 私钥（hex）推导公钥（hex）。

    Hub 端验自己签发的 token 用（Ed25519 公钥由私钥确定性推导）。
    """
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv_hex))
    raw = sk.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return raw.hex()


# ── X25519（新增）──────────────────────────────────────

def generate_x25519_keypair() -> tuple[bytes, bytes]:
    """生成临时/长期 X25519 密钥对 → (private_bytes, public_bytes)，各 32 字节。"""
    priv = x25519.X25519PrivateKey.generate()
    return (
        priv.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ),
        priv.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ),
    )


def x25519_derive(private_bytes: bytes, peer_public_bytes: bytes) -> bytes:
    """DH 密钥协商：X25519(己方私钥, 对方公钥) → 32 字节共享秘密。"""
    priv = x25519.X25519PrivateKey.from_private_bytes(private_bytes)
    peer_pub = x25519.X25519PublicKey.from_public_bytes(peer_public_bytes)
    return priv.exchange(peer_pub)


# ── HKDF（新增）────────────────────────────────────────

def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = SESSION_KEY_SIZE) -> bytes:
    """HKDF-SHA256 派生密钥。ikm=DH 共享秘密，info 绑定会话上下文。"""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=length,
        salt=salt,
        info=info,
    )
    return hkdf.derive(ikm)


def derive_session_key(shared_secret: bytes, from_org: str, to_org: str,
                       nonce_a: str, nonce_b: str) -> bytes:
    """按握手协议派生会话密钥。

    session_key = HKDF-SHA256(ikm=shared, salt=PROTOCOL_LABEL,
                              info=from_org ‖ to_org ‖ nonce_a ‖ nonce_b)
    """
    info = "‖".join([from_org, to_org, nonce_a, nonce_b]).encode()
    return hkdf_sha256(shared_secret, PROTOCOL_LABEL, info)


# ── AES-256-GCM（新增）────────────────────────────────

def aes_gcm_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> dict:
    """AES-256-GCM 加密 → {iv_hex, tag_hex, cipher_b64}。

    key 必须 32 字节；iv 随机 96-bit；aad 绑定收发方（防密文重放）。
    """
    iv = os.urandom(GCM_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(iv, plaintext, aad)
    # encrypt 返回 ciphertext+tag 拼接，tag 在末尾 16 字节
    tag = ciphertext[-GCM_TAG_SIZE:]
    cipher = ciphertext[:-GCM_TAG_SIZE]
    return {
        "iv": iv.hex(),
        "tag": tag.hex(),
        "cipher": base64.b64encode(cipher).decode(),
    }


def aes_gcm_decrypt(key: bytes, iv_hex: str, tag_hex: str, cipher_b64: str, aad: bytes) -> bytes:
    """AES-256-GCM 解密 → 明文 bytes。失败抛异常（认证失败=密文被篡改）。"""
    iv = bytes.fromhex(iv_hex)
    tag = bytes.fromhex(tag_hex)
    cipher = base64.b64decode(cipher_b64)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(iv, cipher + tag, aad)


def generate_msg_nonce() -> str:
    """生成每条消息唯一的 96-bit nonce（hex 字符串）。"""
    return os.urandom(GCM_NONCE_SIZE).hex()
