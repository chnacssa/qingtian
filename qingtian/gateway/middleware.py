"""
网关中间件 — 请求处理管线

中间件链（注册顺序 = 执行顺序）：
  LoggingMiddleware → YonghengMemoryMiddleware → ZhenyueGuardMiddleware → RoleCheckMiddlewareASGI

配置（config.yaml）：
  gateway.middleware.mode: log_only | enforce | disabled
"""
import asyncio
import hmac
import httpx
import logging
import os
import time
from typing import Optional

from starlette.datastructures import State
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from common.config import get as root_get
from common.db import get_pool
from zhenyue.token_service import authenticate

logger = logging.getLogger("gateway.middleware")


def _trace_enabled() -> bool:
    """消息追踪开关 — gateway.trace.enabled，默认关闭"""
    return root_get("gateway.trace.enabled", True)  # 联调期默认开


def _trace(msg: str, *args) -> None:
    """统一追踪日志 — 仅在 gateway.trace.enabled=true 时输出"""
    if _trace_enabled():
        logger.warning("[trace] " + msg, *args)


def _get_middleware_mode() -> str:
    return root_get("gateway.middleware.mode", "enforce")


# A2 (R11) 收窄：公开前缀下的高危端点仍需有效 Bearer token。
# 攻击面：伪造提交结果/取消他人任务/认领多签/冒充发送方/读他人 inbox。
# Skill 子进程经 IPC 代理调用均带 Bearer（admin token + X-Agent-ID 透传），不受影响。
_A2_PROTECTED_PREFIXES = (
    "/v1/zhice/",           # 任务/步骤/多签 写操作
    "/v1/huanyu/messages",  # 发送消息（冒充发送方）
    "/v1/huanyu/inbox",     # inbox 轮询读回（防读他人 inbox）
)


def _is_a2_protected(path: str, method: str) -> bool:
    """是否为 A2 收窄范围内的高危端点

    写方法一律保护；GET 仅 inbox 保护（其余公开读保持兼容）。
    """
    if not path.startswith(_A2_PROTECTED_PREFIXES):
        return False
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    if method == "GET" and path.startswith("/v1/huanyu/inbox"):
        return True
    return False


def _extract_bearer(scope) -> str:
    """从 ASGI scope 提取 Authorization: Bearer <token>"""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            auth = value.decode("latin-1")
            if auth.startswith("Bearer "):
                return auth[7:]
    return ""


async def _resolve_token_identity(token: str) -> Optional[dict]:
    """解析 token 身份（先查缓存，再查 DB）"""
    identity = _get_cached_identity(token)
    if identity is None:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                identity = await authenticate(conn, token)
            if identity:
                _set_cached_identity(token, identity)
        except Exception as e:
            logger.warning("Token 解析异常 (A2 guard): %s", e)
            return None
    return identity


def _is_internal_ipc_scope(scope) -> bool:
    """内部 IPC 通道判定（ASGI scope 版，与 common/ipc_auth.is_internal_ipc 同规）。

    P1 (R11): admin token + X-Agent-ID 身份透传仅限内部 IPC 通道
    （loopback + X-Internal-Token == QINGTIAN_INTERNAL_IPC_TOKEN）。
    外部攻击者（如 enterprise 用户被映射 admin）无法走 loopback，
    也无法持有内部令牌，杜绝冒充任意 agent 身份。
    """
    client = scope.get("client") or ()
    host = client[0] if client else ""
    if host not in ("127.0.0.1", "::1"):
        return False
    expected = os.environ.get("QINGTIAN_INTERNAL_IPC_TOKEN", "")
    if not expected:
        return False  # 未配置内部令牌 → 无可信内部通道
    for name, value in scope.get("headers", []):
        if name == b"x-internal-token":
            provided = value.decode("latin-1")
            if provided.isascii() and expected.isascii():
                return hmac.compare_digest(provided, expected)
            return provided == expected
    return False


async def _drain_body(scope, receive) -> bytes:
    """读取完整请求 body 并缓存回 scope['_drained_body']。

    P1 (R11): hermes HMAC 签名需覆盖 body（防篡改）——适配器认证前先读 body，
    供签名计算，并让下游端点通过包装后的 receive 复用。
    """
    body = b""
    while True:
        try:
            message = await receive()
        except Exception:
            break
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    scope["_drained_body"] = body
    return body


