"""
智能体凭证 — 对标 GB/Z 185.3-2026

标准凭证格式: 身份声明 + 能力声明 + 有效期 + Ed25519数字签名
"""
from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime, timezone, timedelta

from .ed25519_utils import sign_data, verify_signature


# ═══════════════════════════════════════════
# GB/Z 185.3 Agent Credential
# ═══════════════════════════════════════════

class AgentCredential:
    """智能体凭证 — 对标 GB/Z 185.3"""

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        public_key_hex: str,
        capabilities: list[str] | None = None,
        issuer: str = "",
        validity_hours: int = 8760,  # 默认 1 年
    ):
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.public_key = public_key_hex
        self.capabilities = capabilities or []
        self.issuer = issuer  # 注册服务方标识
        self.issued_at = datetime.now(timezone.utc)
        self.expires_at = self.issued_at + timedelta(hours=validity_hours)
        self.signature = ""

    def to_dict(self, include_signature: bool = True) -> dict:
        """转为标准凭证 JSON"""
        data = {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "public_key": self.public_key,
            "capabilities": self.capabilities,
            "issuer": self.issuer,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
        if include_signature and self.signature:
            data["signature"] = self.signature
        return data

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def sign(self, private_key_hex: str) -> str:
        """用签发方私钥对凭证签名"""
        payload = json.dumps({
            "agent_id": self.agent_id,
            "public_key": self.public_key,
            "capabilities": self.capabilities,
            "issuer": self.issuer,
            "expires_at": self.expires_at.isoformat(),
        }, sort_keys=True, ensure_ascii=False)
        self.signature = sign_data(private_key_hex, payload.encode())
        return self.signature

    def verify(self) -> bool:
        """验证凭证签名有效性"""
        if not self.signature:
            return False
        if datetime.now(timezone.utc) > self.expires_at:
            return False  # 已过期
        payload = json.dumps({
            "agent_id": self.agent_id,
            "public_key": self.public_key,
            "capabilities": self.capabilities,
            "issuer": self.issuer,
            "expires_at": self.expires_at.isoformat(),
        }, sort_keys=True, ensure_ascii=False)
        return verify_signature(self.public_key, payload.encode(), self.signature)

    @classmethod
    def from_dict(cls, data: dict) -> AgentCredential:
        """从字典还原凭证对象"""
        cred = cls(
            agent_id=data["agent_id"],
            agent_name=data.get("agent_name", ""),
            public_key_hex=data["public_key"],
            capabilities=data.get("capabilities", []),
            issuer=data.get("issuer", ""),
        )
        if "issued_at" in data:
            cred.issued_at = datetime.fromisoformat(data["issued_at"])
        if "expires_at" in data:
            cred.expires_at = datetime.fromisoformat(data["expires_at"])
        cred.signature = data.get("signature", "")
        return cred


def issue_credential(
    agent_id: str,
    agent_name: str,
    public_key_hex: str,
    capabilities: list[str],
    issuer_private_key_hex: str,
    validity_hours: int = 8760,
) -> AgentCredential:
    """签发智能体凭证（管理服调用）"""
    cred = AgentCredential(
        agent_id=agent_id,
        agent_name=agent_name,
        public_key_hex=public_key_hex,
        capabilities=capabilities,
        validity_hours=validity_hours,
    )
    cred.sign(issuer_private_key_hex)
    return cred


def verify_credential(cred_dict: dict) -> tuple[bool, str]:
    """验证凭证（客户端调用）"""
    try:
        cred = AgentCredential.from_dict(cred_dict)
        if not cred.verify():
            return False, "签名验证失败或凭证已过期"
        return True, "ok"
    except Exception as e:
        return False, f"凭证解析失败: {e}"
