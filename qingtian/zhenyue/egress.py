"""镇岳 — Egress 外联监控。

检查 Agent 进程的 TCP 外联连接，与白名单比对。
不在白名单内的连接记录到审计日志。

Windows 平台跳过（读取 /proc 仅在 Linux 有效）。
"""

import logging
import os
import platform
from typing import Optional

from common.db import get_pool
from . import config as zcfg
from .audit_service import write_audit

logger = logging.getLogger("zhenyue.egress")

DEFAULT_WHITELIST = [
    "api.deepseek.com",
    "dashscope.aliyuncs.com",
    "localhost",
]


def _parse_proc_tcp(pid: int) -> list[dict]:
    """解析 /proc/{pid}/net/tcp 获取 TCP 连接。

    Returns:
        [{"pid": int, "remote_addr": str, "port": int, "local_addr": str, "local_port": int}, ...]
    """
    connections = []
    tcp_path = f"/proc/{pid}/net/tcp"

    if not os.path.exists(tcp_path):
        logger.debug("No /proc/net/tcp for pid %d (process may not exist)", pid)
        return connections

    try:
        with open(tcp_path, "r") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("Cannot read /proc/%d/net/tcp: %s", pid, e)
        return connections

    # 跳过首行（header）
    for line in lines[1:]:
        parts = line.strip().split()
        if len(parts) < 4:
            continue

        local = parts[1]   # local_address
        remote = parts[2]  # rem_address
        conn_state = parts[3]  # st

        # 只检查 established 连接（状态 01）
        if conn_state != "01":
            continue

        try:
            remote_ip_hex, remote_port_hex = remote.split(":")
            remote_ip = ".".join(
                str(int(remote_ip_hex[i : i + 2], 16))
                for i in range(6, -1, -2)
            )
            remote_port = int(remote_port_hex, 16)

            local_ip_hex, local_port_hex = local.split(":")
            local_ip = ".".join(
                str(int(local_ip_hex[i : i + 2], 16))
                for i in range(6, -1, -2)
            )
            local_port = int(local_port_hex, 16)

            connections.append({
                "pid": pid,
                "remote_addr": remote_ip,
                "port": remote_port,
                "local_addr": local_ip,
                "local_port": local_port,
            })
        except (ValueError, IndexError):
            continue

    return connections


def _is_local(addr: str) -> bool:
    """判断是否为本地地址。"""
    return addr.startswith("127.") or addr == "0.0.0.0" or addr == "::1"


async def check_agent_egress(pid: int, whitelist: Optional[list[str]] = None) -> list[dict]:
    """检查 Agent 进程的 TCP 外联。

    Args:
        pid: Agent 进程 PID
        whitelist: 允许的远程地址白名单（域名或 IP）

    Returns:
        [{"pid": int, "remote_addr": str, "port": int, "allowed": bool, "alerted": bool}, ...]
    """
    if platform.system().lower() != "linux":
        logger.warning("Egress check skipped: %s platform does not support /proc", platform.system())
        return []

    whitelist = whitelist or DEFAULT_WHITELIST
    connections = _parse_proc_tcp(pid)

    if not connections:
        return []

    results = []
    for conn in connections:
        remote_addr = conn["remote_addr"]

        # 本地地址自动放行
        if _is_local(remote_addr):
            continue

        allowed = remote_addr in whitelist

        result = {
            "pid": pid,
            "remote_addr": remote_addr,
            "port": conn["port"],
            "allowed": allowed,
            "alerted": False,
        }

        if not allowed:
            # 写审计日志
            schema = zcfg.get_schema_name()
            pool = await get_pool()
            try:
                async with pool.acquire() as conn_pg:
                    await write_audit(conn_pg, {
                        "agent_id": f"pid:{pid}",
                        "agent_role": "system",
                        "action": "egress_violation",
                        "target_type": "network",
                        "target_id": f"{remote_addr}:{conn['port']}",
                        "severity": "medium",
                        "detail": {
                            "remote_addr": remote_addr,
                            "port": conn["port"],
                            "local_addr": conn["local_addr"],
                            "local_port": conn["local_port"],
                        },
                        "approval_status": "auto",
                    })
                result["alerted"] = True
            except Exception as e:
                logger.error("Failed to write egress audit log: %s", e)

        results.append(result)

    return results


async def run_egress_check(agent_id: str, pid: int):
    """对指定 Agent 执行外联检查并记录违规。

    Args:
        agent_id: Agent 标识
        pid: Agent 进程 PID
    """
    violations = await check_agent_egress(pid)
    for v in violations:
        if not v["allowed"]:
            logger.warning(
                "[Egress] Agent %s -> %s:%s (not in whitelist, alerted=%s)",
                agent_id,
                v.get("remote_addr"),
                v.get("port"),
                v.get("alerted"),
            )
