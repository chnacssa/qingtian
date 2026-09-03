"""
ACSSA — Agent Runtime Manager (ARM)
底座级 Agent 进程管理：拉起、监控、重启、优雅下线

设计原则：
  - 子进程模型（asyncio.create_subprocess_exec），非容器
  - 子进程异常退出不影响底座主进程稳定性
  - 重启策略类似 Docker：always / on_failure / never
  - 健康检查同时支持进程存在性 + HTTP 探针
"""

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from . import config as hcfg

logger = logging.getLogger("huanyu.agent_runtime")

SCHEMA = hcfg.get_schema_name()

# ── 批量重启速率限制 ────────────────────────────────────
RESTART_BATCH_SIZE = 5          # 每批最多同时重启的 Agent 数
RESTART_BATCH_INTERVAL = 10     # 每批间隔（秒）
COOLDOWN_WINDOW_SECONDS = 180   # 冷却期窗口（秒）
COOLDOWN_THRESHOLD = 3          # 冷却期内触发冷却的重启次数

# ── 数据类型 ───────────────────────────────────────────

@dataclass
class AgentProcessConfig:
    """Agent 进程配置"""
    agent_id: str
    executable: str                     # python3 /opt/.../agent.py
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    auto_start: bool = True
    restart_policy: str = "always"      # always / on_failure / never
    restart_backoff: list[int] = field(
        default_factory=lambda: [3, 15, 60, 300]  # 递进退避: 3s→15s→60s→5min
    )
    max_retries: int = 4                # 连续重启失败 N 次 → fatal
    health_check_type: str = "process"  # process / http
    health_check_url: str = ""
    health_check_interval: int = 30     # 秒
    health_check_timeout: int = 5
    health_check_retries: int = 3
    stop_timeout: int = 10              # SIGTERM 后等多久再 SIGKILL


class AgentProcess:
    """单个 Agent 进程的运行时封装"""

    def __init__(self, config: AgentProcessConfig):
        self.config = config
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.pid: Optional[int] = None
        self.status: str = "stopped"        # running / stopped / crashed / restarting
        self.started_at: Optional[datetime] = None
        self.stopped_at: Optional[datetime] = None
        self.restart_count: int = 0
        self.last_error: str = ""
        self._health_ok: bool = False
        self._consecutive_failures: int = 0
        self._backoff_index: int = 0        # 当前退避位置
        self._consecutive_restarts: int = 0  # 连续重启次数（不带健康窗口）
        self._healthy_since: Optional[datetime] = None  # 最后健康时间
        # 崩溃信息收集
        self._last_exit_code: Optional[int] = None
        self._last_signal: Optional[int] = None
        self._last_stderr_snippet: str = ""
        self._last_uptime_seconds: float = 0.0
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None


# ── Resource Monitor ─────────────────────────────────

