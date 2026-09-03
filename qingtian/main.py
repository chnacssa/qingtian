#!/usr/bin/env python3
"""
ACSSA 智能体操作系统（底座 OS）入口
聚合所有板块，一键启动

已启用板块：
  - xixing（吸星 — 知识进化）
  - yongheng（永恒 — 记忆检索）
  - huanyu（寰宇 — 通信目录）
  - zhenyue（镇岳 — 安全审计）

用法:
  python3 /opt/qingtian/main.py                    # 启动全部板块
  python3 /opt/qingtian/main.py --reload           # 开发模式（热重载）
"""

import os, socket, sys, logging, time, asyncio, secrets, hashlib
from datetime import datetime, timezone

_qingtian_root = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_qingtian_root)
# 69e460e 后商业 Skill 迁到仓库根 skills/：从 opensource/qingtian/main.py 运行时
# _repo_root 只是 opensource/，仓库根还需再上溯一层，否则 `import skills.*` 失败
# （采购服双 main.py 实测根因：旧入口跑老路径、新入口又 sys.path 缺仓库根）。
# 幂等：仅当上溯后的父级含 skills/ 目录才采用；根目录 main.py 运行时脚本目录即
# 仓库根（sys.path[0]），此处判断为空不改变既有行为。
_parent = os.path.dirname(_repo_root)
if os.path.isdir(os.path.join(_parent, "skills")):
    _repo_root = _parent
sys.path.insert(0, _qingtian_root)
sys.path.insert(0, _repo_root)
_qingtian_sub = os.path.join(_qingtian_root, "qingtian")
if os.path.isdir(_qingtian_sub):
    sys.path.insert(0, _qingtian_sub)


def _configure_logging() -> None:
    """配置根日志：子进程 skill_runner 的 [trace]/INFO 日志经 xihe _forward_stderr 转发到
    'xihe.skill.*' logger，父进程若无 handler 会被 root lastResort（仅 WARNING）丢弃，
    线上全靠 feedback_text 反推根因。这里补 StreamHandler(stderr) + FileHandler 落盘，
    INFO 级含时间戳，子进程日志既可见又可离线查。"""
    _log_dir = os.environ.get("QINGTIAN_LOG_DIR", os.path.join(_qingtian_root, "logs"))
    try:
        os.makedirs(_log_dir, exist_ok=True)
    except OSError:
        _log_dir = _qingtian_root  # 不可建目录则退回项目根
    _fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _root = logging.getLogger()
    _root.setLevel(logging.INFO)
    # 幂等：避免热重载/重复执行时叠加 handler
    if any(getattr(h, "_qt_installed", False) for h in _root.handlers):
        return
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(_fmt)
    _sh._qt_installed = True
    _root.addHandler(_sh)
    try:
        _fh = logging.FileHandler(os.path.join(_log_dir, "qingtian.log"), encoding="utf-8")
        _fh.setFormatter(_fmt)
        _fh._qt_installed = True
        _root.addHandler(_fh)
    except OSError as e:
        print(f"  ⚠️ 日志文件不可写: {e}", flush=True)


_configure_logging()

from fastapi import FastAPI, Request, Body, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from common.config import get, is_management
from huanyu.config import get_schema_name as _huanyu_schema
from zhenyue.config import get_schema_name as _zhenyue_schema
from gateway.middleware import LoggingMiddleware, ZhenyueGuardMiddleware, YonghengMemoryMiddleware, BusSchedulerMiddleware, SkillLicenseMiddleware

app = FastAPI(
    title="ACSSA 智能体操作系统 — 底座 OS",
    version="0.2.0",
    description="智能体世界自主进化底座 — 吸星 → 永恒 → 寰宇 → 镇岳 → 汇川 → 司库 → 执策",
)

# 中间件（路由之前）
# RoleCheckMiddlewareASGI 通过直接 ASGI 包装（非 add_middleware），在所有中间件之前运行
# add_middleware 对非 BaseHTTPMiddleware 子类无效（Starlette 1.3.1 限制）
# 请求流：RoleCheck(ASGI顶层) → RateLimit → Logging → YonghengCapture → ZhenyueGuard → BusScheduler → SkillLicense → Router
app.add_middleware(SkillLicenseMiddleware)          # Skill License 检查（Phase 4）
app.add_middleware(BusSchedulerMiddleware)          # 总线调度（依赖 RoleCheck 注入的 agent_id）
app.add_middleware(ZhenyueGuardMiddleware)          # 安全守卫（依赖 RoleCheck 注入的身份）
app.add_middleware(YonghengMemoryMiddleware)        # 记忆捕获（依赖 RoleCheck 注入的 agent_id）
app.add_middleware(LoggingMiddleware)               # 请求日志（依赖 RoleCheck 注入的 agent_id）
from zhenyue.rate_limit import rate_limit_middleware
app.middleware("http")(rate_limit_middleware)

# ── 可观测性：API 调用计数 + 延迟 ──
@app.middleware("http")
async def metrics_middleware(request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)
    from common.metrics import record_api_call
    _start = time.monotonic()
    response = await call_next(request)
    record_api_call(request.method, request.url.path, response.status_code, (time.monotonic() - _start) * 1000)
    return response

# ── 全局异常处理：AppError → 正确 HTTP 状态码 ──────────

def _make_app_error_handler():
    """捕获各模块的 AppError，返回正确的 HTTP 状态码（而非 500）。"""
    async def handler(request, exc: Exception):
        code = getattr(exc, "code", "INTERNAL_ERROR")
        message = getattr(exc, "message", str(exc))
        status = getattr(exc, "status", 500)
        return JSONResponse(status_code=status, content={"code": code, "message": message})
    return handler

from xixing.models import AppError as XixingAppError
from yongheng.models import AppError as YonghengAppError
from zhenyue.models import AppError as ZhenyueAppError
from huichuan.errors import AppError as HuichuanAppError
from zhice.models import AppError as ZhiceAppError
from huanyu.errors import QACPError

app.add_exception_handler(XixingAppError, _make_app_error_handler())
app.add_exception_handler(YonghengAppError, _make_app_error_handler())
app.add_exception_handler(ZhenyueAppError, _make_app_error_handler())
app.add_exception_handler(HuichuanAppError, _make_app_error_handler())
app.add_exception_handler(ZhiceAppError, _make_app_error_handler())


# QACP 标准错误响应格式
async def _qacp_error_handler(request, exc: QACPError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.error_code,
                "message": exc.error_message,
                "detail": exc.error_detail,
            }
        },
        headers=getattr(exc, "headers", None),
    )

app.add_exception_handler(QACPError, _qacp_error_handler)


async def _validation_error_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    # 清洗不可 JSON 序列化的对象（如 validator 抛出的 ValueError）
    for err in errors:
        for key in ("ctx",):
            if key in err and not isinstance(err[key], (str, int, float, bool, list, dict, type(None))):
                err[key] = str(err[key])
    return JSONResponse(
        status_code=422,
        content={
            "code": "VALIDATION_ERROR",
            "message": "请求参数校验失败",
            "detail": errors,
        },
    )

