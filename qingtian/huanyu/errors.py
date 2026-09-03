"""
QACP v0.4 标准错误码
所有错误响应统一格式：{"error": {"code": "...", "message": "...", "detail": ...}}
"""

from fastapi import HTTPException


class QACPError(HTTPException):
    """QACP 标准错误 — 统一 status_code + error code"""

    def __init__(self, status_code: int, code: str, message: str, detail: dict | None = None):
        super().__init__(status_code=status_code)
        self.error_code = code
        self.error_message = message
        self.error_detail = detail or {}


# ── 401 Unauthorized ──────────────────────────────────

def ain_not_registered(ain: str = "") -> QACPError:
    return QACPError(401, "AIN_NOT_REGISTERED", f"AIN 未注册: {ain}" if ain else "AIN 未注册",
                     detail={"ain": ain, "action": "register_agent"})


# ── 403 Forbidden ─────────────────────────────────────

def signature_invalid(msg: str = "签名验证失败") -> QACPError:
    return QACPError(403, "SIGNATURE_INVALID", msg)


def cert_expired(ain: str = "") -> QACPError:
    return QACPError(403, "CERT_EXPIRED", "证书已过期",
                     detail={"ain": ain, "action": "renew_certificate"})


def nonce_reused() -> QACPError:
    return QACPError(403, "NONCE_REUSED", "Nonce 已被使用，疑似重放攻击")


def tier_restricted(required: str = "pro", current: str = "free") -> QACPError:
    return QACPError(403, "TIER_RESTRICTED", f"需要 {required} 及以上等级，当前为 {current}",
                     detail={"required_tier": required, "current_tier": current, "action": "upgrade_tier"})


# ── 404 Not Found ─────────────────────────────────────

def agent_unreachable(target: str = "") -> QACPError:
    return QACPError(404, "AGENT_UNREACHABLE", f"Agent 不可达: {target}" if target else "Agent 不可达",
                     detail={"target": target})


# ── 429 Rate Limit ────────────────────────────────────

def rate_limited(retry_after: int = 60, limit: int = 20) -> QACPError:
    err = QACPError(429, "RATE_LIMITED", f"请求频率超限 ({limit}/s)，请 {retry_after}s 后重试",
                    detail={"retry_after": retry_after, "limit_per_sec": limit, "action": "backoff"})
    if err.headers is None:
        err.headers = {}
    err.headers["Retry-After"] = str(retry_after)
    return err


# ── 502 Bad Gateway ───────────────────────────────────

def peer_unreachable(host: str = "", port: int = 1996) -> QACPError:
    return QACPError(502, "PEER_UNREACHABLE", f"目标底座不可达: {host}:{port}" if host else "目标底座不可达",
                     detail={"host": host, "port": port})


# ── 403 Cross-Scope Redirect (v0.6 新增) ──────────────

def cross_scope_redirect(requested_ain: str = "", resolver_scope: str = "",
                         upstream_hint: str = "ain.acssa.cn") -> QACPError:
    ain_country = ""
    try:
        from . import ain as ain_mod
        parsed = ain_mod.parse_ain(requested_ain)
        ain_country = parsed["country"].upper() if parsed else ""
    except Exception:
        pass
    return QACPError(403, "CROSS_SCOPE_REDIRECT",
                     f"AIN 国家段 {ain_country} 超出本解析器授权范围 {resolver_scope}",
                     detail={
                         "requested_ain": requested_ain,
                         "resolver_scope": resolver_scope,
                         "upstream_hint": upstream_hint,
                     })


def c_level_restricted(required_level: str = "C1", current_level: str = "C0") -> QACPError:
    return QACPError(403, "C_LEVEL_RESTRICTED",
                     f"此操作要求认证等级 ≥ {required_level}，当前等级 {current_level}",
                     detail={
                         "required_level": required_level,
                         "current_level": current_level,
                         "upgrade_path": "/v1/huanyu/verification/upgrade",
                     })


# ── 503 Service Unavailable ─────────────────────────────

def root_resolver_down(upstream: str = "ain.acssa.cn") -> QACPError:
    return QACPError(503, "ROOT_RESOLVER_DOWN", f"根解析器不可达: {upstream}",
                     detail={"upstream": upstream, "action": "retry_with_backoff"})


def resolution_timeout(ain: str = "", timeout_s: int = 10) -> QACPError:
    return QACPError(504, "RESOLUTION_TIMEOUT", f"AIN 解析超时 ({timeout_s}s): {ain}",
                     detail={"ain": ain, "timeout_seconds": timeout_s})


# ── 507 Insufficient Storage / Tier Quota ─────────────

def tier_quota_exceeded(tier: str = "free", usage: dict | None = None) -> QACPError:
    upgrade_guide = {
        "message": f"当前 {tier} 等级配额已用尽",
        "upgrade_url": "https://acssa.cn/qingtian/upgrade",
        "tiers": {
            "pro": {"monthly_price": "¥299", "msg_per_sec": 100},
            "enterprise": {"monthly_price": "¥999", "msg_per_sec": "custom"},
        },
    }
    return QACPError(507, "TIER_QUOTA_EXCEEDED", f"{tier} 等级配额已用尽，请升级",
                     detail={**upgrade_guide, "usage": usage or {}})
