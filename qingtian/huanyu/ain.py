"""
AIN (Agent Identity Number) 工具模块
QACP v0.4 标准 — 格式: ain:版本:组织:国家-城市-底座:域:角色:实例

v2.0 (IPC-014): 支持 AES-256-GCM 分层加密。
传输格式: ain:版本:组织.<b64层2密文>.<b64层3密文>
"""

import base64
import os
import re
from typing import Optional

from common.db import get_pool

from . import config as hcfg

# ── 正则 ──────────────────────────────────────────────

# AIN 格式正则（角色码 = 领域:角色，如 biz:buyer）
AIN_PATTERN = re.compile(
    r'^ain:(\d+):([a-z0-9][a-z0-9-]{0,31}):([a-z]{2})-([a-z]{2,6})-([a-z0-9][a-z0-9-]{0,23}):'
    r'([a-z][a-z0-9-]{0,15}:[a-z][a-z0-9-]{0,31}):([a-z0-9][a-z0-9-]{0,35})$'
)

# IPC-014 加密传输格式: ain:版本:组织.<b64层2>.<b64层3>
ENCRYPTED_AIN_PATTERN = re.compile(
    r'^ain:(\d+):([a-z0-9][a-z0-9-]{0,31})\.'
    r'([A-Za-z0-9+/=_-]+)\.([A-Za-z0-9+/=_-]+)$'
)

# 标准角色码 — 领域:角色 双层结构
# 领域可无限扩展（当前三类，未来可加 med/edu/gov 等），角色由 ACSA 技术委员会维护
VALID_ROLES = frozenset({
    # 商业领域 (biz)
    'biz:buyer', 'biz:seller', 'biz:broker', 'biz:inspector',
    # 基础设施领域 (infra)
    'infra:scheduler', 'infra:monitor', 'infra:resolver', 'infra:notifier',
    'infra:gateway', 'infra:archive', 'infra:finance',
    # 系统领域 (sys)
    'sys:admin', 'sys:root', 'sys:observer', 'sys:bridge',
})


# ── AES-256-GCM 加密工具 ─────────────────────────────