app.add_exception_handler(RequestValidationError, _validation_error_handler)


# ============================================================
# 注册板块
# ============================================================

from common.bus_api import router as bus_router
from xixing.api import router as xixing_router
from yongheng.api import router as yongheng_router
from huanyu.api_compliance import compliance_router as huanyu_rest_router
from huanyu.api_federation import federation_router, peer_router as huanyu_peer_router
from huanyu.api import router as huanyu_api_router
from huanyu.api_ws import router as huanyu_ws_router
from huanyu.api_xihe import router as xihe_router
from zhenyue.api import router as zhenyue_router
from zhenyue.tool_rules_api import tool_rules_router
from huichuan.api import router as huichuan_router
from siku.api import router as siku_router
from zhice.api import router as zhice_router
from zhice.ws import router as zhice_ws_router
# 产品目录/价目表/文档 API — 企业版功能
try:
    from product.api import router as product_router
except ImportError:
    product_router = None

app.include_router(bus_router)
app.include_router(siku_router)
app.include_router(zhice_router)
app.include_router(zhice_ws_router)
app.include_router(xixing_router)
app.include_router(yongheng_router)
app.include_router(huanyu_rest_router)
app.include_router(huanyu_api_router)
app.include_router(huanyu_peer_router)
app.include_router(federation_router)
app.include_router(huanyu_ws_router)
app.include_router(zhenyue_router)
app.include_router(tool_rules_router)
app.include_router(huichuan_router)
app.include_router(xihe_router)

from common.license_api import router as license_router, client_router, ensure_license_schema
app.include_router(license_router)
app.include_router(client_router)

# 技能库管理 API
from osskill.api import router as skills_router
# 技能库管理后台 API（R3 新增）
from osskill.admin_api import router as skills_admin_router, init_admin_api
# 技能库执行 API（probe + execute HTTP 端点）
from osskill.execute_api import router as skills_execute_router
# 技能库内部推送 API（子进程 → 父进程 → Bus → WS → Agent）
from osskill.push_api import router as skills_push_router
# 技能库运行时服务（R3）
from osskill.runtime_service import RuntimeService
from osskill.scheduler import SkillScheduler
from osskill.monitor import Monitor
# 通用文件服务（所有模块共用）
from common.file_service import router as file_router
app.include_router(file_router)

app.include_router(skills_router)
app.include_router(skills_admin_router)
app.include_router(skills_execute_router)
app.include_router(skills_push_router)

# 产品目录/价目表/图片/文档 API
if product_router:
    app.include_router(product_router)

# 多系统门户：统一登录 + 采购/销售文件工作台（portal.enabled 默认开启）
from common.config import get as _get_config
if _get_config("portal.enabled", True):
    try:
        from osskill.implementations.portal.web import router as portal_router
        app.include_router(portal_router)
    except Exception as _pe:
        print(f"  ⚠️  portal module load failed: {_pe}")

# bidding 为独立业务模块，需在 config.yaml 追加 bidding.enabled: true 后启用
if _get_config("bidding.enabled", False):
    try:
        from skills.bidding.api import router as bidding_router, bidding_exception_handler
        from skills.bidding.domain.bid_parser import ParseError as BiddingParseError
        from skills.bidding.domain.errors import BiddingError
        app.include_router(bidding_router)
        app.add_exception_handler(BiddingParseError, bidding_exception_handler)
        app.add_exception_handler(BiddingError, bidding_exception_handler)
    except Exception as _e:
        print(f"  ⚠️  bidding module load failed: {_e}")

# 工作流审批 — 企业配置启用
_wf_skill = None
if _get_config("workflow.enabled", False):
    try:
        from skills.workflow.api import router as _wf_router
        app.include_router(_wf_router)
        print("  🔧 workflow module loaded")
    except Exception as _e:
        print(f"  ⚠️  workflow module load failed: {_e}")

# ── 汇川 MCP Server (Phase 7) ────────────────────────────
# 挂载在 /mcp 路径下，Agent 通过 SSE 或 HTTP 调用知识引擎工具
try:
    from huichuan.mcp import mcp as huichuan_mcp
    if huichuan_mcp is not None:
        app.mount("/mcp", huichuan_mcp.http_app())
        print("  🔧 huichuan MCP server mounted at /mcp")
except Exception as e:
    print(f"  ⚠️  huichuan MCP server not available: {e}")


