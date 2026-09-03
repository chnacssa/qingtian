"""
汇川 — 内置定时调度（替代 crontab）

仅在 management 角色服务器上激活。
使用 asyncio create_task + 简单循环（复用 xixing/scheduler.py 模式）。

任务（北京时间）：
  - 精炼管道：每天 02:00
  - 过期清理：每天 03:00
  - 已撤销清理：每天 03:30
  - 文件图片清理：每天 04:00
  - 冷启动激活：每小时 05 分
  - 永恒同步重试：每小时 15 分
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from common.config import is_management
from common.db import get_pool
from .api import _sync_to_yongheng
from .database import SCHEMA
from .refine import refine_batch

logger = logging.getLogger("huichuan.cron")

_tasks: list[asyncio.Task] = []
_running = False
_timezone: ZoneInfo | None = None

# 上次执行日期，防止同一天重复触发（daily 任务）
_last_run_date: dict[str, str] = {}
# 上次执行小时，防止同一小时重复触发（hourly 任务）
_last_run_hour: dict[str, str] = {}


def _now() -> datetime:
    global _timezone
    if _timezone is None:
        try:
            _timezone = ZoneInfo("Asia/Shanghai")
        except Exception:
            _timezone = ZoneInfo("UTC")
    return datetime.now(_timezone)


def _should_run_daily(task: str, hour: int, minute: int) -> bool:
    """检查 daily 任务是否应在当前分钟触发。"""
    now = _now()
    today = now.strftime("%Y-%m-%d")
    if _last_run_date.get(task) == today:
        return False
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= target


def _should_run_hourly(task: str, minute: int) -> bool:
    """检查 hourly 任务是否应在当前分钟触发。"""
    now = _now()
    hour_key = f"{now.strftime('%Y-%m-%d')}:{now.hour}"
    if _last_run_hour.get(task) == hour_key:
        return False
    return now.minute >= minute


# ── 任务实现 ──────────────────────────────────────────


async def _refinement_job():
    """精炼管道：处理 refinement_queue 中的 pending 条目。"""
    logger.info("Cron: refinement started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await refine_batch(conn)
        logger.info(
            f"Cron: refinement done — {result.get('processed', 0)} processed, "
            f"{result.get('accepted', 0)} accepted, {result.get('rejected', 0)} rejected"
        )
    except Exception as e:
        logger.error(f"Cron: refinement failed: {e}")


async def _cleanup_expired_job():
    """清理过期知识：valid_until < NOW() - 30 days → status=archived（设计文档 §5.1）。"""
    logger.info("Cron: cleanup_expired started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE {SCHEMA}.knowledge_entries "
                f"SET status = 'archived', updated_at = NOW() "
                f"WHERE valid_until < CURRENT_DATE - INTERVAL '30 days' "
                f"AND status NOT IN ('archived', 'revoked')"
            )
            deleted = int(result.split()[-1]) if result else 0
            if deleted > 0:
                logger.info(f"Cron: cleanup_expired archived {deleted} entries")
    except Exception as e:
        logger.error(f"Cron: cleanup_expired failed: {e}")


async def _cleanup_revoked_job():
    """物理删除已撤销超过 30 天的知识。

    软删除后保留 30 天窗口期，期间可通过 restore 端点恢复。
    超期后硬删除（级联删除版本历史 + knowledge_links）。
    """
    logger.info("Cron: cleanup_revoked started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 1. 物理删除超期 30 天的 revoked entries
            expired = await conn.fetch(
                f"SELECT knowledge_id, title FROM {SCHEMA}.knowledge_entries "
                f"WHERE status = 'revoked' "
                f"AND metadata->>'revoked_at' IS NOT NULL "
                f"AND (metadata->>'revoked_at')::timestamptz < NOW() - INTERVAL '30 days' "
                f"LIMIT 100"
            )
            if expired:
                ids = [r["knowledge_id"] for r in expired]
                result = await conn.execute(
                    f"DELETE FROM {SCHEMA}.knowledge_entries "
                    f"WHERE knowledge_id = ANY($1)",
                    ids,
                )
                deleted = int(result.split()[-1]) if result else 0
                logger.info(
                    "Cron: cleanup_revoked physically deleted %d entries: %s",
                    deleted,
                    ", ".join(str(r["title"])[:30] for r in expired[:5]),
                )

            # 2. 清理可安全删除的 Layer 1 文件
            #    条件: file_registry 状态为 revoked/corrupted/low_quality/expired
            #          且 updated_at > 30 天（保留窗口期）
            stale_files = await conn.fetch(
                f"SELECT storage_path, status, original_filename "
                f"FROM {SCHEMA}.file_registry "
                f"WHERE status IN ('revoked', 'corrupted', 'expired') "
                f"AND updated_at < NOW() - INTERVAL '30 days' "
                f"LIMIT 50"
            )
            deleted_files = 0
            for f_row in stale_files:
                path = f_row["storage_path"]
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        deleted_files += 1
                except OSError:
                    pass
                # 删除注册表记录
                await conn.execute(
                    f"DELETE FROM {SCHEMA}.file_registry WHERE storage_path = $1",
                    path,
                )
            if deleted_files:
                logger.info("Cron: cleanup_revoked removed %d stale Layer 1 files", deleted_files)

    except Exception as e:
        logger.error(f"Cron: cleanup_revoked failed: {e}")


async def _cold_start_job():
    """冷启动激活：draft 48h+ → 自动激活为 active（设计文档 §7.3）。

    注意：Phase 2 简化实现——直接激活所有超过 48h 的 draft 知识。
    设计文档要求按 GET 阅读次数判定，但因 knowledge_versions 不记录访问日志，
    暂时采用保守策略（全部激活），后续与镇岳审计日志集成后精确追踪。
    """
    logger.info("Cron: cold_start_activate started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # P1-5（9-1 修复日）：激活收窄 —— quality < 3（低置信/挂起标记）
            # 的 draft 不再自动转 active，留人工处置。此前所有 draft 48h 一律
            # 激活，低质量 LLM 泛化产物也会自动进入检索面。
            result = await conn.execute(
                f"UPDATE {SCHEMA}.knowledge_entries "
                f"SET status = 'active', updated_at = NOW() "
                f"WHERE status = 'draft' "
                f"AND quality >= 3 "
                f"AND refined_at < NOW() - INTERVAL '48 hours'"
            )
            activated = int(result.split()[-1]) if result else 0
            if activated > 0:
                logger.info(f"Cron: cold_start activated {activated} entries")
    except Exception as e:
        logger.error(f"Cron: cold_start_activate failed: {e}")


async def _sync_retry_job():
    """永恒同步重试：扫描 index_status=pending_retry，指数退避重试（设计文档 §5.8）。

    退避策略：
      retry_count=0 → 1h later, retry_count=1 → 2h, retry_count=2 → 4h, retry_count=3 → 8h
      retry_count >= 3 且首次失败超过 24h → ERROR 日志告警
    """
    logger.info("Cron: yongheng_sync_retry started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT knowledge_id, title, content, domain, tags, visibility, metadata "
                f"FROM {SCHEMA}.knowledge_entries "
                f"WHERE metadata->>'index_status' = 'pending_retry' "
                f"AND (metadata->>'retry_at')::timestamptz <= NOW() "
                f"LIMIT 50"
            )

            recovered = 0
            for r in rows:
                meta = r["metadata"] or {}
                retry_count = meta.get("retry_count", 0)

                success = await _sync_to_yongheng(
                    conn, str(r["knowledge_id"]), r["title"], r["content"],
                    r["domain"], r["tags"] or [], r["visibility"],
                )

                if success:
                    recovered += 1
                else:
                    new_count = retry_count + 1
                    delays = [1, 2, 4, 8]
                    delay_h = delays[min(new_count - 1, 3)]
                    next_retry = datetime.now(timezone.utc) + timedelta(hours=delay_h)

                    await conn.execute(
                        f"UPDATE {SCHEMA}.knowledge_entries "
                        f"SET metadata = metadata || $1::jsonb "
                        f"WHERE knowledge_id = $2",
                        json.dumps({"retry_count": new_count, "retry_at": next_retry.isoformat()}),
                        r["knowledge_id"],
                    )

                    if new_count >= 3:
                        # 检查是否首次失败已超过 24h
                        first_retry_str = meta.get("retry_at", "")
                        if first_retry_str:
                            try:
                                first_retry = datetime.fromisoformat(first_retry_str)
                                if (datetime.now(timezone.utc) - first_retry).total_seconds() > 86400:
                                    logger.error(
                                        f"YongHeng sync exhausted for {r['knowledge_id']}: "
                                        f"{new_count} retries over >24h"
                                    )
                            except ValueError:
                                pass

            if recovered > 0:
                logger.info(f"Cron: yongheng_sync_retry recovered {recovered} entries")

    except Exception as e:
        logger.error(f"Cron: yongheng_sync_retry failed: {e}")


async def _purge_expired_files_job():
    """冷静期到期清理：status='deleted' 且 purge_at 到期的文件 → 物理删 + 删记录。

    30 天冷静期软删到期后真删（对标 zhenyue/quarantine.purge_expired）。
    file_registry.storage_path 已指向回收区路径，物理文件删除后连同记录一并清理。
    """
    logger.info("Cron: purge_expired_files started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            expired = await conn.fetch(
                f"SELECT file_id, storage_path, original_filename "
                f"FROM {SCHEMA}.file_registry "
                f"WHERE status = 'deleted' AND purge_at IS NOT NULL AND purge_at < NOW() "
                f"LIMIT 200"
            )
            removed = 0
            for r in expired:
                path = r["storage_path"]
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                        removed += 1
                    elif path:
                        logger.warning(
                            "Cron: purge_expired_files file_missing file_id=%s path=%r",
                            r["file_id"], path,
                        )
                except OSError as e:
                    logger.warning("Cron: purge_expired_files remove_fail file_id=%s: %s", r["file_id"], e)
                await conn.execute(
                    f"DELETE FROM {SCHEMA}.file_registry WHERE file_id = $1",
                    r["file_id"],
                )
            if expired:
                logger.info(
                    "Cron: purge_expired_files purged %d files (removed %d)", len(expired), removed,
                )
    except Exception as e:
        logger.error(f"Cron: purge_expired_files failed: {e}")


async def _cleanup_file_images_job():
    """清理孤立文件图片：file_id 在 file_registry 中已不存在的条目。"""
    logger.info("Cron: cleanup_file_images started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 查找 file_id 在 file_registry 中已不存在的 file_images 条目
            orphaned = await conn.fetch(
                f"""SELECT fi.image_id, fi.storage_path
                    FROM {SCHEMA}.file_images fi
                    LEFT JOIN {SCHEMA}.file_registry fr ON fi.file_id = fr.file_id
                    WHERE fr.file_id IS NULL
                    LIMIT 200"""
            )
            if not orphaned:
                return

            # 物理删除孤立图片文件
            removed_files = 0
            deleted = await conn.execute(
                f"""DELETE FROM {SCHEMA}.file_images
                    WHERE image_id = ANY($1)""",
                [r["image_id"] for r in orphaned],
            )
            count = int(deleted.split()[-1]) if deleted else 0

            for r in orphaned:
                path = r["storage_path"]
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        removed_files += 1
                except OSError:
                    pass

            logger.info(
                "Cron: cleanup_file_images removed %d orphaned image records, "
                "%d physical files",
                count, removed_files,
            )
    except Exception as e:
        logger.error(f"Cron: cleanup_file_images failed: {e}")


# ── 任务注册 ──────────────────────────────────────────


def _spawn_task(coro, task_name: str) -> asyncio.Task:
    """注册后台任务并消费异常（P2 R11）。

    之前 `asyncio.create_task(job_fn())` fire-and-forget 未跟踪、未消费异常；
    统一在此注册 + add_done_callback 记日志，防未捕获异常被静默吞掉。
    """
    task = asyncio.create_task(coro)

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.error("Cron task %s crashed with unhandled error: %s",
                         task_name, exc, exc_info=exc)
        if t in _tasks:
            try:
                _tasks.remove(t)
            except ValueError:
                pass

    task.add_done_callback(_on_done)
    _tasks.append(task)
    return task


# ── 调度主循环 ────────────────────────────────────────

# (task_name, type, hour, minute, job_fn)
# type: "daily" — 每天在指定 hour:minute 触发
# type: "hourly" — 每小时在指定 minute 触发（hour 字段忽略）
_CRON_SCHEDULE: list[tuple[str, str, int, int, Callable]] = [
    ("refinement",          "daily",   2,  0, _refinement_job),
    ("cleanup_expired",     "daily",   3,  0, _cleanup_expired_job),
    ("cleanup_revoked",     "daily",   3, 30, _cleanup_revoked_job),
    ("cleanup_file_images", "daily",   4,  0, _cleanup_file_images_job),
    ("purge_expired_files", "daily",   4, 30, _purge_expired_files_job),
    ("cold_start_activate", "hourly",  0,  5, _cold_start_job),
    ("yongheng_sync_retry", "hourly",  0, 15, _sync_retry_job),
]


async def _scheduler_loop():
    global _running
    _running = True

    while _running:
        try:
            for task_name, sched_type, hour, minute, job_fn in _CRON_SCHEDULE:
                triggered = False
                if sched_type == "daily":
                    triggered = _should_run_daily(task_name, hour, minute)
                    if triggered:
                        _last_run_date[task_name] = _now().strftime("%Y-%m-%d")
                elif sched_type == "hourly":
                    triggered = _should_run_hourly(task_name, minute)
                    if triggered:
                        _last_run_hour[task_name] = f"{_now().strftime('%Y-%m-%d')}:{_now().hour}"

                if triggered:
                    _spawn_task(job_fn(), task_name)
        except Exception as e:
            logger.error(f"Cron scheduler loop error: {e}")

        await asyncio.sleep(60)


async def start():
    """启动汇川调度器（仅 management 角色）。"""
    global _running
    if _running:
        return

    if not is_management():
        logger.info("Huichuan cron: skipped (not management)")
        return

    logger.info(
        "Starting huichuan cron (tz=Asia/Shanghai): "
        "refinement@02:00, cleanup_expired@03:00, "
        "cold_start@hourly:05, sync_retry@hourly:15"
    )
    task = asyncio.create_task(_scheduler_loop())
    _tasks.append(task)


async def stop():
    """停止汇川调度器。"""
    global _running
    _running = False
    for task in _tasks:
        task.cancel()
    _tasks.clear()
    logger.info("Huichuan cron stopped")
