"""网络环境检测 —— WireGuard / 底座互通前置检查。

ACSSA 智能体操作系统底座间通讯依赖 WireGuard 内网。此模块在启动时检测 wg0，
缺省时发出明确警告、可选自动安装。
"""

import os
import re
import subprocess
import logging
from typing import Optional

from common.config import get as _net_get

logger = logging.getLogger("common.network")

# 已知发行版的 wireguard 安装命令
_INSTALL_COMMANDS = {
    "apt": "apt-get install -y wireguard-tools",
    "yum": "yum install -y wireguard-tools",
    "dnf": "dnf install -y wireguard-tools",
    "apk": "apk add wireguard-tools",
    "pacman": "pacman -S --noconfirm wireguard-tools",
}


def _which(cmd: str) -> Optional[str]:
    """查找命令路径，未找到返回 None。"""
    try:
        result = subprocess.run(
            ["which", cmd], capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception as e:
        logger.debug("which %s failed: %s", cmd, e)
        return None


def _detect_pkg_manager() -> Optional[str]:
    """探测系统包管理器类型。"""
    for mgr in ("apt", "dnf", "yum", "apk", "pacman"):
        if _which(mgr):
            return mgr
    return None


def check_wireguard_installed() -> bool:
    """wireguard-tools 是否已安装。"""
    return _which("wg") is not None


def _get_wg_interface() -> str:
    try:
        return _net_get("network.wireguard.interface", "wg0")
    except Exception as e:
        logger.debug("config read for wireguard interface failed: %s", e)
        return "wg0"


def check_wireguard_up(interface: str | None = None) -> bool:
    """指定 WireGuard 接口是否已配置并处于 UP 状态。"""
    if interface is None:
        interface = _get_wg_interface()
    if not check_wireguard_installed():
        return False
    try:
        result = subprocess.run(
            ["wg", "show", interface], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception as e:
        logger.debug("wg show %s failed: %s", interface, e)
        return False


def get_wireguard_info(interface: str | None = None) -> dict:
    """获取 WireGuard 接口详细信息。

    返回 dict 包含 installed / configured / up / interface / peers 等字段。
    """
    if interface is None:
        interface = _get_wg_interface()
    installed = check_wireguard_installed()

    info: dict = {
        "installed": installed,
        "configured": False,
        "up": False,
        "interface": interface,
        "peers": 0,
        "transfer_rx": "",
        "transfer_tx": "",
    }

    if not installed:
        return info

    # 检查接口是否 UP
    try:
        result = subprocess.run(
            ["ip", "link", "show", interface], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and "UP" in result.stdout:
            info["up"] = True
            info["configured"] = True
    except Exception as e:
        logger.debug("ip link show %s failed: %s", interface, e)

    # wg show 详情
    try:
        result = subprocess.run(
            ["wg", "show", interface], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            info["configured"] = True
            info["up"] = True
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("transfer:"):
                    # 两种格式: interface="transfer: 25.09 GiB 436.78 MiB"
                    #          peer="transfer: 133.75 MiB received, 21.96 MiB sent"
                    # 正则提取 数字+单位 对
                    pairs = re.findall(r"(\d+\.?\d*\s+\w+)", line)
                    if len(pairs) >= 2:
                        info["transfer_rx"] = pairs[0].strip()
                        info["transfer_tx"] = pairs[1].strip()
                    elif len(pairs) == 1:
                        info["transfer_rx"] = pairs[0].strip()
                        info["transfer_tx"] = "0"
                if line.startswith("peer:"):
                    info["peers"] += 1
    except Exception as e:
        logger.debug("wg show %s detail failed: %s", interface, e)

    return info


async def ensure_wireguard(auto_install: bool = False) -> dict:
    """启动时 WireGuard 保障。

    auto_install=False: 仅检测警告，不修改系统。
    auto_install=True:  尝试自动安装 wireguard-tools（不生成配置文件）。

    返回与 get_wireguard_info 相同结构的 status dict。
    """
    info = get_wireguard_info()

    if not info["installed"]:
        msg = "WireGuard 未安装 (wireguard-tools missing)，跨底座通讯将不可用"
        if auto_install:
            mgr = _detect_pkg_manager()
            cmd = _INSTALL_COMMANDS.get(mgr or "", "")
            if cmd:
                logger.warning(f"{msg}，自动安装中...")
                try:
                    subprocess.run(
                        cmd.split(), check=True, capture_output=True, timeout=120
                    )
                    logger.info("wireguard-tools 安装成功")
                    info = get_wireguard_info()
                except Exception as e:
                    logger.error(f"自动安装失败: {e}")
            else:
                logger.warning(f"{msg}，未识别包管理器，无法自动安装")
        else:
            logger.warning(msg)

    if info["installed"] and not info["configured"]:
        logger.warning(
            f"WireGuard 已安装但 wg0 接口未配置，跨底座通讯不可用。"
            f"请将 /etc/wireguard/wg0.conf 部署到位后 wg-quick up wg0"
        )

    return info
