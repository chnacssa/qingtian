"""XiheRuntime — 羲和子进程管理器

职责：
- 生成 Skill 子进程（每个 Skill 独立进程）
- 通过 IPC 与子进程通信（JSON-RPC over STDIO）
- 健康检查 + 崩溃自动重启（指数退避）
- 资源限制（进程数上限、内存上限）
- 优雅关闭

用法:
    runtime = XiheRuntime()
    await runtime.start()

    # 启动一个 Skill
    skill = await runtime.launch_skill("bidding", agent_id="agent_01")

    # 调用
    result = await skill.execute({"action": "score_bid", ...})

    # 停止一个 Skill
    await runtime.stop_skill("bidding", agent_id="agent_01")

    # 关闭全部
    await runtime.shutdown()
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import XiheConfig
from .errors import (
    ProcessError,
    ProcessNotFoundError,
    ResourceExhaustedError,
)
from common.metrics import record_skill_exec

logger = logging.getLogger("xihe.runtime")

# ── 生命周期常量 ──
LIFECYCLE_RESIDENT = "resident"
LIFECYCLE_ON_DEMAND = "on_demand"

# ── IPC 代理路径白名单（A6/R11）──
# trusted Skill 经代理调用底座 API 时，仅允许访问业务数据前缀；
# 凭据/Agent 管理/审计管理/授权类端点一律拒绝，未命中白名单同样拒绝（fail-closed）。
# 黑名单优先（双保险）：即使未来白名单前缀放宽，管理端点仍被拦截。
_PROXY_DENY_PREFIXES = (
    "/v1/auth/",
    "/v1/zhenyue/tokens",
    "/v1/zhenyue/break-glass",
    "/v1/zhenyue/quarantine",
    "/v1/zhenyue/backup",
    "/v1/xihe/",
    "/v1/skills/admin",
    "/v1/license/",
    "/v1/huanyu/agents",
    "/v1/huanyu/runtime",
    "/v1/yongheng/token/",
    "/v1/yongheng/verification/",
    "/v1/yongheng/ingest",
    "/v1/yongheng/batch-",
    "/v1/huichuan/refine",
    "/v1/huichuan/hooks",
    "/v1/huichuan/connector/",
    # 2026-08-27: zhice 两个 admin 门端点显式拉黑（handler 内 role 检查之外的双保险——
    # 代理转发带 admin Bearer，若白名单未来放宽到 /v1/zhice/ 前缀，deny 优先仍拦）
    "/v1/zhice/policies",
    "/v1/zhice/workflows/cleanup",
    "/peers/",
)
_PROXY_ALLOW_PREFIXES = (
    "/v1/huanyu/messages",
    "/v1/huanyu/admin-messages",
    "/v1/huanyu/reminders",
    "/v1/huanyu/inbox",
    "/v1/yongheng/memories",
    "/v1/yongheng/profile",
    "/v1/yongheng/trajectory",
    "/v1/yongheng/health",
    "/v1/yongheng/session/",  # 找回记忆（recover 只读本 agent 自己的记忆，安全）
    "/v1/huichuan/",
    "/v1/zhice/tasks",
    "/v1/zhice/steps/",  # 2026-08-27: SOP 步骤生命周期（start/heartbeat/submit/review…）
                       # work_secretary.zhice_bridge 步骤提交被拦实锤——原白名单只放
                       # tasks 漏了 steps，属覆盖面遗漏非收严回归（8ee40c7b 前后判定一致）
    "/v1/zhenyue/audit/logs",
    "/v1/product/",
    "/v1/bidding/",
    "/v1/siku/",
)


def _proxy_path_allowed(path: str) -> bool:
    """IPC 代理路径放行判定：黑名单优先，未命中白名单拒绝。

    P1 (2026-08-27 review #6): httpx 发送前对明文 dot-segment 做 RFC3986 归一化
    （实测 "/v1/yongheng/profile/../../../v1/zhenyue/tokens" 实发
    "/v1/zhenyue/tokens"），原实现 startswith 匹配的是归一化前的原串 →
    白名单可被 ../ 穿越、黑名单可被绕过。修：先归一化再匹配（归一化后
    语义即 httpx 实发语义，两层判断与真实请求一致）。
    """
    if not path or not path.startswith("/"):
        return False
    # 先 percent-解码再 dot-segment 归一化：ASGI 服务端 scope["path"] 是解码后
    # 的串，%2e%2e 到达服务端即 ".."——代理层须按解码+归一化语义判定，不能
    # 只依赖服务端路由 404 兜底。业务白名单前缀均为纯 ASCII 无编码字符，
    # 解码不产生误放行（解码后不匹配 → fail-closed 拒绝）。
    import posixpath
    from urllib.parse import unquote
    norm = posixpath.normpath(unquote(path))
    # normpath 可能把异常前缀变形（"//x" → "/x"），统一以归一化结果判定
    if not norm.startswith("/"):
        norm = "/" + norm
    for deny in _PROXY_DENY_PREFIXES:
        if norm.startswith(deny):
            return False
    for allow in _PROXY_ALLOW_PREFIXES:
        if norm.startswith(allow):
            return True
    return False

# ── 全局进程计数器（用于命名） ──
_process_counter = 0


def _effective_memory_limit(skill_name: str, cfg) -> int:
    """Skill 有效内存上限（字节）：per-skill 覆盖优先于全局。

    bidding 2GiB（2026-08-27 线上实锤：OCR/PyMuPDF/docx 嵌图内存密集，
    全局 512MiB 下 OpenBLAS Memory allocation failed 崩进程）。
    _spawn 注入与巡检 check_memory_pressure 同口径取值。"""
    per = getattr(cfg, "per_skill_memory_limit_bytes", None) or {}
    if skill_name in per:
        return int(per[skill_name])
    return cfg.memory_limit_bytes





@dataclass
class ChildProcess:
    """单个 Skill 子进程的状态"""
    skill_name: str
    agent_id: str
    lifecycle: str = LIFECYCLE_RESIDENT
    """生命周期模式: resident (常驻) / on_demand (空闲超时自动卸载)"""
    process: asyncio.subprocess.Process | None = None
    transport: Any = None  # StdioTransport (parent side)
    ipc_server: Any = None  # IPCServer (parent side)
    started_at: float = 0.0
    restart_count: int = 0
    last_error: str = ""
    downgraded: bool = False
    """是否已被资源监控降级到 low 优先级（重启后重置）"""
    trust_level: str = "trusted"
    """信任级别: trusted / untrusted / revoked"""
    idle_since: float | None = None
    """on_demand 模式下，最后一次 execute() 完成的时间戳。None = 正在使用或 resident"""
    _idle_timer_task: asyncio.Task | None = None
    """on_demand 空闲超时卸载定时器"""
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SkillHandle:
    """子进程 Skill 的操作句柄（供外部调用）

    通过此句柄向子进程发送 IPC 请求。
    on_demand 模式的 Skill 在 execute() 完成后进入空闲计时，
    超时未使用则自动卸载释放进程槽。
    """

    def __init__(self, child: ChildProcess, timeout: float, runtime: "XiheRuntime | None" = None):
        self._child = child
        self._timeout = timeout
        self._runtime = runtime
        self._closed = False

    @property
    def skill_name(self) -> str:
        return self._child.skill_name

    @property
    def agent_id(self) -> str:
        return self._child.agent_id

    @property
    def is_running(self) -> bool:
        if self._closed:
            return False
        proc = self._child.process
        return proc is not None and proc.returncode is None

    async def execute(self, params: dict) -> dict:
        """调用子进程的 execute 方法，完成后 on_demand 模式进入空闲计时

        IPC 失败（超时等）时强制卸载，防止恶意 Skill 阻塞永不释放进程槽。
        """
        _sn = self._child.skill_name
        _method = params.get("action", "execute") if isinstance(params, dict) else "execute"
        _start = time.monotonic()
        try:
            self._mark_active()
            result = await self._ipc_call("execute", params)
            record_skill_exec(_sn, _method, "ok", (time.monotonic() - _start) * 1000)
            return result
        except ProcessError:
            record_skill_exec(_sn, _method, "error", (time.monotonic() - _start) * 1000)
            if self._runtime is not None and self._child.lifecycle == LIFECYCLE_ON_DEMAND:
                try:
                    await self._runtime.stop_skill(self._child.skill_name, self._child.agent_id)
                except ProcessNotFoundError:
                    pass
            raise
        except Exception:
            record_skill_exec(_sn, _method, "error", (time.monotonic() - _start) * 1000)
            raise
        finally:
            self._mark_idle()

    async def validate(self, params: dict) -> list:
        """调用子进程的 validate 方法"""
        return await self._ipc_call("validate", params)

    async def get_metadata(self) -> dict:
        """获取元数据"""
        return await self._ipc_call("get_metadata")

    async def ping(self) -> bool:
        """健康检查"""
        try:
            result = await self._ipc_call("ping", timeout=5.0)
            return result == "pong"
        except Exception:
            return False

    def _mark_active(self) -> None:
        """标记为活跃：取消空闲定时器"""
        child = self._child
        child.idle_since = None
        if child._idle_timer_task is not None:
            child._idle_timer_task.cancel()
            child._idle_timer_task = None

    def _mark_idle(self) -> None:
        """标记为空闲：on_demand 模式启动超时卸载定时器"""
        child = self._child
        if child.lifecycle != LIFECYCLE_ON_DEMAND:
            return
        child.idle_since = time.time()
        # 如果没有 runtime 上下文或没有配置空闲超时，不启动定时器
        if self._runtime is None or self._runtime.config.idle_timeout_seconds <= 0:
            return
        timeout = self._runtime.config.idle_timeout_seconds
        child._idle_timer_task = asyncio.create_task(
            self._idle_timeout(timeout),
            name=f"idle-timer-{child.skill_name}",
        )

    async def _idle_timeout(self, timeout: float) -> None:
        """空闲超时后自动卸载"""
        try:
            await asyncio.sleep(timeout)
            # 超时后仍然空闲 → 卸载
            if self._runtime is not None and self._child.idle_since is not None:
                await self._runtime._unload_one(self._child)
        except asyncio.CancelledError:
            pass  # 被 _mark_active 取消，说明又活跃了

    async def _ipc_call(self, method: str, params: Any = None,
                        timeout: float | None = None) -> Any:
        """通过 IPC 服务器发起请求"""
        if self._closed:
            raise ProcessError("Skill handle is closed")

        from common.ipc import IPCError, Request, Response

        server = self._child.ipc_server
        if server is None:
            raise ProcessError("IPC server not initialized")

        # 构造请求
        import uuid
        req_id = uuid.uuid4().hex[:16]
        request = Request(method=method, params=params, id=req_id)

        # 等待响应
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = server._pending_responses
        pending[req_id] = future

        try:
            await server._transport.send(request)
            result = await asyncio.wait_for(
                future,
                timeout=timeout if timeout is not None else self._timeout,
            )
            return result
        except asyncio.TimeoutError:
            pending.pop(req_id, None)
            raise ProcessError(f"IPC call '{method}' timed out")
        except Exception:
            pending.pop(req_id, None)
            raise
        finally:
            pending.pop(req_id, None)

    async def close(self):
        """关闭句柄"""
        self._closed = True


class XiheRuntime:
    """羲和子进程管理器

    管理一组 Skill 子进程，处理生成/通信/监控/销毁全生命周期。
    线程安全（内部使用 asyncio.Lock）。

    生命周期模式：
    - resident（常驻）：进程一直保持运行，不自动卸载
    - on_demand（按需）：execute() 完成后进入空闲计时，超时自动卸载
    """

    def __init__(self, config: XiheConfig | None = None):
        self.config = config or XiheConfig()
        self._children: dict[str, ChildProcess] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._health_task: asyncio.Task | None = None
        self._idle_scan_task: asyncio.Task | None = None
        self._license_task: asyncio.Task | None = None
        self._heartbeat_seq: int = 0  # 心跳轮次计数

    async def start(self) -> None:
        """启动运行时（准备环境，启动健康检查 + 空闲扫描）"""
        self._running = True
        if self.config.health_check_interval > 0:
            self._health_task = asyncio.create_task(
                self._health_check_loop(),
                name="xihe-health",
            )
        # 空闲扫描：每 60 秒扫描一次，或与 idle_timeout 同步
        scan_interval = min(
            max(self.config.idle_timeout_seconds // 3, 15),
            60,
        ) if self.config.idle_timeout_seconds > 0 else 60
        self._idle_scan_task = asyncio.create_task(
            self._idle_scan_loop(scan_interval),
            name="xihe-idle-scan",
        )
        # 收费 Skill 到期巡检（仅当配置了检查名单）
        if self.config.license_checked_skills:
            self._license_task = asyncio.create_task(
                self._license_sweep_loop(),
                name="xihe-license-sweep",
            )
        logger.info(
            "XiheRuntime started (max_procs=%d, idle_timeout=%ds, scan=%ds, license_checked=%s)",
            self.config.max_processes,
            self.config.idle_timeout_seconds,
            scan_interval,
            self.config.license_checked_skills,
        )

    async def launch_skill(
        self,
        skill_name: str,
        agent_id: str = "",
        config: dict | None = None,
        version: str = "1.0.0",
        lifecycle: str = LIFECYCLE_RESIDENT,
        trust_level: str | None = None,
    ) -> SkillHandle:
        """启动一个 Skill 子进程

        Args:
            skill_name: Skill 名称（对应 osskill.implementations 中的目录名）
            agent_id: 所属 Agent ID
            config: 传递给子进程的配置
            version: Skill 版本
            lifecycle: 生命周期模式
            trust_level: 信任级别 — trusted(完整监管) / untrusted(裸奔不管) / revoked(禁止运行)。
                None（默认）时启动即执行完整验证链（verify_skill + 本地吊销名单）自动判定，
                保证被吊销的 Skill 即使被 auto_bind 等重新拉起也会被拦截。

        Returns:
            SkillHandle: 操作句柄

        Raises:
            ResourceExhaustedError: 进程数已达上限
            PermissionError: trust_level="revoked"
            ProcessError: 启动失败
        """
        # 未显式指定 trust_level：启动时执行验证链，从 verify_skill 读取结果
        # P1 (2026-08-27 review #8): 原实现显式传 trust_level="trusted" 可完全
        # 跳过验证链——吊销名单与 verify_skill 均不执行，被吊销 Skill 可经此
        # 参数拉起。改：显式传参仅接受提权"语义"（内部实现仍走吊销名单兜底），
        # revoked 恒以验证链结果为准。
        resolved: str = ""
        if trust_level is None:
            trust_level = await self._resolve_trust_level(skill_name)
        else:
            # 显式指定时仍执行吊销检查（verify_skill 全链可不重跑，但吊销名单必须过）
            resolved = await self._resolve_trust_level(skill_name)
            if resolved == "revoked":
                trust_level = "revoked"
                logger.warning(
                    "[trace] launch_skill(%s) 显式 trust_level=%s 被吊销名单否决 → revoked",
                    skill_name, trust_level,
                )

        # 吊销的 Skill 直接禁止运行
        if trust_level == "revoked":
            raise PermissionError(f"Skill '{skill_name}' 已被吊销，禁止运行")

        # 常驻白名单：名单内的 Skill 无论声明/调用方如何指定，强制 resident 常驻，
        # 避免 on_demand 空闲卸载与 execute 超时分支 stop_skill 导致的反复启停。
        if skill_name in self.config.resident_skill_whitelist:
            lifecycle = LIFECYCLE_RESIDENT

        # 收费 Skill 到期检查：到期拒绝启动（防白漂）
        if skill_name in self.config.license_checked_skills:
            if not await self.check_skill_access(skill_name, agent_id):
                logger.warning(
                    "Skill '%s' agent=%s 订阅/许可到期，拒绝启动",
                    skill_name, agent_id,
                )
                raise PermissionError(
                    f"Skill '{skill_name}' 订阅/许可已到期，请续费后使用",
                )

        async with self._lock:
            # 常驻 Slot 保留检查
            # 注意：resident 一旦创建不可卸载，计数只增不减。
            # 当前无卸载 resident 的路径，单调递增安全。
            if lifecycle == LIFECYCLE_RESIDENT:
                resident_count = sum(
                    1 for c in self._children.values()
                    if c.lifecycle == LIFECYCLE_RESIDENT
                )
                _rs = self.config.resident_slots
                if resident_count >= _rs:
                    raise ResourceExhaustedError(
                        f"Resident skill slots exhausted ({_rs})",
                    )

            # 资源限制检查
            if len(self._children) >= self.config.max_processes:
                raise ResourceExhaustedError(
                    f"Max processes reached ({self.config.max_processes})",
                )

            # 检查是否已存在；共享单例 Skill 所有 agent 共用同一进程
            _effective_agent = "__shared__" if skill_name in self.config.singleton_skills else agent_id
            key = _make_key(skill_name, _effective_agent)
            existing = self._children.get(key)
            if existing is not None and existing.process is not None:
                if existing.process.returncode is None:
                    raise ProcessError(
                        f"Skill '{skill_name}' for agent '{agent_id}' already running",
                    )
                # 僵尸进程，清理后重新创建
                self._children.pop(key, None)

            # 创建子进程
            child = ChildProcess(
                skill_name=skill_name,
                agent_id=agent_id,
                lifecycle=lifecycle,
                trust_level=trust_level,
                started_at=time.time(),
            )

            try:
                await self._spawn(child, config or {}, version)
            except Exception:
                self._children.pop(key, None)
                raise

            handle = SkillHandle(child, self.config.ipc_request_timeout, runtime=self)
            child._handle = handle
            self._children[key] = child

            logger.info(
                "Launched skill '%s' (agent=%s, lifecycle=%s, pid=%d)",
                skill_name, agent_id or "-", lifecycle,
                child.process.pid if child.process else 0,
            )
            return handle

    async def check_skill_access(self, skill_name: str, agent_id: str) -> bool:
        """收费 Skill 到期检查。非收费 skill / 未配置回调 → 放行。

        main.py 注入的 callback 判定订阅/本地 license 是否有效（两者任一到期 → False）。
        检查异常视为到期（收费安全优先，宁可误伤不放漂）。
        """
        if skill_name not in self.config.license_checked_skills:
            return True
        cb = self.config.license_check_callback
        if cb is None:
            return True
        try:
            return bool(await cb(skill_name, agent_id))
        except Exception as e:
            logger.warning("Skill '%s' license check error → 视为到期: %s", skill_name, e)
            return False

    async def _license_sweep_loop(self) -> None:
        """定期巡检常驻收费 Skill，到期自动停止进程。"""
        interval = max(self.config.license_check_interval, 60)
        while self._running:
            try:
                await asyncio.sleep(interval)
                await self._sweep_expired_licenses()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("License sweep error: %s", e)

    async def _sweep_expired_licenses(self) -> None:
        """扫描常驻进程，命中收费 skill 且到期 → 停止进程。"""
        if not self.config.license_checked_skills:
            return
        cb = self.config.license_check_callback
        if cb is None:
            return
        async with self._lock:
            children = list(self._children.values())
        for child in children:
            if child.skill_name not in self.config.license_checked_skills:
                continue
            try:
                ok = await cb(child.skill_name, child.agent_id)
            except Exception as e:
                # 巡检中检查失败跳过（避免误杀常驻进程），下轮再查
                logger.warning("License check error skill=%s agent=%s → skip: %s",
                               child.skill_name, child.agent_id, e)
                continue
            if not ok:
                logger.warning("Skill '%s' agent=%s 订阅/许可到期，停止常驻进程",
                               child.skill_name, child.agent_id)
                try:
                    await self.stop_skill(child.skill_name, child.agent_id)
                except Exception:
                    pass

    async def _resolve_trust_level(self, skill_name: str) -> str:
        """默认启动时的信任级别解析：本地吊销名单（S4 离线兜底）+ 完整验证链 S1→S2→S4→S5。

        调用方未显式指定 trust_level 时使用。吊销判定优先且不依赖网络/证书，
        因此已吊销的 Skill 即便被 auto_bind 重新拉起，也会在此返回 revoked 并被拦截。

        Returns:
            "trusted" | "untrusted" | "revoked"
        """
        # 快速路径：本地吊销黑名单（离线、低开销，是开源版 S4 吊销的判定依据）
        try:
            from osskill.market_integration import RevocationManager
            if RevocationManager().is_blacklisted(skill_name):
                logger.warning("Skill '%s' 在吊销黑名单中，拒绝启动", skill_name)
                return "revoked"
        except Exception as e:
            logger.debug("Revocation blacklist check failed for '%s': %s", skill_name, e)

        # 完整验证链：S1 本地证书验签 → S5 时钟防回拨 → S2/S4 在线状态（闭源钩子）
        try:
            from osskill.loader import ManifestLoader, verify_skill
            from common.config import get as cfg_get

            manifest = None
            pkg_dir = cfg_get("skill.package_dir", "")
            if pkg_dir:
                manifest = ManifestLoader.from_package_dir(
                    os.path.join(pkg_dir, skill_name),
                )

            # A5 (R11): 无 manifest（skill.json 不可验）→ fail-closed 默认 untrusted。
            # 开发模式直接 import 不再自动获 trusted（untrusted 仅失 cgroup 监管 + IPC 代理特权）。
            if manifest is None:
                logger.warning(
                    "Skill '%s' 无 manifest，无法验证，默认 untrusted", skill_name,
                )
                return "untrusted"

            license_data = None
            try:
                from osskill.market_integration import LicenseManager
                license_data = LicenseManager().load_license(skill_name)
            except Exception:
                pass

            level, err = await verify_skill(skill_name, manifest, license_data)
            if level != "trusted":
                logger.warning(
                    "Skill '%s' 验证结果 trust_level=%s (%s)",
                    skill_name, level, err,
                )
            return level
        except Exception as e:
            # A5 (R11): 验证链路异常 fail-closed 默认 untrusted（可用性优先不再以安全让步）。
            logger.warning(
                "Trust verification failed for '%s': %s — 默认 untrusted",
                skill_name, e,
            )
            return "untrusted"

    async def _spawn(self, child: ChildProcess, config: dict, version: str) -> None:
        """生成子进程并建立 IPC 连接"""
        import sys as _sys

        from common.ipc import StdioTransport

        global _process_counter
        _process_counter += 1

        # 注入 SKILL_HOME（存储隔离：data_root/agent_id/skill_name/）
        # review 修复（2026-08-16）：skill_runner._setup_environment 以
        # skill_data_root（或 skill_data_dir 兜底）为 base 再追加 agent_id/skill_name。
        # 原实现只注入 scoped 的 skill_data_dir → base 已是 {root}/{agent}/{skill}，
        # 子进程再拼一层 → SKILL_HOME 双重嵌套 {root}/{agent}/{skill}/{agent}/{skill}。
        # 补 skill_data_root 传未 scoped 的根目录，runner 拼一次即得正确路径。
        skill_home = os.path.join(
            # review(2026-08-16): agent_id/skill_name 可能来自外部，先清洗防路径穿越（_safe_segment）
            self.config.data_dir, _safe_segment(child.agent_id), _safe_segment(child.skill_name),
        )
        config["skill_data_root"] = self.config.data_dir
        config["skill_data_dir"] = skill_home

        # ── 内存上限注入（2026-08-27）：_spawn 收到的 config 常为空 dict（main.py 启动
        # 常驻 Skill 不传 config），skill_runner 只能落到硬编码 512MiB 兜底——XiheConfig
        # 的值从未真正到达子进程。现显式注入：全局限额 setdefault（尊重调用方显式覆盖），
        # 按 Skill 覆盖优先（bidding 2GiB：OCR/PyMuPDF/docx 嵌图内存密集，512MiB 下
        # OpenBLAS Memory allocation failed 崩进程，线上 2026-08-27 实锤）。──
        config.setdefault("memory_limit_bytes", self.config.memory_limit_bytes)
        _per_skill_mem = getattr(self.config, "per_skill_memory_limit_bytes", None) or {}
        if child.skill_name in _per_skill_mem:
            config["memory_limit_bytes"] = int(_per_skill_mem[child.skill_name])

        # ── TCP IPC：先分配端口，再构建启动命令 ──
        import socket as _socket
        _sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _sock.bind(("127.0.0.1", 0))
        _ipc_port = _sock.getsockname()[1]
        _sock.listen(1)
        _sock.setblocking(False)

        runner_path = os.path.join(
            os.path.dirname(__file__), "skill_runner.py",
        )
        cmd = [
            _sys.executable,
            runner_path,
            "--skill-name", child.skill_name,
            "--agent-id", child.agent_id,
            "--config", json.dumps(config, ensure_ascii=False),
            "--version", version,
            "--ipc-port", str(_ipc_port),
        ]

        logger.debug("Spawning: %s", " ".join(cmd))
        logger.debug("IPC TCP server listening on 127.0.0.1:%d", _ipc_port)

        # 创建子进程（stdin/stdout 仅用于启动信号，IPC 走 TCP）
        # 注入 QINGTIAN_IPC_PORT 环境变量，让子进程 _ChildTCPTransport 连接回父进程
        _env = os.environ.copy()
        _env["QINGTIAN_IPC_PORT"] = str(_ipc_port)
        # P2 (R11): 每次启动生成随机 IPC 握手令牌，仅注入本子进程环境
        # （环境变量非 world-readable，比 cmdline 安全，防同机其他进程读取后冒充）。
        ipc_auth_token = secrets.token_hex(32)
        _env["QINGTIAN_IPC_AUTH_TOKEN"] = ipc_auth_token
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_env,
        )
        child.process = proc
        logger.info(
            "[trace] xihe spawn pid=%d skill=%s agent=%s trust=%s",
            proc.pid, child.skill_name, child.agent_id, child.trust_level,
        )

        # 启动 stderr 收集
        child._stderr_task = asyncio.create_task(
            _forward_stderr(proc.stderr, child.skill_name),
            name=f"xihe-stderr-{child.skill_name}",
        )

        # 启动 TCP accept（子进程 connect 在 startup_ready 之前，需先就绪）
        _accept_task = asyncio.create_task(
            asyncio.get_running_loop().sock_accept(_sock),
        )

        # 等待子进程 TCP 连接 + 启动就绪行
        try:
            _conn, _addr = await asyncio.wait_for(_accept_task, timeout=self.config.startup_timeout)
        except asyncio.TimeoutError:
            _accept_task.cancel()  # 防悬挂 accept 任务残留（review 2026-08-16）
            _sock.close()
            # 子进程可能在 TCP 连接前通过 stdout 打印了错误
            try:
                stderr_hint = (await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)).decode("utf-8", errors="replace").strip()
            except Exception:
                stderr_hint = ""
            proc.kill()
            await proc.wait()
            raise ProcessError(f"Skill '{child.skill_name}' TCP accept timeout. stderr: {stderr_hint}")

        # 用 connect_accepted_socket 正确包裹服务端 accepted socket
        # （asyncio.open_connection(sock=) 走 create_connection，是客户端 API，
        #   在 Windows ProactorEventLoop 下可能导致 reader/writer 方向异常）
        _loop = asyncio.get_running_loop()
        _tcp_reader = asyncio.StreamReader()
        _tcp_protocol = asyncio.StreamReaderProtocol(_tcp_reader)
        _tcp_transport, _ = await _loop.connect_accepted_socket(
            lambda: _tcp_protocol, _conn,
        )
        _tcp_writer = asyncio.StreamWriter(
            _tcp_transport, _tcp_protocol, _tcp_reader, _loop,
        )

        # P2 (R11): IPC 握手 — 防同机其他进程冒充 Skill 抢占连接。父进程发送随机
        # challenge，子进程须以 HMAC(challenge, QINGTIAN_IPC_AUTH_TOKEN) 应答；
        # 校验失败（非本次启动的子进程）→ 拒绝并关闭，不建立后续 IPC。
        _challenge = secrets.token_hex(32)
        _tcp_writer.write((
            json.dumps(
                {"v": 1, "type": "ipc.handshake", "challenge": _challenge},
                ensure_ascii=False, separators=(",", ":"),
            ) + "\n"
        ).encode("utf-8"))
        await _tcp_writer.drain()
        try:
            hs_line = await asyncio.wait_for(
                _tcp_reader.readline(),
                timeout=self.config.startup_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _sock.close()
            raise ProcessError(
                f"Skill '{child.skill_name}' IPC handshake timeout after "
                f"{self.config.startup_timeout}s",
            )
        try:
            hs = json.loads(hs_line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as e:
            proc.kill()
            await proc.wait()
            _sock.close()
            raise ProcessError(
                f"Skill '{child.skill_name}' invalid IPC handshake: {e}",
            )
        if not _verify_ipc_handshake(
            hs.get("response") if isinstance(hs, dict) else None,
            ipc_auth_token, _challenge,
        ):
            logger.warning(
                "[trace] Skill '%s' (pid=%d) IPC handshake failed — 拒绝非授权连接",
                child.skill_name, proc.pid,
            )
            proc.kill()
            await proc.wait()
            _sock.close()
            raise ProcessError(
                f"Skill '{child.skill_name}' IPC handshake failed (token mismatch)",
            )

        # review(2026-08-16): ready_line 超时 / JSON 解析失败原实现直接抛异常 → 子进程
        # 成孤儿 + TCP 连接泄漏。统一兜底：kill 进程 + 关 socket 再抛 ProcessError。
        try:
            ready_line = await asyncio.wait_for(
                _tcp_reader.readline(),
                timeout=self.config.startup_timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _sock.close()
            raise ProcessError(
                f"Skill '{child.skill_name}' startup_ready timeout after "
                f"{self.config.startup_timeout}s",
            )

        try:
            ready = json.loads(ready_line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as e:
            proc.kill()
            await proc.wait()
            _sock.close()
            raise ProcessError(
                f"Skill '{child.skill_name}' invalid startup_ready: {e}",
            )

        if ready.get("error"):
            proc.kill()
            await proc.wait()
            _sock.close()
            raise ProcessError(
                f"Skill '{child.skill_name}' startup failed: "
                f"{ready['error'].get('message', 'unknown')}",
            )

        # P2 (R11): 设置 CPU 权重（cgroups v2）— trusted 按声明优先级；untrusted
        # 同样施加 cpu.max 低配额隔离（不再裸奔）。环境不支持（无 cgroup）时
        # fail-safe 记录明确降级原因，并继续 untrusted 隔离。
        await self._apply_cpu_limit(child, config, proc)

        # 预热 CPU 监控初始值（仅 trusted）（解决首次 sample() 无差值的 None 问题）
        try:
            from .scheduler import cpu_monitor
            cpu_monitor.sample(proc.pid)
        except ImportError:
            pass

        # 清理旧 IPC server（重启场景）
        old_server = child.ipc_server
        if old_server is not None:
            await old_server.close()
            child.ipc_server = None

        _sock.close()

        transport_obj = StdioTransport(_tcp_reader, _tcp_writer)
        child.transport = transport_obj

        # 创建 IPC 服务器（处理来自子进程的请求）
        server = _ParentIPCServer(
            agent_id=child.agent_id,
            trust_level=child.trust_level,
        )
        server._transport = transport_obj
        child.ipc_server = server

        # 启动接收循环
        server._recv_task = asyncio.create_task(
            server._receive_loop(),
            name=f"xihe-recv-{child.skill_name}",
        )

        # 进程退出回调
        child._exit_watcher = asyncio.create_task(
            self._watch_exit(child),
            name=f"xihe-exit-{child.skill_name}",
        )

        # 保存启动参数供崩溃重启时使用
        child._last_config = config
        child._last_version = version

        child.started_at = time.time()

    async def _apply_cpu_limit(self, child: ChildProcess, config: dict, proc) -> bool:
        """P2 (R11): 施加 CPU 限制（cgroups v2 cpu.max + cpu.weight）。

        trusted 按声明优先级（high/normal/low）；untrusted 一律按 low 低配额隔离，
        不再裸奔。环境不支持（无 cgroup/权限）时 fail-safe 记录明确降级原因并返回
        False（继续 untrusted 隔离，不阻断启动）。
        """
        priority = (
            config.get("priority", "normal")
            if child.trust_level == "trusted" else "low"
        )
        cpu_limited = False
        try:
            from .scheduler import set_cpu_weight
            cpu_limited = set_cpu_weight(proc.pid, priority)
        except ImportError:
            cpu_limited = False
        except Exception as e:
            cpu_limited = False
            logger.warning(
                "CPU limit error for skill '%s' (pid=%d): %s",
                child.skill_name, proc.pid, e,
            )

        # 发送 CPU 限制通知
        try:
            from common.admin_message import create_admin_bus, AdminMessage
            bus = create_admin_bus()
            if child.trust_level == "trusted":
                level = "info" if cpu_limited else "warning"
                title = (
                    f"Skill '{child.skill_name}' CPU 限制已应用"
                    if cpu_limited
                    else f"Skill '{child.skill_name}' CPU 限制失败（cgroup 不可用）"
                )
                body = f"Agent={child.agent_id}, priority={priority}, pid={proc.pid}"
                await bus.send(AdminMessage(
                    level=level, source="system", title=title, body=body,
                ))
            else:
                # untrusted：已施加低配额隔离；或明确记录隔离失败原因（降级不静默）
                logger.info(
                    "Skill '%s' 非 trusted (%s), CPU 隔离=%s (priority=%s)",
                    child.skill_name, child.trust_level, cpu_limited, priority,
                )
                await bus.send(AdminMessage(
                    level="warning",
                    source="system",
                    title=(
                        f"⚠️ Skill '{child.skill_name}' 为非正规渠道安装，"
                        f"未进行安全检测"
                        + ("" if cpu_limited else "，且 CPU 隔离失败（cgroup 不可用）")
                    ),
                    body=(
                        f"Agent={child.agent_id}, trust_level={child.trust_level}, "
                        f"pid={proc.pid}, cpu_limited={cpu_limited}"
                    ),
                    dedup_key=f"untrusted:{child.skill_name}:{child.agent_id}",
                ))
        except ImportError:
            pass

        return cpu_limited

    async def stop_skill(self, skill_name: str, agent_id: str = "") -> None:
        """停止指定的 Skill 子进程（优雅关闭，超时则强制 kill）

        先发 on_unload，等待进程退出，超时则 SIGKILL。
        """
        key = _make_key(skill_name, agent_id)
        async with self._lock:
            child = self._children.pop(key, None)
            if child is None:
                raise ProcessNotFoundError(
                    f"Skill '{skill_name}' for agent '{agent_id}' not found",
                )

        await self._stop_child(child)

    async def _stop_child(self, child: ChildProcess) -> None:
        """停止单个子进程"""
        # 取消 on_demand 空闲定时器
        if child._idle_timer_task is not None:
            child._idle_timer_task.cancel()
            child._idle_timer_task = None

        proc = child.process
        if proc is None or proc.returncode is not None:
            return  # 已退出

        try:
            # 尝试优雅关闭：发送 on_unload 通知
            from common.ipc import Request as IPCReq
            if child.transport is not None:
                notif = IPCReq(id="", method="on_unload")  # notification = no response
                # P2 (R11): transport.send 内含 drain()，子进程停止读（TCP 缓冲满）时
                # 可永久阻塞 → 加超时，超时直接走强杀。
                await asyncio.wait_for(child.transport.send(notif), timeout=2.0)
                # 等待进程退出
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Skill '%s' didn't exit gracefully, killing (pid=%d)",
                        child.skill_name, proc.pid,
                    )
                    proc.kill()
                    await proc.wait()
        except asyncio.TimeoutError:
            # P2 (R11): on_unload 发送超时 → 直接强杀，避免停止流程永久阻塞
            logger.warning(
                "Skill '%s' on_unload send timed out, force killing (pid=%d)",
                child.skill_name, proc.pid,
            )
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        except asyncio.CancelledError:
            # review(2026-08-16): 停止协程被取消（shutdown/超时）时，原实现进程永远不会
            # kill → 子进程孤儿残留。取消场景强制 kill 后再抛，交给上层。
            logger.warning(
                "Stop cancelled for skill '%s', force killing (pid=%s)",
                child.skill_name, proc.pid,
            )
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise
        except Exception as e:
            logger.warning("Error stopping skill '%s': %s", child.skill_name, e)
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        finally:
            # 清理 cgroup 目录
            _cleanup_cgroup(proc.pid)
            # 清理传输层
            if child.transport is not None:
                try:
                    await child.transport.close()
                except Exception:
                    pass
            if child.ipc_server is not None:
                await child.ipc_server.close()

    async def _watch_exit(self, child: ChildProcess) -> None:
        """监视子进程退出（检测崩溃后自动重启）"""
        proc = child.process
        if proc is None:
            return

        try:
            returncode = await proc.wait()
        except Exception:
            return

        # 检测 OOM（SIGKILL = -9，需 dmesg 内核日志确认）
        try:
            from .scheduler import is_oom_kill, OOM_SIGNAL
            if await is_oom_kill(returncode, proc.pid):
                # 真实 OOM → critical 告警
                logger.warning(
                    "Skill '%s' (pid=%d) was killed by OOM (SIGKILL)",
                    child.skill_name, proc.pid,
                )
                # 发送管理员告警
                try:
                    from common.admin_message import create_admin_bus, AdminMessage
                    bus = create_admin_bus()
                    await bus.send(AdminMessage(
                        level="critical",
                        source="skill",
                        title=f"Skill '{child.skill_name}' OOM 被杀死",
                        body=(
                            f"Agent={child.agent_id}, pid={proc.pid}, "
                            f"restart_count={child.restart_count + 1}"
                        ),
                        dedup_key=f"oom:{child.skill_name}:{child.restart_count}",
                    ))
                except ImportError:
                    pass
            elif returncode == OOM_SIGNAL:
                # SIGKILL 但不是 OOM（自杀/管理员 kill 等）→ warning 日志，不发告警
                logger.warning(
                    "Skill '%s' (pid=%d) received SIGKILL (not OOM, suppressed)",
                    child.skill_name, proc.pid,
                )
        except ImportError:
            pass

        # 清理 cgroup 孤儿目录
        _cleanup_cgroup(proc.pid)

        if not self._running:
            return

        # 检查是否已被主动停止
        key = _make_key(child.skill_name, child.agent_id)
        async with self._lock:
            current = self._children.get(key)
            if current is None or current is not child:
                return  # 已被主动移除

            if child.restart_count >= self.config.restart_max_attempts:
                logger.error(
                    "Skill '%s' crashed %d times, giving up",
                    child.skill_name, child.restart_count,
                )
                self._children.pop(key, None)
                return

            # 指数退避
            delay = min(
                self.config.restart_base_delay * (2 ** child.restart_count),
                self.config.restart_max_delay,
            )
            child.restart_count += 1
            child.last_error = f"exit code {returncode}"

        logger.warning(
            "[trace] Skill '%s' (pid=%d) exited code=%d elapsed=%.1fs since spawn, "
            "restarting in %.1fs (attempt %d/%d)",
            child.skill_name, proc.pid, returncode,
            (time.time() - child.started_at) if child.started_at else 0,
            delay,
            child.restart_count, self.config.restart_max_attempts,
        )

        await asyncio.sleep(delay)

        # review(2026-08-16): sleep 退避期间可能有 stop_skill / shutdown 移除或替换该 child
        # ——直接 _spawn 会生成无人跟踪的孤儿进程，或覆盖新 child 的 process 引用。
        # 醒来后加锁复核：系统仍运行且该 key 仍由本 child 占位才重启，否则放弃。
        async with self._lock:
            if not self._running:
                return
            if self._children.get(key) is not child:
                logger.info(
                    "[trace] Skill '%s' child no longer tracked during backoff, skip restart",
                    child.skill_name,
                )
                return

        # 重新启动
        try:
            # 获取存储的配置
            config = getattr(child, "_last_config", {})
            version = getattr(child, "_last_version", "1.0.0")
            child.downgraded = False  # 重启后清除降级标记
            await self._spawn(child, config, version)
        except Exception as e:
            logger.error("Failed to restart skill '%s': %s", child.skill_name, e)

    async def get_handle(self, skill_name: str, agent_id: str = "") -> SkillHandle:
        """获取正在运行的 Skill 句柄"""
        key = _make_key(skill_name, agent_id)
        child = self._children.get(key)
        if child is None:
            raise ProcessNotFoundError(
                f"Skill '{skill_name}' for agent '{agent_id}' not found",
            )
        handle = getattr(child, "_handle", None)
        if handle is None:
            handle = SkillHandle(child, self.config.ipc_request_timeout)
            child._handle = handle
        return handle

    async def _unload_one(self, child: ChildProcess) -> bool:
        """卸载单个 on_demand Skill，释放进程槽

        Returns:
            True 已卸载, False 不需要卸载（已被其他路径清理）
        """
        key = _make_key(child.skill_name, child.agent_id)
        async with self._lock:
            current = self._children.get(key)
            if current is None or current is not child:
                return False
            # R2-1: TOCTOU 防护 — 确认进入锁后仍处于空闲状态
            # 防止 _mark_active 在 sleep 后、锁获取前将 idle_since 置 None
            if child.idle_since is None:
                return False  # 已被 _mark_active 重新激活
            self._children.pop(key, None)

        logger.info(
            "Unloading idle skill '%s' (agent=%s, idle=%.0fs)",
            child.skill_name, child.agent_id or "-",
            time.time() - child.idle_since if child.idle_since else 0,
        )
        await self._stop_child(child)
        return True

    async def _unload_idle_skills(self) -> int:
        """扫描并卸载所有空闲超时的 on_demand Skill

        Returns:
            卸载数量
        """
        now = time.time()
        timeout = self.config.idle_timeout_seconds
        if timeout <= 0:
            return 0

        to_unload: list[ChildProcess] = []
        async with self._lock:
            for child in list(self._children.values()):
                if child.lifecycle != LIFECYCLE_ON_DEMAND:
                    continue
                if child.idle_since is None:
                    continue  # 正在使用
                if now - child.idle_since >= timeout:
                    to_unload.append(child)

        count = 0
        for child in to_unload:
            try:
                if await self._unload_one(child):
                    count += 1
            except Exception as e:
                logger.warning(
                    "Error unloading idle skill '%s': %s",
                    child.skill_name, e,
                )
        return count

    async def _monitor_resources(self) -> None:
        """监控所有运行中 Skill 的 CPU/内存使用

        超阈值则降级到 low 优先级并发送管理员消息。
        资源恢复后自动升回 normal 优先级。
        系统过载时只降级最耗资源的 Skill（而非全部）。
        在 _idle_scan_loop 中定期调用。
        """
        async with self._lock:
            children = list(self._children.values())

        # 系统总 CPU 监控（独立于循环，用于精准降级）
        try:
            from .scheduler import check_system_overload
            system_overloaded = check_system_overload()
        except ImportError:
            system_overloaded = False

        overload_target: ChildProcess | None = None
        overload_pct = -1.0

        for child in children:
            proc = child.process
            if proc is None or proc.returncode is not None:
                continue
            pid = proc.pid
            if pid is None:
                continue

            exceeded = False

            # 1. 单 Skill CPU 监控（cgroup usage_usec 两次差值）
            pct: float | None = None
            try:
                from .scheduler import cpu_monitor
                pct = cpu_monitor.sample(pid)
                if pct is not None and pct > 85.0:
                    exceeded = True
            except ImportError:
                pass
            except Exception:
                logger.warning("CPU monitor error for pid=%d", pid, exc_info=True)

            # 2. 内存监控（限额取该 Skill 有效值：per-skill 覆盖 > 全局——
            # bidding 2GiB 提额后若仍按全局 512MiB 判，常态内存即被误降级 CPU 权重）
            try:
                from .scheduler import check_memory_pressure
                _eff_mem = _effective_memory_limit(child.skill_name, self.config)
                if check_memory_pressure(pid, _eff_mem):
                    exceeded = True
            except ImportError:
                pass
            except Exception:
                logger.warning("Memory check error for pid=%d", pid, exc_info=True)

            # 跟踪最高 CPU 占用者（用于系统过载精准降级）
            if pct is not None and pct > overload_pct:
                overload_pct = pct
                overload_target = child

            # ── 降级判定 ──
            if exceeded and not child.downgraded:
                child.downgraded = True
                try:
                    from .scheduler import set_cpu_weight
                    set_cpu_weight(pid, "low")
                    logger.info(
                        "Downgraded skill '%s' (pid=%d) to low priority "
                        "due to resource pressure",
                        child.skill_name, pid,
                    )
                except ImportError:
                    pass
                except Exception as e:
                    logger.warning(
                        "Failed to downgrade skill '%s': %s",
                        child.skill_name, e,
                    )

                try:
                    from common.admin_message import create_admin_bus, AdminMessage
                    bus = create_admin_bus()
                    await bus.send(AdminMessage(
                        level="warning",
                        source="system",
                        title=f"Skill '{child.skill_name}' 资源超限，已降级",
                        body=(
                            f"Agent={child.agent_id}, pid={pid}"
                        ),
                        dedup_key=(
                            f"resource:downgrade:"
                            f"{child.skill_name}:{child.agent_id}"
                        ),
                    ))
                except ImportError:
                    pass

            # ── 恢复判定（资源压力解除后恢复 normal 优先级） ──
            elif not exceeded and child.downgraded:
                child.downgraded = False
                try:
                    from .scheduler import set_cpu_weight
                    set_cpu_weight(pid, "normal")
                    logger.info(
                        "Restored skill '%s' (pid=%d) to normal priority",
                        child.skill_name, pid,
                    )
                except ImportError:
                    pass
                except Exception as e:
                    logger.warning(
                        "Failed to restore skill '%s': %s",
                        child.skill_name, e,
                    )

                try:
                    from common.admin_message import create_admin_bus, AdminMessage
                    bus = create_admin_bus()
                    await bus.send(AdminMessage(
                        level="info",
                        source="system",
                        title=f"Skill '{child.skill_name}' 资源已恢复",
                        body=(
                            f"Agent={child.agent_id}, pid={pid}"
                        ),
                        dedup_key=(
                            f"resource:restore:"
                            f"{child.skill_name}:{child.agent_id}"
                        ),
                    ))
                except ImportError:
                    pass

            # ── 运行时安全审计：出站连接检测 + 风险决策 ──
            # 仅对非 trusted 的 Skill 进行出站安全审计（已验证的 Skill 放行）
            if child.trust_level != "trusted":
                # 检测异常出站连接，上报镇岳，按风险评分执行决策
                try:
                    suspicious = _check_egress(pid, extra_whitelist=set(self.config.egress_whitelist))
                    if suspicious:
                        egress_severity = "high" if len(suspicious) > 1 else "medium"
                        try:
                            from zhenyue.audit_runtime import report_event, get_risk_score

                            # 先查询风险分（上报前的历史分），再上报事件
                            score_info = await get_risk_score(
                                agent_id=child.agent_id,
                                skill_name=child.skill_name,
                            )

                            await report_event(
                                agent_id=child.agent_id,
                                skill_name=child.skill_name,
                                event_type="egress_anomaly",
                                severity=egress_severity,
                                detail={
                                    "pid": pid,
                                    "connections": suspicious,
                                },
                            )

                            # 决策矩阵（基于上报前的历史分，避免自我膨胀）
                            decision = _apply_runtime_decision(
                                score_info["score"], egress_severity,
                            )

                            if score_info["score"] > 0:
                                logger.info(
                                    "Security audit: %s/%s score=%d "
                                    "decision=%s connections=%s",
                                    child.agent_id, child.skill_name,
                                    score_info["score"], decision,
                                    [c["remote"] for c in suspicious],
                                )

                            # ── 执行决策 ──
                            if decision in ("downgrade", "pause", "revoke"):
                                if not child.downgraded:
                                    child.downgraded = True
                                    try:
                                        from .scheduler import set_cpu_weight
                                        set_cpu_weight(pid, "low")
                                    except ImportError:
                                        pass

                            if decision in ("pause", "revoke"):
                                # 暂停 / 吊销：停止并移除 Skill
                                logger.warning(
                                    "Security %s: stopping skill '%s' (agent=%s)",
                                    decision, child.skill_name, child.agent_id,
                                )
                                key = _make_key(child.skill_name, child.agent_id)
                                async with self._lock:
                                    self._children.pop(key, None)
                                await self._stop_child(child)

                                try:
                                    from common.admin_message import (
                                        create_admin_bus, AdminMessage,
                                    )
                                    bus = create_admin_bus()
                                    level = "critical" if decision == "revoke" else "warning"
                                    await bus.send(AdminMessage(
                                        level=level,
                                        source="system",
                                        title=(
                                            f"Skill '{child.skill_name}' "
                                            f"已被安全{('吊销' if decision == 'revoke' else '暂停')}"
                                        ),
                                        body=(
                                            f"Agent={child.agent_id}, pid={pid}, "
                                            f"风险分={score_info['score']}, "
                                            f"reason=异常出站 {suspicious}"
                                        ),
                                        dedup_key=(
                                            f"security:{decision}:"
                                            f"{child.skill_name}:{child.agent_id}"
                                        ),
                                    ))
                                except ImportError:
                                    pass

                        except ImportError:
                            pass  # 镇岳未部署，安全审计降级
                except Exception:
                    logger.warning(
                        "Security audit error for skill '%s' (pid=%d)",
                        child.skill_name, pid, exc_info=True,
                    )

        # ── 系统过载：只降级 CPU 占用最高的那个 Skill（如尚未降级） ──
        if system_overloaded and overload_target is not None and not overload_target.downgraded:
            overload_target.downgraded = True
            tgt_proc = overload_target.process
            if tgt_proc is not None and tgt_proc.pid is not None:
                try:
                    from .scheduler import set_cpu_weight
                    set_cpu_weight(tgt_proc.pid, "low")
                    logger.info(
                        "System overload: downgraded skill '%s' (pid=%d) "
                        "to low priority",
                        overload_target.skill_name, tgt_proc.pid,
                    )
                except ImportError:
                    pass
                except Exception as e:
                    logger.warning(
                        "Failed to downgrade overload target: %s", e,
                    )

                try:
                    from common.admin_message import create_admin_bus, AdminMessage
                    bus = create_admin_bus()
                    await bus.send(AdminMessage(
                        level="warning",
                        source="system",
                        title="系统过载，已降级最耗资源 Skill",
                        body=(
                            f"Skill={overload_target.skill_name}, "
                            f"Agent={overload_target.agent_id}, "
                            f"pid={tgt_proc.pid}, cpu={overload_pct:.0f}%"
                        ),
                        dedup_key=(
                            f"resource:overload:"
                            f"{overload_target.skill_name}:"
                            f"{overload_target.agent_id}"
                        ),
                    ))
                except ImportError:
                    pass

    async def _idle_scan_loop(self, interval: float) -> None:
        """定期扫描：资源监控 → 卸载空闲 → 心跳上报"""
        while self._running:
            try:
                await self._monitor_resources()
                count = await self._unload_idle_skills()
                if count > 0:
                    logger.info("Idle scan: unloaded %d skill(s)", count)
                # 每 5 次巡检（~5 分钟）为所有运行中的 skill agent 发心跳
                self._heartbeat_seq += 1
                if self._heartbeat_seq % 5 == 0:
                    await self._heartbeat_all_agents()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Idle scan error: %s", e)
            await asyncio.sleep(interval)

    async def _heartbeat_all_agents(self):
        """为所有运行中的 skill 进程上报心跳。"""
        import httpx
        import os as _os
        base = _os.environ.get("QINGTIAN_API_URL", "http://127.0.0.1:1996")
        async with self._lock:
            agents = [(key, child) for key, child in self._children.items()
                      if child.agent_id and child.process is not None]
        if not agents:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            for key, child in agents:
                try:
                    url = f"{base.rstrip('/')}/v1/huanyu/agents/{child.agent_id}/heartbeat"
                    resp = await client.post(url, json={})
                    if resp.status_code >= 400:
                        logger.warning("Heartbeat failed for %s: %s", child.agent_id, resp.status_code)
                except Exception:
                    pass  # 心跳非关键，单次失败不阻塞巡检

    async def list_skills(self) -> list[dict]:
        """列出所有运行的 Skill"""
        results = []
        async with self._lock:
            for key, child in self._children.items():
                proc = child.process
                results.append({
                    "key": key,
                    "skill_name": child.skill_name,
                    "agent_id": child.agent_id,
                    "lifecycle": child.lifecycle,
                    "idle_since": child.idle_since,
                    "pid": proc.pid if proc else 0,
                    "running": proc is not None and proc.returncode is None,
                    "uptime": time.time() - child.started_at if child.started_at else 0,
                    "restart_count": child.restart_count,
                })
        return results

    async def _health_check_loop(self) -> None:
        """定期健康检查 — 用进程状态而非 IPC ping（避免排队超时）。"""
        while self._running:
            await asyncio.sleep(self.config.health_check_interval)
            async with self._lock:
                for key, child in list(self._children.items()):
                    proc = child.process
                    if proc is None:
                        continue
                    # 进程已结束 → 异常，记录 warning
                    if proc.returncode is not None:
                        logger.warning(
                            "Health check: skill '%s' agent=%s exited with code %s",
                            child.skill_name, child.agent_id, proc.returncode,
                        )

    async def shutdown(self) -> None:
        """关闭全部子进程"""
        logger.info("XiheRuntime shutting down...")
        self._running = False

        # 取消健康检查
        if self._health_task is not None:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # 取消空闲扫描
        if self._idle_scan_task is not None:
            self._idle_scan_task.cancel()
            try:
                await self._idle_scan_task
            except asyncio.CancelledError:
                pass

        # 取消收费 Skill 到期巡检
        if self._license_task is not None:
            self._license_task.cancel()
            try:
                await self._license_task
            except asyncio.CancelledError:
                pass

        # 取消所有 on_demand 空闲定时器
        async with self._lock:
            for child in self._children.values():
                if child._idle_timer_task is not None:
                    child._idle_timer_task.cancel()

        # 停止所有子进程
        async with self._lock:
            children = list(self._children.values())
            self._children.clear()

        for child in children:
            await self._stop_child(child)

        logger.info("XiheRuntime shutdown complete")


# ── 全局管理器引用（main.py 启动时通过 set_manager 注入）──

_runtime: "XiheRuntime | None" = None


def get_manager() -> "XiheRuntime | None":
    """获取全局 XiheRuntime 实例（供 auto_bind 等调用 launch_skill）"""
    return _runtime


def set_manager(runtime: "XiheRuntime") -> None:
    """设置全局 XiheRuntime 实例（main.py 启动时调用）"""
    global _runtime
    _runtime = runtime


# ── 内部辅助 ──


def _make_key(skill_name: str, agent_id: str) -> str:
    """生成内部索引键"""
    return f"{agent_id}:{skill_name}" if agent_id else skill_name


def _safe_segment(value: str) -> str:
    """路径段清洗：去路径分隔符与 .、..，防路径穿越。

    review(2026-08-16): agent_id/skill_name 可来自外部（注册/桥接/消息路由），
    _spawn 用它们拼 SKILL_HOME 目录。原实现直接 os.path.join(data_dir, agent_id,
    skill_name) → 含 ../../ 可逃逸隔离根。清洗后只保留安全字符段。
    """
    if not value:
        return value
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "_".join(parts)


def _ipc_handshake_response(token: str, challenge: str) -> str:
    """P2 (R11): 计算 IPC 握手应答 HMAC-SHA256(challenge, token)。

    skill_runner 子进程复用同一实现（import 本函数），保证两侧算法一致。
    """
    return hmac.new(
        token.encode("utf-8"), challenge.encode("utf-8"), hashlib.sha256,
    ).hexdigest()


def _verify_ipc_handshake(response_hex: str, token: str, challenge: str) -> bool:
    """P2 (R11): 校验 IPC 握手应答是否等于 HMAC(challenge, token)。

    challenge/应答均由父进程生成/核验，不泄露令牌本身；compare_digest 防时序。
    """
    if not isinstance(response_hex, str):
        return False
    expected = _ipc_handshake_response(token, challenge)
    if response_hex.isascii() and expected.isascii():
        return hmac.compare_digest(response_hex, expected)
    return response_hex == expected


class _ParentIPCServer:
    """父进程端 IPC 服务器（处理子进程发来的请求与响应）"""

    def __init__(self, agent_id: str = "", trust_level: str = "trusted"):
        self._transport = None
        self._pending_responses: dict[str, asyncio.Future] = {}
        self._recv_task = None
        self._closed = False
        self._agent_id = agent_id
        self._trust_level = trust_level

    async def _get_admin_token(self, base_url: str) -> str:
        """获取并缓存 admin token（1h TTL，过期自动刷新）。

        防止 token 失效后 IPC 代理永久失败（P2-12）。
        """
        import httpx
        now_t = time.time()
        if (
            not hasattr(self, '_api_token')
            or not hasattr(self, '_api_token_at')
            or now_t - self._api_token_at >= 3600
        ):
            admin_pwd = os.environ.get('ZHENYUE_ADMIN_TOKEN', '')
            async with httpx.AsyncClient(timeout=10) as tc:
                tresp = await tc.post(
                    f"{base_url}/v1/auth/token",
                    json={'username': 'ipc-proxy', 'password': admin_pwd},
                )
                if tresp.status_code == 200:
                    self._api_token = tresp.json().get('token', '')
                else:
                    self._api_token = ''
            self._api_token_at = now_t
        return self._api_token

    async def _receive_loop(self):
        from common.ipc import Request, Response
        import json as _json
        while not self._closed and self._transport is not None:
            msg = None
            try:
                msg = await self._transport.receive()
            except (EOFError, ConnectionError):
                break
            except _json.JSONDecodeError as e:
                logger.debug("Skipping malformed IPC message: %s", e)
                continue
            except Exception as e:
                logger.debug("Receive error, continuing: %s", e)
                continue

            if msg is None:
                continue

            try:
                if isinstance(msg, Response):
                    future = self._pending_responses.pop(msg.id, None)
                    if future is not None and not future.done():
                        if msg.error is not None:
                            from common.ipc import IPCError
                            future.set_exception(
                                IPCError(msg.error.get("message", "error"),
                                         data=msg.error.get("data")),
                            )
                        else:
                            future.set_result(msg.result)
                elif isinstance(msg, Request):
                    if msg.method == "llm.chat":
                        from common.llm import llm_chat
                        params = msg.params or {}
                        try:
                            raw = await llm_chat(**params)
                            # llm_chat 返回 str，子进程期望 dict（含 content）
                            result = raw if isinstance(raw, dict) else {"content": raw}
                            response = Response(id=msg.id, result=result)
                        except Exception as e:
                            logger.exception("llm.chat failed: %s", e)
                            response = Response(
                                id=msg.id, result=None,
                                error={"code": -32002, "message": str(e)[:500]},
                            )
                        await self._transport.send(response)
                    elif msg.method in ("api.get", "api.post", "api.put", "api.delete"):
                        # 代理子进程的 API 请求到底座（带 admin Bearer token）。
                        # 仅限 trusted Skill —— untrusted（非正规渠道）不得经代理
                        # 拿到 admin token 越权调用底座管理接口。
                        if self._trust_level != "trusted":
                            await self._transport.send(Response(
                                id=msg.id, result=None,
                                error={"code": -32601, "message":
                                       "untrusted skill 不允许通过 IPC 代理访问底座 API"},
                            ))
                            continue
                        from common.config import get as cfg_get
                        params = msg.params or {}
                        base_url = f"http://127.0.0.1:{cfg_get('port', 1996)}"
                        path = params.get("path", "")
                        # A6 (R11): 代理路径白名单——管理/凭据端点先拒绝，
                        # 未命中业务白名单同样拒绝；且拦截发生在取 admin token 之前，
                        # 被拒路径不产生/不使用 admin 令牌。
                        if not _proxy_path_allowed(path):
                            await self._transport.send(Response(
                                id=msg.id, result=None,
                                error={"code": -32601, "message":
                                       f"IPC 代理路径被白名单拦截: {path}"},
                            ))
                            continue
                        try:
                            token = await self._get_admin_token(base_url)
                            headers = {"Authorization": f"Bearer {token}"} if token else {}
                            if self._agent_id:
                                headers["X-Agent-ID"] = self._agent_id
                            # P1 (R11): X-Agent-ID 身份透传仅放行内部 IPC 通道
                            # （loopback + X-Internal-Token）——middleware 据此判断，
                            # 无内部令牌则透传不生效（agent 身份回落 admin token 默认）。
                            _internal = os.environ.get("QINGTIAN_INTERNAL_IPC_TOKEN", "")
                            if _internal:
                                headers["X-Internal-Token"] = _internal

                            url = f"{base_url}{path}"
                            async with httpx.AsyncClient(timeout=30) as client:
                                if msg.method == "api.get":
                                    p = params.get("params", {})
                                    resp = await client.get(url, params=p, headers=headers)
                                elif msg.method == "api.post":
                                    body = params.get("body", {})
                                    resp = await client.post(url, json=body, headers=headers)
                                elif msg.method == "api.put":
                                    body = params.get("body", {})
                                    resp = await client.put(url, json=body, headers=headers)
                                else:  # api.delete
                                    p = params.get("params", {})
                                    resp = await client.delete(url, params=p, headers=headers)
                                result = {"status": resp.status_code, "data": resp.json() if resp.text else {}}
                            response = Response(id=msg.id, result=result)
                        except Exception as e:
                            logger.exception("api proxy failed: %s", e)
                            response = Response(
                                id=msg.id, result=None,
                                error={"code": -32003, "message": str(e)[:500]},
                            )
                        await self._transport.send(response)
                    elif not self._closed:
                        logger.warning("Unexpected child request: %s", msg.method)
            except Exception as e:
                logger.debug("Message handler error: %s", e)
                continue

        self._closed = True
        for f in self._pending_responses.values():
            if not f.done():
                f.set_exception(ConnectionError("Connection lost"))
        self._pending_responses.clear()

    async def close(self):
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._transport is not None:
            await self._transport.close()
        # 清理所有 pending
        for f in self._pending_responses.values():
            if not f.done():
                f.set_exception(ConnectionError("Server closed"))
        self._pending_responses.clear()


def _cleanup_cgroup(pid: int | None):
    """清理指定 PID 的 cgroup 目录（子进程退出后调用）"""
    if pid is None:
        return
    try:
        cg_path = os.path.join("/sys/fs/cgroup/qingtian", f"skill_{pid}")
        if os.path.isdir(cg_path):
            os.rmdir(cg_path)
            logger.debug("Cleaned up cgroup dir: %s", cg_path)
    except OSError:
        pass  # cgroup 目录不存在或权限不足，不必报错


async def _forward_stderr(stderr_stream: asyncio.StreamReader,
                          skill_name: str) -> None:
    """将子进程的 stderr 日志转发到父进程的日志系统"""
    logger_stderr = logging.getLogger(f"xihe.skill.{skill_name}")
    try:
        while True:
            line = await stderr_stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger_stderr.info("[child] %s", text)
    except Exception:
        pass


# ── 运行时安全审计 ──────────────────────────────────
# 羲和 → 镇岳 联动：出站连接检测 + 风险决策矩阵
# 设计文档：docs/羲和-镇岳运行时安全审计-设计.md

EGRESS_WHITELIST = {"127.0.0.1", "::1", "10.", "172.16.", "172.17.", "172.18.", "192.168."}
"""出站连接白名单前缀，匹配的地址不报异常"""

# DNS 解析缓存：域名 → set[IP前缀]
_egress_dns_cache: dict[str, set[str]] = {}


def _resolve_whitelist_domains(extra_whitelist: set[str]) -> set[str]:
    """将白名单中的域名解析为 IP 前缀"""
    result: set[str] = set()
    for entry in extra_whitelist:
        entry = entry.strip()
        if not entry:
            continue
        if entry[0].isdigit() or '/' in entry:
            result.add(entry)
            continue
        cached = _egress_dns_cache.get(entry)
        if cached is not None:
            result.update(cached)
            continue
        try:
            ips = set()
            for _, _, _, _, addr in socket.getaddrinfo(entry, 80, socket.AF_INET):
                ip = addr[0]
                parts = ip.split('.')
                if len(parts) == 4:
                    prefix = f"{parts[0]}.{parts[1]}."
                    ips.add(prefix)
            if ips:
                _egress_dns_cache[entry] = ips
                result.update(ips)
                logger.debug("Resolved %s → %s", entry, ips)
        except OSError:
            logger.warning("Failed to resolve whitelist domain: %s", entry)
    return result
"""出站连接白名单前缀，匹配的地址不报异常"""


def _hex_to_ip(hex_str: str) -> str:
    """将 /proc/net 中的 16 进制 IP 转点分十进制

    /proc/net/tcp 以 host byte order 显示 32 位整数的 hex，
    x86 下为 little-endian，需要反转 4 字节得到网络序。
    """
    try:
        b = bytes.fromhex(hex_str.zfill(8))
        if len(b) == 4:
            return f"{b[3]}.{b[2]}.{b[1]}.{b[0]}"
        # IPv6（32 hex chars）
        if len(b) == 16:
            groups = []
            for i in range(0, 16, 2):
                groups.append(f"{b[i]:02x}{b[i+1]:02x}")
            return ":".join(groups)
        return "0.0.0.0"
    except (ValueError, IndexError):
        return "0.0.0.0"


def _proc_socket_inodes(pid: int) -> set[int]:
    """收集指定 pid 打开的全部 socket inode（遍历 /proc/<pid>/fd 符号链接）。

    P2 (R11): /proc/<pid>/net/* 实为宿主共享 netns 的全量连接表，无法按 pid 归因；
    需以该进程 fd 表中的 socket inode 对照连接表 inode 列做精确归属。
    读取失败（权限/进程已退出）→ 返回空集，调用方不误报。
    """
    inodes: set[int] = set()
    try:
        fd_dir = f"/proc/{pid}/fd"
        for entry in os.scandir(fd_dir):
            try:
                link = os.readlink(os.path.join(fd_dir, entry.name))
            except OSError:
                continue
            # socket fd 的符号链接形如 socket:[12345]
            if link.startswith("socket:[") and link.endswith("]"):
                try:
                    inodes.add(int(link[8:-1]))
                except ValueError:
                    continue
    except (FileNotFoundError, PermissionError, ProcessLookupError, NotADirectoryError):
        return set()
    return inodes


def _check_egress(pid: int, extra_whitelist: set[str] | None = None) -> list[dict]:
    """检查子进程的异常出站连接

    P2 (R11): 原实现直接读 /proc/<pid>/net/tcp（共享 netns 全量表）→ 无法按 pid
    归因，会把宿主上其他进程的连接误报成该 Skill 的出站。现改为按 socket inode
    精确归属：收集该进程 fd 表的 socket inode，只在宿主连接表中过滤属于它的行。

    若无法读取进程 fd（权限/进程已退出）→ 返回空（不误报），仅在日志标注降级。

    Args:
        pid: 子进程 PID
        extra_whitelist: 额外白名单前缀，如 {"api.acssa.cn", "8.8.8."}

    Returns:
        [{ proto: "tcp", remote: "45.33.32.156:443" }, ...]
    """
    suspicious: list[dict] = []
    resolved = _resolve_whitelist_domains(extra_whitelist or set())
    whitelist = EGRESS_WHITELIST | resolved

    # P2 (R11): 精确归属 — 先收集该进程的 socket inode，读不到则降级不误报
    inodes = _proc_socket_inodes(pid)
    if not inodes:
        logger.warning(
            "Egress check: cannot attribute sockets for pid=%d (fd scan empty), "
            "degraded to no-op to avoid false positives", pid,
        )
        return []

    for proto in ("tcp", "udp"):
        try:
            with open(f"/proc/net/{proto}") as f:
                lines = f.readlines()
        except (FileNotFoundError, PermissionError):
            # 宿主连接表不可用
            continue

        # 跳过表头
        for line in lines[1:]:
            fields = line.split()
            # tcp: sl local rem st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode ...
            if len(fields) < 10:
                continue
            # inode 是第 10 个字段（index 9）—— 用于归属该连接属于哪个 pid
            try:
                inode = int(fields[9])
            except ValueError:
                continue
            if inode not in inodes:
                continue  # 不属于该 pid，跳过（不误报其他进程的连接）

            # rem_address 是第三个字段（index 2）
            rem_hex = fields[2]
            if ":" not in rem_hex:
                continue
            ip_hex, port_hex = rem_hex.split(":", 1)
            remote_ip = _hex_to_ip(ip_hex)
            try:
                remote_port = int(port_hex, 16)
            except ValueError:
                continue

            # 0.0.0.0:0 = 未建立连接
            if remote_ip == "0.0.0.0" and remote_port == 0:
                continue

            # 白名单放行
            if any(remote_ip.startswith(w) for w in whitelist):
                continue

            suspicious.append({
                "proto": proto,
                "remote": f"{remote_ip}:{remote_port}",
            })

    return suspicious


def _apply_runtime_decision(history_score: int, current_severity: str) -> str:
    """应用风险决策矩阵（显式 if/elif，不依赖列表顺序）

    Args:
        history_score: 历史风险分（0-100）
        current_severity: 当前事件 severity
            critical / high / medium / low / ""（无事件）

    Returns:
        决策动作: "log" | "alert" | "downgrade" | "pause" | "revoke"
    """
    SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "": 0}
    cur = SEVERITY_ORDER.get(current_severity, 0)

    if history_score >= 80:
        return "revoke"                     # >80 + 任何 → 吊销
    if history_score >= 50:
        if cur >= 3:                        # 50-80 + high+ → 暂停
            return "pause"
        if cur >= 2:                        # 50-80 + medium+ → 降级
            return "downgrade"
        return "alert"                      # 50-80 + low → 告警
    if history_score >= 20:
        if cur >= 3:                        # 20-50 + high+ → 降级+告警
            return "downgrade"
        if cur >= 2:                        # 20-50 + medium+ → 告警
            return "alert"
        # 20-50 + low → 仅记录
        return "log"
    return "log"                            # <20 + 任何 → 仅记录