class ResourceMonitor:
    """每 Agent 资源采集 + 阈值告警

    通过 psutil 或 /proc/[pid]/status 定期采集资源用量。
    超过阈值时写告警日志 + 推送到 Agent inbox。
    """

    def __init__(self, arm):
        self._arm = arm
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self, interval_seconds: int = 60):
        """启动资源监控循环"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(interval_seconds))
        logger.info("Resource monitor started (interval=%ds)", interval_seconds)

    async def stop(self):
        """停止资源监控"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _monitor_loop(self, interval: int):
        """每 interval 秒采集所有 Agent 的资源用量"""
        while self._running:
            try:
                await self._collect_all()
            except Exception as e:
                logger.error("Resource monitor collect error: %s", e)
            await asyncio.sleep(interval)

    async def _collect_all(self):
        """采集所有已接管 Agent 的资源用量"""
        from common.db import get_pool

        now = datetime.now(timezone.utc)
        pool = await get_pool()
        alerts = []

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT agent_id, pid FROM huanyu.agent_processes WHERE status = 'running'"
            )
            for row in rows:
                agent_id = row["agent_id"]
                pid = row["pid"]
                if not pid:
                    continue

                metrics = await asyncio.get_event_loop().run_in_executor(
                    None, self._collect_one, pid
                )
                if not metrics:
                    continue

                # 阈值检查
                threshold = self._get_threshold(agent_id)
                memory_mb = metrics.get("memory_mb", 0)
                cpu = metrics.get("cpu_percent", 0)
                fd_count = metrics.get("fd_count", 0)

                alerts_for_agent = []
                if memory_mb > threshold.get("memory_mb", 1024):
                    alerts_for_agent.append(f"memory={memory_mb}MB > {threshold['memory_mb']}MB")
                if cpu > threshold.get("cpu_percent", 80):
                    alerts_for_agent.append(f"cpu={cpu}% > {threshold['cpu_percent']}%")
                if fd_count > threshold.get("fd_count", 500):
                    alerts_for_agent.append(f"fd={fd_count} > {threshold['fd_count']}")

                if alerts_for_agent:
                    alert_text = "; ".join(alerts_for_agent)
                    logger.warning("[Resource] %s 超限: %s", agent_id, alert_text)
                    try:
                        from zhenyue.audit_service import write_audit
                        await write_audit(conn, {
                            "agent_id": agent_id,
                            "action": "resource.alert",
                            "target_id": agent_id,
                            "target_type": "agent",
                            "detail": {"metrics": metrics, "alerts": alerts_for_agent},
                            "severity": "warning",
                        })
                    except Exception:
                        pass

                    try:
                        from huanyu.directory import write_inbox
                        await write_inbox(agent_id, {
                            "type": "resource_alert",
                            "source": "xihe",
                            "timestamp": now.isoformat(),
                            "payload": {"alerts": alerts_for_agent, "metrics": metrics},
                        })
                    except Exception:
                        pass

                # 写入资源快照
                try:
                    await self._write_resource_snapshot(conn, agent_id, metrics, now)
                except Exception:
                    pass

                # 外联检查（集成 zhenyue.egress）
                try:
                    if self._arm:
                        await self._arm._check_agent_egress(agent_id, pid)
                except Exception:
                    pass

            # 系统级检查
            await self._check_system_level(pool, now)

    def _collect_one(self, pid: int) -> Optional[dict]:
        """采集单 Agent 资源用量（同步 IO，放在 executor 中执行）

        FD 采集单独 try/except：非 root 下 proc.open_files() 抛 AccessDenied，
        单独捕获后 fd_count=-1，不阻断内存/CPU 采集。
        """
        try:
            import psutil
            try:
                proc = psutil.Process(pid)
                with proc.oneshot():
                    memory_mb = proc.memory_info().rss / (1024 * 1024)
                    cpu_percent = proc.cpu_percent(interval=0.1)
                    # 单独 try FD 采集（非 root 可能 AccessDenied）
                    try:
                        fd_count = len(proc.open_files())
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        fd_count = -1  # 不可用标记
                    try:
                        children = len(proc.children())
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        children = -1
                return {
                    "memory_mb": round(memory_mb, 1),
                    "cpu_percent": round(cpu_percent, 1),
                    "fd_count": fd_count,
                    "children_count": children,
                    "pid": pid,
                }
            except psutil.NoSuchProcess:
                return None
            except psutil.AccessDenied:
                # 整个进程无法访问，返回空指标而非 None
                return {
                    "memory_mb": -1,
                    "cpu_percent": -1,
                    "fd_count": -1,
                    "children_count": -1,
                    "pid": pid,
                }
        except ImportError:
            # psutil not available, try /proc
            return self._collect_procfs(pid)

    def _collect_procfs(self, pid: int) -> Optional[dict]:
        """降级方案：通过 /proc 采集（Linux only）"""
        try:
            memory_kb = 0
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        memory_kb = int(line.split()[1])
                        break
            fd_dir = f"/proc/{pid}/fd"
            fd_count = len(os.listdir(fd_dir)) if os.path.isdir(fd_dir) else 0
            return {
                "memory_mb": round(memory_kb / 1024, 1),
                "cpu_percent": 0.0,
                "fd_count": fd_count,
                "children_count": 0,
                "pid": pid,
            }
        except Exception:
            return None

    def _get_threshold(self, agent_id: str) -> dict:
        """获取 Agent 的资源阈值（支持按角色覆盖）"""
        from common.config import get as cfg_get

        default = cfg_get("xihe.resource_limits.default", {"memory_mb": 1024, "cpu_percent": 80, "fd_count": 500})
        overrides = cfg_get("xihe.resource_limits.overrides", {})

        for prefix, override in overrides.items():
            if agent_id.startswith(prefix):
                merged = dict(default)
                merged.update(override)
                return merged
        return default

    async def _write_resource_snapshot(self, conn, agent_id: str, metrics: dict, now: datetime):
        """每分钟资源快照写入 audit_log（供监控面板）"""
        try:
            from zhenyue.audit_service import write_audit
            await write_audit(conn, {
                "agent_id": agent_id,
                "action": "resource.snapshot",
                "target_id": agent_id,
                "target_type": "agent",
                "detail": metrics,
                "severity": "info",
            })
        except Exception:
            pass

    async def _check_system_level(self, pool, now: datetime):
        """系统级资源检查 -> L4/L5 降级"""
        try:
            import psutil
            svmem = await asyncio.get_event_loop().run_in_executor(
                None, psutil.virtual_memory
            )
            available_percent = svmem.available / svmem.total * 100

            if available_percent < 5:
                logger.critical("[Overload] L5: system memory < 5%%, emergency protection")
                if self._arm:
                    await self._arm._enter_l5_protection()
            elif available_percent < 10:
                logger.warning("[Overload] L4: system memory < 10%%, degrading non-essential activity")
                if self._arm:
                    await self._arm._enter_l4_protection()
        except ImportError:
            pass
        except Exception:
            pass


# ── ARM 核心 ──────────────────────────────────────────

