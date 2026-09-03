"""
证书管理 — QACP Ed25519 自签名证书
create / verify / revoke / CRL 缓存
"""

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from cryptography.hazmat.primitives import serialization

from . import ed25519_utils as ed

logger = logging.getLogger("huanyu.certificate")

# 证书有效期：Free 1 年，Pro 2 年，Enterprise 3 年，Alliance 5 年
TIER_VALIDITY = {"free": 365, "pro": 730, "enterprise": 1095, "alliance": 1825}

# 内存吊销列表
_revoked: set[str] = set()
_revoked_updated_at: Optional[datetime] = None


def _cert_fingerprint(cert_body: dict) -> str:
    """计算证书指纹（SHA256，不含 signature 字段）"""
    body = {k: v for k, v in cert_body.items() if k != "signature"}
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def create_self_signed_cert(ain: str, private_key_bytes: bytes, tier: str = "free") -> dict:
    """生成自签名证书 → JSON 证书体

    使用 Ed25519 私钥对证书体自签名，公钥从私钥派生并存入证书。
    Returns dict with ain, public_key, tier, issued_at, expires_at, signature, fingerprint
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed

    now = datetime.now(timezone.utc)
    days = TIER_VALIDITY.get(tier, 365)
    expires = now + timedelta(days=days)

    sk = _ed.Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    pk = sk.public_key()
    public_key_bytes = pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_key_pem = ed.public_key_to_pem(public_key_bytes)

    body = {
        "ain": ain,
        "public_key": public_key_pem,
        "tier": tier,
        "issued_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }

    # 自签名：用私钥对证书体 JSON 签名
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    signature = ed.sign_message(private_key_bytes, payload)
    body["signature"] = signature

    fingerprint = _cert_fingerprint(body)
    body["fingerprint"] = fingerprint

    return body


def verify_cert(cert: dict) -> bool:
    """验证证书：签名 + 有效期"""
    signature = cert.get("signature", "")
    if not signature:
        return False

    # 验有效期
    try:
        expires_at = datetime.fromisoformat(cert.get("expires_at", ""))
        # P2 (R11): naive expires_at（无 tzinfo）与 aware now 直接比较会抛
        # TypeError，被 except 吞掉 → 证书恒判无效。统一时区：naive 视为 UTC。
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            return False
    except (ValueError, TypeError):
        return False

    # 验吊销（CRL）— 吊销的证书即使签名有效也拒绝
    if is_revoked(_cert_fingerprint(cert)):
        return False

    # 验签名
    body = {k: v for k, v in cert.items() if k not in ("signature", "fingerprint")}
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)

    # review(2026-08-24 P0-2): 新版证书由签发方签名（issuer_public_key 携带签发方
    # 公钥，证书 public_key 绑定持有者）；legacy 自签证书回退用持有者公钥验自身。
    # 注意：本地默认签发方的信任锚是本底座自身（DefaultCredentialProvider），
    # 跨企业场景必须换 GB/Z 185 注册服务方凭证，不接受外来证书自述的 issuer。
    issuer_pem = cert.get("issuer_public_key", "")
    public_key_pem = issuer_pem or cert.get("public_key", "")
    if not public_key_pem:
        return False

    try:
        public_key_bytes = ed.public_key_from_pem(public_key_pem)
        return ed.verify_signature(public_key_bytes, payload, signature)
    except Exception:
        return False


def revoke_cert(cert_fingerprint: str) -> None:
    """吊销证书"""
    _revoked.add(cert_fingerprint)
    logger.warning("cert revoked: %s", cert_fingerprint)


def is_revoked(cert_fingerprint: str) -> bool:
    """检查证书是否已吊销"""
    return cert_fingerprint in _revoked


def get_crl() -> list[str]:
    """获取当前吊销列表"""
    return sorted(_revoked)


async def refresh_crl_from_db():
    """从 DB 加载吊销列表到内存缓存"""
    global _revoked, _revoked_updated_at
    try:
        from common.db import get_pool
        from . import config as hcfg

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT cert_fingerprint FROM {hcfg.get_schema_name()}.cert_revocations"
            )
            _revoked = {r["cert_fingerprint"] for r in rows}
            _revoked_updated_at = datetime.now(timezone.utc)
    except Exception:
        logger.exception("failed to refresh CRL from DB")
