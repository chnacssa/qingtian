"""汇川巡检引擎 — Phase 6

知识库自检自愈：孤立页面、断链修复、矛盾标记、过期清理、置信度衰减。

每日 cron（凌晨 4:00）执行。

API:
  - GET  /v1/huichuan/lint/report   — 巡检报告
  - POST /v1/huichuan/lint/auto-fix  — 自动修复
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("huichuan.lint")


def _now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


async def lint_report(conn, schema: str = "huichuan") -> dict:
    """巡检报告：检查知识库 5 类健康问题。

    五项检查：
      1. 孤立页面 — 无 knowledge_links 引用的 active 条目
      2. 断链 — knowledge_links 指向不存在的 target_id
      3. 矛盾 — link_type='contradicts' 未处理的标记
      4. 过期 — valid_until < NOW() 但 status 仍为 active
      5. 衰减 — 超过 90 天未更新的 active 条目

    Returns:
        dict with keys: orphans, broken_links, contradictions,
        expired, decayed, ran_at
    """
    report = {
        "orphans": [],
        "broken_links": [],
        "contradictions": [],
        "expired": [],
        "decayed": [],
        "ran_at": _now_str(),
    }

    # 1. 孤立页面检测：没有 knowledge_links 引用的条目
    try:
        orphans = await conn.fetch(
            f"""SELECT k.knowledge_id, k.title, k.domain
                FROM {schema}.knowledge_entries k
                LEFT JOIN {schema}.knowledge_links l
                  ON k.knowledge_id = l.target_id
                 OR k.knowledge_id = l.source_id
                WHERE l.link_id IS NULL
                  AND k.status = 'active'
                LIMIT 100"""
        )
        report["orphans"] = [
            {"knowledge_id": str(r["knowledge_id"]), "title": r["title"], "domain": r["domain"]}
            for r in orphans
        ]
    except Exception as e:
        logger.warning("Lint orphans check failed: %s", e)

    # 2. 断链检测：knowledge_links 指向不存在的条目
    try:
        broken = await conn.fetch(
            f"""SELECT l.link_id, l.source_id, l.target_id, l.link_type
                FROM {schema}.knowledge_links l
                LEFT JOIN {schema}.knowledge_entries k
                  ON l.target_id = k.knowledge_id
                WHERE k.knowledge_id IS NULL
                LIMIT 100"""
        )
        report["broken_links"] = [
            {"link_id": r["link_id"], "source_id": str(r["source_id"]),
             "target_id": str(r["target_id"]), "link_type": r["link_type"]}
            for r in broken
        ]
    except Exception as e:
        logger.warning("Lint broken_links check failed: %s", e)

    # 3. 矛盾标记：两个 entry 声称同一事实但 contradict 标记未处理
    try:
        contradictions = await conn.fetch(
            f"""SELECT l.link_id, l.source_id, l.target_id, l.confidence
                FROM {schema}.knowledge_links l
                WHERE l.link_type = 'contradicts'
                  AND l.confidence > 0
                LIMIT 100"""
        )
        report["contradictions"] = [
            {"link_id": r["link_id"], "source_id": str(r["source_id"]),
             "target_id": str(r["target_id"]), "confidence": r["confidence"]}
            for r in contradictions
        ]
    except Exception as e:
        logger.warning("Lint contradictions check failed: %s", e)

    # 4. 过期条目：valid_until < NOW() 但 status 仍为 active
    try:
        expired = await conn.fetch(
            f"""SELECT knowledge_id, title, domain, valid_until
                FROM {schema}.knowledge_entries
                WHERE valid_until < CURRENT_DATE
                  AND status NOT IN ('archived', 'revoked')
                LIMIT 100"""
        )
        report["expired"] = [
            {"knowledge_id": str(r["knowledge_id"]), "title": r["title"],
             "domain": r["domain"], "valid_until": str(r["valid_until"])}
            for r in expired
        ]
    except Exception as e:
        logger.warning("Lint expired check failed: %s", e)

    # 5. 置信度衰减：超过 90 天未更新的 knowledge → 标记（不直接改 confidence）
    try:
        decayed = await conn.fetch(
            f"""SELECT knowledge_id, title, domain, updated_at
                FROM {schema}.knowledge_entries
                WHERE updated_at < NOW() - INTERVAL '90 days'
                  AND status = 'active'
                ORDER BY updated_at ASC
                LIMIT 100"""
        )
        report["decayed"] = [
            {"knowledge_id": str(r["knowledge_id"]), "title": r["title"],
             "domain": r["domain"], "last_updated": str(r["updated_at"])}
            for r in decayed
        ]
    except Exception as e:
        logger.warning("Lint decayed check failed: %s", e)

    return report


async def auto_fix(conn, categories: list[str] | None = None,
                   schema: str = "huichuan") -> dict:
    """自动修复可处理的知识库问题。

    三项自动修复：
      1. broken_links — 删除引用不存在条目的链接
      2. expired — 归档 valid_until < NOW() 的条目
      3. decayed — 降低 90 天未更新条目的 quality（减 1，下限 1）

    Args:
        conn: asyncpg connection
        categories: 要修复的类别列表，None = 全部
        schema: 数据库 schema 名

    Returns:
        {"fixed": N, "skipped": N, "errors": N}
    """
    fix_all = categories is None
    fixed = 0
    errors = 0

    # 1. 修复断链（删除引用不存在的 entry）
    if fix_all or "broken_links" in (categories or []):
        try:
            result = await conn.execute(
                f"""DELETE FROM {schema}.knowledge_links l
                    WHERE NOT EXISTS (
                      SELECT 1 FROM {schema}.knowledge_entries k
                      WHERE k.knowledge_id = l.target_id
                    )"""
            )
            deleted = int(result.split()[-1]) if result else 0
            fixed += deleted
        except Exception as e:
            logger.error("auto_fix broken_links failed: %s", e)
            errors += 1

    # 2. 归档过期条目
    if fix_all or "expired" in (categories or []):
        try:
            result = await conn.execute(
                f"""UPDATE {schema}.knowledge_entries
                    SET status = 'archived', updated_at = NOW()
                    WHERE valid_until < CURRENT_DATE
                      AND status NOT IN ('archived', 'revoked')"""
            )
            archived = int(result.split()[-1]) if result else 0
            fixed += archived
        except Exception as e:
            logger.error("auto_fix expired failed: %s", e)
            errors += 1

    # 3. 置信度衰减：更新 quality
    if fix_all or "decayed" in (categories or []):
        try:
            result = await conn.execute(
                f"""UPDATE {schema}.knowledge_entries
                    SET quality = GREATEST(1, quality - 1),
                        updated_at = NOW()
                    WHERE updated_at < NOW() - INTERVAL '90 days'
                      AND status = 'active'
                      AND quality > 1"""
            )
            decayed = int(result.split()[-1]) if result else 0
            fixed += decayed
        except Exception as e:
            logger.error("auto_fix decayed failed: %s", e)
            errors += 1

    logger.info("Lint auto_fix: %d fixed, %d errors", fixed, errors)
    return {"fixed": fixed, "skipped": 0, "errors": errors}