def encrypt_segment(plaintext: str, key: bytes) -> str:
    """AES-256-GCM 加密单段，返回 base64(nonce + ciphertext)"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_segment(encrypted: str, key: bytes) -> Optional[str]:
    """解密 base64(nonce + ciphertext)，失败返回 None"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        data = base64.urlsafe_b64decode(encrypted)
        nonce, ct = data[:12], data[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
    except Exception:
        return None


# ── AIN 核心函数 ────────────────────────────────────────


def generate_identity_code(org: str, local_ain: str = "") -> str:
    """生成 GB/Z 185.2 标准身份码: <注册服务方标识>/<智能体本地标识>"""
    return f"{org}/{local_ain}"


def generate_ain(org: str, country: str, city: str,
                 base_name: str, role: str, instance: str,
                 encrypt: bool = False,
                 layer2_key: Optional[bytes] = None,
                 layer3_key: Optional[bytes] = None) -> str:
    """生成 AIN 字符串

    role 需带领域前缀（如 'biz:buyer'）。
    encrypt=True 时输出 IPC-014 加密格式:
      ain:版本:组织.<b64层2>.<b64层3>
    其中层1=公开(org)，层2=底座+领域，层3=角色码+实例。
    未提供密钥时自动回退到旧格式。
    """
    if ":" not in role:
        raise ValueError(
            f"role must contain domain prefix (e.g. 'biz:buyer'), got '{role}'"
        )

    if not encrypt:
        return f"ain:1:{org}:{country}-{city}-{base_name}:{role}:{instance}"

    if not layer2_key or not layer3_key:
        return f"ain:1:{org}:{country}-{city}-{base_name}:{role}:{instance}"

    # 层1（公开层）: ain:版本:组织
    layer1 = f"ain:1:{org}"

    # 层2（内部层）: 国家-城市-底座:领域
    domain = role.split(":")[0] if ":" in role else ""
    layer2_plain = f"{country}-{city}-{base_name}:{domain}"

    # 层3（私有层）: 角色码:实例
    role_code = role.split(":")[1] if ":" in role else role
    layer3_plain = f"{role_code}:{instance}"

    enc2 = encrypt_segment(layer2_plain, layer2_key)
    enc3 = encrypt_segment(layer3_plain, layer3_key)
    return f"{layer1}.{enc2}.{enc3}"


def parse_ain(ain: str) -> Optional[dict]:
    """解析 AIN，返回各段 dict；非法格式返回 None

    旧格式（冒号分隔 7 段）→ 返回完整字段。
    加密格式（点分隔 3 层）→ 返回公开信息 + 密文 blob。
    """
    # 优先匹配旧格式
    m = AIN_PATTERN.match(ain)
    if m:
        role_full = m.group(6)  # "biz:buyer"
        domain, role_code = role_full.split(":", 1) if ":" in role_full else ("", role_full)
        return {
            "version": int(m.group(1)),
            "org": m.group(2),
            "country": m.group(3),
            "city": m.group(4),
            "base_name": m.group(5),
            "role": role_full,
            "domain": domain,
            "role_code": role_code,
            "instance": m.group(7),
            "encrypted": False,
        }

    # 尝试加密格式
    m2 = ENCRYPTED_AIN_PATTERN.match(ain)
    if m2:
        return {
            "version": int(m2.group(1)),
            "org": m2.group(2),
            "encrypted": True,
            "encrypted_layer2": m2.group(3),
            "encrypted_layer3": m2.group(4),
            # 向后兼容 — 这些字段不可用
            "country": None,
            "city": None,
            "base_name": None,
            "role": None,
            "domain": None,
            "role_code": None,
            "instance": None,
        }

    return None


def parse_ain_decrypt(ain: str,
                      layer2_key: bytes,
                      layer3_key: bytes) -> Optional[dict]:
    """解析 AIN 并解密加密层；解密失败返回 None"""
    parsed = parse_ain(ain)
    if not parsed:
        return None
    if not parsed.get("encrypted"):
        return parsed  # 已经是明文

    # 解密层2: "国家-城市-底座:领域"
    plain2 = decrypt_segment(parsed["encrypted_layer2"], layer2_key)
    if plain2 is None:
        return None
    if ":" not in plain2:
        return None
    base_seg, domain = plain2.rsplit(":", 1)

    # "国家-城市-底座" → 按 - 分割兼容多字符国家码
    seg_parts = base_seg.split("-")
    if len(seg_parts) < 3:
        return None
    country = seg_parts[0]
    city = seg_parts[1]
    base_name = "-".join(seg_parts[2:])

    # 解密层3: "角色码:实例"
    plain3 = decrypt_segment(parsed["encrypted_layer3"], layer3_key)
    if plain3 is None:
        return None
    if ":" not in plain3:
        return None
    role_code, instance = plain3.rsplit(":", 1)
    role_full = f"{domain}:{role_code}"

    return {
        "version": parsed["version"],
        "org": parsed["org"],
        "country": country,
        "city": city,
        "base_name": base_name,
        "role": role_full,
        "domain": domain,
        "role_code": role_code,
        "instance": instance,
        "encrypted": False,
    }


def validate_ain(ain: str) -> bool:
    """校验 AIN 格式和角色码；加密 AIN 无法校验角色码返回 False"""
    parsed = parse_ain(ain)
    if not parsed:
        return False
    if parsed.get("encrypted"):
        return False  # 加密 AIN 需解密后才能校验角色
    if parsed["role"] not in VALID_ROLES:
        return False
    return True


def validate_ain_format(ain: str) -> bool:
    """只校验 AIN 格式合法性（不校验角色码），加密 AIN 也通过"""
    return ENCRYPTED_AIN_PATTERN.match(ain) is not None or AIN_PATTERN.match(ain) is not None


def ain_to_base_segment(ain: str) -> Optional[str]:
    """从 AIN 提取底座段: 'cn-hf-management'；加密 AIN 返回 None"""
    parsed = parse_ain(ain)
    if not parsed or parsed.get("encrypted"):
        return None
    return f"{parsed['country']}-{parsed['city']}-{parsed['base_name']}"


def instance_from_ain(ain: str) -> Optional[str]:
    """从 AIN 提取实例号: '001'；加密 AIN 返回 None"""
    parsed = parse_ain(ain)
    if not parsed or parsed.get("encrypted"):
        return None
    return parsed["instance"]


def role_from_ain(ain: str) -> Optional[str]:
    """从 AIN 提取完整角色码（含领域前缀）；加密 AIN 返回 None"""
    parsed = parse_ain(ain)
    if not parsed or parsed.get("encrypted"):
        return None
    return parsed["role"]


def org_from_ain(ain: str) -> Optional[str]:
    """从 AIN 提取组织标识（加密和明文均可）"""
    parsed = parse_ain(ain)
    if not parsed:
        return None
    return parsed["org"]


async def next_instance(org: str, country: str, city: str,
                         base_name: str, role: str) -> str:
    """查询同一底座下已有实例数，返回下一实例号（3 位补零）"""
    base_prefix = f"ain:1:{org}:{country}-{city}-{base_name}:{role}:"
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            f"SELECT COUNT(*) FROM {hcfg.get_schema_name()}.agents "
            f"WHERE ain LIKE $1",
            base_prefix + "%",
        )
    return f"{(count or 0) + 1:03d}"
