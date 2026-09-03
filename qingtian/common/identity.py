"""
身份管理模块 — 对标 GB/Z 185.2 + 185.3

设计原则: 抽象接口定义 → 默认简易实现 → 国标对接桩(TODO)。
国标基础设施就绪后，只需替换实现类，调用方代码不受影响。

结构:
  IdentityProvider (抽象)    — 身份码生成 + 国标映射
  CredentialProvider (抽象)  — 凭证签发/更新/吊销
  AuthProvider (抽象)        — 身份鉴别协议

默认实现: 自签 AIN + Ed25519 自签名证书 + 简单签名验证
国标桩:   接口一致，内部标记 TODO，等待注册服务方/凭证发行方/验证方就绪
"""

from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("huanyu.identity")


# ═══════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════

from dataclasses import dataclass, field

@dataclass
class IdentityResult:
    """身份码生成结果"""
    ain: str                              # ACSSA AIN（始终有效）
    gbz_identity_code: str = ""           # GB/Z 185.2 身份码（注册服务方签发后填入）
    registrar: str = ""                   # 签发此码的注册服务方
    mapped: bool = False                  # 是否已建立国标映射


@dataclass
class CredentialResult:
    """凭证操作结果"""
    certificate: dict                     # 证书体
    fingerprint: str                      # SHA-256 指纹
    issued_at: str
    expires_at: str
    revoked: bool = False


@dataclass
class AuthChallenge:
    """身份鉴别 — 挑战"""
    challenge_id: str                     # 本次鉴别唯一标识
    random_nonce: str                     # 动态随机数
    auth_policy: str = "ed25519_signature"  # 鉴别策略


@dataclass
class AuthAssertion:
    """身份鉴别 — 断言"""
    challenge_id: str
    result: str                           # success / failed / need_further_verification
    agent_ain: str = ""
    verified_at: str = ""
    detail: str = ""


# ═══════════════════════════════════════════════════════
# 抽象接口
# ═══════════════════════════════════════════════════════

class IdentityProvider(ABC):
    """身份码管理 — 对标 GB/Z 185.2"""

    @abstractmethod
    async def generate_identity(self, org: str, country: str, city: str,
                                base_name: str, role: str) -> IdentityResult:
        """生成 AIN + 预留国标映射位"""
        ...

    @abstractmethod
    async def map_to_gbz(self, ain: str, gbz_identity_code: str,
                         registrar: str) -> bool:
        """建立 AIN ↔ GB/Z 185.2 身份码一对一映射"""
        ...

    @abstractmethod
    async def resolve(self, identifier: str) -> Optional[IdentityResult]:
        """通过 AIN 或国标码反查身份信息"""
        ...


class CredentialProvider(ABC):
    """凭证管理 — 对标 GB/Z 185.3 第8章"""

    @abstractmethod
    async def issue(self, ain: str, public_key_bytes: bytes,
                    tier: str = "free") -> CredentialResult:
        """签发凭证"""
        ...

    @abstractmethod
    async def renew(self, certificate_fingerprint: str) -> CredentialResult:
        """更新凭证"""
        ...

    @abstractmethod
    async def revoke(self, certificate_fingerprint: str) -> bool:
        """吊销凭证"""
        ...

    @abstractmethod
    async def check_status(self, certificate_fingerprint: str) -> dict:
        """查询凭证状态（有效/已吊销/已过期/已锁定）"""
        ...


class AuthProvider(ABC):
    """身份鉴别 — 对标 GB/Z 185.3 第9章"""

    @abstractmethod
    async def create_challenge(self, agent_ain: str) -> AuthChallenge:
        """生成鉴别挑战（动态随机数）"""
        ...

    @abstractmethod
    async def verify(self, challenge: AuthChallenge,
                     process_credential_package: dict) -> AuthAssertion:
        """验证过程凭证包 → 生成鉴别断言"""
        ...


# ═══════════════════════════════════════════════════════
# 默认实现：自签 AIN + Ed25519 自签名证书
# ═══════════════════════════════════════════════════════

import hashlib
import secrets
import json

from huanyu.ain import generate_ain, parse_ain
from huanyu.certificate import create_self_signed_cert, verify_cert, revoke_cert, is_revoked


