"""
寰宇 — 内置定时维护任务

使用 asyncio create_task + 简单循环（与 huichuan/cron.py 同模式）。

任务（北京时间）：
  - 消息清理：每天 03:00（清理已归档超过 30 天的消息 + 投递失败超过 7 天的消息）
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from common.config import is_management
from common.db import get_pool
from . import config as hcfg

logger = logging.getLogger("huanyu.cron")

SCHEMA = hcfg.get_schema_name()

_tasks: list[asyncio.Task] = []
_running = False
_timezone: ZoneInfo | None = None

# 上次执行日期，防止同一天重复触发
_last_run_date: dict[str, str] = {}


def _now() -> datetime:
    global _timezone
    if _timezone is None:
        try:
            _timezone = ZoneInfo("Asia/Shanghai")
        except Exception:
            _timezone = ZoneInfo("UTC")
    return datetime.now(_timezone)


def _should_run_daily(task: str, hour: int, minute: int) -> bool:
    now = _now()
    today = now.strftime("%Y-%m-%d")
    if _last_run_date.get(task) == today:
        return False
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= target


# ── 任务实现 ──────────────────────────────────────────


async def _cleanup_messages_job():
    """清理陈旧消息。

    策略：
      1. 已归档超过 30 天的消息 → 物理删除
      2. 投递失败超过 7 天的消息 → 物理删除
      3. 已读且超过 90 天的消息 → 归档（自动归档非删除）
    """
    logger.info("Cron: huanyu message cleanup started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 1. 删除已归档超过 30 天的消息
            result1 = await conn.execute(
                f"DELETE FROM {SCHEMA}.messages "
                f"WHERE status = 'archived' "
                f"AND created_at < NOW() - INTERVAL '30 days'"
            )
            deleted_archived = int(result1.split()[-1]) if result1 else 0
            if deleted_archived > 0:
                logger.info(
                    "Cron: deleted %d archived messages (>30d)", deleted_archived
                )

            # 2. 删除投递失败超过 7 天的消息
            result2 = await conn.execute(
                f"DELETE FROM {SCHEMA}.messages "
                f"WHERE delivery_status = 'failed' "
                f"AND created_at < NOW() - INTERVAL '7 days'"
            )
            deleted_failed = int(result2.split()[-1]) if result2 else 0
            if deleted_failed > 0:
                logger.info(
                    "Cron: deleted %d failed messages (>7d)", deleted_failed
                )

            # 3. 已读超过 90 天 → 自动归档
            result3 = await conn.execute(
                f"UPDATE {SCHEMA}.messages "
                f"SET status = 'archived' "
                f"WHERE status = 'read' "
                f"AND read_at < NOW() - INTERVAL '90 days'"
            )
            archived_read = int(result3.split()[-1]) if result3 else 0
            if archived_read > 0:
                logger.info(
                    "Cron: auto-archived %d read messages (>90d)", archived_read
                )

            total = deleted_archived + deleted_failed + archived_read
            if total == 0:
                logger.info("Cron: huanyu message cleanup — nothing to do")
    except Exception as e:
        logger.error("Cron: huanyu message cleanup failed: %s", e)


# ── Agent 注册表同步 ──────────────────────────────────


async def _push_local_agents_job():
    """非管理底座：每 5 分钟把本地全部 active agent（含 server_ip）整表上报管理服 /peers/sync。

    管理服比对后：同底座不在新表 → 置 inactive（职责②）。
    仅非管理底座执行；管理服为 hub 汇总方，跳过（内部 is_management 已判断）。
    """
    from . import config as hcfg
    hub_url = hcfg.get_hub_endpoint()
    if not hub_url:
        return  # 没有 hub_endpoint 时跳过
    try:
        from .peers import push_local_agents_to_hub
        await push_local_agents_to_hub()
    except Exception as e:
        logger.warning("Cron: push local agents to hub failed: %s", e)


async def _sync_agent_registry_job():
    """从管理服拉取全量 Agent 注册表，同步到本地。

    每 5 分钟执行一次。跳过管理底座自身（管理底座不需要同步）。
    也跳过未配 hub_endpoint 的底座。
    """
    from . import config as hcfg
    hub_url = hcfg.get_hub_endpoint()
    if not hub_url:
        return  # 没有 hub_endpoint 时跳过

    try:
        from .peers import pull_agent_registry_from_hub
        result = await pull_agent_registry_from_hub()
        if result.get("status") == "ok":
            synced = result.get("synced", 0)
            if synced > 0:
                logger.info(
                    "Cron: synced %d agents from hub", synced
                )
        elif result.get("status") == "skipped":
            pass  # no hub configured, 正常
        else:
            logger.warning(
                "Cron: agent registry sync failed: %s", result.get("reason", "unknown")
            )
    except Exception as e:
        logger.error("Cron: agent registry sync error: %s", e)


# ── 待重试消息投递 ──────────────────────────────────

# P2 (R11): 重试轮数上限（进程内计数，DB 无计数字段）。无投递目标的消息已在
# retry_delivery 内标记 failed（由日清任务 7 天后清理）；此处再对反复失败的
# pending/failed 消息设上限，超过后本进程内暂停重试，防止无限打同一批消息。
_MAX_RETRY_ROUNDS = 5
_retry_rounds: dict[str, int] = {}


async def _retry_pending_deliveries_job():
    """重试待投递的跨底座消息。

    每 30 分钟执行一次，获取所有 pending/failed 消息并逐一重试。
    P2 (R11): 连续失败超 _MAX_RETRY_ROUNDS 轮的消息暂停重试（保持 failed，
    由日清任务 7 天后清理），避免无目标/永不可达消息每轮空转。
    """
    from .messaging import get_pending_deliveries, retry_delivery

    logger.info("Cron: retry pending deliveries started")
    try:
        pending = await get_pending_deliveries(limit=100)
        if not pending:
            logger.info("Cron: no pending deliveries to retry")
            return

        success = 0
        paused = 0
        seen: set[str] = set()
        for msg in pending:
            mid = msg["message_id"]
            seen.add(mid)
            rounds = _retry_rounds.get(mid, 0)
            if rounds >= _MAX_RETRY_ROUNDS:
                # P2 (R11): 超过重试上限 → 本进程内暂停重试（保持 failed 待日清清理）
                paused += 1
                continue
            result = await retry_delivery(mid)
            if result.get("status") == "delivered":
                success += 1
                _retry_rounds.pop(mid, None)
                continue
            rounds += 1
            _retry_rounds[mid] = rounds
            if rounds >= _MAX_RETRY_ROUNDS:
                logger.warning(
                    "Cron: message %s 连续 %d 轮投递失败，暂停重试（7 天后日清清理）",
                    mid[:8], rounds,
                )

        # 清理已不在待投递集合中的计数（delivered/被清理），防内存无限增长
        for stale in [m for m in _retry_rounds if m not in seen]:
            _retry_rounds.pop(stale, None)

        logger.info(
            "Cron: retry pending deliveries finished — %d/%d delivered, %d paused",
            success, len(pending), paused,
        )
    except Exception as e:
        logger.error("Cron: retry pending deliveries failed: %s", e)


# ── 调度主循环 ────────────────────────────────────────

_CRON_SCHEDULE: list[tuple[str, int, int, Callable]] = [
    ("huanyu_cleanup_messages", 3, 0, _cleanup_messages_job),
]

async def _cleanup_stale_agents_job():
    """本地底座心跳清理：本底座 agent 超过 24h 无心跳 → inactive。

    仅管理本地注册的 agent（server_host 为本底座 host），不影响跨底座同步来的数据。
    管理服跳过（管理服只做目录汇总，不负责各底座 agent 的生命周期）。
    """
    from common.config import is_management
    if is_management():
        return  # 管理服不清理

    from common.db import get_pool
    from common.config import get as root_get
    from . import config as hcfg

    host_name = root_get("host", "")
    if not host_name:
        return

    pool = await get_pool()
    schema = hcfg.get_schema_name()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"""UPDATE {schema}.agents SET status = 'inactive', updated_at = NOW()
                WHERE status = 'active'
                  AND server_host = $1
                  AND (last_heartbeat IS NULL OR last_heartbeat < NOW() - INTERVAL '24 hours')""",
            host_name,
        )
        cleaned = int(result.split()[-1]) if result else 0
        if cleaned:
            logger.info("Cron: cleaned %d stale agents (>24h no heartbeat)", cleaned)


# 间隔型调度任务（秒级间隔）
_INTERVAL_SCHEDULE: list[tuple[str, int, Callable]] = [
    ("huanyu_retry_deliveries", 1800, _retry_pending_deliveries_job),  # 30 分钟
    ("huanyu_sync_agent_registry", 300, _sync_agent_registry_job),    # 5 分钟
    ("huanyu_push_local_agents", 300, _push_local_agents_job),        # 5 分钟（非管理底座上报整表）
    ("huanyu_cleanup_stale_agents", 3600, _cleanup_stale_agents_job),  # 1 小时（本地心跳清理，管理服跳过）
]

# 上次执行时间戳
_last_run_ts: dict[str, float] = {}


def _should_run_interval(task: str, interval_seconds: int) -> bool:
    now = time.time()
    last = _last_run_ts.get(task, 0)
    return now - last >= interval_seconds


async def _scheduler_loop():
    global _running
    _running = True

    while _running:
        try:
            # 每日清理仅 management 角色执行（非管理底座无清理权限）
            if is_management():
                for task_name, hour, minute, job_fn in _CRON_SCHEDULE:
                    if _should_run_daily(task_name, hour, minute):
                        _last_run_date[task_name] = _now().strftime("%Y-%m-%d")
                        asyncio.create_task(job_fn())

            for task_name, interval, job_fn in _INTERVAL_SCHEDULE:
                # 🧭 管理服 hub 特征：管理服是唯一 hub（汇总各底座注册 + 提供拉取），
                # 自身不做 registry pull（否则会 pull 自己成死循环）。仅非管理底座从管理服拉取。
                if is_management() and task_name == "huanyu_sync_agent_registry":
                    continue
                if _should_run_interval(task_name, interval):
                    _last_run_ts[task_name] = time.time()
                    asyncio.create_task(job_fn())
        except Exception as e:
            logger.error("Cron scheduler loop error: %s", e)

        await asyncio.sleep(60)


async def start():
    """启动寰宇定时任务。

    每日清理仅在 management 角色执行；间隔任务（重试投递、注册表同步）全角色运行。
    """
    global _running
    if _running:
        return

    if is_management():
        logger.info("Starting huanyu cron: message cleanup@03:00 daily")
    else:
        logger.info("Starting huanyu cron: interval tasks only (not management)")
        if not hcfg.get_hub_endpoint():
            logger.warning(
                "huanyu.hub_endpoint 未配置 — 跨底座消息路由、Agent 注册表同步不可用。"
                "非管理底座必须配置 hub_endpoint 指向管理服。"
            )

    task = asyncio.create_task(_scheduler_loop())
    _tasks.append(task)


async def stop():
    """停止寰宇定时任务。"""
    global _running
    _running = False
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    logger.info("Huanyu cron stopped")
