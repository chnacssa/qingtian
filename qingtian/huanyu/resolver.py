"""
寰宇 — 递归解析引擎 (QACP v0.6 §3.7)
本地 agents 表 → 本国根解析器 → Global Root → 目标国根解析器

RAC (Resolver Authorization Certificate):
  ACSSA 理事会 7 席（中方 4 + 国际 3）多签，≥4 席通过即生效
  单一理事不可单独签发 RAC
"""

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from common.config import get as root_get
from common.db import get_pool
from . import ain as ain_mod
from . import config as hcfg

logger = logging.getLogger("huanyu.resolver")

SCHEMA = hcfg.get_schema_name()

# ── 理事会成员（启动时预置 ≥1 席公钥指纹，联网后拉取完整列表）──
# 格式: { seat_id: { name, public_key_pem, fingerprint } }
# 初期硬编码 ACSSA 7 席占位，Global Root 上线后从 /v1/huanyu/federation/council 拉取

_BOOTSTRAP_COUNCIL: dict[str, dict] = {}

# ── 全局配置 ────────────────────────────────────────────

def _global_root_url() -> str:
    return root_get("huanyu.global_root_url", "https://ain.acssa.cn")


def _resolver_scope() -> str:
    """本解析器授权的国家范围（ISO 3166-1 alpha-2 逗号分隔）"""
    return root_get("huanyu.resolver_scope", hcfg.get_country().upper())


# ── 数据模型 ────────────────────────────────────────────

@dataclass
class ResolutionResult:
    """AIN 解析结果"""
    ain: str
    found: bool
    # Agent 信息 (found=True)
    agent_id: str = ""
    name: str = ""
    category: str = ""
    server_host: str = ""
    public_key: str = ""
    cert_fingerprint: str = ""
    c_level: str = "C0"
    industry: str = ""
    scale: str = ""
    status: str = ""
    # 解析链信息
    resolved_by: str = "local"  # local / country_resolver / global_root / target_resolver
    resolution_chain: list[str] = field(default_factory=list)
    # 错误信息 (found=False)
    error: str = ""
    error_code: str = ""
    upstream_hint: str = ""


@dataclass
class RACData:
    """解析器授权证书"""
    rac_fingerprint: str
    resolver_ain: str
    resolver_org: str
    scope: list[str]  # 授权的 ISO 3166-1 alpha-2 国家码列表
    endpoint: str
    public_key: str  # 解析器 Ed25519 公钥
    valid_until: str  # ISO 8601
    council_signatures: list[dict]  # [{ seat_id, signature_b64, signed_at }]
    raw: dict = field(default_factory=dict)


# ── 理事会管理 ──────────────────────────────────────────

class CouncilManager:
    """管理 ACSSA 理事会成员公钥"""

    def __init__(self):
        self._members: dict[str, dict] = dict(_BOOTSTRAP_COUNCIL)
        self._last_fetch: float = 0
        self._fetch_interval: int = 3600  # 1 小时刷新

    @property
    def seats(self) -> list[str]:
        return sorted(self._members.keys())

    @property
    def quorum(self) -> int:
        """RAC 生效所需的最小签名数"""
        return 4

    def get_public_key(self, seat_id: str) -> Optional[str]:
        """获取某个理事的公钥 PEM"""
        member = self._members.get(seat_id)
        return member.get("public_key_pem") if member else None

    def has_bootstrap(self) -> bool:
        return len(self._members) > 0

    async def fetch_from_global_root(self) -> bool:
        """从 Global Root 拉取完整理事会成员列表"""
        url = f"{_global_root_url()}/v1/huanyu/federation/council"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    members = data.get("council", {})
                    if members:
                        self._members = members
                        self._last_fetch = time.time()
                        logger.info("理事会成员列表已更新: %d 席 (from Global Root)", len(members))
                        return True
        except Exception:
            logger.warning("无法从 Global Root 获取理事会成员列表，使用本地缓存")
        return False

    async def ensure_loaded(self):
        """确保理事会成员已加载（本地预置或远程拉取）"""
        if not self.has_bootstrap() or (time.time() - self._last_fetch > self._fetch_interval):
            await self.fetch_from_global_root()

    def verify_signatures(self, rac: RACData) -> tuple[bool, str]:
        """验证 RAC 中理事会签名 ≥4 条来自有效理事会成员

        使用 Ed25519 逐条验签（rac_fingerprint 为签名原文）。
        """
        if not rac.council_signatures:
            return False, "RAC 无理事会签名"

        valid_count = 0
        seen_seats: set[str] = set()
        message = rac.rac_fingerprint.encode()

        for sig in rac.council_signatures:
            seat_id = sig.get("seat_id", "")
            if not seat_id or seat_id in seen_seats:
                continue
            if seat_id not in self._members:
                logger.warning("RAC 签名包含未知理事席位: %s", seat_id)
                continue

            pk_pem = self.get_public_key(seat_id)
            if not pk_pem:
                continue

            signature_b64 = sig.get("signature_b64", "")
            if not signature_b64:
                continue

            try:
                pub_key = load_pem_public_key(pk_pem.encode())
                if not isinstance(pub_key, Ed25519PublicKey):
                    logger.warning("理事 %s 公钥非 Ed25519 类型", seat_id)
                    continue
                signature_bytes = base64.b64decode(signature_b64)
                pub_key.verify(signature_bytes, message)
                valid_count += 1
                seen_seats.add(seat_id)
            except (ValueError, TypeError, InvalidSignature) as e:
                logger.warning("RAC 签名验证失败 seat=%s: %s", seat_id, e)
                continue

        if valid_count < self.quorum:
            return False, f"有效签名数 {valid_count} < 法定人数 {self.quorum}"
        return True, f"{valid_count}/{self.quorum}+ 签名有效"