class DefaultIdentityProvider(IdentityProvider):
    """默认身份码管理 — 自签 AIN，国标映射留空"""

    def __init__(self):
        # ain → [(gbz_identity_code, issuer, issued_at, expires_at), ...]
        self._mappings: dict[str, list[tuple[str, str, str, str]]] = {}
        # gbz → ain (反向索引，用于 resolve)
        self._reverse: dict[str, str] = {}

    async def generate_identity(self, org: str, country: str, city: str,
                                base_name: str, role: str) -> IdentityResult:
        ain = generate_ain(org, country, city, base_name, role, "001")
        return IdentityResult(ain=ain, mapped=False)

    async def map_to_gbz(self, ain: str, gbz_identity_code: str,
                         registrar: str = "") -> bool:
        if ain not in self._mappings:
            self._mappings[ain] = []
        now = datetime.now(timezone.utc).isoformat()
        self._mappings[ain].append((gbz_identity_code, registrar, now, ""))
        self._reverse[gbz_identity_code] = ain
        logger.info("AIN %s mapped to GB/Z identity %s by %s", ain[:20], gbz_identity_code[:30], registrar)
        return True

    async def resolve(self, identifier: str) -> Optional[IdentityResult]:
        """解析身份——有国标码返回，没有只返回 AIN，永不抛异常"""
        ain = identifier if identifier.startswith("ain:") else self._reverse.get(identifier)
        if not ain:
            return None
        gbz_list = self._mappings.get(ain, [])
        gbz = gbz_list[0][0] if gbz_list else ""  # 取第一个国标码
        return IdentityResult(ain=ain, gbz_identity_code=gbz, mapped=bool(gbz))


class DefaultCredentialProvider(CredentialProvider):
    """默认凭证管理 — Ed25519 证书（本地签发方签名）

    review(2026-08-24 P0-2 修复): 原 issue() 无视传入的申请者公钥，内部生成
    新密钥对自签后把私钥丢弃——证书公钥与申请者无任何绑定，凭证不可用且逻辑错误。
    现改为：证书公钥 = 申请者公钥（形成"签发方→持有者"绑定），由本地签发方
    密钥对证书体签名（issuer_public_key 随证书携带，verify_cert 优先用它验签）。
    持有者证明私钥持有由 DefaultAuthProvider.verify 的挑战签名完成。
    """

    async def issue(self, ain: str, public_key_bytes: bytes,
                    tier: str = "free") -> CredentialResult:
        from cryptography.hazmat.primitives import serialization
        from huanyu import ed25519_utils as ed
        from huanyu.certificate import TIER_VALIDITY, _cert_fingerprint
        from datetime import timedelta

        if not public_key_bytes:
            raise ValueError("issue 需要申请者公钥（public_key_bytes），不能为空")

        # P1 (2026-08-27 review #9): 本 Provider 是**纯自证**简化实现——issuer
        # 密钥对每次现场生成且公钥嵌证书本体内，任何人都能为任意 AIN 签出
        # 通过 verify_cert 的"合法"证书。仅适用于单租户自管/开发环境。
        # 生产联邦互信必须配置正式 CredentialProvider（注册服务方签发）。
        # 设 QINGTIAN_FORBID_SELF_SIGNED=1 可硬禁用本实现（fail-closed）。
        import os as _os
        if _os.environ.get("QINGTIAN_FORBID_SELF_SIGNED", "") == "1":
            raise PermissionError(
                "DefaultCredentialProvider 自签证书已被 QINGTIAN_FORBID_SELF_SIGNED 禁用，"
                "请配置正式凭证签发服务"
            )
        logger.warning(
            "DefaultCredentialProvider.issue: 自签证书（纯自证，无外部信任锚）ain=%s tier=%s"
            "——生产联邦互信请配置正式 CredentialProvider", ain, tier,
        )

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=TIER_VALIDITY.get(tier, 365))
        issuer_sk, issuer_pk = ed.generate_keypair()
        body = {
            "ain": ain,
            "public_key": ed.public_key_to_pem(public_key_bytes),
            "tier": tier,
            "issued_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "issuer": "local-default",
            "issuer_public_key": ed.public_key_to_pem(issuer_pk),
        }
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
        body["signature"] = ed.sign_message(issuer_sk, payload)
        body["fingerprint"] = _cert_fingerprint(body)
        return CredentialResult(
            certificate=body,
            fingerprint=body["fingerprint"],
            issued_at=body["issued_at"],
            expires_at=body["expires_at"],
        )

    async def renew(self, certificate_fingerprint: str) -> CredentialResult:
        logger.warning("DefaultCredentialProvider.renew: 自签证书不支持更新——请向注册服务方申请")
        raise NotImplementedError("自签证书不支持更新。请通过注册服务方获取正式凭证。")

    async def revoke(self, certificate_fingerprint: str) -> bool:
        revoke_cert(certificate_fingerprint)
        return True

    async def check_status(self, certificate_fingerprint: str) -> dict:
        revoked = is_revoked(certificate_fingerprint)
        return {"fingerprint": certificate_fingerprint, "revoked": revoked, "status": "revoked" if revoked else "active"}