class AgentRuntimeManager:
    """Agent Runtime Manager — 全局单例"""

    def __init__(self):
        self._processes: dict[str, AgentProcess] = {}
        self._running = False
        self._main_task: Optional[asyncio.Task] = None
        self._builtin_configs: list[AgentProcessConfig] = []
        # 过载保护状态
        self._l4_active = False
        self._l5_active = False
        self._adopt_locked = False
        self._health_check_paused = False
        self._l5_recovery_task: Optional[asyncio.Task] = None
        self._health_check_interval = 30
        self._resource_monitor: Optional[ResourceMonitor] = None

    # ── 生命周期 ─────────────────────────────────────

    async def start(self):
        """启动 ARM：加载配置 → 拉起 auto_start 的 Agent"""
        if self._running:
            return
        self._running = True
        logger.info("[ARM] 启动 Agent Runtime Manager")

        # 从 config 加载内置 Agent 配置
        self._load_builtin_configs()

        # 从 DB 加载持久化的自定义 Agent 配置
        await self._load_db_configs()

        # Reconciliation：从 DB 恢复已知 Agent 状态
        await self._reconcile()

        # 拉起 auto_start=true 的 Agent
        for config in self._builtin_configs:
            if config.auto_start:
                await self.start_agent(config)

        # 启动全局监控循环
        self._main_task = asyncio.create_task(self._monitor_loop())

        # 启动资源监控（羲和 ResourceMonitor）
        self._resource_monitor = ResourceMonitor(self)
        await self._resource_monitor.start()

        logger.info(f"[ARM] 启动完成，管理 {len(self._processes)} 个 Agent")

    async def stop(self):
        """关闭 ARM：优雅停止所有 Agent"""
        logger.info("[ARM] 关闭所有 Agent…")
        self._running = False

        # 停止资源监控
        if self._resource_monitor:
            await self._resource_monitor.stop()

        # 停止 L5 恢复监控
        if self._l5_recovery_task:
            self._l5_recovery_task.cancel()
            try:
                await self._l5_recovery_task
            except asyncio.CancelledError:
                pass
            self._l5_recovery_task = None

        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        stop_tasks = []
        for ap in list(self._processes.values()):
            if ap.status == "running":
                stop_tasks.append(self.stop_agent(ap.config.agent_id))
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        logger.info("[ARM] 所有 Agent 已停止")

    # ── 配置加载 ─────────────────────────────────────

    def _load_builtin_configs(self):
        """从 config.yaml 加载内置 Agent 配置"""
        try:
            from common.config import get
            agents_cfg = get("agents.builtin", {})
        except Exception:
            agents_cfg = {}

        builtins = {
            "infra:monitor-01": AgentProcessConfig(
                agent_id="infra:monitor-01",
                executable="python3",
                args=["-m", "qingtian.builtin.monitor_agent"],
                auto_start=agents_cfg.get("monitor", {}).get("auto_start", True),
                restart_policy=agents_cfg.get("monitor", {}).get("restart_policy", "always"),
                health_check_type="process",
                health_check_interval=30,
            ),
            "infra:scheduler-01": AgentProcessConfig(
                agent_id="infra:scheduler-01",
                executable="python3",
                args=["-m", "qingtian.builtin.scheduler_agent"],
                auto_start=agents_cfg.get("scheduler", {}).get("auto_start", True),
                restart_policy=agents_cfg.get("scheduler", {}).get("restart_policy", "always"),
                health_check_type="process",
                health_check_interval=30,
            ),
        }

        enabled = agents_cfg.get("enabled", True)
        if enabled:
            for agent_id, config in builtins.items():
                if agent_id not in self._processes:
                    self._processes[agent_id] = AgentProcess(config)
                    self._builtin_configs.append(config)

    async def _load_db_configs(self):
        """从 DB agent_processes 表加载持久化 Agent 配置"""
        # Phase 2+ 实现 — 预留
        pass

    # ── Agent 进程管理 ───────────────────────────────

    async def start_agent(self, config: AgentProcessConfig) -> bool:
        """拉起一个 Agent 进程（含 LLM 代理劫持环境注入）"""
        agent_id = config.agent_id
        ap = self._processes.get(agent_id)

        if ap is None:
            ap = AgentProcess(config)
            self._processes[agent_id] = ap

        if ap.status == "running":
            logger.warning(f"[ARM] {agent_id} 已在运行")
            return True

        # 如果之前已有进程残留，先清场
        if ap.proc is not None or ap.pid is not None:
            await self._cleanup_process(ap)

        ap.status = "starting"
        ap._backoff_index = min(ap._backoff_index, len(config.restart_backoff) - 1)
        ap.last_error = ""
        ap.config = config

        try:
            env = os.environ.copy()
            env.update(config.env)
            # 基础环境变量
            env["QINGTIAN_AGENT_ID"] = agent_id
            env["PYTHONUNBUFFERED"] = "1"
            # LLM 代理劫持 — 拦截 Agent 对 LLM 的调用
            # Agent SDK 初始化时读取这些变量，自动指向ACSSA LLM 代理层
            # 使得所有 LLM 请求都经过认知中枢（审计/限流/注入）
            try:
                from common.config import get as cfg_get
                base_url = cfg_get("service.base_url", "http://localhost:1996")
                llm_base = cfg_get("llm_proxy.base_url", f"{base_url}/v1/llm")
                env["QINGTIAN_BASE_URL"] = base_url
                env["OPENAI_BASE_URL"] = llm_base
                env["OPENAI_API_BASE"] = llm_base       # LangChain SDK
                env["DEEPSEEK_BASE_URL"] = llm_base
                env["ARK_BASE_URL"] = llm_base           # ArkClaw SDK
                # 可选：注入 DashScope/阿里云 API Key 供 embedding 使用
                dashscope_key = cfg_get("llm_proxy.dashscope_api_key", "")
                if dashscope_key:
                    env["DASHSCOPE_API_KEY"] = dashscope_key
            except Exception:
                # 配置不可用时只注入默认值
                base_url = "http://localhost:1996"
                llm_base = f"{base_url}/v1/llm"
                env["QINGTIAN_BASE_URL"] = base_url
                env["OPENAI_BASE_URL"] = llm_base
                env["OPENAI_API_BASE"] = llm_base
                env["DEEPSEEK_BASE_URL"] = llm_base
                env["ARK_BASE_URL"] = llm_base

            proc = await asyncio.create_subprocess_exec(
                config.executable,
                *config.args,
                env=env,
                cwd=config.cwd or None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            ap.proc = proc
            ap.pid = proc.pid
            ap.status = "running"
            ap.started_at = datetime.now(timezone.utc)
            ap._healthy_since = None

            # 启动 stdout/stderr 收集（带进程退出感知）
            ap._task = asyncio.create_task(self._collect_logs(ap))

            # 更新 DB 进程表
            await self._update_process_db(agent_id, "running", proc.pid)

            logger.info(f"[ARM] 已拉起 {agent_id} (pid={proc.pid}, restart#{ap.restart_count})")
            return True

        except Exception as e:
            ap.status = "crashed"
            ap.last_error = str(e)
            logger.error(f"[ARM] 拉起 {agent_id} 失败: {e}")
            return False

    async def stop_agent(self, agent_id: str) -> bool:
        """优雅停止 Agent 进程"""
        ap = self._processes.get(agent_id)
        if not ap or not ap.proc:
            logger.warning(f"[ARM] {agent_id} 无运行中进程")
            return True

        old_pid = ap.pid
        logger.info(f"[ARM] 停止 {agent_id} (pid={old_pid})")

        try:
            # SIGTERM
            ap.proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(ap.proc.wait(), timeout=ap.config.stop_timeout)
            except asyncio.TimeoutError:
                # 超时后 SIGKILL
                logger.warning(f"[ARM] {agent_id} SIGTERM 超时，发送 SIGKILL")
                ap.proc.kill()
                await ap.proc.wait()

            ap.status = "stopped"
            ap.stopped_at = datetime.now(timezone.utc)
            ap.pid = None
            ap.proc = None

            await self._update_process_db(agent_id, "stopped")
            logger.info(f"[ARM] {agent_id} 已停止")

            # 延迟从 _processes 移除（24h 后清理，防内存泄漏）
            self._schedule_process_cleanup(agent_id)
            return True

        except ProcessLookupError:
            # 进程已不存在
            ap.status = "stopped"
            ap.pid = None
            ap.proc = None
            await self._update_process_db(agent_id, "stopped")
            logger.info(f"[ARM] {agent_id} 已停止")
            return True
        except Exception as e:
            logger.error(f"[ARM] 停止 {agent_id} 失败: {e}")
            return False

    def _schedule_process_cleanup(self, agent_id: str):
        """延迟从 _processes 字典移除已停止的 Agent（防内存泄漏）"""
        async def _delayed_cleanup():
            await asyncio.sleep(86400)  # 24 小时
            ap = self._processes.get(agent_id)
            if ap and ap.status == "stopped":
                self._processes.pop(agent_id, None)
                logger.info("[ARM] 清理停用 Agent %s 的 _processes 记录", agent_id)
        asyncio.create_task(_delayed_cleanup())

    # ── 退避计算 ───────────────────────────────────────

    @staticmethod
    def _get_backoff(restart_count: int) -> int:
        """根据重启次数计算递进退避时间"""
        backoff_seq = [3, 15, 60, 300]  # 3s → 15s → 60s → 5min
        idx = min(restart_count, len(backoff_seq) - 1)
        return backoff_seq[idx]

    async def _check_cooldown(self, agent_id: str) -> bool:
        """检查 Agent 是否在冷却期内

        冷却条件：最近 COOLDOWN_WINDOW_SECONDS 秒内
        连续重启超过 COOLDOWN_THRESHOLD 次，且尚未恢复健康。
        """
        ap = self._processes.get(agent_id)
        if not ap:
            return False
        if (ap._consecutive_restarts >= COOLDOWN_THRESHOLD
                and ap._healthy_since is not None):
            elapsed = (datetime.now(timezone.utc) - ap._healthy_since).total_seconds()
            if elapsed < COOLDOWN_WINDOW_SECONDS:
                remaining = COOLDOWN_WINDOW_SECONDS - elapsed
                logger.warning(
                    "[ARM] %s 冷却期: %d次重启/%ds, 剩余%ds",
                    agent_id, ap._consecutive_restarts, COOLDOWN_WINDOW_SECONDS,
                    int(remaining),
                )
                return True
        return False

    async def _check_pid_reuse(self, ap: AgentProcess) -> bool:
        """检测 PID 是否已被 OS 复用

        对比进程启动时间与 agent_processes.started_at，
        时间不匹配 → PID 已复用 → 按崩溃处理。
        """
        if not ap.pid:
            return False
        try:
            import psutil
            proc = psutil.Process(ap.pid)
            create_time = proc.create_time()
            if ap.started_at:
                expected = ap.started_at.timestamp()
                # 如果进程创建时间早于预期 60 秒以上，说明是旧进程
                if abs(create_time - expected) > 60:
                    logger.warning(
                        "[ARM] %s PID %d 可能被复用 "
                        "(create_time=%d, expected=%d)",
                        ap.config.agent_id, ap.pid, int(create_time), int(expected),
                    )
                    return True
        except (ImportError, psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return False

    async def _batch_restart_throttle(self):
        """批量重启速率限制

        检测到多个 Agent 需要重启时，控制并发数。
        在 _monitor_loop 中每次重启 Agent 前调用。
        """
        restarting = sum(
            1 for ap in self._processes.values()
            if ap.status in ("crashed", "restarting")
        )
        if restarting > RESTART_BATCH_SIZE:
            logger.info(
                "[ARM] 批量重启限流: %d agents 待重启，等待 %ds",
                restarting, RESTART_BATCH_INTERVAL,
            )
            await asyncio.sleep(RESTART_BATCH_INTERVAL)

    async def restart_agent(self, agent_id: str) -> bool:
        """重启 Agent"""
        await self.stop_agent(agent_id)
        ap = self._processes.get(agent_id)
        if ap:
            return await self.start_agent(ap.config)
        return False

    # ── 过载保护 ───────────────────────────────────────
    #
    # L4: 系统可用内存 < 10% → 降低健康检查频率，暂停新接管
    # L5: 系统可用内存 < 5%  → 停止所有非必要活动，自动恢复
    # ────────────────────────────────────────────────────

    async def _enter_l4_protection(self):
        """L4 紧急保护：系统可用内存 < 10%

        行为：
        - 暂停所有非必要活动
        - 降低健康检查频率到 60s
        - 停止资源采集
        - 拒绝新的 adopt-self
        """
        if getattr(self, "_l4_active", False):
            return
        self._l4_active = True
        logger.warning("[Overload] L4 protection activated")

        # 降低健康检查频率
        self._health_check_interval = 60

        # 拒绝新接管
        self._adopt_locked = True

        # 写告警消息
        try:
            from common.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    # review(2026-08-16): 列名 from_agent→from_agent_id（schema 无 from_agent，
                    # 原实现 42703 被 except 吞掉 → L4 告警从不落库）；payload 传 dict 防 jsonb 双重编码
                    """INSERT INTO huanyu.messages (from_agent_id, to_agent_id, message_type, payload, status)
                       VALUES ('xihe', 'infra:monitor', 'overload_l4', $1::jsonb, 'pending')""",
                    {"action": "L4_protection_activated", "timestamp": str(datetime.now(timezone.utc))},
                )
        except Exception:
            logger.warning("[Overload] L4 alert insert failed", exc_info=True)

    async def _enter_l5_protection(self):
        """L5 OOM 防护：系统可用内存 < 5%

        行为：
        - 停止处理新 adopt-self
        - 暂停所有健康检查
        - 仅维持进程存活检测 + 心跳
        - 等资源恢复后自动恢复正常
        """
        if getattr(self, "_l5_active", False):
            return
        self._l5_active = True
        self._adopt_locked = True
        logger.critical("[Overload] L5 protection activated — stopping all non-essential activity")

        # 暂停健康检查循环
        self._health_check_paused = True

        try:
            from common.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    # review(2026-08-16): 同 L4——列名修正 + 防 jsonb 双重编码
                    """INSERT INTO huanyu.messages (from_agent_id, to_agent_id, message_type, payload, status)
                       VALUES ('xihe', 'infra:monitor', 'overload_l5', $1::jsonb, 'pending')""",
                    {"action": "L5_protection_activated", "timestamp": str(datetime.now(timezone.utc))},
                )
        except Exception:
            logger.warning("[Overload] L5 alert insert failed", exc_info=True)

        # 监控恢复: 每 30s 检查一次，可用内存 > 15% 持续 2 分钟 -> 恢复
        # 放入后台 task，避免 while 循环阻塞调用方（ResourceMonitor 采集循环）
        if not getattr(self, "_l5_recovery_task", None) or self._l5_recovery_task.done():
            self._l5_recovery_task = asyncio.create_task(self._l5_recovery_loop())

    async def _l5_recovery_loop(self):
        """后台恢复监控：每 30s 采样，内存 > 15% 持续 2 分钟解除 L5。"""
        recovery_checks = 0
        while self._l5_active:
            await asyncio.sleep(30)
            try:
                import psutil
                svmem = await asyncio.get_event_loop().run_in_executor(
                    None, psutil.virtual_memory
                )
                available_percent = svmem.available / svmem.total * 100
                if available_percent > 15:
                    recovery_checks += 1
                    if recovery_checks >= 4:  # 持续 2 分钟 > 15%
                        logger.info(
                            "[Overload] L5 recovery: memory restored to %.1f%%",
                            available_percent,
                        )
                        self._l5_active = False
                        self._adopt_locked = False
                        self._health_check_paused = False
                        break
                else:
                    recovery_checks = 0
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _get_load_level(self) -> str:
        """返回当前负载级别: normal / L4 / L5"""
        if getattr(self, "_l5_active", False):
            return "L5"
        if getattr(self, "_l4_active", False):
            return "L4"
        return "normal"

    async def _check_agent_egress(self, agent_id: str, pid: int):
        """检查 Agent TCP 外联（集成 zhenyue.egress）"""
        try:
            from zhenyue.egress import check_agent_egress
            violations = await check_agent_egress(pid)
            if violations:
                logger.warning(
                    "[Egress] %s: %d non-whitelisted connections",
                    agent_id, len(violations),
                )
        except ImportError:
            pass  # egress module not available
        except Exception as e:
            logger.debug("[Egress] %s check skipped: %s", agent_id, e)

    # ── 外部进程接管 ───────────────────────────────────

    async def adopt_external(self, agent_id: str, request: dict) -> dict:
        """接管外部进程（非底座启动的 Agent）

        request 字段:
          - pid: int                  — Agent 进程 PID
          - launch_command: str       — 启动命令（可选，用于识别）
          - cwd: str                  — 工作目录
          - env: dict                 — 环境变量覆写
          - health_check: dict        — 健康检查配置
              - type: str             — http / process / script
              - endpoint: str         — HTTP 端点
              - timeout_seconds: int  — 超时
              - expected_status: int  — 期望 HTTP 状态码
          - version: str              — Agent 版本
        """
        if getattr(self, "_adopt_locked", False):
            return {"status": "error", "error": "overload: adopt locked, system in L4/L5 protection"}

        pid = request.get("pid")
        if not pid:
            return {"status": "error", "error": "pid 必填"}

        hc = request.get("health_check", {})
        config = AgentProcessConfig(
            agent_id=agent_id,
            executable=request.get("launch_command", ""),
            cwd=request.get("cwd", ""),
            env=request.get("env", {}),
            restart_policy="always",
            health_check_type=hc.get("type", "process"),
            health_check_url=hc.get("endpoint", ""),
            health_check_timeout=hc.get("timeout_seconds", 5),
            health_check_retries=hc.get("retries", 3) or 3,
        )

        # 检查是否已接管
        existing = self._processes.get(agent_id)
        if existing and existing.status == "running":
            # 已接管 — 更新 PID
            existing.pid = pid
            existing.started_at = datetime.now(timezone.utc)
            existing.status = "running"
            await self._update_process_db(agent_id, "running", pid)
            return {"status": "ok", "agent_id": agent_id, "adopted": False}

        # 检查 PID 是否真实有效
        try:
            import os as _os
            _os.kill(pid, 0)  # 检查进程是否存在
        except (ProcessLookupError, PermissionError, OSError) as e:
            return {"status": "error", "error": f"PID {pid} 无效: {e}"}

        # 创建 AgentProcess 并开始监控
        ap = AgentProcess(config)
        ap.pid = pid
        ap.status = "running"
        ap.started_at = datetime.now(timezone.utc)
        self._processes[agent_id] = ap

        await self._update_process_db(agent_id, "running", pid)

        # 运行 6 步自动集成（不阻塞接管）
        integration_result = await self._run_integrations(agent_id)

        logger.info("[ARM] 接管外部进程 %s (pid=%d)", agent_id, pid)

        return {
            "status": "ok",
            "agent_id": agent_id,
            "adopted": True,
            "pid": pid,
            "integration_status": integration_result.get("status", "partial"),
            "integration_errors": integration_result.get("errors", []),
            "integrations": integration_result.get("results", {}),
        }

    async def _run_integrations(self, agent_id: str) -> dict:
        """6 步自动集成 — 接管时执行

        各步骤独立 try/except，失败不阻塞接管。
        入口获取一次 pool/conn，传给各步骤复用。
        """
        errors = []
        results = {}

        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:

            # ① Yongheng 命名空间
            try:
                await conn.execute(
                    "INSERT INTO yongheng.namespaces (namespace, agent_id, created_at) "
                    "VALUES ($1, $2, NOW()) ON CONFLICT DO NOTHING",
                    f"agent:{agent_id}", agent_id,
                )
                results["yongheng_namespace"] = "ok"
            except Exception as e:
                errors.append({"step": "yongheng_namespace", "error": str(e)})
                results["yongheng_namespace"] = "failed"

            # ② Huichuan 知识订阅
            try:
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.topic_subscriptions "
                    f"(agent_id, topic) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    agent_id, f"knowledge:{agent_id.split(':')[0] if ':' in agent_id else 'default'}",
                )
                results["knowledge_subscription"] = "ok"
            except Exception as e:
                errors.append({"step": "knowledge_subscription", "error": str(e)})
                results["knowledge_subscription"] = "failed"

            # ③ Osskill 技能绑定（预留）
            results["osskill_binding"] = "skipped"

            # ④ Siku 账户创建
            try:
                from siku.account_service import ensure_account
                await ensure_account(conn, agent_id)
                results["siku_account"] = "ok"
            except ImportError:
                results["siku_account"] = "skipped"
            except Exception as e:
                errors.append({"step": "siku_account", "error": str(e)})
                results["siku_account"] = "failed"

            # ⑤ Audit log 记录
            try:
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.audit_log "
                    f"(actor_id, action, target_type, target_id, result) "
                    f"VALUES ($1, $2, $3, $4, 'success')",
                    "xihe", "adopt_agent", "agent", agent_id,
                )
                results["audit_log"] = "ok"
            except Exception as e:
                errors.append({"step": "audit_log", "error": str(e)})
                results["audit_log"] = "failed"

            # ⑥ 返回接入凭证（预留）
            results["credentials"] = "skipped"

        status = "ok" if not errors else "partial"
        return {"status": status, "errors": errors, "results": results}

    async def _cleanup_process(self, ap: AgentProcess):
        """清场：查残留进程 → 确认已停止"""
        old_proc = ap.proc
        if old_proc is not None and old_proc.returncode is None:
            try:
                old_proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(old_proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    old_proc.kill()
                    await asyncio.wait_for(old_proc.wait(), timeout=3)
            except ProcessLookupError:
                pass
            except Exception:
                pass
        ap.proc = None
        ap.pid = None

    def get_agent_status(self, agent_id: str) -> Optional[dict]:
        """查询 Agent 进程状态"""
        ap = self._processes.get(agent_id)
        if not ap:
            return None
        return {
            "agent_id": agent_id,
            "status": ap.status,
            "pid": ap.pid,
            "restart_count": ap.restart_count,
            "consecutive_restarts": ap._consecutive_restarts,
            "started_at": ap.started_at.isoformat() if ap.started_at else None,
            "stopped_at": ap.stopped_at.isoformat() if ap.stopped_at else None,
            "last_error": ap.last_error,
            "last_exit_code": ap._last_exit_code,
            "last_uptime_seconds": round(ap._last_uptime_seconds, 1),
            "restart_policy": ap.config.restart_policy,
        }

    def list_agents(self) -> list[dict]:
        """列出所有托管 Agent 状态"""
        return [self.get_agent_status(aid) for aid in self._processes]

    async def get_stats(self) -> dict:
        """返回羲和管理统计（含过载级别）"""
        state_counts = {}
        for ap in self._processes.values():
            state_counts[ap.status] = state_counts.get(ap.status, 0) + 1
        return {
            "managed_agents": len(self._processes),
            "state_counts": state_counts,
            "overload_level": self._get_load_level(),
        }

    # ── 启动 Reconciliation ────────────────────────────

    async def _reconcile(self):
        """底座启动时从 DB 恢复所有已知 Agent 状态

        交叉验证：
          ① huanyu.agents 中活跃 → 标记为 running
          ② agent_processes 中 status=running → 验证 PID 仍存活
          ③ PID 不存在或进程已退出 → 标记为 crashed
        """
        from common.db import get_pool
        pool = await get_pool()

        async with pool.acquire() as conn:
            # ① 查询 huanyu.agents 中所有活跃 agent
            registered = await conn.fetch(
                "SELECT agent_id FROM huanyu.agents WHERE status = 'active'"
            )
            for row in registered:
                aid = row["agent_id"]
                if aid not in self._processes:
                    cfg = AgentProcessConfig(agent_id=aid, executable="", restart_policy="never")
                    ap = AgentProcess(cfg)
                    ap.status = "stopped"
                    self._processes[aid] = ap

            # ② 查询 agent_processes 表中运行中的进程
            processes = await conn.fetch(
                f"SELECT agent_id, pid, status, started_at, config_json "
                f"FROM {SCHEMA}.agent_processes "
                f"WHERE status IN ('running', 'restarting')"
            )

        recovered_count = 0
        fatal_count = 0
        for row in processes:
            aid = row["agent_id"]
            pid = row["pid"]
            db_status = row["status"]

            ap = self._processes.get(aid)
            if not ap:
                # 从 config_json 读取原始重启策略
                config_json = row.get("config_json") or {}
                rp = config_json.get("restart_policy", "always")
                cfg = AgentProcessConfig(agent_id=aid, executable="", restart_policy=rp)
                ap = AgentProcess(cfg)
                self._processes[aid] = ap

            # ③ 验证 PID 存活
            pid_alive = False
            if pid:
                try:
                    import os as _os
                    _os.kill(pid, 0)
                    pid_alive = True
                except (ProcessLookupError, OSError):
                    pid_alive = False

            if pid_alive:
                ap.pid = pid
                ap.status = "running"
                ap.started_at = row["started_at"] or datetime.now(timezone.utc)
                ap._healthy_since = datetime.now(timezone.utc)
                recovered_count += 1
            else:
                # PID 不存在或进程已退出
                if db_status == "running":
                    ap.status = "crashed"
                    ap.last_error = "reconcile: PID 不存在"
                    fatal_count += 1
                else:
                    ap.status = "stopped"

        # ③ 更新 DB 状态
        for aid, ap in self._processes.items():
            if ap.status == "crashed":
                await self._update_process_db(aid, "crashed",
                                              last_error=ap.last_error)

        logger.info(
            "[ARM] Reconciliation: %d recovered, %d fatal, %d total",
            recovered_count, fatal_count, len(self._processes),
        )

    # ── 健康检查与监控 ───────────────────────────────

    async def _monitor_loop(self):
        """全局监控循环：健康检查 + 自动重启"""
        try:
            from common.config import get
            self._health_check_interval = get("xihe.health_check.default_interval", 30)
        except Exception:
            self._health_check_interval = 30
        while self._running:
            await asyncio.sleep(self._health_check_interval)

            # L5 保护态跳过健康检查
            if getattr(self, "_health_check_paused", False):
                continue

            for agent_id, ap in list(self._processes.items()):
                try:
                    await self._check_agent_health(ap)
                except Exception as e:
                    logger.error(f"[ARM] 健康检查异常 {agent_id}: {e}")

    async def _check_agent_health(self, ap: AgentProcess):
        """检查单个 Agent 健康状态，异常时触发重启"""
        if ap.status != "running":
            return

        proc = ap.proc

        # 无子进程对象 = 外部接管进程，用 pid_exists 检查
        if proc is None:
            if ap.pid:
                try:
                    import os as _os
                    _os.kill(ap.pid, 0)  # 检查进程是否存活
                except (ProcessLookupError, OSError):
                    await self._handle_agent_crash(ap, f"进程消失 (pid={ap.pid})")
                    return
                # PID 存活，但需检查是否被 OS 复用
                if await self._check_pid_reuse(ap):
                    await self._handle_agent_crash(ap, "PID 被 OS 复用")
                    return
            else:
                await self._handle_agent_crash(ap, "无进程对象和 PID")
                return
        else:
            # 检查子进程是否存在
            if proc.returncode is not None:
                await self._handle_agent_crash(ap, f"进程退出 code={proc.returncode}")
                return

        # process 检查通过 → 标记健康
        if ap._healthy_since is None:
            ap._healthy_since = datetime.now(timezone.utc)
        # 连续健康运行超过健康窗口 → 重置连续重启计数（仅 _consecutive_restarts，
        # _backoff_index 只在 _handle_agent_crash 中修改，避免竞态）
        HEALTH_WINDOW = 300  # 5 分钟健康窗口
        if (ap._consecutive_restarts > 0 and ap._healthy_since
                and (datetime.now(timezone.utc) - ap._healthy_since).total_seconds() > HEALTH_WINDOW):
            logger.info(f"[ARM] {ap.config.agent_id} 已健康运行超过 {HEALTH_WINDOW}s，重置连续重启计数")
            ap._consecutive_restarts = 0

        # HTTP 健康检查（如有配置）
        if ap.config.health_check_type == "http" and ap.config.health_check_url:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=ap.config.health_check_timeout) as client:
                    resp = await client.get(ap.config.health_check_url)
                if 200 <= resp.status_code < 300:
                    ap._consecutive_failures = 0
                    ap._healthy_since = datetime.now(timezone.utc)
                else:
                    ap._consecutive_failures += 1
            except Exception:
                ap._consecutive_failures += 1

            if ap._consecutive_failures >= ap.config.health_check_retries:
                await self._handle_agent_crash(
                    ap, f"健康检查连续 {ap._consecutive_failures} 次失败"
                )

    async def _get_process_stats(self, agent_id: str) -> Optional[dict]:
        """获取进程资源统计（每 Agent）

        返回内存、CPU、FD 数、子进程数，供监控 Agent 和 /v1/xihe/stats 使用。
        依赖 psutil，不可用时返回 None。
        """
        ap = self._processes.get(agent_id)
        if not ap or not ap.pid:
            return None
        try:
            import psutil
            proc = psutil.Process(ap.pid)
            with proc.oneshot():
                mem = proc.memory_info()
                cpu = proc.cpu_percent(interval=0.1)
                children = proc.children()
                try:
                    fds = proc.num_fds()
                except (AttributeError, psutil.AccessDenied):
                    fds = 0
            return {
                "agent_id": agent_id,
                "status": ap.status,
                "pid": ap.pid,
                "memory_rss_mb": round(mem.rss / 1024 / 1024, 1),
                "memory_vms_mb": round(mem.vms / 1024 / 1024, 1),
                "cpu_percent": round(cpu, 1),
                "num_fds": fds,
                "num_children": len(children),
                "uptime_seconds": round(
                    (datetime.now(timezone.utc) - ap.started_at).total_seconds()
                ) if ap.started_at else 0,
            }
        except (ImportError, psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    async def _get_system_stats(self) -> dict:
        """获取系统资源总览，供 /v1/xihe/stats 使用"""
        stats = {
            "managed_agents": len(self._processes),
            "status_counts": {},
            "system": {},
        }
        for ap in self._processes.values():
            s = ap.status
            stats["status_counts"][s] = stats["status_counts"].get(s, 0) + 1

        try:
            import psutil
            loop = asyncio.get_event_loop()
            cpu_pct = await loop.run_in_executor(
                None, lambda: psutil.cpu_percent(interval=0.5)
            )
            svmem = await loop.run_in_executor(None, psutil.virtual_memory)
            disk = await loop.run_in_executor(
                None, lambda: psutil.disk_usage("/")
            )
            stats["system"] = {
                "cpu_percent": cpu_pct,
                "memory_total_gb": round(svmem.total / 1024**3, 1),
                "memory_used_gb": round(svmem.used / 1024**3, 1),
                "memory_percent": svmem.percent,
                "disk_percent": disk.percent,
            }
        except ImportError:
            pass

        return stats

    async def _handle_agent_crash(self, ap: AgentProcess, reason: str):
        """处理 Agent 崩溃：收集崩溃信息 → 递进退避 → 按策略重启

        退避策略（羲和设计文档 §3.3）：
          第 1 次崩溃 → 等 3s  后重启
          第 2 次崩溃 → 等 15s 后重启
          第 3 次崩溃 → 等 60s 后重启
          第 4+次崩溃 → 等 5min 后重启，超过 max_retries 进入 fatal

        清场流程：
          1. 检查进程是否残留
          2. SIGTERM（5s 超时）
          3. SIGKILL（3s 超时）
          4. 确认清理完成
        """
        agent_id = ap.config.agent_id
        old_status = ap.status

        # ── ① 收集崩溃信息 ──
        if ap.proc:
            ap._last_exit_code = ap.proc.returncode
        ap._last_stderr_snippet = self._get_recent_stderr(ap)
        if ap.started_at:
            ap._last_uptime_seconds = (
                datetime.now(timezone.utc) - ap.started_at
            ).total_seconds()
        ap.status = "crashed"
        ap.last_error = reason
        ap.stopped_at = datetime.now(timezone.utc)

        crash_info = (
            f"exit_code={ap._last_exit_code}, "
            f"uptime={ap._last_uptime_seconds:.1f}s, "
            f"stderr='{ap._last_stderr_snippet[:200]}'"
        )
        logger.warning(f"[ARM] {agent_id} 崩溃: {reason} ({crash_info})")

        # 更新 DB
        await self._update_process_db(agent_id, "crashed", last_error=reason)

        # ── ② 清场 ──
        await self._cleanup_process(ap)

        # ── ③ 判断是否重启 ──
        should_restart = False
        if ap.config.restart_policy == "always":
            should_restart = True
        elif ap.config.restart_policy == "on_failure":
            # on_failure: 只有非正常退出（code != 0）才重启
            should_restart = not (
                ap._last_exit_code is not None and ap._last_exit_code == 0
            )

        if not should_restart:
            logger.info(f"[ARM] {agent_id} 策略={ap.config.restart_policy}，不重启")
            return

        # adopt_external 进程无可执行文件 → 跳过重启，标记 stopped
        if ap.proc is None and not ap.config.executable:
            logger.warning("[ARM] %s 是外部接管进程（无可执行文件），跳过重启", agent_id)
            ap.status = "stopped"
            return

        # ── ④ 递增重启计数 ──
        ap.restart_count += 1
        ap._consecutive_restarts += 1

        # ── ⑤ 退避判断 + 冷却检查 ──
        if ap._consecutive_restarts >= ap.config.max_retries:
            ap.status = "fatal"
            logger.error(
                f"[ARM] {agent_id} 连续重启失败 {ap._consecutive_restarts} 次 "
                f"(>=max_retries={ap.config.max_retries})，进入 fatal 状态，停止重启"
            )
            await self._update_process_db(agent_id, "fatal",
                                          last_error=f"fatal: 超过 max_retries={ap.config.max_retries}")
            return

        # 冷却期检查：短时间内连续重启超过阈值 → 强制冷却
        if await self._check_cooldown(agent_id):
            cooldown_sleep = COOLDOWN_WINDOW_SECONDS
            logger.warning(
                "[ARM] %s 进入冷却期 %ds 后再试", agent_id, cooldown_sleep,
            )
            await asyncio.sleep(cooldown_sleep)

        # 批量重启速率限制
        await self._batch_restart_throttle()

        # ── ⑥ 递进退避 ──
        delay = self._get_backoff(ap._backoff_index)
        ap._backoff_index += 1  # 下次用更长的退避

        logger.info(f"[ARM] {agent_id} 退避 {delay}s 后重启 "
                     f"(backoff_idx={ap._backoff_index - 1}, consecutive={ap._consecutive_restarts})")
        await asyncio.sleep(delay)

        # ── ⑦ 重启 ──
        await self.start_agent(ap.config)

    def _get_recent_stderr(self, ap: AgentProcess, max_chars: int = 1024) -> str:
        """从 stderr 收集器中获取最近的错误输出"""
        return getattr(ap, "_last_stderr_snippet", "")[:max_chars]

    async def _collect_logs(self, ap: AgentProcess):
        """异步收集 Agent 的 stdout/stderr 到日志

        与进程退出联动：用 wait_task 驱动退出，避免 stdout 管道永远挂着。
        """
        agent_id = ap.config.agent_id
        stderr_snippets: list[str] = []

        # 仅保留最近 MAX_SNIPPET_LINES 行，防常驻进程 stderr 无限增长
        MAX_SNIPPET_LINES = 500

        async def _read_stream(stream, log_fn):
            try:
                async for line in stream:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        log_fn(f"[Agent:{agent_id}] {text}")
                        stderr_snippets.append(text)
                        if len(stderr_snippets) > MAX_SNIPPET_LINES:
                            del stderr_snippets[: len(stderr_snippets) - MAX_SNIPPET_LINES]
            except Exception:
                pass

        tasks = []
        if ap.proc:
            if ap.proc.stdout:
                tasks.append(asyncio.create_task(
                    _read_stream(ap.proc.stdout, logger.info)))
            if ap.proc.stderr:
                tasks.append(asyncio.create_task(
                    _read_stream(ap.proc.stderr, logger.warning)))

        # 等待进程退出或流结束（最长 1 小时，防止永久挂起）
        # 用 _safe_wait 包装 proc.wait()，防止进程已退出时抛 ProcessLookupError
        async def _safe_wait():
            try:
                if ap.proc:
                    await ap.proc.wait()
            except ProcessLookupError:
                pass  # 进程已清理，忽略
        wait_set = tasks + ([asyncio.create_task(_safe_wait())] if ap.proc else [])
        if not wait_set:
            return  # 无进程也无流可收集
        done, _ = await asyncio.wait(
            wait_set,
            return_when=asyncio.FIRST_COMPLETED,
            timeout=3600,
        )
        # 进程退出后，保存最近的 stderr 片段（最多最后 20 行）
        ap._last_stderr_snippet = "\n".join(stderr_snippets[-20:])

        # 取消其余任务
        for t in tasks:
            if not t.done():
                t.cancel()

    # ── DB 持久化 ─────────────────────────────────────

    async def _update_process_db(
        self,
        agent_id: str,
        status: str,
        pid: Optional[int] = None,
        last_error: str = "",
    ):
        """更新 agent_processes 表"""
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                if status == "running" and pid is not None:
                    await conn.execute(
                        f"""INSERT INTO {SCHEMA}.agent_processes
                            (agent_id, pid, status, started_at, restart_count)
                            VALUES ($1, $2, $3, NOW(), 0)
                            ON CONFLICT (agent_id) DO UPDATE
                            SET pid = EXCLUDED.pid,
                                status = EXCLUDED.status,
                                started_at = NOW(),
                                restart_count = CASE
                                    WHEN {SCHEMA}.agent_processes.status IN ('crashed', 'fatal')
                                    THEN {SCHEMA}.agent_processes.restart_count + 1
                                    ELSE {SCHEMA}.agent_processes.restart_count
                                END
                        """,
                        agent_id, pid, status,
                    )
                elif status in ("stopped", "crashed", "fatal", "paused"):
                    await conn.execute(
                        f"""UPDATE {SCHEMA}.agent_processes
                            SET status = $1, stopped_at = NOW(),
                                last_error = CASE WHEN $2 != '' THEN $2 ELSE last_error END
                            WHERE agent_id = $3
                        """,
                        status, last_error, agent_id,
                    )
                elif status in ("starting", "unhealthy"):
                    await conn.execute(
                        f"""UPDATE {SCHEMA}.agent_processes
                            SET status = $1, last_error = CASE WHEN $2 != '' THEN $2 ELSE last_error END
                            WHERE agent_id = $3
                        """,
                        status, last_error, agent_id,
                    )
        except Exception as e:
            logger.warning(f"[ARM] DB 更新失败 {agent_id}: {e}")


# ── 全局单例 ──────────────────────────────────────────

_manager: Optional[AgentRuntimeManager] = None


def get_manager() -> AgentRuntimeManager:
    """获取 ARM 单例"""
    global _manager
    if _manager is None:
        _manager = AgentRuntimeManager()
    return _manager


async def start_agent_runtime_manager():
    """启动 ARM — 供 main.py startup 调用"""
    mgr = get_manager()
    await mgr.start()
    return mgr


async def stop_agent_runtime_manager():
    """停止 ARM — 供 main.py shutdown 调用"""
    mgr = get_manager()
    await mgr.stop()
