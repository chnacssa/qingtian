"""
寰宇 — C0-C3 认证引擎
VP（验证提供商）路由 + 升级/降级 + webhook 风险监控
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from . import config as hcfg

logger = logging.getLogger("huanyu.verification")

SCHEMA = hcfg.get_schema_name()

# C-Level 权重（QACP v0.6 §6.2.2）
C_LEVEL_WEIGHT = {"C0": 0.3, "C1": 0.6, "C2": 1.0, "C3": 1.5}

# 升级顺序
C_LEVEL_ORDER = ["C0", "C1", "C2", "C3"]


def _now():
    return datetime.now(timezone.utc)


# ── VP 路由 ────────────────────────────────────────────

# VP 注册表：country_code → provider config
# 企查查 API Code 参考（QACP v0.6 §9.3）：
#   C1: 271 工商照面基础
#   C2: 856+213+884+231（三要素核验+年报+对外投资+资质）
#   C3: 736+2006（风险扫描+合作风险排查）
VP_REGISTRY = {
    "CN": {
        "name": "企查查",
        "api_base": "https://api.qcc.com",
        "supports": ["C1", "C2", "C3", "watch"],
        "auth_header": "Token",
    },
}

DEFAULT_VP = {
    "name": "self_declaration",
    "supports": [],
}


def _lookup_provider(country_code: str) -> dict:
    """按国别路由 VP，未注册国家回退到自声明"""
    return VP_REGISTRY.get(country_code.upper(), DEFAULT_VP)


# ── VP API Key 获取 ────────────────────────────────────

def _get_qcc_api_key() -> str:
    """获取企查查 API Key — 环境变量优先，其次 config.yaml"""
    import os
    from common.config import get as root_get
    return os.getenv("QCC_API_KEY", root_get("huanyu.qcc_api_key", ""))


# ── VP HTTP 客户端 ─────────────────────────────────────

async def _vp_http_call(
    provider: dict,
    endpoint: str,
    params: dict,
    timeout: float = 30.0,
    max_retries: int = 2,
) -> dict:
    """统一的 VP HTTP 调用 — 超时 + 重试 + 错误处理

    API Key 未配置时返回 {"_stub": True}，调用方降级为桩模式。
    """
    import httpx

    api_base = provider.get("api_base", "")
    api_key = ""
    if provider.get("name") == "企查查":
        api_key = _get_qcc_api_key()

    if not api_key:
        logger.warning("[VP] %s API Key 未配置，使用桩模式", provider["name"])
        return {"_stub": True}

    headers = {provider.get("auth_header", "Authorization"): api_key}
    url = f"{api_base}{endpoint}"

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=params, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                elif resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "5"))
                    logger.warning("[VP] %s 429 限流，等待 %s 秒", provider["name"], retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    logger.error("[VP] %s %s 调用失败: %s", provider["name"], endpoint, last_error)
        except httpx.TimeoutException:
            last_error = f"超时 ({timeout}s)"
            logger.warning("[VP] %s %s 超时 (attempt %d)", provider["name"], endpoint, attempt + 1)
        except Exception as e:
            last_error = str(e)
            logger.exception("[VP] %s %s 异常", provider["name"], endpoint)

        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # 指数退避 1s/2s

    return {"_error": True, "message": last_error or "未知错误"}


# ── 企查查核验（生产就绪）────────────────────────────────

async def _qcc_verify_c1(uscc: str, company_name: str) -> dict:
    """
    C1 核验：工商照面基础信息
    API Code: 271  成本: ¥1.0
    API Key 配置后走真实接口；未配置时桩模式默认通过。
    """
    provider = VP_REGISTRY["CN"]
    logger.info("[VP-企查查] C1 核验: uscc=%s, name=%s", uscc, company_name)

    result = await _vp_http_call(provider, "/Company/GetCompanyDetail", {
        "keyword": company_name,
        "creditCode": uscc,
    })

    if result.get("_error"):
        return {"pass": False, "reason": "vp_api_error", "message": result.get("message", "")}
    if result.get("_stub"):
        # P1 (R11): 桩模式默认通过 = 无 Key 也能免费刷 C 认证（C3 权重 1.5 直接抬
        # 排序分，破坏信任模型）。收紧为 fail-closed：企查查 API Key 未配置 → 不授予认证。
        return {"pass": False, "reason": "qcc_api_key_not_configured",
                "message": "企查查 API Key 未配置，无法自动核验，需人工审核"}

    data = result.get("Result", result)
    industry = data.get("Industry", {}).get("IndustryCode", "") if isinstance(data.get("Industry"), dict) else ""
    return {
        "pass": data.get("Status", "") == "Active",
        "industry": industry,
        "scale": "",
        "risk_flags": [],
    }


async def _qcc_verify_c2(uscc: str, company_name: str) -> dict:
    """
    C2 核验：C1 + 三要素核验 + 年报 + 对外投资 + 资质
    API Code: 856+213+884+231  成本: ¥4.2
    """
    provider = VP_REGISTRY["CN"]
    logger.info("[VP-企查查] C2 核验: uscc=%s, name=%s", uscc, company_name)

    result = await _vp_http_call(provider, "/Company/CompanyVerification", {
        "creditCode": uscc,
        "companyName": company_name,
        "checkItems": ["basic_info", "legal_person", "shareholder"],
    })

    if result.get("_error"):
        return {"pass": False, "reason": "vp_api_error", "message": result.get("message", "")}
    if result.get("_stub"):
        # P1 (R11): 桩模式默认通过 = 无 Key 也能免费刷 C 认证，收紧为 fail-closed。
        return {"pass": False, "reason": "qcc_api_key_not_configured",
                "message": "企查查 API Key 未配置，无法自动核验，需人工审核"}

    data = result.get("Result", result)
    return {
        "pass": data.get("VerifyResult", False),
        "financial_data": data.get("Financial", {}),
        "tax_records": data.get("Tax", {}),
    }


async def _qcc_verify_c3(uscc: str, company_name: str) -> dict:
    """
    C3 核验：C2 + 风险扫描 + 合作风险排查
    API Code: 736+2006  成本: ¥16.2
    """
    provider = VP_REGISTRY["CN"]
    logger.info("[VP-企查查] C3 核验: uscc=%s, name=%s", uscc, company_name)

    result = await _vp_http_call(provider, "/Company/CompanyRisk", {
        "creditCode": uscc,
        "companyName": company_name,
    })

    if result.get("_error"):
        return {"pass": False, "reason": "vp_api_error", "message": result.get("message", "")}
    if result.get("_stub"):
        # P1 (R11): 桩模式默认通过 = 无 Key 也能免费刷 C 认证，收紧为 fail-closed。
        return {"pass": False, "reason": "qcc_api_key_not_configured",
                "message": "企查查 API Key 未配置，无法自动核验，需人工审核"}

    data = result.get("Result", result)
    risk_flags = data.get("RiskItems", []) if isinstance(data.get("RiskItems"), list) else []
    return {
        "pass": len(risk_flags) == 0,
        "full_risk_report": data,
    }


# ── 自声明核验（DEFAULT VP）────────────────────────────

async def _self_declaration_verify(uscc: str, company_name: str, target_level: str) -> dict:
    """自声明模式：仅记录，不核验。返回 pass=False 提示需人工审核"""
    logger.info("[VP-自声明] %s 核验请求: uscc=%s, name=%s", target_level, uscc, company_name)
    return {
        "pass": False,
        "reason": "self_declaration_requires_manual_review",
        "message": "自声明模式需要人工审核，暂不支持自动核验",
    }


# ── VP 核验调度 ─────────────────────────────────────────

async def _vp_verify(
    provider: dict,
    target_level: str,
    uscc: str,
    company_name: str,
) -> dict:
    """按目标等级调度 VP 核验"""
    name = provider.get("name", "")

    if name == "企查查":
        if target_level == "C1":
            return await _qcc_verify_c1(uscc, company_name)
        elif target_level == "C2":
            return await _qcc_verify_c2(uscc, company_name)
        elif target_level == "C3":
            return await _qcc_verify_c3(uscc, company_name)

    if name == "self_declaration":
        return await _self_declaration_verify(uscc, company_name, target_level)

    return {"pass": False, "reason": f"unsupported_provider: {name}"}


# ── 升级 ────────────────────────────────────────────────

async def upgrade_c_level(
    agent_id: str,
    target_level: str,
    uscc: str = "",
    company_name: str = "",
    country_code: str = "CN",
) -> dict:
    """
    C-Level 升级：C0→C1→C2→C3

    1. 校验 agent 存在且当前等级正确
    2. 路由 VP 核验
    3. 通过 → 更新 c_level，回填 industry/scale
    4. 写 audit_log
    """
    if target_level not in ("C1", "C2", "C3"):
        return {"success": False, "error": f"invalid target_level: {target_level}"}

    pool = await get_pool()
    async with pool.acquire() as conn:
        agent = await conn.fetchrow(
            f"SELECT agent_id::text, c_level, uscc, company_name FROM {SCHEMA}.agents WHERE agent_id = $1",
            agent_id,
        )
        if not agent:
            return {"success": False, "error": "agent not found"}

        current_level = agent["c_level"]
        current_idx = C_LEVEL_ORDER.index(current_level)
        target_idx = C_LEVEL_ORDER.index(target_level)
        if target_idx <= current_idx:
            return {
                "success": False,
                "error": f"invalid upgrade: current level {current_level} >= target {target_level}",
            }

    # 释放连接后再调用 VP（HTTP 可能耗时数秒，避免占用连接池）
    _uscc = uscc or agent.get("uscc", "")
    _company = company_name or agent.get("company_name", "")

    provider = _lookup_provider(country_code)

    result = await _vp_verify(provider, target_level, _uscc, _company)

    if not result.get("pass"):
        return {
            "success": False,
            "error": "verification_failed",
            "detail": result,
        }

    industry = result.get("industry", "")
    scale = result.get("scale", "")

    # 更新 c_level + 审计写同一事务（审计失败回滚升级，保持一致）
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"UPDATE {SCHEMA}.agents SET c_level = $1, industry = CASE WHEN $3 != '' THEN $3 ELSE industry END, "
                f"scale = CASE WHEN $4 != '' THEN $4 ELSE scale END, updated_at = NOW() "
                f"WHERE agent_id = $2",
                target_level, agent_id, industry, scale,
            )

            await conn.execute(
                f"INSERT INTO {SCHEMA}.audit_log (actor_id, action, target_type, target_id, detail, result, created_at) "
                f"VALUES ($1, $2, $3, $4, $5, $6, NOW())",
                agent_id, f"c_level_upgrade_{target_level}", "agent", agent_id,
                json.dumps({"from": current_level, "to": target_level, "vp": provider.get("name"), "risk_flags": result.get("risk_flags", [])}, ensure_ascii=False),
                "success",
            )

    logger.info("C-Level upgrade: %s %s->%s via %s", agent_id, current_level, target_level, provider.get("name"))
    return {
        "success": True,
        "agent_id": agent_id,
        "c_level": target_level,
        "previous_level": current_level,
        "industry": industry,
        "scale": scale,
        "risk_flags": result.get("risk_flags", []),
    }


# ── 降级 ────────────────────────────────────────────────

async def auto_downgrade(agent_id: str, reason: str) -> dict:
    """VP 风险事件触发自动降级至 C0"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        agent = await conn.fetchrow(
            f"SELECT agent_id::text, c_level FROM {SCHEMA}.agents WHERE agent_id = $1",
            agent_id,
        )
        if not agent:
            return {"success": False, "error": "agent not found"}

        old_level = agent["c_level"]
        if old_level == "C0":
            return {"success": True, "message": "already C0, no downgrade needed"}

        # 更新 c_level + 审计写同一事务
        async with conn.transaction():
            await conn.execute(
                f"UPDATE {SCHEMA}.agents SET c_level = 'C0', updated_at = NOW() WHERE agent_id = $1",
                agent_id,
            )

            await conn.execute(
                f"INSERT INTO {SCHEMA}.audit_log (actor_id, action, target_type, target_id, detail, result, created_at) "
                f"VALUES ($1, $2, $3, $4, $5, $6, NOW())",
                "system", "c_level_downgrade", "agent", agent_id,
                json.dumps({"from": old_level, "to": "C0", "reason": reason}, ensure_ascii=False),
                "success",
            )

        logger.warning("C-Level downgrade: %s %s->C0 reason=%s", agent_id, old_level, reason)
        return {"success": True, "agent_id": agent_id, "previous_level": old_level, "c_level": "C0", "reason": reason}


