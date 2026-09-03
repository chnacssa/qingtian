"""平台能力探测 —— 启动自检，显式报告降级状态（不再静默）。

背景：
  生产级能力（cgroups 资源隔离、dmesg OOM 检测、systemd 自动部署、/proc 出站检测）
  依赖 Linux 内核接口。非 Linux 裸机/VM 环境（Docker 只读 cgroup、Windows/macOS）下
  这些能力会静默降级，用户无感知地"裸奔"。本模块在启动时探测当前环境各项能力是否
  可用，通过启动日志 + /health 显式报告，见 docs/platform-support.md。

探测项：
  - cgroup : cgroups v2 可写性（羲和资源隔离）
  - dmesg  : 内核日志可读性（羲和 OOM 检测）
  - systemd: systemd 运行中（吸星自动部署/自愈）
  - proc   : /proc 可用性（出站检测 / CPU 监控）

结果全局缓存，probe 一次，/health 与启动日志复用。
"""

import logging
import os
import subprocess

logger = logging.getLogger("common.platform_probe")

# 全局缓存：探测结果只算一次（幂等）
_probe_result: dict | None = None


def _cgroup_writable() -> bool:
    """cgroups v2 可写：能否创建 /sys/fs/cgroup/qingtian 子目录。

    Docker 默认容器内 /sys/fs/cgroup 只读挂载 → 此处 PermissionError → False。
    非 Linux（os.name != posix）或无 cgroup v2 文件系统 → 直接 False，避免在
    Windows/macOS 上把 /sys/fs/cgroup 误解析为驱动器路径而误判。
    """
    if os.name != "posix":
        return False
    # cgroup v2 文件系统标志：根挂载点存在 cgroup.controllers 文件
    if not os.path.exists("/sys/fs/cgroup/cgroup.controllers"):
        return False
    test_path = os.path.join("/sys/fs/cgroup", "qingtian", "__probe__")
    try:
        os.makedirs(test_path, exist_ok=True)
        os.rmdir(test_path)
        return True
    except OSError:
        return False


def _dmesg_readable() -> bool:
    """dmesg 可读：能否读内核日志（OOM 检测依赖）。

    Docker 容器内 dmesg 通常 Operation not permitted → 非零退出 → False。
    """
    try:
        r = subprocess.run(
            ["dmesg"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _systemd_running() -> bool:
    """/run/systemd/system 存在 = systemd 作为 PID 1 运行（自动部署依赖）。"""
    return os.path.isdir("/run/systemd/system")


def _proc_available() -> bool:
    """/proc 可用（出站检测 / CPU 监控依赖）。"""
    return os.path.isdir("/proc/self")


def probe_platform() -> dict:
    """探测并缓存平台能力。幂等，二次调用直接返回缓存。

    返回 dict：
      os: 'posix' | 'nt'
      cgroup / dmesg / systemd / proc: bool
      production_ready: cgroup + dmesg + systemd 三者全可用（完整生产级能力）
    """
    global _probe_result
    if _probe_result is not None:
        return _probe_result

    logger.info("[trace] platform probe entry")
    result = {
        "os": os.name,
        "cgroup": _cgroup_writable(),
        "dmesg": _dmesg_readable(),
        "systemd": _systemd_running(),
        "proc": _proc_available(),
    }
    result["production_ready"] = all(
        result[k] for k in ("cgroup", "dmesg", "systemd")
    )

    _probe_result = result

    if result["production_ready"]:
        logger.info(
            "[trace] platform probe done: production-ready "
            "(cgroup/dmesg/systemd/proc all OK)"
        )
    else:
        degraded = [k for k in ("cgroup", "dmesg", "systemd", "proc") if not result[k]]
        logger.warning(
            "[trace] platform probe done: DEGRADED — %s unavailable. "
            "资源隔离/OOM 检测/自动部署等生产级能力降级；"
            "生产请用 Linux 裸机/VM + systemd（docs/platform-support.md）",
            ", ".join(degraded),
        )

    return result


def get_platform_capabilities() -> dict:
    """供 /health 端点调用。"""
    return probe_platform()