def _make_body_aware_receive(scope, receive):
    """包装 receive——首次返回缓存的 body，后续交给原 receive。"""
    sent = False

    async def wrapped():
        nonlocal sent
        if not sent:
            sent = True
            return {
                "type": "http.request",
                "body": scope.get("_drained_body", b""),
                "more_body": False,
            }
        return await receive()

    return wrapped




def _huanyu_path_public(path: str, method: str) -> bool:
    """寰宇白名单收窄（2026-08-24 安全评审 P0-1）。

    原 `/v1/huanyu/` 整段前缀放行（注释自认"测试用"长期未收窄）导致寰宇全线
    免鉴权：任意读他人收件箱/会话、伪造 from_agent 冒名发消息、runtime 启动
    任意 executable（RCE 面）、C 级认证伪造、agent 删除全部裸奔。

    收窄原则：仅放行**内部无 token 调用方实际在用**的子路径 + agents 读操作；
    敏感写操作（verification / runtime / negotiations / agreements / reminders /
    auto-flow / DELETE agents）一律走网关 Bearer 鉴权。
    注：Skill 经 ctx.api 的调用走羲和 IPC 代理**自带 admin Bearer**（xihe/
    agent_runtime.py api.* 分支），不受本白名单影响；直接 HTTP 无 token 的调用方
    （Skill 子进程 messages/inbox、portal 服务端、跨底座 peers、builtin agents
    心跳）按下表放行。
    """
    # Skill 子进程 / builtin agents / GBZ 客户端直连（无 token）
    if path == "/v1/huanyu/messages" or path.startswith("/v1/huanyu/inbox/"):
        return True
    # GBZ 185 客户端（conversations/tools）+ 采购成交上报（跨底座，无 token）
    if path.startswith(("/v1/huanyu/conversations", "/v1/huanyu/tools",
                        "/v1/huanyu/orders/")):
        return True
    # 跨底座公钥拉取（orgs/{id}/keys）；orgs/register|token 端点自带 _admin_guard
    if path.startswith("/v1/huanyu/orgs/"):
        return True
    # agents 子树：读放行（目录/详情/搜索跨底座解析）；写仅放行内部调用方在用子路径
    if path == "/v1/huanyu/agents" or path.startswith("/v1/huanyu/agents/"):
        if method in ("GET", "HEAD"):
            return True
        if path in ("/v1/huanyu/agents/register", "/v1/huanyu/agents/search"):
            return True
        if path.endswith(("/heartbeat", "/identity/resolve", "/resolve",
                          "/credential", "/description")):
            return True
        if "/bindings" in path:  # portal 服务端通道绑定
            return True
        return False
    return False


def _xixing_path_public(path: str, method: str) -> bool:
    """吸星白名单收窄（2026-08-28 吸星 review P0-1）。

    原 `/v1/xixing/` 整段前缀放行导致吸星全线免鉴权：知识库读取/导出
    （/knowledge/export 数据外泄）、sources/collect（SSRF 采集链）、
    proposals/evolve（知识注入）等 14 端点裸奔。

    收窄原则：仅放行**内部无 token 调用方实际在用**的 agent 子树 3 条：
      - POST /v1/xixing/agent/{id}/learn          （zhice xixing_client + osskill qingtian.py）
      - POST /v1/xixing/agent/{id}/report-pitfall （同上）
      - GET  /v1/xixing/agent/{id}/insights       （osskill qingtian.py）
    其余（knowledge/sources/collect/proposals/process/evolve…）走网关 Bearer
    鉴权；sources 管理端另有 is_management() 二道门。
    已核实（2026-08-28 gateway 插件调用面排查）：agent-gateway-plugin 不调
    /v1/xixing/ 任何路径，收窄不断插件。
    """
    if path.startswith("/v1/xixing/agent/") and path != "/v1/xixing/agent/":
        if path.endswith("/learn") and method == "POST":
            return True
        if path.endswith("/report-pitfall") and method == "POST":
            return True
        if path.endswith("/insights") and method in ("GET", "HEAD"):
            return True
    return False


