"""
infra:monitor — 系统监控 Agent
功能：
  - 定时采集系统指标（CPU/内存/磁盘/进程数）
  - 上报到 Yongheng 记忆存储
  - 异常阈值告警

由 ARM 自动拉起，无需手动配置。
"""

import asyncio
import httpx
import json
import logging
import os
import platform
import time
from datetime import datetime, timezone

# 由 ARM 注入的环境变量
AGENT_ID = os.getenv("QINGTIAN_AGENT_ID", "infra:monitor-01")
BASE_URL = os.getenv("QINGTIAN_BASE_URL", "http://localhost:1996")

logger = logging.getLogger("builtin.monitor_agent")
logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

# ── 指标采集 ──────────────────────────────────────────


def _get_cpu_percent() -> float:
    """获取 CPU 使用率（跨平台）"""
    try:
        import psutil
        return psutil.cpu_percent(interval=1)
    except ImportError:
        return 0.0


def _get_memory_info() -> dict:
    """获取内存信息"""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return {"total_gb": round(mem.total / (1024**3), 1),
                "used_gb": round(mem.used / (1024**3), 1),
                "percent": mem.percent}
    except ImportError:
        return {"total_gb": 0, "used_gb": 0, "percent": 0}


def _get_disk_info() -> dict:
    """获取磁盘信息"""
    try:
        import psutil
        disk = psutil.disk_usage("/")
        return {"total_gb": round(disk.total / (1024**3), 1),
                "used_gb": round(disk.used / (1024**3), 1),
                "percent": disk.percent}
    except ImportError:
        return {"total_gb": 0, "used_gb": 0, "percent": 0}


def _get_process_count() -> int:
    """获取系统进程数"""
    try:
        import psutil
        return len(psutil.pids())
    except ImportError:
        return 0


async def collect_metrics() -> dict:
    """采集全系统指标"""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "hostname": platform.node(),
        "cpu_percent": _get_cpu_percent(),
        "memory": _get_memory_info(),
        "disk": _get_disk_info(),
        "process_count": _get_process_count(),
        "platform": f"{platform.system()} {platform.release()}",
    }


# ── 阈值告警 ──────────────────────────────────────────

ALERT_THRESHOLDS = {
    "cpu_percent": 90,
    "memory.percent": 85,
    "disk.percent": 90,
}


def check_alerts(metrics: dict) -> list[dict]:
    """检查指标是否超过阈值"""
    alerts = []
    if metrics.get("cpu_percent", 0) >= ALERT_THRESHOLDS["cpu_percent"]:
        alerts.append({
            "level": "warning",
            "metric": "cpu_percent",
            "value": metrics["cpu_percent"],
            "threshold": ALERT_THRESHOLDS["cpu_percent"],
            "message": f"CPU 使用率 {metrics['cpu_percent']}% 超过阈值 {ALERT_THRESHOLDS['cpu_percent']}%",
        })
    mem_pct = metrics.get("memory", {}).get("percent", 0)
    if mem_pct >= ALERT_THRESHOLDS["memory.percent"]:
        alerts.append({
            "level": "warning",
            "metric": "memory.percent",
            "value": mem_pct,
            "threshold": ALERT_THRESHOLDS["memory.percent"],
            "message": f"内存使用率 {mem_pct}% 超过阈值 {ALERT_THRESHOLDS['memory.percent']}%",
        })
    disk_pct = metrics.get("disk", {}).get("percent", 0)
    if disk_pct >= ALERT_THRESHOLDS["disk.percent"]:
        alerts.append({
            "level": "warning",
            "metric": "disk.percent",
            "value": disk_pct,
            "threshold": ALERT_THRESHOLDS["disk.percent"],
            "message": f"磁盘使用率 {disk_pct}% 超过阈值 {ALERT_THRESHOLDS['disk.percent']}%",
        })
    return alerts


# ── 上报 ──────────────────────────────────────────────

async def report_to_yongheng(metrics: dict):
    """通过底座 API 写入 Yongheng 记忆"""
    try:
        async with httpx.AsyncClient(timeout=10, base_url=BASE_URL) as client:
            resp = await client.post(
                "/v1/yongheng/memories",
                json={
                    "namespace": f"infra:{AGENT_ID}",
                    "content": json.dumps(metrics, ensure_ascii=False),
                    "type": "metric",
                    "source": "builtin.monitor_agent",
                    "metadata": {
                        "agent_id": AGENT_ID,
                        "metric_type": "system",
                        "timestamp": metrics["timestamp"],
                    },
                },
            )
            if resp.status_code not in (200, 201):
                logger.warning("Yongheng 上报失败: HTTP %s", resp.status_code)
    except Exception as e:
        logger.warning("Yongheng 上报异常: %s", e)


async def send_alert(alert: dict):
    """发送告警到底座通知系统"""
    try:
        async with httpx.AsyncClient(timeout=10, base_url=BASE_URL) as client:
            await client.post(
                "/v1/huanyu/messages",
                json={
                    "from_agent": AGENT_ID,
                    "to_agent": "infra:notifier-01",
                    "message_type": "info",
                    "priority": "high" if alert["level"] == "critical" else "normal",
                    "payload": alert,
                },
            )
    except Exception:
        pass


# ── 主循环 ────────────────────────────────────────────

async def main():
    logger.info("Monitor Agent 启动: %s", AGENT_ID)

    # 发送启动心跳
    try:
        async with httpx.AsyncClient(timeout=10, base_url=BASE_URL) as client:
            await client.post(f"/v1/huanyu/agents/{AGENT_ID}/heartbeat")
    except Exception:
        pass

    interval = int(os.getenv("MONITOR_INTERVAL", "60"))

    while True:
        try:
            metrics = await collect_metrics()
            logger.info("指标: CPU=%s%% Mem=%s%% Disk=%s%%",
                        metrics["cpu_percent"],
                        metrics["memory"]["percent"],
                        metrics["disk"]["percent"])

            # 上报指标
            await report_to_yongheng(metrics)

            # 检查告警
            alerts = check_alerts(metrics)
            for alert in alerts:
                logger.warning("告警: %s", alert["message"])
                await send_alert(alert)

        except Exception as e:
            logger.error("采集异常: %s", e)

        # 心跳（减少对底座的压力，用监控间隔的心跳代替）
        try:
            async with httpx.AsyncClient(timeout=10, base_url=BASE_URL) as client:
                await client.post(f"/v1/huanyu/agents/{AGENT_ID}/heartbeat")
        except Exception:
            pass

        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