# 全局单例
_council_mgr: Optional[CouncilManager] = None


def get_council_manager() -> CouncilManager:
    global _council_mgr
    if _council_mgr is None:
        _council_mgr = CouncilManager()
    return _council_mgr


# ── CRL 管理 ────────────────────────────────────────────

class CRLManager:
    """证书吊销列表管理"""

    def __init__(self):
        self._revoked: dict[str, float] = {}  # rac_fingerprint → revoked_at_ts
        self._last_fetch: float = 0
        self._fetch_interval: int = 300  # 5 分钟刷新

    def is_revoked(self, rac_fingerprint: str) -> bool:
        return rac_fingerprint in self._revoked

    async def fetch_from_global_root(self):
        """从 Global Root 拉取 CRL"""
        url = f"{_global_root_url()}/v1/huanyu/federation/crl"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    entries = data.get("revoked", [])
                    now = time.time()
                    self._revoked = {
                        e["rac_fingerprint"]: e.get("revoked_at", now)
                        for e in entries
                    }
                    self._last_fetch = now
                    logger.info("CRL 已更新: %d 条吊销记录", len(self._revoked))
        except Exception:
            logger.warning("无法从 Global Root 获取 CRL，使用本地缓存")

    async def ensure_fresh(self):
        if time.time() - self._last_fetch > self._fetch_interval:
            await self.fetch_from_global_root()


_crl_mgr: Optional[CRLManager] = None


def get_crl_manager() -> CRLManager:
    global _crl_mgr
    if _crl_mgr is None:
        _crl_mgr = CRLManager()
    return _crl_mgr


# ── RAC 验证 ────────────────────────────────────────────