def _is_path_public(path: str, method: str = "GET") -> bool:
    """白名单路径：无需认证即可访问"""
    # 注意："/" 不能放在 startswith 元组中，否则所有路径都被视为公开！
    if path.rstrip("/") == "" or path == "/":
        return True
    # 门户 HTML 壳精确放行（大师 2026-08-09：采购服 enforce 模式 /portal 401，
    # 用户进不了统一登录页/预训练工作台）。页面仅 HTML 壳，JS 校验 token 无则跳 /portal；
    # 数据接口（/v1/portal/files、/v1/procurement/import|knowledge、/v1/sales/import 等）
    # 不在此列，仍走网关 Bearer 鉴权。用精确匹配（非 startswith）避免放行其子路径。
    # /v1/bidding 为投标系统页面（浏览器加载页面不带 token，缺则 401）——
    # 门户反代到 1996 后必命中网关，须与 /v1/procurement 等页面一致公开 HTML 壳。
    if path.rstrip("/") in ("/v1/bidding", "/v1/bidding/resolve/department", "/portal", "/v1/procurement", "/v1/sales", "/v1/admin"):
        return True
    # 寰宇：方法感知收窄（见 _huanyu_path_public），不再整段前缀放行
    if path == "/v1/huanyu" or path.startswith("/v1/huanyu/"):
        return _huanyu_path_public(path, method)
    # 吸星：方法+路径收窄（见 _xixing_path_public），不再整段前缀放行
    if path == "/v1/xixing" or path.startswith("/v1/xixing/"):
        return _xixing_path_public(path, method)
    public_prefixes = (
        "/health", "/docs", "/openapi.json", "/redoc",
        "/v1/auth/token",
        "/v1/xihe/",             # 羲和管理 API 跳过认证
        "/v1/yongheng/",         # 永恒内部模块跳过认证（同 /v1/xihe/ 理由）
        "/v1/huichuan/files",
        "/v1/huichuan/agents",    # 汇川文件上传/下载/搜索
        "/api/v1/skills/",       # 内部 Skill API（gateway 插件本地调用，不走外部 token）
        "/v1/zhice/",            # 执策 API（gateway 插件本地调用，不走外部 token）
        "/v1/zhenyue/tokens/verify",  # 镇岳 token 验证端点（Skill 子进程/内部跨服务校验 token；创建/撤销端点仍有 X-Admin-Token 二次校验）
        "/peers/",               # 跨底座联邦（peer 签名认证，不走 Bearer token）
        "/v1/ws/health",
        # "/mcp/" 已移除（9-1 修复日，汇川 P0）：MCP 写类工具（ingest/auto_fix/subscribe）
        # 零鉴权 + ingest 硬编码 enterprise → 未认证知识投毒。/mcp/ 走默认 Bearer；
        # mcp.py 侧写类工具另有 req.state.agent_id 身份门（双保险 fail-closed）。
        # MCP host 需在配置中带 Bearer token（部署说明见 fix-log）。
    )
    return path.startswith(public_prefixes)


# ── Token 缓存（LRU，60s TTL）────────────────────────

_token_cache: dict[str, tuple[dict, float]] = {}
TOKEN_CACHE_TTL = 60  # 秒
TOKEN_CACHE_MAX_ENTRIES = 5000  # P2 (R11): 缓存硬上限（防活跃 token 无界膨胀）


def _get_cached_identity(token: str) -> Optional[dict]:
    """从缓存获取 token 身份"""
    entry = _token_cache.get(token)
    if entry and (time.monotonic() - entry[1]) < TOKEN_CACHE_TTL:
        return entry[0]
    if entry:
        _token_cache.pop(token, None)
    return None