class DefaultAuthProvider(AuthProvider):
    """默认身份鉴别 — 简单 Ed25519 签名校验"""

    async def create_challenge(self, agent_ain: str) -> AuthChallenge:
        nonce = secrets.token_hex(16)
        challenge_id = hashlib.sha256(f"{agent_ain}:{nonce}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:16]
        return AuthChallenge(challenge_id=challenge_id, random_nonce=nonce)

    async def verify(self, challenge: AuthChallenge,
                     process_credential_package: dict) -> AuthAssertion:
        cert = process_credential_package.get("certificate", {})
        signature = process_credential_package.get("signature", "")
        if not cert or not signature:
            return AuthAssertion(challenge_id=challenge.challenge_id, result="failed", detail="missing certificate or signature")
        if not verify_cert(cert):
            return AuthAssertion(challenge_id=challenge.challenge_id, result="failed", detail="certificate verification failed")
        if is_revoked(cert.get("fingerprint", "")):
            return AuthAssertion(challenge_id=challenge.challenge_id, result="failed", detail="certificate revoked")
        # review(2026-08-24 P0-2 修复): 原实现取出 signature 后从不验签——鉴别协议
        # 纯装饰，任何人对任意 AIN 自签证书即可冒充。现验"持有证书私钥的证明"：
        # 用证书内公钥验证对挑战随机数（random_nonce）的 Ed25519 签名。
        from huanyu import ed25519_utils as _ed
        public_key_pem = cert.get("public_key", "")
        try:
            _pk = _ed.public_key_from_pem(public_key_pem)
            if not _ed.verify_signature(_pk, challenge.random_nonce, signature):
                return AuthAssertion(
                    challenge_id=challenge.challenge_id, result="failed",
                    detail="challenge signature verification failed",
                )
        except Exception as e:
            return AuthAssertion(
                challenge_id=challenge.challenge_id, result="failed",
                detail=f"challenge signature error: {e}",
            )
        return AuthAssertion(
            challenge_id=challenge.challenge_id,
            result="success",
            agent_ain=cert.get("ain", ""),
            verified_at=datetime.now(timezone.utc).isoformat(),
        )


# ═══════════════════════════════════════════════════════
# GB/Z 185 对接桩 — 等基础设施就绪后替换实现
# ═══════════════════════════════════════════════════════

class GBZIdentityProvider(IdentityProvider):
    """GB/Z 185.2 对接 — 待注册服务方就绪后启用"""

    async def generate_identity(self, org: str, country: str, city: str,
                                base_name: str, role: str) -> IdentityResult:
        raise NotImplementedError(
            "GB/Z 185.2 身份码生成需要注册服务方支持。"
            "请确保已在国标注册服务方完成备案，"
            "并在配置中设置 gbz.registration_server 地址。"
            "当前可使用 DefaultIdentityProvider 通过自签 AIN 运行。"
        )


class GBZCredentialProvider(CredentialProvider):
    """GB/Z 185.3 凭证管理对接 — 待凭证发行方就绪后启用"""

    async def issue(self, ain: str, public_key_bytes: bytes,
                    tier: str = "free") -> CredentialResult:
        raise NotImplementedError(
            "GB/Z 185.3 凭证签发需要凭证发行方支持。"
            "当前可使用 DefaultCredentialProvider 通过 Ed25519 自签名证书运行。"
        )

    async def renew(self, certificate_fingerprint: str) -> CredentialResult:
        raise NotImplementedError("GB/Z 185.3 凭证发行方尚未就绪")

    async def revoke(self, certificate_fingerprint: str) -> bool:
        raise NotImplementedError("GB/Z 185.3 凭证发行方尚未就绪")

    async def check_status(self, certificate_fingerprint: str) -> dict:
        raise NotImplementedError("GB/Z 185.3 凭证发行方尚未就绪")


class GBZAuthProvider(AuthProvider):
    """GB/Z 185.3 身份鉴别对接 — 待身份验证方就绪后启用"""

    async def create_challenge(self, agent_ain: str) -> AuthChallenge:
        raise NotImplementedError(
            "GB/Z 185.3 身份鉴别需要身份验证方支持。"
            "当前可使用 DefaultAuthProvider 通过 Ed25519 签名验证运行。"
        )

    async def verify(self, challenge: AuthChallenge,
                     process_credential_package: dict) -> AuthAssertion:
        raise NotImplementedError("GB/Z 185.3 身份验证方尚未就绪")


# ═══════════════════════════════════════════════════════
# 工厂 — 配置驱动切换
# ═══════════════════════════════════════════════════════

_identity_provider: Optional[IdentityProvider] = None
_credential_provider: Optional[CredentialProvider] = None
_auth_provider: Optional[AuthProvider] = None


def get_identity_provider() -> IdentityProvider:
    global _identity_provider
    if _identity_provider is None:
        # TODO: 国标基础设施就绪后从配置文件读取，切换为 GBZIdentityProvider
        _identity_provider = DefaultIdentityProvider()
    return _identity_provider


def get_credential_provider() -> CredentialProvider:
    global _credential_provider
    if _credential_provider is None:
        _credential_provider = DefaultCredentialProvider()
    return _credential_provider


def get_auth_provider() -> AuthProvider:
    global _auth_provider
    if _auth_provider is None:
        _auth_provider = DefaultAuthProvider()
    return _auth_provider