# ── Webhook 风险监控 ────────────────────────────────────

# 触发降级的风险事件类型
DOWNGRADE_EVENT_TYPES = frozenset({
    "business_abnormal",     # 经营异常
    "dishonesty",            # 失信
    "blacklist",             # 黑名单
    "license_revoked",       # 吊销
    "deregistered",          # 注销
    "tax_irregularity",      # 税务异常
    "judicial_freeze",       # 司法冻结
})


async def handle_risk_event(payload: dict, signature: str = "", timestamp: str = "") -> dict:
    """
    接收 VP webhook 风险事件，判断是否触发降级

    预期 payload 格式:
    {
      "company_name": "...",
      "registration_number": "...",
      "event_type": "business_abnormal",
      "severity": "high",
      "timestamp": "...",
      "detail": "..."
    }

    review(2026-08-24): webhook 原先无任何来源认证——任意人可伪造 high/critical
    风险事件把竞争对手打到 C0。补 HMAC 验签（与消息签名同密钥）+ 时间戳防重放。
    """
    # 验签：HMAC-SHA256(sign_key, "risk_event:" + timestamp + ":" + canonical_json)
    import time as _time
    import hmac as _hmac
    import hashlib as _hashlib
    from .signing import _get_key
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        ts = 0.0
    if abs(_time.time() - ts) > 300:
        return {"action": "rejected", "reason": "timestamp out of window (±300s)"}
    _canon = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    _expected = _hmac.new(
        _get_key(), f"risk_event:{timestamp}:{_canon}".encode(), _hashlib.sha256
    ).hexdigest()
    if not signature or not _hmac.compare_digest(_expected, signature):
        logger.warning("[trace] risk webhook 验签失败/缺失，拒绝")
        return {"action": "rejected", "reason": "signature verification failed"}

    company_name = payload.get("company_name", "")
    registration_number = payload.get("registration_number", "")
    event_type = payload.get("event_type", "")
    severity = payload.get("severity", "low")

    if event_type not in DOWNGRADE_EVENT_TYPES:
        return {"action": "ignored", "reason": f"event_type {event_type} not in downgrade triggers"}

    if severity not in ("high", "critical"):
        return {"action": "logged_only", "reason": f"severity={severity} below downgrade threshold"}

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT agent_id::text FROM {SCHEMA}.agents "
            f"WHERE (uscc = $1 AND $1 != '') OR company_name = $2 "
            f"LIMIT 1",
            registration_number, company_name,
        )

        if not row:
            logger.warning("Risk event for unknown agent: %s / %s", company_name, registration_number)
            return {"action": "unmatched", "reason": "no agent matched by uscc or company_name"}

        agent_id = row["agent_id"]
        result = await auto_downgrade(
            agent_id,
            reason=f"{event_type}: {payload.get('detail', '')}",
        )
        return {"action": "downgraded", "agent_id": agent_id, "event_type": event_type, **result}