@app.on_event("startup")
async def startup():
    from common.bus import bus_scheduler, bus

    # 平台能力探测（显式报告降级，不静默）
    try:
        from common.platform_probe import probe_platform
        caps = probe_platform()
        if caps.get("production_ready"):
            print("  🖥️  平台能力: 生产就绪 (cgroup/dmesg/systemd/proc 全部可用)")
        else:
            degraded = [k for k in ("cgroup", "dmesg", "systemd", "proc") if not caps.get(k)]
            print(f"  ⚠️  平台能力降级: {', '.join(degraded)} 不可用 — 资源隔离/OOM检测/自动部署降级（生产请用 Linux 裸机/VM + systemd）")
    except Exception as e:
        print(f"  ⚠️  平台能力探测失败: {e}")

    # License 校验（不锁功能，仅日志告警）
    # Skill 市场未上线，加 5s 超时防止外部 license 服务器不可达时阻塞启动
    try:
        from common.license import check_license_on_startup
        license_info = await asyncio.wait_for(check_license_on_startup(), timeout=5.0)
        plan = license_info.get("plan", "basic")
        if plan == "enterprise" and license_info.get("status") == "valid":
            print(f"  📋 License: 企业版 | {license_info.get('agent_count', 0)}/{license_info.get('agent_limit', 0)} Agent")
        elif plan == "enterprise":
            print(f"  ⚠️  License: 企业版(异常) | 详见日志")
        else:
            print(f"  📋 License: 基础版（免费）")
    except asyncio.TimeoutError:
        print("  ⚠️  License check timeout (>5s), skipping — Skill 市场未上线，后续恢复")
    except Exception as e:
        print(f"  ⚠️  License check skipped: {e}")

    # 数据库 schema — 寰宇
    from huanyu.database import ensure_schema as huanyu_schema
    try:
        await huanyu_schema()
        print("  🌐 huanyu schema ready")
    except Exception as e:
        print(f"  ⚠️  huanyu schema init: {e}")

    # 数据库 schema — 吸星
    from xixing.database import ensure_schema as xixing_schema
    try:
        await xixing_schema()
        print("  ⭐ xixing schema ready")
    except Exception as e:
        print(f"  ⚠️  xixing schema init: {e}")

    # 数据库 schema — 永恒
    from yongheng.database import ensure_schema as yongheng_schema
    try:
        await yongheng_schema()
        print("  🧠 yongheng schema ready")
    except Exception as e:
        print(f"  ⚠️  yongheng schema init: {e}")

    # 数据库 schema — 镇岳
    from zhenyue.database import ensure_schema as zhenyue_schema
    try:
        await zhenyue_schema()
        print("  🛡️  zhenyue schema ready")
    except Exception as e:
        print(f"  ⚠️  zhenyue schema init: {e}")

    # 数据库 schema — 汇川
    from huichuan.database import ensure_schema as huichuan_schema
    try:
        await huichuan_schema()
        print("  📚 huichuan schema ready")
    except Exception as e:
        print(f"  ⚠️  huichuan schema init: {e}")

    # Schema 迁移：knowledge → huichuan（存量部署自动处理）
    try:
        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                DO $$ BEGIN
                  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'knowledge')
                     AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'huichuan') THEN
                    ALTER SCHEMA knowledge RENAME TO huichuan;
                  END IF;
                END $$;
            """)
    except Exception as e:
        print(f"  ⚠️  schema migration knowledge→huichuan: {e}")

    # 数据库 schema — 司库
    from siku.database import ensure_schema as siku_schema
    try:
        await siku_schema()
        print("  💰 siku schema ready")
    except Exception as e:
        print(f"  ⚠️  siku schema init: {e}")

    # 数据库 schema — 执策
    from zhice.database import ensure_schema as zhice_schema
    try:
        await zhice_schema()
        print("  📋 zhice schema ready")
    except Exception as e:
        print(f"  ⚠️  zhice schema init: {e}")

    # 数据库 schema — 技能库
    from osskill.database import ensure_schema as skills_schema
    try:
        await skills_schema()
        print("  🛠️  skills schema ready")
    except Exception as e:
        print(f"  ⚠️  skills schema init: {e}")

    # 工作流审批 — 企业配置启用
    global _wf_skill
    if _get_config("workflow.enabled", False):
        from skills.workflow.skill import WorkflowSkill
        _wf_skill = WorkflowSkill()
        try:
            await _wf_skill.init_engine()
            await _wf_skill.start_bg_tasks()
            print("  🔧 workflow engine ready")
        except Exception as e:
            print(f"  ⚠️  workflow engine init: {e}")
            _wf_skill = None

    # 自动注册 + 绑定 — 给所有 Agent 注册并绑定 bundled Skill（如工作秘书）
    try:
        from common.db import get_pool as _gp
        from osskill.database import (
            get_skill_by_name, bind_skill, register_bundled_skill, SCHEMA as _SK,
        )

        # Step 0: 从管理服同步 agent 注册表（确保新增 agent 在本地可见）
        try:
            from huanyu.peers import pull_agent_registry_from_hub
            _sync_result = await pull_agent_registry_from_hub()
            _synced = _sync_result.get("synced", 0) if isinstance(_sync_result, dict) else 0
            if _synced:
                print(f"  📡 从 hub 同步了 {_synced} 个 agent")
        except Exception:
            pass

        _pool = await _gp()
        # 本底座管辖判定（与 huanyu/inbox_scanner._is_local_agent 同逻辑）：只绑本服
        # agent，不绑跨服 agent。修复大师 2026-08-09 报告：auto_bind 无本服过滤，
        # work_secretary 等无 category 过滤的 skill 绑给目录里所有 agent（含 8 家销售
        # agent，server_ip=10.0.100.3）并全拉起常驻，占满 xihe resident 槽位（16）
        # → bidding 抢不到槽 → 投标 503 LAUNCH_FAILED。
        _host_ip = (_get_config("host_ip", "") or "").strip()
        _local_hosts = {h for h in (socket.gethostname(), _get_config("host", "")) if h}

        def _is_local(_r) -> bool:
            _sip = (_r.get("server_ip") or "").strip()
            _shost = (_r.get("server_host") or "").strip()
            if _sip and _host_ip and _sip != _host_ip:
                return False  # 归属明确为其他底座 → 跨服，跳过
            if _shost and _local_hosts and _shost not in _local_hosts:
                return False
            return True

        async with _pool.acquire() as _conn:
            # 1. 注册 bundled Skill（如不存在则创建，设 active）
            # 69e460e 后商业 Skill 迁到仓库根 skills/，skill.json 路径改指 _repo_root/skills/
            _skill_json = os.path.join(
                _repo_root, "skills", "work_secretary", "skill.json",
            )
            _skill = await register_bundled_skill(_skill_json)
            if _skill and _skill["status"] == "active":
                _h_schema = _huanyu_schema()
                agent_rows = await _conn.fetch(
                    f"SELECT agent_id, category, server_ip, server_host FROM {_h_schema}.agents WHERE status = 'active'"
                )
                _bound = 0
                _new_agents: list[str] = []
                for _ar in agent_rows:
                    if not _is_local(_ar):
                        continue  # 跨服 agent 不绑本服工作秘书
                    if await bind_skill(_ar["agent_id"], _skill["id"]):
                        _bound += 1
                        _new_agents.append(_ar["agent_id"])
                if _bound:
                    print(f"  🤖 已为 {_bound} 个 Agent 自动绑定工作秘书")
                    # 2. 触发运行时加载事件，让 XiheRuntime 启动 Skill 进程
                    try:
                        from huanyu.pubsub import publish
                        for _aid in _new_agents:
                            await publish("huanyu:skill_bind_changed", {
                                "agent_id": _aid,
                                "action": "bind",
                                "skill_id": _skill["id"],
                                "skill_name": "work_secretary",
                            })
                    except ImportError:
                        pass  # pubsub 不可用时降级（仅 DB 绑定）
            else:
                print(f"  🤖 工作秘书注册结果: {_skill}")

            # === 按配置启用 Skill（与联邦 role 解耦） ===
            _role = _get_config("role", "company")
            print(f"  🔍 auto_bind role={_role}")
            # skills.enabled 列表控制，未配置时按 role 兜底
            _enabled_skills = _get_config("skills.enabled", None)
            if _enabled_skills is None:
                # 兜底：旧 role 映射
                _bind_map = {
                    "company":      ("procurement", "sales", "bidding", "work_secretary"),
                    "management":   ("procurement", "sales", "bidding", "work_secretary"),
                    "procurement": ("procurement", "bidding", "work_secretary"),
                    "sales":       ("sales", "bidding", "work_secretary"),
                }
                _to_bind = _bind_map.get(_role, ("procurement", "sales", "bidding", "work_secretary"))
            else:
                _to_bind = tuple(_enabled_skills)
            print(f"  🔍 to_bind={_to_bind}")
            for _sn in _to_bind:
                _sj = os.path.join(
                    _repo_root, "skills", _sn, "skill.json",
                )
                _sk = await register_bundled_skill(_sj)
                print(f"  🔍 {_sn}: register_bundled_skill → {_sk}")
                if _sk and _sk["status"] == "active":
                    _b2_new = 0
                    _b2_existing = 0
                    _n2: list[str] = []
                    # 按 Skill 类型过滤 agent：sales→seller, procurement→buyer, 其余→全部
                    _cat_filter = {"sales": "biz:seller", "procurement": "biz:buyer"}.get(_sn)
                    for _ar in agent_rows:
                        if not _is_local(_ar):
                            continue  # 只绑本底座 agent（跨服不绑）
                        if _cat_filter and _ar.get("category", "") != _cat_filter:
                            continue
                        is_new = await bind_skill(_ar["agent_id"], _sk["id"])
                        if is_new:
                            _b2_new += 1
                        else:
                            _b2_existing += 1
                        # 无论新旧绑定，agent 都需要 Skill 进程 → 全加入启动列表
                        _n2.append(_ar["agent_id"])
                    if _n2:
                        print(f"  🤖 {_sn}: {_b2_new} new + {_b2_existing} existing = {len(_n2)} agents (role={_role})")
                    else:
                        print(f"  🔍 {_sn}: 0 agents (agent_rows={len(agent_rows)}, filter={_cat_filter or 'all'})")
                    try:
                        from huanyu.pubsub import publish
                        for _aid in _n2:
                            await publish("huanyu:skill_bind_changed", {
                                "agent_id": _aid,
                                "action": "bind",
                                "skill_id": _sk["id"],
                                "skill_name": _sn,
                            })
                    except ImportError:
                        pass
                    # 3. 记录需要启动的 Skill（xihe runtime 初始化后统一启动）
                    # 启动白名单（2026-08-31 新服实锤修复）：默认 resident 三件套按 agent 拉起；
                    # bidding/bid_prep 等重型 skill 默认不随启动拉起（依赖 execute_api on-demand
                    # 自动拉起）——但消息若走 agent LLM 循环而非语义路由直达 execute_api，
                    # 未运行的 skill agent 根本看不到 → 13 轮 LLM 空转无产出。
                    # 需要随启动拉起的服务器用 config.yaml skills.launch_at_startup 覆盖。
                    _launch_whitelist = _get_config("skills.launch_at_startup", None)
                    if _launch_whitelist is None:
                        _launch_whitelist = ("procurement", "sales", "work_secretary")
                    if _sn in _launch_whitelist and not hasattr(startup, "_skills_to_launch"):
                        startup._skills_to_launch = []
                    if _sn in _launch_whitelist:
                        # per-agent: 每个 agent 一个独立 Skill 进程
                        _launch_ids = _n2 if _n2 else [f"{_sn}-auto-init"]
                    else:
                        _launch_ids = []
                    if _launch_ids:
                        startup._skills_to_launch.append((_sn, _launch_ids))
    except Exception as e:
        print(f"  ⚠️  auto_bind skills: {e}")

    # 加载指令词注册表（!!command!! 路由）
    try:
        from osskill.command_resolver import get_resolver
        resolver = get_resolver()
        for _attempt in range(3):
            try:
                await resolver.load()
                break
            except Exception as _e:
                if _attempt < 2:
                    await asyncio.sleep(1 + _attempt * 2)
                else:
                    raise
        print(f"  🎯 CommandResolver loaded: {len(resolver.list_all())} commands")
    except Exception as e:
        print(f"  ⚠️  command_resolver load: {e}")

    # ── 永恒插件 Token 自动初始化 ──
    try:
        _yh_pool = await get_pool()
        async with _yh_pool.acquire() as _yh_conn:
            _yh_row = await _yh_conn.fetchrow(
                f"SELECT token_prefix FROM {_zhenyue_schema()}.tokens WHERE token_prefix = 'yh_adm_' AND revoked = FALSE"
            )
            if not _yh_row:
                _raw = "yh_adm_" + secrets.token_hex(24)
                _yh_hash = hashlib.sha256(_raw.encode()).hexdigest()
                await _yh_conn.execute(
                    f"INSERT INTO {_zhenyue_schema()}.tokens (agent_id, token_prefix, token_hash, role, expires_at) "
                    "VALUES ($1, 'yh_adm_', $2, 'admin', '2099-12-31 23:59:59+00')",
                    "agent:sys-eng", _yh_hash,
                )
                print(f"  🔑 yongheng plugin token created: {_raw[:17]}...  ← 配到 yongheng-plugin config")
            else:
                print("  🔑 yongheng plugin token exists (yh_adm_)")

            # 管理 token（zt_admin_）— 测试/运维用
            # fix(2026-09-03): 原在 async with 块外 fetchrow（连接已归还池必抛
            # "connection has been released"）→ zt_admin_ 干净部署上永远建不出来
            _zt_row = await _yh_conn.fetchrow(
                f"SELECT token_prefix FROM {_zhenyue_schema()}.tokens WHERE token_prefix = 'zt_admin_' AND revoked = FALSE"
            )
            if not _zt_row:
                _zt_raw = "zt_admin_" + secrets.token_hex(16)
                _zt_hash = hashlib.sha256(_zt_raw.encode()).hexdigest()
                await _yh_conn.execute(
                    f"INSERT INTO {_zhenyue_schema()}.tokens (agent_id, token_prefix, token_hash, role, expires_at) "
                    "VALUES ('admin', 'zt_admin_', $1, 'admin', '2099-12-31 23:59:59+00')",
                    _zt_hash,
                )
                print(f"  🔑 admin token created: {_zt_raw}  ← 勿泄露")
            else:
                print("  🔑 admin token exists (zt_admin_)")
    except Exception as e:
        print(f"  ⚠️  token init: {e}")

    # 管理员消息总线 — 启用 DB 持久化
    try:
        from common.admin_message import create_admin_bus, AdminMessageBus
        from common.db import get_pool
        bus = create_admin_bus()
        pool = await get_pool()
        await bus.enable_persistence(pool)
        print("  📨 admin message bus persistence enabled")
    except Exception as e:
        print(f"  ⚠️  admin message bus persistence: {e}")

    # 数据库 schema — 产品目录
    from product.database import ensure_schema as product_schema
    try:
        await product_schema()
        print("  📦 product schema ready")
    except Exception as e:
        print(f"  ⚠️  product schema init: {e}")

    # License / 订阅管理 schema（管理服）
    try:
        await ensure_license_schema()
        print("  📋 license schema ready")
    except Exception as e:
        print(f"  ⚠️  license schema init: {e}")

    if _get_config("bidding.enabled", False):
        from skills.bidding.domain.database import ensure_schema as bidding_schema
        try:
            await bidding_schema()
            print("  📋 bidding schema ready")
        except Exception as e:
            print(f"  ⚠️  bidding schema init: {e}")

    # 跨底座通信（Redis Pub/Sub）
    try:
        from huanyu.peers import get_engine, handle_incoming_notification
        engine = get_engine()
        engine.set_incoming_handler(handle_incoming_notification)
        await engine.start()
        print("  📡 huanyu peers engine started")
    except Exception as e:
        print(f"  ⚠️  huanyu peers engine: {e}")

    # 永恒 — embedding 队列
    try:
        from yongheng.embedding import embedding_queue
        await embedding_queue.start()
        print("  🧬 yongheng embedding queue started")
    except Exception as e:
        print(f"  ⚠️  yongheng embedding queue: {e}")

    # 镇岳 — 危险操作规则从 DB 加载（替换硬编码兜底）
    try:
        from zhenyue.guard import init_guard_from_db
        await init_guard_from_db()
        print("  🛡️  zhenyue guard rules loaded from DB")
    except Exception as e:
        print(f"  ⚠️  zhenyue guard DB init: {e}")

    # 镇岳 — 断网应急令牌初始化
    try:
        from zhenyue.api import init_break_glass
        init_break_glass()
    except Exception:
        pass

    # 技能库 — 预热已注册 Skill
    try:
        from osskill.loader import warmup_skills
        asyncio.create_task(warmup_skills())
        print("  🔄 skills warmup scheduled")
    except Exception as e:
        print(f"  ⚠️  skills warmup: {e}")

    # 技能库 — XiheRuntime 初始化
    try:
        from xihe import XiheRuntime, XiheConfig
        # 从 config.yaml 读取出站连接白名单
        _egress_wl = get("egress_monitor", {}).get("whitelist", [])
        # 长任务 Skill（bidding 标书生成：分章生成+评审内循环）单次 execute 可能远超
        # 通用 30s IPC 超时 → 显式放宽到 15 分钟，防止 execute 超时误杀（2026-08-06 线上 500）。
        # 核心 Skill 常驻白名单：投标/采购/销售/工作秘书/工作流 强制常驻，
        # 避免 on_demand 空闲卸载与 execute 超时分支 stop_skill 的反复启停。
        _resident_wl = (
            (get("skill", None) or {}).get("resident_whitelist")
            or ["work_secretary", "procurement", "sales", "bidding", "workflow"]
        )
        # 收费 Skill 到期检查回调：subscriptions 订阅 + 本地 license 任一到期 → 拒绝。
        # 企业级部署假设：agent_id 即 enterprise_id（subscriptions 表按企业粒度，不区分 module）。
        async def _check_paid_skill_license(skill_name: str, agent_id: str) -> bool:
            # 运维总开关：QINGTIAN_DISABLE_LICENSE_CHECK=1 → 直接放行（临时白名单，license 机制对齐后移除）。
            # 与 execute_api.py:520 预检同源同一开关，补齐 launch_skill 内检（agent_runtime.py:324）
            # 与常驻巡检 sweep（agent_runtime.py:421）未覆盖的两处——否则 execute 放行到 launch 仍被拦
            # （2026-08-08 大师报 bidding LAUNCH_FAILED 根因；销售 skill 即靠此开关 + 常驻放行）。
            if os.environ.get("QINGTIAN_DISABLE_LICENSE_CHECK", "").lower() in ("1", "true", "yes"):
                print(f"  ⚠️ [license] 总开关 QINGTIAN_DISABLE_LICENSE_CHECK=1 → {skill_name} agent={agent_id} 临时放行")
                return True
            try:
                if not skill_name or not agent_id:
                    return False
                # 1) subscriptions 订阅有效？（plan='pro' 且未到期）
                # C3 (R11): subscriptions 表列为 plan/end_date（非 tier/expires_at），
                # 旧列名查询 UndefinedColumnError 被当"到期"→ 付费 Skill 全部被拦。
                from skills.bidding.domain.db import get_pool
                pool = await get_pool()
                row = await pool.fetchrow(
                    "SELECT plan, end_date FROM billing.subscriptions "
                    "WHERE enterprise_id=$1 AND plan='pro' AND end_date >= NOW() "
                    "ORDER BY end_date DESC LIMIT 1",
                    agent_id,
                )
                if row is None:
                    print(f"  ⚠️ [license] {skill_name} agent={agent_id} 订阅缺失/非 pro/已到期 → 拒绝")
                    return False
                # 2) 本地 license 文件有效？（约定 licenses/{skill_name}.license，含试用签发）
                from osskill.market_integration import _LICENSE_DIR
                lic_path = _LICENSE_DIR / f"{skill_name}.license"
                if not lic_path.exists():
                    print(f"  ⚠️ [license] {skill_name} agent={agent_id} 本地 license 缺失: {lic_path}")
                    return False
                lic = json.loads(lic_path.read_text(encoding="utf-8"))
                lic_exp = lic.get("expires_at", "")
                if lic_exp:
                    lic_dt = datetime.fromisoformat(lic_exp.replace("Z", "+00:00"))
                    if lic_dt.tzinfo is None:
                        lic_dt = lic_dt.replace(tzinfo=timezone.utc)
                    if lic_dt <= datetime.now(timezone.utc):
                        print(f"  ⚠️ [license] {skill_name} agent={agent_id} 本地 license 到期 {lic_exp} → 拒绝")
                        return False
                return True
            except Exception as e:
                print(f"  ⚠️ [license] {skill_name} agent={agent_id} 检查异常 → 视为到期: {e}")
                return False

        # 收费 Skill 到期检查：名单内 Skill 在 launch/execute 拦截 + 常驻巡检 stop。
        # 订阅或本地 license 任一到期 → 拒绝（防白漂）。
        _paid_skills = (
            (get("skill", None) or {}).get("license_checked_skills")
            or ["bidding", "sales"]
        )
        # 多租户共享底座按内存档位定常驻槽位（波哥 2026-08-08 约定：16G→32、32G→64）。
        # config.yaml xihe.resident_slots / xihe.max_processes 可显式覆盖；否则按 /proc/meminfo 推断。
        # max_processes ≥ 常驻槽位 × 2：留 on_demand 临时进程余量，避免常驻占满后按需进程无槽可拉。
        _cfg_xihe = get("xihe", {}) or {}
        _resident_slots = _cfg_xihe.get("resident_slots")
        if not _resident_slots:
            try:
                with open("/proc/meminfo") as _f:
                    for _line in _f:
                        if _line.startswith("MemTotal:"):
                            _gb = int(_line.split()[1]) / 1024 / 1024
                            _resident_slots = 192 if _gb >= 64 else (96 if _gb >= 32 else (48 if _gb >= 16 else 16))
                            break
            except Exception:
                _resident_slots = 48
        _max_procs = _cfg_xihe.get("max_processes") or _resident_slots * 3
        # 按 Skill 内存上限：以 config.py 默认映射为底（bidding/bid_prep 2GiB——标书
        # 生成 OCR/PyMuPDF/docx 嵌图、技术规范整理 LibreOffice 无头转 .doc 都是内存
        # 密集型，全局 512MiB 崩溃线上实锤）+ config.yaml xihe.per_skill_memory_limit_bytes
        # override 合并。2026-08-29 小智实锤修复：原硬编码 {"bidding": 2GiB} 字面量会
        # 整体挤掉 config.py 默认并集——bid_prep 等 skill 只改 config.py 不生效，
        # 运行时仍 512MiB；改为以 XiheConfig 默认为底，新 skill 提额只动 config.py 一处。
        _per_skill_mem = dict(XiheConfig().per_skill_memory_limit_bytes)
        _mem_ovr = _cfg_xihe.get("per_skill_memory_limit_bytes")
        if isinstance(_mem_ovr, dict):
            _per_skill_mem.update({str(k): int(v) for k, v in _mem_ovr.items() if isinstance(v, (int, float))})
        print(f"  🧮 xihe slots: resident={_resident_slots} max_processes={_max_procs}")
        runtime_config = XiheConfig(
            egress_whitelist=_egress_wl,
            ipc_request_timeout=900.0,
            resident_skill_whitelist=_resident_wl,
            license_checked_skills=_paid_skills,
            license_check_callback=_check_paid_skill_license,
            resident_slots=_resident_slots,
            max_processes=_max_procs,
            per_skill_idle_timeout={"bidding": 1800, "procurement": 1800},
            per_skill_memory_limit_bytes=_per_skill_mem,
        )
        _skill_runtime = XiheRuntime(config=runtime_config)
        await _skill_runtime.start()
        # 注册到 agent_runtime 模块全局（供 auto_bind 等调用 launch_skill）
        from xihe.agent_runtime import set_manager
        set_manager(_skill_runtime)
        # 注入到 admin API
        _skill_runtime_service = RuntimeService(
            xihe_runtime=_skill_runtime,
            bus=bus,
        )
        init_admin_api(runtime_service=_skill_runtime_service)

        # 将 runtime 引用注入 osskill.api（供运行时端点使用）
        from osskill.api import api_runtime_start, api_runtime_stop, api_runtime_restart, api_runtime_stats
        api_runtime_start._runtime = _skill_runtime
        api_runtime_stop._runtime = _skill_runtime
        api_runtime_restart._runtime = _skill_runtime
        api_runtime_stats._runtime = _skill_runtime

        # 将 runtime 引用注入 osskill.execute_api（供 execute/probe 端点使用）
        from osskill.execute_api import api_execute_skill, api_probe_skill
        api_execute_skill._runtime = _skill_runtime
        api_probe_skill._runtime = _skill_runtime

        print("  🚀 xihe runtime started")

        # ── 启动之前 auto_bind 记录的 Skill 进程 ──
        _skills_to_launch = getattr(startup, "_skills_to_launch", [])
        for _sn, _launch_ids in _skills_to_launch:
            try:
                _launched = 0
                _skipped = 0
                for _aid in _launch_ids:
                    try:
                        await _skill_runtime.launch_skill(_sn, agent_id=_aid)
                        _launched += 1
                    except Exception as _le2:
                        if "already running" in str(_le2):
                            _skipped += 1
                        else:
                            print(f"  ⚠️  {_sn}/{_aid} 启动失败: {_le2}")
                if _launched or _skipped:
                    print(f"  🚀 {_sn}: {_launched} launched + {_skipped} already running = {len(_launch_ids)} agents")
            except Exception as _le:
                print(f"  ⚠️  {_sn} Skill 启动失败: {_le}")

        # 统一 WS 管理器 — 对接 Bus，支持子进程推送 → WS 实时投递
        try:
            from common.ws_manager import ws_manager, ws_endpoint as _ws_ep
            # 注册 WS 端点（用于 Bus → Agent 实时推送）
            _raw_app.add_api_websocket_route("/v1/bus/ws/{agent_id}", _ws_ep)
            # 接入 Bus
            bus.set_ws_manager(ws_manager)
            # 启动健康检查
            await ws_manager.start_health_check()
            print("  🔌 bus ws manager ready (endpoint=/v1/bus/ws/{agent_id})")
        except Exception as e:
            print(f"  ⚠️  bus ws manager init: {e}")
    except Exception as e:
        print(f"  ⚠️  xihe runtime init: {e}")
        _skill_runtime = None

    # InboxScanner — 共享轮询 + IPC 直派（替代 Skill 各自轮询）
    if _skill_runtime:
        try:
            from huanyu.inbox_scanner import get_scanner
            _scanner = get_scanner()
            _scanner.set_runtime(_skill_runtime)
            await _scanner.start()
            print("  📬 inbox scanner started")
        except Exception as e:
            print(f"  ⚠️  inbox scanner init: {e}")

    # 技能库 — 吊销服务（Skill 市场未上线，加超时保护）
    try:
        if _skill_runtime:
            from osskill_acssa.acssa_client import AcssaClient
            from osskill_acssa.revocation_service import RevocationService
            _acssa_client = AcssaClient()
            _revocation_svc = RevocationService(
                acssa_client=_acssa_client,
                runtime_service=_skill_runtime_service,
            )
            await asyncio.wait_for(_revocation_svc.start(), timeout=5.0)
            # 注入到 admin API
            init_admin_api(
                runtime_service=_skill_runtime_service,
                revocation_service=_revocation_svc,
            )
            # 启动定时任务
            _skill_scheduler = SkillScheduler()
            _skill_scheduler.add_task(
                "blacklist_poll", 24, _revocation_svc.poll_once,
            )
            # 技能自动升级：每 6 小时检查市场更新
            try:
                from osskill.skill_updater import check_and_upgrade_all
                _skill_scheduler.add_task(
                    "skill_auto_update", 6,
                    lambda: check_and_upgrade_all(dry_run=False),
                    run_immediately=False,  # 首次不立即执行，等 6h 后再查
                )
            except ImportError:
                pass
            await _skill_scheduler.start()
            print("  🔔 skill revocation service started")
    except ImportError:
        pass  # osskill_acssa 是企业版模块，社区版不包含
    except Exception as e:
        print(f"  ⚠️  skill revocation service: {e}")

    # 吸星 — 内置调度器
    try:
        from xixing.scheduler import start as xixing_scheduler_start
        await xixing_scheduler_start()
        print("  ⏰ xixing scheduler started")
    except Exception as e:
        print(f"  ⚠️  xixing scheduler: {e}")

    # 汇川 — 内置调度器
    try:
        from huichuan.cron import start as huichuan_cron_start
        await huichuan_cron_start()
        print("  ⏰ huichuan cron started")
    except Exception as e:
        print(f"  ⚠️  huichuan cron: {e}")

    # Agent 适配器接口层 — 初始化启用的适配器
    try:
        from gateway.adapters.registry import get_registry
        await get_registry().initialize_from_config()
        print("  🔌 agent adapter layer initialized")
    except Exception as e:
        print(f"  ⚠️  agent adapter init: {e}")

    # 总线 — 启动时重建 Agent 生命周期状态
    try:
        await bus_scheduler.startup_reconcile()
        print("  🚌 bus scheduler reconcile complete")
    except Exception as e:
        print(f"  ⚠️  bus scheduler reconcile: {e}")

    # 寰宇 — Agent 心跳监控（后台异步任务）
    try:
        from huanyu.directory import start_heartbeat_monitor
        asyncio.create_task(start_heartbeat_monitor(300))
        print("  💓 huanyu heartbeat monitor started")
    except Exception as e:
        print(f"  ⚠️  huanyu heartbeat monitor: {e}")

    # 寰宇 — 消息定时清理
    try:
        from huanyu.cron import start as huanyu_cron_start
        await huanyu_cron_start()
        print("  🧹 huanyu cron started")
    except Exception as e:
        print(f"  ⚠️  huanyu cron: {e}")

    # 寰宇 — skill 后台 info 消息出站投递（谈判清单/销售日报 → 飞书）
    try:
        from huanyu.outbound import start as huanyu_outbound_start
        await huanyu_outbound_start()
        print("  📤 huanyu outbound pusher started")
    except Exception as e:
        print(f"  ⚠️  huanyu outbound pusher: {e}")

    # 产品目录 — 定时任务（价目表过期处理）
    try:
        from product.cron import start as product_cron_start
        await product_cron_start(is_management_role=is_management())
        print("  📋 product cron started")
    except Exception as e:
        print(f"  ⚠️  product cron: {e}")

    # 财神 — 年费到期定时检查（后台异步任务）
    try:
        from siku.annual_cron import start as siku_cron_start
        await siku_cron_start()
        print("  💰 siku annual cron started")
    except Exception as e:
        print(f"  ⚠️  siku annual cron: {e}")

    # 财神 — 财务 Agent（监听 payment_notify → 银联查账 → 自动充值）
    try:
        from siku.finance_agent import start as finance_agent_start
        await finance_agent_start()
        print("  🏦 siku finance agent started")
    except Exception as e:
        print(f"  ⚠️  siku finance agent: {e}")

    # 执策 — 看门狗（后台 timeout_checker）
    try:
        from zhice.timeout_checker import start as zhice_watchdog_start
        await zhice_watchdog_start()
        print("  📋 zhice timeout checker started")
    except Exception as e:
        print(f"  ⚠️  zhice timeout checker: {e}")

    # 镇岳 — 后台调度器（审批过期 + 告警 flush）
    try:
        from zhenyue.scheduler import start as zhenyue_scheduler_start
        await zhenyue_scheduler_start()
        print("  ⏰ zhenyue scheduler started")
    except Exception as e:
        print(f"  ⚠️  zhenyue scheduler: {e}")

    # 镇岳 — 审计日志定期清理（每天凌晨 3 点）
    try:
        from zhenyue.audit_service import cleanup_old_audit_logs
        async def _audit_cleanup_loop():
            while True:
                await asyncio.sleep(86400)  # 24h
                try:
                    deleted = await cleanup_old_audit_logs()
                    if deleted:
                        print(f"  🧹 audit log cleanup: {deleted} records removed")
                except Exception:
                    pass
        asyncio.create_task(_audit_cleanup_loop())
        print("  🧹 zhenyue audit log cleanup scheduled (daily)")
    except Exception as e:
        print(f"  ⚠️  zhenyue audit cleanup: {e}")

    # Redis — 跨底座 Pub/Sub 前置检查
    try:
        from huanyu.config import get_redis_url
        from redis.asyncio import from_url as redis_from_url
        redis_url = get_redis_url()
        r = redis_from_url(redis_url)
        try:
            await r.ping()
            print(f"  📡 Redis OK: {redis_url}")
        finally:
            await r.aclose()
    except Exception as e:
        print(f"  ⚠️  Redis 不可达: {e}")

    # WireGuard — 底座间通讯前置检查
    try:
        from common.network import ensure_wireguard, get_wireguard_info
        wg_info = await ensure_wireguard(auto_install=False)
        if not wg_info["installed"]:
            print("  ⚠️  WireGuard 未安装，跨底座通讯不可用。请执行: apt-get install -y wireguard-tools")
        elif not wg_info["configured"]:
            print("  ⚠️  WireGuard 已安装但 wg0 未配置。请配置后执行: wg-quick up wg0")
        else:
            print(f"  🔒 WireGuard wg0 UP — {wg_info['peers']} peers, rx={wg_info['transfer_rx']} tx={wg_info['transfer_tx']}")
    except Exception as e:
        print(f"  ⚠️  WireGuard check: {e}")

    # fastembed 状态（embedding queue 已在上面第146行启动）
    try:
        from yongheng.embedding import embedding_queue
        print("  🧬 yongheng fastembed: queue active (local ONNX)")
    except ImportError:
        print("  ⚠️  fastembed not installed, yongheng search uses keyword-only")

    # ── 跨企业通讯：企业底座长连 Hub（HubClient，2026-08-20 贪狼接线）──
    try:
        from huanyu import config as _hcfg
        from huanyu.hub_client import HubClient, set_hub_client
        from huanyu.messaging import handle_hub_envelope
        _org_id = _hcfg.get_org_id()
        if _org_id and _hcfg.get_cross_org_enabled():
            _hub_ws = _hcfg.get_hub_ws_url()
            _token = _hcfg.get_org_token()
            if not _hub_ws:
                print("  ⚠️  跨企业通讯：已配置企业码但缺 huanyu.hub_endpoint，跳过 Hub 长连")
            elif not _token:
                print("  ⚠️  跨企业通讯：已配置企业码但缺 HUANYU_ORG_TOKEN，跳过 Hub 长连（认证据 Hub 签发）")
            else:
                _hub_client = HubClient(
                    org_id=_org_id, token=_token, hub_url=_hub_ws,
                    on_message=handle_hub_envelope,
                )
                set_hub_client(_hub_client)
                await _hub_client.start()
                print(f"  🛰️  跨企业通讯：Hub 长连已拉起 org={_org_id}")
        else:
            print("  🔸 跨企业通讯：未配置企业码（huanyu.organization_id），企业内/WG 路由照常")
    except Exception as e:
        print(f"  ⚠️  跨企业通讯 HubClient 启动失败: {e}")


@app.on_event("shutdown")
async def shutdown():
    async def _stop_with_timeout(coro, name: str, timeout: float = 10.0):
        try:
            await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            print(f"  ⚠️  {name} shutdown timed out after {timeout}s")
        except Exception:
            pass

    from xixing.scheduler import stop as xixing_scheduler_stop
    await _stop_with_timeout(xixing_scheduler_stop(), "xixing scheduler")

    from huichuan.cron import stop as huichuan_cron_stop
    await _stop_with_timeout(huichuan_cron_stop(), "huichuan cron")

    from huanyu.cron import stop as huanyu_cron_stop
    await _stop_with_timeout(huanyu_cron_stop(), "huanyu cron")

    from product.cron import stop as product_cron_stop
    await _stop_with_timeout(product_cron_stop(), "product cron")

    from huanyu.peers import get_engine
    await _stop_with_timeout(get_engine().stop(), "huanyu peers engine")

    from huanyu.hub_client import get_hub_client, set_hub_client
    _hub_client = get_hub_client()
    if _hub_client is not None:
        await _stop_with_timeout(_hub_client.stop(), "huanyu hub client")
        set_hub_client(None)

    from yongheng.embedding import embedding_queue
    await _stop_with_timeout(embedding_queue.stop(), "yongheng embedding queue")

    from zhice.timeout_checker import stop as zhice_watchdog_stop
    await _stop_with_timeout(zhice_watchdog_stop(), "zhice timeout checker")

    from zhenyue.scheduler import stop as zhenyue_scheduler_stop
    await _stop_with_timeout(zhenyue_scheduler_stop(), "zhenyue scheduler")

    # 技能库 — XiheRuntime 停止
    if '_skill_runtime' in dir() and _skill_runtime is not None:
        await _stop_with_timeout(_skill_runtime.shutdown(), "xihe runtime")

    # 工作流 Skill 关闭
    if '_wf_skill' in dir() and _wf_skill is not None:
        await _stop_with_timeout(_wf_skill.shutdown(), "workflow skill")

    from common.db import close_pool
    await _stop_with_timeout(close_pool(), "db pool close")


@app.get("/health")
async def health():
    wg_info = {}
    try:
        from common.network import get_wireguard_info
        wg_info = get_wireguard_info()
    except Exception:
        pass
    try:
        from common.platform_probe import get_platform_capabilities
        platform = get_platform_capabilities()
    except Exception:
        platform = {}
    return {
        "status": "ok",
        "service": "qingtian",
        "version": "0.2.0",
        "wireguard": wg_info,
        "platform": platform,
    }


@app.get("/version")
async def version():
    return {"version": "0.2.0", "service": "ACSSA 智能体操作系统 — 底座 OS"}


@app.get("/metrics")
async def metrics():
    from common.metrics import get_metrics_text
    return PlainTextResponse(content=get_metrics_text(), media_type="text/plain; charset=utf-8")


# ── 静态文件（acssa.cn 网站资源）──
# 波哥 2026-08-10：acssa 官网与本企业门户为两个独立系统，互不搭界。
# 原 /static 挂载 acssa 官网资源已摘除（企业门户各系统页面均用自己的
# static 目录，不依赖 /static）；acssa 站点由独立部署自行服务静态资源。


@app.get("/")
async def root():
    """根路径 → 统一门户登录页（默认入口）。

    波哥 2026-08-10：统一企业门户（/portal）作为系统默认入口，
    访问域名根地址即进登录页，按账号类型跳转投标/采购/销售/管理。
    acssa.cn 官网为独立系统页面（独立站点/域名），不属于本系统根路径职责。
    """
    return RedirectResponse(url="/portal", status_code=302)


# ── Web 登录端点（门户统一登录 + 各系统前端） ──────────


def _app_for_account(category: str, subcategory: str) -> str:
    """按账号类型计算跳转目标系统（app 路由）。

    账号类型由 huanyu.agents 的 category + subcategory 共同标识：
      - 投标员:   biz:buyer  + bidding
      - 采购员:   biz:buyer  + standard-procurement
      - 销售员:   biz:seller + standard-sales
      - 主管:     biz:buyer / biz:seller + supervisor → 采购 / 销售
      - 管理:     sys:admin（bootstrap admin）→ admin（独立管理门户页）
      - 默认 → bidding
    """
    if category == "sys:admin":
        return "admin"
    if subcategory == "bidding":
        return "bidding"
    if subcategory == "standard-procurement":
        return "procurement"
    if subcategory == "standard-sales":
        return "sales"
    if subcategory == "supervisor":
        if category == "biz:seller":
            return "sales"
        if category == "biz:buyer":
            return "procurement"
    return "bidding"


@app.post("/v1/auth/token")
async def v1_auth_token(body: dict = Body(...)):
    """登录获取 Bearer Token。

    支持两种登录方式：
    1. 镇岳 Bootstrap Admin：username 任意，password = ZHENYUE_ADMIN_TOKEN 环境变量值
    2. Agent 密码登录：username = agent_id，password = 已设置的密码

    Returns:
        token: Bearer token（可直接用于 Authorization header）
        agent_id: Agent 标识
        role: 角色（admin / agent）
        enterprise_id: 企业标识（与 agent_id 相同，供各系统前端使用）
        category: 账号类别（biz:buyer / biz:seller / sys:admin 等）
        subcategory: 账号子类别（bidding / standard-procurement / standard-sales ...）
        app: 跳转目标系统（bidding / procurement / sales，门户据此路由）
    """
    username = (body.get("username", "") or "").strip()
    password = body.get("password", "") or ""

    if not username or not password:
        raise HTTPException(400, detail="用户名和密码不能为空")

    # 路径 1：Bootstrap Admin Token 作为密码登录（超级管理员）
    bootstrap = os.environ.get("ZHENYUE_ADMIN_TOKEN", "")
    # review(2026-08-24 P1): 常数时间比较，防时序侧信道
    import secrets as _secrets
    if bootstrap and _secrets.compare_digest(password, bootstrap):
        # 不直接返回 bootstrap token（R6-7: 避免暴露长生命周期共享凭据）
        # 改为签发一个可吊销的 session token
        from common.db import get_pool
        from zhenyue.token_service import create_token
        _pool = await get_pool()
        async with _pool.acquire() as _conn:
            result = await create_token(_conn, "admin", "admin")
        return {
            "token": result["token"],
            "agent_id": "admin",
            "role": "admin",
            "enterprise_id": "admin",
            "category": "sys:admin",
            "subcategory": "supervisor",
            "app": _app_for_account("sys:admin", "supervisor"),
        }

    # 路径 2：Agent 密码登录（查 zhenyue.agents 表）
    from common.db import get_pool
    from common.password import verify_password
    from zhenyue.token_service import create_token

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT agent_id, name, password_hash, status, trust_level, category, subcategory FROM {_huanyu_schema()}.agents WHERE agent_id = $1",
            username,
        )
        if row is None:
            raise HTTPException(401, detail="用户名或密码错误")

        if row["status"] != "active":
            raise HTTPException(403, detail="账号未激活，请联系管理员审核")

        if not row["password_hash"]:
            raise HTTPException(401, detail="该账号未设置密码，请使用管理员方式登录后设置")

        if not verify_password(password, row["password_hash"]):
            raise HTTPException(401, detail="用户名或密码错误")

        # 确定角色：trust_level 为 'enterprise' 或 admin 类别映射为 admin
        role = "admin" if (row["trust_level"] in ("enterprise", "admin")) else "agent"

        result = await create_token(conn, username, role)

    category = row.get("category") or ""
    subcategory = row.get("subcategory") or ""
    return {
        "token": result["token"],
        "agent_id": result["agent_id"],
        "role": result["role"],
        "enterprise_id": username,
        "category": category,
        "subcategory": subcategory,
        "app": _app_for_account(category, subcategory),
    }


def main():
    import uvicorn
    port = get("service.port", 1996)
    print(f"  🚀 ACSSA 智能体操作系统启动 — :{port}")
    print(f"  📦 板块: xixing | yongheng | huanyu | zhenyue | huichuan | siku | zhice")
    print(f"  📍 配置: /opt/qingtian/config.yaml")
    print(f"  🧭 角色: {get('role', 'unknown')}")
    print()
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload="--reload" in sys.argv,
        log_level="info",
        timeout_keep_alive=65,
        timeout_graceful_shutdown=30,
    )


# 直接 ASGI 包装：在所有中间件和路由注册之后执行，确保 RoleCheck 在最外层包裹 app
# 保留原始 FastAPI app 引用，供 startup 中注册 WebSocket 路由用
_raw_app = app
from gateway.middleware import RoleCheckMiddlewareASGI
app = RoleCheckMiddlewareASGI(app)


if __name__ == "__main__":
    main()
