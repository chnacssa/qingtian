"""
永恒 — 蒸馏（Digest）逻辑
移植自 /opt/yongheng/service/routes/digest.py
同步 v2.1：date 可选，默认当天
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from common.db import get_pool
from . import config as cfg
from .models import AppError

logger = logging.getLogger("yongheng.digest")

# 分布式锁 TTL（秒）
_DIGEST_LOCK_TTL = 120


async def _acquire_digest_lock(namespace: str, target_date: str) -> bool:
    """Redis SETNX 分布式锁，防止重复生成同一天摘要。"""
    try:
        import redis.asyncio as aioredis
        from huanyu.config import get_redis_url
        redis = aioredis.from_url(get_redis_url())
        try:
            key = f"yongheng:digest:lock:{namespace}:{target_date}"
            acquired = await redis.set(key, "1", nx=True, ex=_DIGEST_LOCK_TTL)
            return bool(acquired)
        finally:
            await redis.aclose()
    except Exception:
        # Redis 不可用时 fail-open，避免阻塞 digest 生成
        logger.warning("Digest lock: Redis unavailable, proceeding without lock")
        return True


async def generate_digest(namespace: str, date: Optional[str] = None) -> dict:
    """生成指定 namespace 和日期的蒸馏内容。date 为空时默认当天（UTC）。"""
    # P2 (R11): 原 date_type.today() 取本地日，而查询窗口按 UTC 拼装（date_start.replace(tzinfo=utc)），
    # 非 UTC 时区会导致"当天"窗口偏移。统一用 UTC 日。
    target_date = date or datetime.now(timezone.utc).date().isoformat()

    if not await _acquire_digest_lock(namespace, target_date):
        return {"status": "locked", "message": f"Digest for {namespace}/{target_date} is already being generated"}

    try:
        date_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise AppError("VALIDATION_ERROR", f"invalid date: {target_date!r}, expected YYYY-MM-DD", 400)
    date_end = date_start + timedelta(days=1)
    schema = cfg.get_schema_name()

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT content, timestamp, source, protected
               FROM {schema}.memories
               WHERE namespace = $1 AND timestamp >= $2 AND timestamp < $3
               ORDER BY timestamp""",
            namespace, date_start, date_end
        )

    if not rows:
        return {"status": "noop", "message": "No records for this date", "content": ""}

    full_text = "\n".join(f"[{r['source']}] {r['content']}" for r in rows)
    protected_contents = [r['content'] for r in rows if r['protected']]

    return {
        "status": "pending",
        "record_count": len(rows),
        "full_text_length": len(full_text),
        "protected_highlights": protected_contents[:5],
    }


async def review_digest(namespace: str, date: str, action: str,
                         highlight_ids: Optional[List[int]] = None) -> dict:
    """审核蒸馏结果：approved/rejected/highlight"""
    try:
        review_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise AppError("VALIDATION_ERROR", f"invalid date: {date!r}, expected YYYY-MM-DD", 400)
    schema = cfg.get_schema_name()
    pool = await get_pool()

    if action == "approved":
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {schema}.digests SET review_status = 'approved', reviewed_at = NOW() "
                "WHERE namespace = $1 AND target_date = $2",
                namespace, review_date
            )
        return {"status": "approved", "namespace": namespace, "date": date}

    elif action == "rejected":
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {schema}.digests WHERE namespace = $1 AND target_date = $2",
                namespace, review_date
            )
        return {"status": "rejected", "message": "Digest discarded"}

    elif action == "highlight" and highlight_ids:
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {schema}.memories SET protected = TRUE WHERE id = ANY($1::bigint[]) AND namespace = $2",
                highlight_ids, namespace
            )
        return {"status": "highlighted", "count": len(highlight_ids)}

    raise AppError("VALIDATION_ERROR", f"invalid action: {action}", 400)
