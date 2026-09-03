"""
CPU 调度 — cgroups v2 CPU 权重 + OOM 检测

将子进程加入 qingtian cgroup 并设置 CPU 权重（Fair Share）。
三种优先级：high(2x) / normal(1x, 默认) / low(0.5x)

平台支持：Linux cgroups v2，其他平台静默降级。
Windows/macOS 暂不适配，set_cpu_weight 返回 False。
"""

import asyncio
import logging
import os

logger = logging.getLogger("xihe.scheduler")

CGROUP_ROOT = "/sys/fs/cgroup"
QINGTIAN_CGROUP = "qingtian"

# priority → cgroups v2 CPU weight
# 范围 1-10000，默认 100
PRIORITY_WEIGHTS = {
    "high": 200,     # 2x
    "normal": 100,   # 1x（默认）
    "low": 50,       # 0.5x
}

# priority → cpu.max 配额（单核占比）
# 格式: (quota_us, period_us)，100000us = 100ms 周期
CPU_QUOTA_MAP = {
    "high":   (100000, 100000),   # 100%（不限制单核）
    "normal": ( 50000, 100000),   # 50%
    "low":    ( 25000, 100000),   # 25%
}

OOM_SIGNAL = -9
"""SIGKILL — OOM killer 发出的信号"""


def ensure_cgroup_path() -> str | None:
    """确保 qingtian 父 cgroup 存在，返回路径"""
    cg_path = os.path.join(CGROUP_ROOT, QINGTIAN_CGROUP)
    try:
        os.makedirs(cg_path, exist_ok=True)
        return cg_path
    except PermissionError:
        logger.warning("Cannot create cgroup '%s' (no permission)", cg_path)
        return None
    except OSError as e:
        logger.warning("Cannot create cgroup '%s': %s", cg_path, e)
        return None


def set_cpu_weight(pid: int, priority: str = "normal") -> bool:
    """将进程加入 qingtian cgroup 并设置 CPU 权重

    Args:
        pid: 进程 ID
        priority: high / normal / low

    Returns:
        True if successful, False if unsupported or permission denied
    """
    parent_path = ensure_cgroup_path()
    if parent_path is None:
        return False

    weight = PRIORITY_WEIGHTS.get(priority, 100)

    try:
        # 每个子进程独立的 cgroup 目录
        child_path = os.path.join(parent_path, f"skill_{pid}")
        os.makedirs(child_path, exist_ok=True)

        # Step 1: 先将进程移入 cgroup（防止权重/配额已设但进程不在 cgroup 中）
        with open(os.path.join(child_path, "cgroup.procs"), "w") as f:
            f.write(str(pid))

        # Step 2: 验证进程是否成功加入 cgroup
        with open(os.path.join(child_path, "cgroup.procs"), "r") as f:
            pids = f.read().strip()
        if str(pid) not in pids:
            logger.warning("Failed to add pid=%d to cgroup (not in cgroup.procs)", pid)
            try:
                os.rmdir(child_path)
            except OSError:
                pass
            return False

        # Step 3: 设置 CPU 权重（Fair Share）
        with open(os.path.join(child_path, "cpu.weight"), "w") as f:
            f.write(str(weight))

        # Step 4: 设置绝对配额（cpu.max），防止单 Skill 吃满 CPU
        quota = CPU_QUOTA_MAP.get(priority, (50000, 100000))
        with open(os.path.join(child_path, "cpu.max"), "w") as f:
            f.write(f"{quota[0]} {quota[1]}")

        logger.info(
            "CPU weight set: pid=%d priority=%s weight=%d",
            pid, priority, weight,
        )
        return True
    except FileNotFoundError:
        logger.debug("cgroups v2 not available, skipping CPU limit")
        return False
    except PermissionError:
        logger.warning("Permission denied setting CPU weight for pid=%d", pid)
        return False
    except OSError as e:
        logger.warning("Failed to set CPU weight for pid=%d: %s", pid, e)
        return False


async def is_oom_kill(returncode: int, pid: int | None = None) -> bool:
    """判断子进程退出是否因 OOM（SIGKILL + dmesg 内核日志确认）

    仅凭 returncode == -9 不足以判定 OOM（恶意 Skill 可自杀伪造），
    需辅以 dmesg 内核日志确认 Out of memory 事件中包含此 PID。

    Args:
        returncode: 子进程退出码
        pid: 子进程 PID，用于 dmesg 匹配（可选，None 时返回 False）

    Returns:
        True if confirmed OOM kill by kernel; False otherwise.
    """
    if returncode != OOM_SIGNAL:
        return False

    # dmesg 内核日志确认：搜索 "Out of memory" + "pid=<pid>" 或 "Killed process <pid>"
    if pid is not None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "dmesg",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=2,
            )
            await proc.wait()  # 彻底回收子进程，避免僵尸进程
            text = stdout.decode("utf-8", errors="replace")
            if "Out of memory" in text and (
                f"pid={pid}" in text or f"Killed process {pid}" in text
            ):
                return True
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            pass

    return False


# ============================================================
# CPU 监控 + 预警
# ============================================================

CPU_WARNING_THRESHOLD = 85.0
"""系统总 CPU 使用率超过此阈值时触发告警（百分比）"""


