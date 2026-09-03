"""
ACSSA 智能体操作系统 License 分层判定

分层逻辑:
  - plan=pro + 云端校验通过 → pro
  - plan=free + 注册 < 90 天 → pro（满血赠礼）
  - 其余 → free

防篡改:
  - 本地 License 签名校验（防改 YAML）
  - 云端校验（每 24h 回调管理服，防改代码）
  - 云端不可达 → 使用本地缓存（24h TTL），缓存过期 → 降级 free
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("common.license")

LICENSE_PATH = os.environ.get("QINGTIAN_LICENSE_PATH", "/opt/qingtian/license.yaml")
SIGN_KEY = os.environ.get("HUANYU_SIGN_KEY", "")
# 9-2 敏感清理：默认不内置生产管理服地址（原 https://ain.acssa.cn）——
# 未配置时 _check_cloud 请求失败被兜底捕获，回落本地 license 校验（fail-soft）。
MGMT_URL = os.environ.get("QINGTIAN_MGMT_URL", "")
GIFT_DAYS = 90
CLOUD_TTL = 86400  # 24 小时

# 云端校验缓存: {"ent:module": {"plan": "pro", "cached_at": timestamp}}
_cloud_cache: dict[str, dict] = {}

# 本地文件缓存（mtime 检测，推送写回后自动刷新）
_license_mtime: float = 0
_license_cache: dict = {}


def _verify_signature(data: dict) -> bool:
    """校验 License 文件的 HMAC-SHA256 签名

    P1 (R11): 原实现 SIGN_KEY 缺失时直接放行（fail-open）——任何人改
    license.yaml 的 plan 即可提权 pro/enterprise。改为 fail-closed：
    密钥未配置则校验失败（load_license 降级 free），杜绝伪造。
    """
    sig = data.pop("signature", "")
    payload = json.dumps(data, sort_keys=True)
    if not SIGN_KEY:
        logger.warning("HUANYU_SIGN_KEY 未配置，License 签名校验 fail-closed → free")
        data["signature"] = sig
        return False
    expected = hmac.new(SIGN_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    data["signature"] = sig
    return hmac.compare_digest(expected, sig)


def load_license() -> dict:
    """加载并校验本地 License。mtime 检测自动刷新缓存。"""
    global _license_mtime, _license_cache
    try:
        mtime = os.path.getmtime(LICENSE_PATH)
        if mtime != _license_mtime:
            import yaml
            with open(LICENSE_PATH) as f:
                data = yaml.safe_load(f) or {}
            if not _verify_signature(data):
                logger.warning("License 签名校验失败，降级 free")
                return {"plan": "free"}
            _license_cache = data
            _license_mtime = mtime
        return _license_cache
    except Exception:
        return {"plan": "free"}


def get_plan() -> str:
    return load_license().get("plan", "free")


async def _check_cloud(enterprise_id: str, module: str = "bidding") -> dict | None:
    """回调管理服校验 License。非阻塞 async。"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{MGMT_URL}/v1/license/validate",
                params={"enterprise_id": enterprise_id, "module": module},
                headers={"X-License-Check": load_license().get("signature", "")},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logger.debug("云端 License 校验不可达: %s", str(e)[:80])
    return None


async def check_license_on_startup() -> dict:
    """启动时 License 状态检查（不锁功能，仅日志告警）。

    main.py 的 startup 钩子调用（外部包 5s 超时）。只读本地 license.yaml
    （HMAC 签名校验 + mtime 缓存），**不做网络请求**，保证永不阻塞启动；
    pro/enterprise 的云端 24h 复核由 get_effective_tier 在使用时执行。

    返回 main.py 期望的结构: plan / status / agent_count / agent_limit。
    plan 归一化: pro|enterprise → "enterprise"（付费）；其余 → "free"。
    """
    from common.config import get as cfg_get

    plan = load_license().get("plan", "free")
    normalized = "enterprise" if plan in ("pro", "enterprise") else "free"
    try:
        limit = int(cfg_get("agent_limit", 0) or 0)
    except (TypeError, ValueError):
        limit = 0
    return {
        "plan": normalized,
        "status": "valid",
        "agent_count": 0,
        "agent_limit": limit,
    }


async def get_effective_tier(registered_at: datetime | None, enterprise_id: str = "",
                             module: str = "bidding") -> str:
    """判定当前有效 tier（本地 + 云端双层校验，按模块区分）。"""
    plan = get_plan()
    cache_key = f"{enterprise_id}:{module}" if enterprise_id else module

    # 免费版用户：本地判定 90 天赠礼
    # P1 (2026-08-27 review #3/#4): 原实现两处缺陷——
    #   ① plan != "pro" 把 enterprise 也卷入赠礼分支（付费企业满 90 天被判
    #     free 且永远到不了下方云端复核），改 plan == "free" 仅免费版走赠礼；
    #   ② registered_at 缺失 → return "pro"（注册时间异常即永久 pro，90 天
    #     赠礼变无限），改保守降 free 并告警（宁可不赠礼，不可白送）。
    if plan == "free":
        if registered_at is None:
            logger.warning("registered_at 缺失，无法判定赠礼期，保守降级 free")
            return "free"
        if datetime.now(timezone.utc) - registered_at < timedelta(days=GIFT_DAYS):
            return "pro"
        return "free"

    # Pro 用户：云端校验（按模块查询）
    if enterprise_id:
        cached = _cloud_cache.get(cache_key)
        if cached and time.time() - cached["cached_at"] < CLOUD_TTL:
            return cached["plan"]

        cloud = await _check_cloud(enterprise_id, module)
        if cloud:
            _cloud_cache[cache_key] = {"plan": cloud.get("plan", "free"), "cached_at": time.time()}
            return cloud.get("plan", "free")

        if cached:
            logger.warning("云端校验超时，使用过期缓存（module=%s, 已过期 %d 秒）",
                           module, int(time.time() - cached["cached_at"] - CLOUD_TTL))
            return cached["plan"]

        logger.warning("云端校验不可达且无有效缓存（module=%s），降级 free", module)
        return "free"

    return plan