def _set_cached_identity(token: str, identity: dict):
    """缓存 token 身份"""
    _token_cache[token] = (identity, time.monotonic())
    # P2 (R11): 缓存硬上限——清理过期项后若仍超限（活跃 token 过多），
    # 按时间戳清最旧条目，保证有界，避免长期运行无界膨胀。
    if len(_token_cache) > TOKEN_CACHE_MAX_ENTRIES:
        now = time.monotonic()
        stale = [k for k, v in _token_cache.items()
                 if (now - v[1]) >= TOKEN_CACHE_TTL]
        for k in stale:
            _token_cache.pop(k, None)
        overflow = len(_token_cache) - TOKEN_CACHE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(_token_cache, key=lambda t: _token_cache[t][1])[:overflow]
            for k in oldest:
                _token_cache.pop(k, None)


# ── Role Check ────────────────────────────────────────

class RoleCheckMiddlewareASGI:
    """纯 ASGI 中间件 — 替代 BaseHTTPMiddleware，兼容 Starlette 1.x。

    从 Bearer Token 解析 agent_id + role + capabilities，
    注入 scope["state"] 供下游中间件和路由使用。

    三种模式：
      - enforce: 无 token 或无效 token → 401
      - log_only: 无 token → 放行（空身份），无效 token → 401
      - disabled: 完全跳过
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        mode = _get_middleware_mode()
        path = scope.get("path", "")
        if not path:
            path = scope.get("root_path", "")

        logger.info("ASGI middleware: %s %s mode=%s",
                     scope.get("method", ""), path, mode)

        # 默认空身份
        scope["state"] = State({
            "agent_id": "",
            "role": "",
            "capabilities": [],
        })

        if mode == "disabled":
            _trace("RoleCheck mode=disabled → 放行 %s", path)
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if _is_path_public(path, method):
            # A2 (R11) 收窄：公开前缀下的高危端点仍要求有效 Bearer token
            if _is_a2_protected(path, method):
                # A2 内部通道豁免（2026-08-25）：内部无 token 调用方（执策网关插件
                # fetch / skill 子进程 sdk 直连 messages/inbox）凭 loopback +
                # X-Internal-Token 放行——b91a1953 合入 R11 后这些调用方 401，
                # 订单确认→询价提示推送全部被拦。外部攻击者无法走 loopback，
                # 也无法持有内部令牌，冒充面不重开。
                if _is_internal_ipc_scope(scope):
                    _trace("RoleCheck A2 内部通道豁免 → 放行 %s %s", method, path)
                    await self.app(scope, receive, send)
                    return
                token = _extract_bearer(scope)
                if not token:
                    _trace("RoleCheck A2 拒绝: 高危写端点无 token %s %s", method, path)
                    resp = JSONResponse(
                        status_code=401,
                        content={"code": "UNAUTHORIZED",
                                 "message": "写操作需要有效 token"},
                    )
                    await resp(scope, receive, send)
                    return
                identity = await _resolve_token_identity(token)
                if not identity:
                    _trace("RoleCheck A2 拒绝: 无效 token %s %s", method, path)
                    resp = JSONResponse(
                        status_code=401,
                        content={"code": "INVALID_TOKEN", "message": "Token 无效或已过期"},
                    )
                    await resp(scope, receive, send)
                    return
                scope["state"] = State({
                    "agent_id": identity.get("agent_id", ""),
                    "role": identity.get("role", "agent"),
                    "capabilities": identity.get("capabilities", []),
                })
                _trace("RoleCheck A2 放行: %s %s agent=%s", method, path,
                       identity.get("agent_id", ""))
                await self.app(scope, receive, send)
                return
            _trace("RoleCheck 公开路径 → 放行 %s", path)
            await self.app(scope, receive, send)
            return

        # ── Phase 2：适配器认证链 ─────────────────────
        if root_get("gateway.adapters.enabled", False):
            _trace("RoleCheck 适配器认证链 → 开始 %s", path)
            # P1 (R11): hermes HMAC 签名需覆盖 body——先读 body 缓存回 scope，
            # 供适配器参与签名计算，并包装 receive 供下游端点复用。
            body = await _drain_body(scope, receive)
            if body:
                receive = _make_body_aware_receive(scope, receive)
            from gateway.adapters.registry import get_registry
            identity = await get_registry().authenticate(scope)
            if identity:
                _trace("RoleCheck 适配器认证 → 通过 adapter=%s agent=%s",
                       identity.adapter_name, identity.agent_id)
                scope["state"] = State({
                    "agent_id": identity.agent_id,
                    "role": identity.role,
                    "capabilities": identity.capabilities,
                })
            elif mode == "enforce":
                _trace("RoleCheck 适配器认证 → 拒绝 401 %s", path)
                resp = JSONResponse(
                    status_code=401,
                    content={"code": "UNAUTHORIZED",
                             "message": "认证失败，无有效凭据"},
                )
                await resp(scope, receive, send)
                return
            else:
                logger.info("RoleCheck 适配器认证 → 无身份 log_only %s", path)
                _trace("RoleCheck 适配器认证 → 无身份 log_only %s", path)
            await self.app(scope, receive, send)
            return

        # ── 旧认证路径（向后兼容） ────────────────────

        # 从 ASGI scope headers 提取 Authorization
        auth = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth = value.decode("latin-1")
                break

        logger.info("ASGI middleware: %s %s auth=%s mode=%s",
                     scope.get("method", ""), path,
                     "yes" if auth else "no", mode)

        token = ""
        if auth.startswith("Bearer "):
            token = auth[7:]

        if not token:
            if mode == "enforce":
                logger.warning("ASGI middleware 拒绝: 无 token %s %s",
                               scope.get("method", ""), path)
                resp = JSONResponse(
                    status_code=401,
                    content={"code": "UNAUTHORIZED", "message": "缺少 Authorization header"},
                )
                await resp(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # 解析 token（先查缓存，再查 DB）
        try:
            identity = _get_cached_identity(token)
            if identity is None:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    identity = await authenticate(conn, token)
                if identity:
                    _set_cached_identity(token, identity)

            if identity:
                scope["state"] = State({
                    "agent_id": identity.get("agent_id", ""),
                    "role": identity.get("role", "agent"),
                    "capabilities": identity.get("capabilities", []),
                })

                # IPC 代理身份透传：admin 级别的 token 携带 X-Agent-ID
                # Skill 子进程通过 IPC 调用 API 时，IPC 代理（agent_runtime.py）
                # 以 admin token 认证，用 X-Agent-ID 头传递实际 agent 身份。
                # 仅 admin 角色允许此覆盖，且必须是内部 IPC 通道
                # （loopback + X-Internal-Token，见 _is_internal_ipc_scope）——
                # P1 (R11): 原实现任何 admin token 均可覆写任意 X-Agent-ID，
                # enterprise 用户被映射 admin 即可冒充任意 agent。
                if (identity.get("role") == "admin"
                        and _is_internal_ipc_scope(scope)):
                    for hname, hval in scope.get("headers", []):
                        if hname == b"x-agent-id":
                            real_agent = hval.decode("utf-8")
                            if real_agent:
                                scope["state"]["agent_id"] = real_agent
                                _trace("RoleCheck X-Agent-ID → %s", real_agent)
                            break
            else:
                logger.warning("ASGI middleware 拒绝: 无效 token %s %s",
                               scope.get("method", ""), path)
                resp = JSONResponse(
                    status_code=401,
                    content={"code": "INVALID_TOKEN", "message": "Token 无效或已过期"},
                )
                await resp(scope, receive, send)
                return
        except Exception as e:
            logger.warning("Token 解析异常 (path=%s): %s", path, e)
            resp = JSONResponse(
                status_code=500,
                content={"code": "AUTH_ERROR", "message": "认证服务异常"},
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)


# ── Zhenyue Guard ─────────────────────────────────────

class ZhenyueGuardMiddleware(BaseHTTPMiddleware):
    """镇岳安全守卫 — 拦截危险操作。

    从 guard.py 动态加载危险操作注册表（DB 驱动，DB 不可达时硬编码兜底）。
    注册表热更新无需重启。根据配置的 guard.mode 决定行为：
      - enforce: 拦截匹配危险规则的操作
      - log_only: 记录但不拦截
      - disabled: 完全跳过
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._matcher = None
        self._approval_paths = (
            "/v1/zhenyue/break-glass",
            "/v1/zhenyue/tokens/bulk-revoke",
        )

    def _get_matcher(self):
        """懒加载 guard 匹配器，优先用 guard.py 的动态注册表"""
        if self._matcher is not None:
            return self._matcher
        try:
            from zhenyue.guard import matcher
            self._matcher = matcher
        except ImportError:
            # guard.py 不可用，用硬编码兜底
            from zhenyue.guard import DangerRule, PathMatcher
            fallback_rules = [
                DangerRule("DELETE", "/v1/huanyu/agents/{agent_id}", "delete_agent", "critical", ["admin"]),
                DangerRule("POST", "/v1/zhenyue/break-glass", "break_glass", "critical", ["admin"]),
                DangerRule("POST", "/v1/zhenyue/tokens/bulk-revoke", "bulk_revoke_tokens", "high", ["admin"]),
            ]
            self._matcher = PathMatcher(fallback_rules)
        return self._matcher

    async def dispatch(self, request: Request, call_next):
        mode = root_get("zhenyue.guard.mode", root_get("gateway.middleware.mode", "log_only"))
        if mode == "disabled":
            return await call_next(request)

        path = request.url.path
        method = request.method

        # 只检查写操作
        if method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        # 检查是否需要审批
        if path.startswith(self._approval_paths):
            if mode == "enforce":
                return JSONResponse(
                    status_code=403,
                    content={
                        "code": "APPROVAL_REQUIRED",
                        "message": "该操作需要审批流程，请通过镇岳审批接口提交",
                    },
                )
            logger.warning("[ZhenyueGuard] 需要审批的操作 (log_only): %s %s", method, path)

        # 用动态注册表匹配，未匹配到则放行
        try:
            matcher = self._get_matcher()
            rule = matcher.match(method, path)
            if rule:
                role = getattr(request.state, "role", "")
                caps = getattr(request.state, "capabilities", [])

                # 检查角色和能力
                if rule.capabilities_required:
                    has_any = any(c in caps for c in rule.capabilities_required)
                    if not has_any and role not in rule.capabilities_required:
                        if mode == "enforce":
                            return JSONResponse(
                                status_code=403,
                                content={
                                    "code": "FORBIDDEN",
                                    "message": f"需要 {rule.capabilities_required} 权限才能执行 {rule.action_name}",
                                    "action": rule.action_name,
                                    "severity": rule.severity,
                                },
                            )
                        logger.warning(
                            "[ZhenyueGuard] 权限不足: %s %s (role=%s, need=%s)",
                            method, path, role, rule.capabilities_required,
                        )

                if rule.severity in ("critical", "high") and mode == "enforce":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "code": "DANGEROUS_OPERATION",
                            "message": f"危险操作 '{rule.action_name}' 已被拦截",
                            "action": rule.action_name,
                            "severity": rule.severity,
                        },
                    )
        except Exception as e:
            logger.warning("[ZhenyueGuard] 规则匹配异常 (path=%s): %s", path, e)

        return await call_next(request)