def read_cpu_usage(pid: int) -> float | None:
    """读取进程 CPU 使用率（百分比，0-100）

    优先从 cgroup cpu.stat 读取，不可用时回退到 /proc/<pid>/stat。
    """
    # 尝试 cgroups v2 cpu.stat
    cg_path = f"/sys/fs/cgroup/qingtian/skill_{pid}/cpu.stat"
    try:
        with open(cg_path) as f:
            usage_usec = None
            for line in f:
                if line.startswith("usage_usec"):
                    usage_usec = int(line.split()[1])
                    break
            if usage_usec is not None:
                # usage_usec — 累计微秒。两次采样差值 / 间隔 → CPU%
                return None  # 需要两次采样才能算百分比，调用方应缓存
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    # 回退 /proc/<pid>/stat — utime+stime ticks
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
            if len(fields) < 15:
                return None
            utime = int(fields[13])  # 用户态 ticks
            stime = int(fields[14])  # 内核态 ticks
            total_ticks = utime + stime
            # 简单近似：线程数 × 100 / 系统核心数
            import os as _os
            ncores = _os.cpu_count() or 1
            return min(100.0, (total_ticks / ncores))
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    return None


class CpuMonitor:
    """CPU 监控器 — 采样 + 预警

    用法:
        monitor = CpuMonitor()
        pct = monitor.sample(pid)           # 返回本次 CPU%
        if monitor.check_threshold(pid):    # 检查是否超阈值
            # 触发降级/告警
    """

    def __init__(self):
        self._last_sample: dict[int, tuple[float, float]] = {}
        """{pid: (prev_usage_usec, prev_timestamp)}"""

    def sample(self, pid: int, interval: float = 1.0) -> float | None:
        """采样 CPU 使用率（cgroups v2 两次差值法）

        Args:
            pid: 进程 ID
            interval: 采样间隔（秒）

        Returns:
            CPU 使用率百分比 (0-100)，不可用时返回 None
        """
        cg_path = f"/sys/fs/cgroup/qingtian/skill_{pid}/cpu.stat"
        try:
            with open(cg_path) as f:
                usage_usec = None
                for line in f:
                    if line.startswith("usage_usec"):
                        usage_usec = int(line.split()[1])
                        break
                if usage_usec is None:
                    return None

                import time as _time
                now = _time.monotonic()

                if pid in self._last_sample:
                    prev_usec, prev_ts = self._last_sample[pid]
                    delta_usec = usage_usec - prev_usec
                    delta_sec = now - prev_ts
                    if delta_sec > 0 and delta_usec >= 0:
                        # usage_usec 是累计值，差值 / (interval * 1e6) = CPU 占比
                        pct = (delta_usec / (delta_sec * 1_000_000)) * 100
                        pct = min(100.0, pct)
                        self._last_sample[pid] = (usage_usec, now)
                        return pct

                self._last_sample[pid] = (usage_usec, now)
                return None  # 首次采样，无差值
        except (FileNotFoundError, PermissionError, ValueError):
            return None

    def check_threshold(
        self, pid: int, threshold: float = CPU_WARNING_THRESHOLD,
    ) -> bool:
        """检查单个进程是否超过 CPU 阈值"""
        pct = self.sample(pid)
        if pct is not None and pct > threshold:
            return True
        return False


# 全局单例
cpu_monitor = CpuMonitor()


# ============================================================
# 内存监控 + 预警
# ============================================================

MEM_WARNING_THRESHOLD = 85.0
"""单个 Skill 内存使用率超过此阈值时触发告警（百分比，相对于 RLIMIT_AS）"""


def read_memory_usage(pid: int) -> float | None:
    """读取进程内存使用量（字节）

    优先从 cgroup memory.current 读取，不可用时回退 /proc/<pid>/status。
    """
    # 尝试 cgroups v2 memory.current
    cg_path = f"/sys/fs/cgroup/qingtian/skill_{pid}/memory.current"
    try:
        with open(cg_path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    # 回退 /proc/<pid>/status — VmRSS
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # kB → bytes
    except (FileNotFoundError, PermissionError, ValueError):
        pass

    return None


def check_memory_pressure(pid: int, limit_bytes: int | None = None) -> bool:
    """检查内存是否超过阈值"""
    usage = read_memory_usage(pid)
    if usage is None or limit_bytes is None or limit_bytes <= 0:
        return False
    pct = (usage / limit_bytes) * 100
    return pct > MEM_WARNING_THRESHOLD


def read_system_cpu_pct() -> float | None:
    """读取系统总 CPU 使用率（百分比，粗略值）"""
    try:
        import os as _os
        with open("/proc/stat") as f:
            fields = f.readline().split()
            if len(fields) < 5:
                return None
            # cpu user nice system idle iowait irq softirq
            idle = int(fields[4])
            total = sum(int(x) for x in fields[1:8])
            return (1 - idle / total) * 100 if total > 0 else 0.0
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return None


def check_system_overload() -> bool:
    """检查系统总 CPU 是否超阈值（防止死机）"""
    pct = read_system_cpu_pct()
    if pct is not None and pct > CPU_WARNING_THRESHOLD:
        return True
    return False