async def verify_rac(rac: RACData) -> tuple[bool, str]:
    """验证解析器授权证书 (QACP v0.6 §3.7.2)

    1. 验证 ≥4 条签名来自理事会成员
    2. 验证 RAC 未被吊销
    3. 验证 RAC 在有效期内
    4. 验证 scope 非空
    """
    council = get_council_manager()
    await council.ensure_loaded()

    # 1. 理事会多签验证
    valid, reason = council.verify_signatures(rac)
    if not valid:
        return False, f"签名验证失败: {reason}"

    # 2. CRL 吊销检查
    crl = get_crl_manager()
    await crl.ensure_fresh()
    if crl.is_revoked(rac.rac_fingerprint):
        return False, "RAC 已被吊销"

    # 3. 有效期检查
    if rac.valid_until:
        try:
            from datetime import datetime, timezone
            valid_until = datetime.fromisoformat(rac.valid_until.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > valid_until:
                return False, f"RAC 已过期 (valid_until={rac.valid_until})"
        except ValueError:
            pass

    # 4. Scope 检查
    if not rac.scope:
        return False, "RAC scope 为空"

    return True, "ok"


# ── 递归解析 ────────────────────────────────────────────

async def _resolve_local(ain: str) -> Optional[dict]:
    """在本地 agents 表中查找 AIN"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT agent_id::text, ain, name, category, server_host, status, "
            f"public_key, cert_fingerprint, c_level, industry, scale "
            f"FROM {SCHEMA}.agents WHERE ain = $1 AND status = 'active'",
            ain,
        )
    return dict(row) if row else None


def _country_from_ain(ain: str) -> Optional[str]:
    """从 AIN 提取国家段"""
    parsed = ain_mod.parse_ain(ain)
    return parsed["country"].upper() if parsed else None


def _is_in_scope(ain_country: str, scope: str) -> bool:
    """检查 AIN 所属国家是否在解析器 scope 内"""
    countries = [s.strip().upper() for s in scope.split(",") if s.strip()]
    return ain_country in countries or "*" in countries


async def _query_upstream_resolver(ain: str, endpoint: str) -> Optional[dict]:
    """向上一级解析器查询 AIN"""
    url = f"{endpoint}/v1/huanyu/resolve/{ain}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("resolution", data)
            elif resp.status_code == 403:
                data = resp.json()
                if data.get("error") == "CROSS_SCOPE_REDIRECT":
                    return {
                        "_redirect": True,
                        "upstream_hint": data.get("upstream_hint", ""),
                    }
    except httpx.TimeoutException:
        logger.warning("上游解析器超时: %s", endpoint)
    except Exception:
        logger.exception("查询上游解析器失败: %s", endpoint)
    return None


async def resolve_recursive(ain: str, max_depth: int = 5) -> ResolutionResult:
    """递归解析 AIN (QACP v0.6 §3.7.5)

    解析链: 本地 → 本国根解析器 → Global Root → 目标国根解析器

    单底座部署：仅查本地 agents 表。
    多底座部署：本地未命中时逐级向上递归。
    """
    chain: list[str] = ["local"]
    parsed = ain_mod.parse_ain(ain)
    if not parsed:
        return ResolutionResult(ain=ain, found=False, error="AIN 格式无效", error_code="INVALID_AIN")

    # Step 1: 查本地 agents 表
    local = await _resolve_local(ain)
    if local:
        return ResolutionResult(
            ain=ain, found=True,
            agent_id=local["agent_id"], name=local["name"], category=local["category"],
            server_host=local["server_host"], public_key=local.get("public_key", ""),
            cert_fingerprint=local.get("cert_fingerprint", ""),
            c_level=local.get("c_level", "C0"), industry=local.get("industry", ""),
            scale=local.get("scale", ""), status=local["status"],
            resolved_by="local", resolution_chain=chain,
        )

    # Step 2: 确定 AIN 的国家段
    ain_country = _country_from_ain(ain)
    my_scope = _resolver_scope()

    # 如果本解析器 scope 覆盖该国家，说明是该国的授权解析器但本地没有记录
    # → 返回 AIN_NOT_REGISTERED（不向上递归，节省 Global Root 资源）
    if _is_in_scope(ain_country or "", my_scope):
        return ResolutionResult(
            ain=ain, found=False,
            error=f"AIN {ain} 未在本地注册表找到",
            error_code="AIN_NOT_REGISTERED",
            resolution_chain=chain,
        )

    # Step 3: AIN 国家段超出本解析器 scope
    # 尝试向 Global Root 查询
    if max_depth <= 0:
        return ResolutionResult(
            ain=ain, found=False,
            error="解析深度超限",
            error_code="RESOLUTION_DEPTH_EXCEEDED",
            resolution_chain=chain,
        )

    global_root = _global_root_url()
    chain.append(f"global_root({global_root})")

    upstream_result = await _query_upstream_resolver(ain, global_root)
    if upstream_result is None:
        return ResolutionResult(
            ain=ain, found=False,
            error=f"Global Root ({global_root}) 不可达",
            error_code="ROOT_RESOLVER_DOWN",
            upstream_hint=global_root,
            resolution_chain=chain,
        )

    if upstream_result.get("_redirect"):
        # Global Root 返回了目标国解析器提示
        next_hint = upstream_result.get("upstream_hint", "")
        if next_hint and max_depth > 1:
            chain.append(f"target_resolver({next_hint})")
            target_result = await _query_upstream_resolver(ain, next_hint)
            if target_result and not target_result.get("_redirect"):
                return ResolutionResult(
                    ain=ain, found=True,
                    agent_id=target_result.get("agent_id", ""),
                    name=target_result.get("name", ""),
                    category=target_result.get("category", ""),
                    server_host=target_result.get("server_host", ""),
                    public_key=target_result.get("public_key", ""),
                    cert_fingerprint=target_result.get("cert_fingerprint", ""),
                    c_level=target_result.get("c_level", "C0"),
                    industry=target_result.get("industry", ""),
                    scale=target_result.get("scale", ""),
                    status=target_result.get("status", "active"),
                    resolved_by="target_resolver",
                    resolution_chain=chain,
                )

    # 如果 Global Root 返回了直接结果（它代理了目标国解析）
    if upstream_result.get("found"):
        return ResolutionResult(
            ain=ain, found=True,
            agent_id=upstream_result.get("agent_id", ""),
            name=upstream_result.get("name", ""),
            category=upstream_result.get("category", ""),
            server_host=upstream_result.get("server_host", ""),
            public_key=upstream_result.get("public_key", ""),
            cert_fingerprint=upstream_result.get("cert_fingerprint", ""),
            c_level=upstream_result.get("c_level", "C0"),
            industry=upstream_result.get("industry", ""),
            scale=upstream_result.get("scale", ""),
            status=upstream_result.get("status", "active"),
            resolved_by="global_root",
            resolution_chain=chain,
        )

    return ResolutionResult(
        ain=ain, found=False,
        error=f"AIN {ain} 在解析链中未找到",
        error_code="AIN_NOT_REGISTERED",
        resolution_chain=chain,
    )