# ── Yongheng Memory ──────────────────────────────────

class YonghengMemoryMiddleware(BaseHTTPMiddleware):
    """永恒记忆捕获 — 自动记录请求/响应到 Agent 记忆。

    Phase 1: 仅记录关键操作的轨迹，不开启大流量捕获。
    """

    # 需要捕获的路径模式
    CAPTURE_PATHS = (
        "/v1/huanyu/agents/register",
        "/v1/huanyu/agents/",
        "/v1/zhice/tasks",
        "/v1/zhice/steps/",
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        try:
            if _get_middleware_mode() == "disabled":
                return response

            path = request.url.path
            method = request.method

            # 只捕获写操作在关键路径
            if method in ("GET", "HEAD", "OPTIONS"):
                return response
            if not path.startswith(self.CAPTURE_PATHS):
                return response

            agent_id = getattr(request.state, "agent_id", "")
            if not agent_id:
                return response

            # 异步写入记忆（不阻塞请求）
            task = asyncio.create_task(self._capture_memory(
                agent_id=agent_id,
                path=path,
                method=method,
                status_code=response.status_code,
            ))
            # 捕获 task 异常，防止静默吞掉
            task.add_done_callback(
                lambda t: logger.debug(
                    "记忆捕获 task 异常: %s", t.exception()
                ) if not t.cancelled() and t.exception() else None
            )
        except Exception:
            pass  # 记忆捕获失败不影响请求

        return response

    async def _capture_memory(self, agent_id: str, path: str, method: str, status_code: int):
        """写一条操作轨迹到 Agent 的 Yongheng 记忆"""
        try:
            base_url = root_get("service.internal_url", "http://localhost:1996")
            async with httpx.AsyncClient(timeout=5, base_url=base_url) as client:
                await client.post(
                    "/v1/yongheng/memories",
                    json={
                        "namespace": f"agent:{agent_id}",
                        "content": f"{method} {path} → {status_code}",
                        "type": "trajectory",
                        "source": "gateway.middleware",
                        "metadata": {
                            "method": method,
                            "path": path,
                            "status_code": status_code,
                        },
                    },
                )
        except Exception:
            logger.debug("记忆捕获失败: %s %s %s", agent_id, method, path)


# ── Bus Scheduler ──────────────────────────────────────

class BusSchedulerMiddleware(BaseHTTPMiddleware):
    """总线调度中间件 — 在身份解析后主动编排 agent 生命周期

    依赖 ZhenyueGuardMiddleware 已在 request.state 中注入 agent_id。
    插入位置：ZhenyueGuardMiddleware 之后，Router 之前。
    """

    SKIP_PREFIXES = (
        "/health", "/favicon", "/.well-known",
        "/v1/auth", "/v1/zhenyue/approvals",
        "/v1/xihe", "/docs", "/openapi",
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 跳过非 agent 路径
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await call_next(request)

        # ZhenyueGuard 已在 request.state 中注入 agent_id
        agent_id = getattr(request.state, "agent_id", None)
        if not agent_id:
            return await call_next(request)

        # 交给 BusScheduler 处理
        _trace("BusScheduler → dispatch agent=%s path=%s", agent_id, path)
        try:
            from common.bus import bus_scheduler
            return await bus_scheduler.dispatch(request, call_next)
        except Exception as e:
            logger.warning("[BusScheduler] 调度异常 (path=%s): %s", path, e)
            return await call_next(request)


# ── Logging ──────────────────────────────────────────

class LoggingMiddleware(BaseHTTPMiddleware):
    """结构化请求日志"""

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()

        response = await call_next(request)

        elapsed = time.monotonic() - start
        agent_id = getattr(request.state, "agent_id", "") or "-"
        status = response.status_code

        logger.info(
            "%.3f %s %s %s %d %s",
            elapsed,
            request.method,
            request.url.path,
            request.client.host if request.client else "?",
            status,
            agent_id,
        )

        return response


# ── Skill License 中间件 ──────────────────────────────

class SkillLicenseMiddleware(BaseHTTPMiddleware):
    """Skill License 检查中间件 — 在 Skill 调用前校验 License。

    只检查 /api/v1/skills/ 路径下的请求。
    三种模式：
      - enforce: License 无效时返回 403
      - log_only: 记录但不拦截
      - disabled: 完全跳过
    """

    async def dispatch(self, request: Request, call_next):
        mode = _get_middleware_mode()
        path = request.url.path

        if mode == "disabled":
            return await call_next(request)

        if not path.startswith("/api/v1/skills/"):
            return await call_next(request)

        if path.startswith("/api/v1/skills/admin/"):
            return await call_next(request)

        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)

        if mode == "log_only":
            logger.info("[SkillLicense] %s %s (log_only)", request.method, path)
            return await call_next(request)

        try:
            parts = path.strip("/").split("/")
            if len(parts) >= 4:
                skill_id_or_name = parts[3]
                if skill_id_or_name.isdigit():
                    from osskill_acssa.license_manager import verify_skill_license
                    result = verify_skill_license(skill_id_or_name)
                    if not result.valid:
                        return JSONResponse(
                            status_code=403,
                            content={
                                "code": "LICENSE_REQUIRED",
                                "message": f"Skill '{skill_id_or_name}' requires valid License",
                                "level": result.level,
                                "license_type": result.license_type,
                            },
                        )
        except Exception as e:
            logger.warning("[SkillLicense] check failed: %s", e)

        return await call_next(request)
