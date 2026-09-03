"""
吸星 — REST API 路由
知识采集 / 吸收 / 竞品扫描 / 踩坑 / 蒸馏 / Agent 进化
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from common.db import get_pool
from . import config as xcfg
from . import crawler, quality_gate, classifier
from common.config import get_role, get_host
from common import config as cc  # is_management 走模块属性，保持可 mock（测试 patch common.config.is_management）
from .crawler import _compute_hash, _extract_text
from .models import (
    AppError,
    SourceCreate, SourceUpdate, SourceResponse,
    CollectRequest, CollectResponse, CollectionResult,
    IngestRequest, IngestResponse,
    IngestToYonghengRequest,
    LearnRequest, LearnResponse,
    XizhenjiCreate, XizhenjiUpdate, XizhenjiResponse,
    ReportPitfallRequest,
    ScanRequest, ScanResponse, ScanResultItem,
    DistillRequest, DistillResponse,
    EvolveRequest, EvolveResponse,
)

logger = logging.getLogger("xixing.api")

router = APIRouter(prefix="/v1/xixing", tags=["吸星"])

SCHEMA = xcfg.get_schema_name()
BASE_DIR = xcfg.get_base_dir()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


# ── 知识源管理 ────────────────────────────────────────

@router.get("/sources")
async def list_sources():
    """获取知识源清单（从 DB）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.sources ORDER BY created_at DESC"
        )
    return {
        "action": "list_sources",
        "timestamp": _ts(),
        "sources": [dict(r) for r in rows],
        "total": len(rows),
    }


@router.post("/sources")
async def create_source(req: SourceCreate):
    """新增知识源。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可管理知识源")

    # 2026-08-28 P0 修复（SSRF 源头拦截）：采集源 URL 请求方可控，入库前过
    # url_guard（scheme 白名单 + DNS 解析后私网拦截），拒绝内网/环回/保留地址
    if req.url:
        from common.url_guard import check_external_url_async
        ok, reason = await check_external_url_async(req.url)
        if not ok:
            raise HTTPException(status_code=400, detail={
                "code": "BAD_SOURCE_URL", "message": f"采集源 URL 不合规: {reason}"})

    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            f"SELECT id FROM {SCHEMA}.sources WHERE id = $1", req.id
        )
        if existing:
            raise HTTPException(409, f"知识源已存在: {req.id}")
        await conn.execute(
            f"""INSERT INTO {SCHEMA}.sources (id, name, url, source_type, schedule, day_of_week, categories, notes, enabled)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            req.id, req.name, req.url, req.source_type, req.schedule,
            req.day_of_week, req.categories, req.notes, req.enabled,
        )
    return {"action": "create_source", "source_id": req.id, "timestamp": _ts()}


@router.put("/sources/{source_id}")
async def update_source(source_id: str, req: SourceUpdate):
    """更新知识源。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可管理知识源")

    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            f"SELECT id FROM {SCHEMA}.sources WHERE id = $1", source_id
        )
        if not existing:
            raise HTTPException(404, f"知识源不存在: {source_id}")
        updates = req.model_dump(exclude_none=True)
        if updates:
            set_clauses = [f"{k} = ${i+2}" for i, k in enumerate(updates)]
            values = list(updates.values())
            await conn.execute(
                f"UPDATE {SCHEMA}.sources SET {', '.join(set_clauses)}, updated_at = NOW() WHERE id = $1",
                source_id, *values,
            )
    return {"action": "update_source", "source_id": source_id, "timestamp": _ts()}


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str):
    """删除知识源。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可管理知识源")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"DELETE FROM {SCHEMA}.sources WHERE id = $1", source_id
        )
        if result == "DELETE 0":
            raise HTTPException(404, f"知识源不存在: {source_id}")
    return {"action": "delete_source", "source_id": source_id, "timestamp": _ts()}


# ── 采集 ──────────────────────────────────────────────

@router.post("/collect", response_model=CollectResponse)
async def collect(req: CollectRequest = CollectRequest()):
    """触发知识采集。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可触发采集")

    result = await crawler.run_collect(dry_run=req.dry_run, source_ids=req.source_ids)
    return CollectResponse(
        action="collect",
        dry_run=req.dry_run,
        sources_total=result["sources_total"],
        sources_collected=result["sources_collected"],
        sources_failed=result["sources_failed"],
        results=[CollectionResult(**r) for r in result["results"]],
        timestamp=_ts(),
    )


# ── 吸收（质量门 + 分类 + 存储）──────────────────────


def _read_content(row) -> str | None:
    """读取采集内容：优先读 crawler 提取的 .txt 纯文本，回退到 DB 存储的文本，最后尝试 HTML 提取。"""
    raw_path = row.get("raw_path")
    meta = row.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}

    # 1) 优先读取 .txt 纯文本文件
    if raw_path:
        text_path = raw_path.replace(".html", ".txt")
        if os.path.exists(text_path):
            try:
                with open(text_path, "r", encoding="utf-8") as f:
                    content = f.read()
                if content:
                    return content.replace("\x00", "")
            except Exception:
                pass

        # 2) 回退到 .html 文件 + BS4 提取
        if os.path.exists(raw_path):
            try:
                with open(raw_path, "r", encoding="utf-8") as f:
                    content = f.read()
                text = _extract_text(content)
                if text:
                    return text.replace("\x00", "")
            except Exception:
                pass

    # 3) 文件不可用：回退到 DB 中存储的文本（crawler 在 metadata.text_content 中保留的截断副本）
    if isinstance(meta, dict):
        db_text = meta.get("text_content")
        if db_text and isinstance(db_text, str) and len(db_text.strip()) >= 100:
            return db_text.replace("\x00", "")

    # 4) 完全不可用
    return None


async def run_ingest(conn, run_ids: list[int] | None = None, dry_run: bool = False) -> dict:
    """核心吸收逻辑：质量门 + 分类 → knowledge_items。供 API 和 scheduler 共用。

    注：不再接受 date 参数 —— 吸收对象是全部未摄入的成功采集，
    若需按采集日期筛选应传 run_ids。
    """
    conditions = [f"cr.status = 'success'"]
    params = []

    if run_ids:
        placeholders = ", ".join(f"${i+1}" for i in range(len(run_ids)))
        conditions.append(f"cr.id IN ({placeholders})")
        params.extend(run_ids)

    # 排除已摄入的 collection_runs（通过 knowledge_items.run_id 判断）
    rows = await conn.fetch(
        f"""SELECT cr.id as run_id, cr.source_id, cr.content_hash, cr.content_size, cr.raw_path, cr.metadata,
                   s.name as source_name
            FROM {SCHEMA}.collection_runs cr
            JOIN {SCHEMA}.sources s ON s.id = cr.source_id
            LEFT JOIN {SCHEMA}.knowledge_items ki ON ki.run_id = cr.id
            WHERE {' AND '.join(conditions)}
              AND ki.id IS NULL
            ORDER BY cr.id""",
        *params,
    )

    total = len(rows)
    passed_count = 0
    rejected_count = 0
    injected_count = 0
    results = []

    for row in rows:
        item_result = {
            "run_id": row["run_id"],
            "source_id": row["source_id"],
            "passed": False,
            "category": None,
            "quality_score": 0,
            "knowledge_id": None,
            "reject_reason": None,
        }

        raw_content = _read_content(row)
        if raw_content is None:
            item_result["reject_reason"] = "内容不可用（文件丢失且 DB 无缓存）"
            rejected_count += 1
            results.append(item_result)
            continue

        # 运行阻塞质量门（①-④）
        blocking_result = await quality_gate.run_blocking_gates(
            conn, raw_content, row["source_id"]
        )

        item_result["quality_score"] = blocking_result["quality_score"]

        if not blocking_result["passed"]:
            item_result["reject_reason"] = blocking_result["reject_reason"]
            rejected_count += 1
            results.append(item_result)
            continue

        # 分类（C1/R11: metadata 列是 JSONB，asyncpg 已解码为 dict——兼容 str 与 dict）
        meta = row["metadata"] or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        text_length = meta.get("text_length", 0)
        title = f"{row['source_name']} #{row['run_id']}"
        category = await classifier.classify(raw_content, title=title)

        # Gate ⑤ 时效性（分类感知：price/plugin 类内容过期直接拒绝）
        freshness_result = await quality_gate.run_quality_gates(
            conn, raw_content, row["source_id"], category=category
        )

        item_result["category"] = category
        item_result["quality_score"] = freshness_result["quality_score"]

        if not freshness_result["passed"]:
            item_result["reject_reason"] = freshness_result["reject_reason"]
            rejected_count += 1
            results.append(item_result)
            continue

        item_result["passed"] = True
        passed_count += 1

        if not dry_run:
            content = raw_content[:text_length] if text_length > 0 else raw_content[:5000]
            # P2 (R11): content_hash 语义统一——crawler 存的是 raw HTML hash，此处入库的是提取文本，
            # 两者不相等导致 ON CONFLICT(content_hash) 去重永不命中。改为对入库文本求 hash。
            content_hash = _compute_hash(content)

            knowledge_id = await conn.fetchval(
                f"""INSERT INTO {SCHEMA}.knowledge_items (source_id, run_id, title, content, content_hash, category, quality_score, gate_results)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (content_hash) DO UPDATE SET category = EXCLUDED.category,
                        quality_score = EXCLUDED.quality_score, run_id = EXCLUDED.run_id
                    RETURNING id""",
                row["source_id"], row["run_id"], title, content, content_hash,
                category, freshness_result["quality_score"],
                json.dumps(freshness_result["gate_results"], ensure_ascii=False),
            )
            item_result["knowledge_id"] = knowledge_id
            injected_count += 1

        results.append(item_result)

    return {
        "total_items": total,
        "passed": passed_count,
        "rejected": rejected_count,
        "injected": injected_count,
        "results": results,
    }


@router.post("/ingest", response_model=IngestResponse)
async def ingest(req: IngestRequest = IngestRequest()):
    """运行质量门 + 分类，将采集结果存入 knowledge_items。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可触发吸收")

    if req.date is None:
        req.date = datetime.now().strftime("%Y-%m-%d")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await run_ingest(conn, run_ids=req.run_ids, dry_run=req.dry_run)

    return IngestResponse(
        action="ingest",
        dry_run=req.dry_run,
        date=req.date,
        total_items=result["total_items"],
        passed=result["passed"],
        rejected=result["rejected"],
        injected=result["injected"],
        results=result["results"],
        timestamp=_ts(),
    )


# ── 注入永恒 ──────────────────────────────────────────


async def _find_existing_yongheng_memory(conn, knowledge_id: int) -> int | None:
    """注入幂等：查 yongheng.memories 中是否已存在该 knowledge 的注入记录。

    write_memory 成功后若标记 injected_to_yongheng 失败（如进程中断），
    重试时会跳过重复注入（metadata.xixing_knowledge_id 作为去重键）。
    """
    from yongheng.config import get_schema_name as yh_schema
    return await conn.fetchval(
        f"SELECT id FROM {yh_schema()}.memories "
        "WHERE namespace = $1 AND metadata->>'xixing_knowledge_id' = $2 LIMIT 1",
        xcfg.get_global_namespace(), str(knowledge_id),
    )


async def run_ingest_to_yongheng(conn, dry_run: bool = False, limit: int = 50) -> dict:
    """核心注入逻辑：将 knowledge_items 写入永恒记忆。供 API 和 scheduler 共用。"""
    from yongheng.memory_service import write_memory

    rows = await conn.fetch(
        f"""SELECT ki.*, s.name as source_name, s.url as source_url
            FROM {SCHEMA}.knowledge_items ki
            JOIN {SCHEMA}.sources s ON s.id = ki.source_id
            WHERE ki.injected_to_yongheng = FALSE
            ORDER BY ki.quality_score DESC
            LIMIT {limit}"""
    )

    stored = []
    failed = []

    for row in rows:
        if dry_run:
            stored.append({"id": row["id"], "title": row["title"], "dry_run": True})
            continue

        # P2 (R11): 注入幂等——上次标记失败后重试，先查是否已写入，避免重复注入记忆
        try:
            existing_mem_id = await _find_existing_yongheng_memory(conn, row["id"])
        except Exception as e:
            failed.append({"id": row["id"], "title": row["title"], "error": str(e)})
            continue
        if existing_mem_id is not None:
            # P2 (R11): 记忆已存在即视为注入成功；标记失败不阻断，留待下轮幂等兜底
            try:
                await conn.execute(
                    f"UPDATE {SCHEMA}.knowledge_items SET injected_to_yongheng = TRUE, injected_memory_id = $1 WHERE id = $2",
                    existing_mem_id, row["id"],
                )
            except Exception as e:
                logger.warning("mark injected_to_yongheng (dedup) failed for knowledge #%s: %s", row["id"], e)
            stored.append({"id": row["id"], "title": row["title"], "memory_id": existing_mem_id})
            continue

        metadata = {
            "xixing_source_id": row["source_id"],
            "xixing_knowledge_id": row["id"],
            "xixing_category": row["category"],
            "source_url": row["source_url"],
        }

        try:
            mem_result = await write_memory(
                conn,
                namespace=xcfg.get_global_namespace(),
                content=row["content"],
                mem_type="knowledge",
                source=f"xixing:{row['source_id']}",
                metadata=metadata,
            )
        except AppError as e:
            failed.append({"id": row["id"], "title": row["title"], "error": e.message})
            continue
        except Exception as e:
            failed.append({"id": row["id"], "title": row["title"], "error": str(e)})
            continue

        # P2 (R11): 注入成功即标记 injected——只记 memory_id 失败不阻断标记，
        # 否则下次重试会重复注入同一记忆（幂等查询仅作兜底）。
        try:
            await conn.execute(
                f"UPDATE {SCHEMA}.knowledge_items SET injected_to_yongheng = TRUE, injected_memory_id = $1 WHERE id = $2",
                mem_result["id"], row["id"],
            )
        except Exception as e:
            logger.warning("mark injected_to_yongheng failed for knowledge #%s: %s", row["id"], e)
            try:
                await conn.execute(
                    f"UPDATE {SCHEMA}.knowledge_items SET injected_to_yongheng = TRUE WHERE id = $1",
                    row["id"],
                )
            except Exception:
                pass
        stored.append({"id": row["id"], "title": row["title"], "memory_id": mem_result["id"]})

    return {
        "stored": stored,
        "failed": failed,
        "total_checked": len(rows),
    }


@router.post("/ingest-to-yongheng")
async def ingest_to_yongheng(req: IngestToYonghengRequest = IngestToYonghengRequest()):
    """将 knowledge_items 中未注入的知识写入永恒记忆（内部调用）。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可触发注入")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await run_ingest_to_yongheng(conn, dry_run=req.dry_run)

    return {
        "action": "ingest_to_yongheng",
        "dry_run": req.dry_run,
        "stored": result["stored"],
        "failed": result["failed"],
        "total_checked": result["total_checked"],
        "timestamp": _ts(),
    }


# ── 知识分发（从属服务器拉取） ──────────────────────


@router.get("/knowledge/export")
async def export_knowledge(
    since: str = Query(default=""),
    category: str = Query(default=""),
    limit: int = Query(default=100, le=500),
):
    """导出可分发知识，供从属服务器定时拉取并注入本地永恒。

    仅 management 角色可调用（从属服务器通过本地 API 中转）。
    返回 quality_score 最高的 N 条已注入永恒的知识。
    """
    conditions = ["ki.injected_to_yongheng = TRUE"]
    params = []
    idx = 1

    if since:
        conditions.append(f"ki.created_at >= ${idx}")
        params.append(since)
        idx += 1
    if category:
        conditions.append(f"ki.category = ${idx}")
        params.append(category)
        idx += 1

    where = " AND ".join(conditions)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT ki.id, ki.title, ki.content, ki.category, ki.quality_score,
                       ki.source_id, ki.created_at, s.name as source_name, s.url as source_url
                FROM {SCHEMA}.knowledge_items ki
                JOIN {SCHEMA}.sources s ON s.id = ki.source_id
                WHERE {where}
                ORDER BY ki.quality_score DESC
                LIMIT {limit}""",
            *params,
        )

    items = []
    for r in rows:
        items.append({
            "title": r["title"],
            "content": r["content"],
            "category": r["category"],
            "quality_score": r["quality_score"],
            "source_name": r["source_name"],
            "source_url": r["source_url"],
            "source_id": r["source_id"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "xixing_knowledge_id": r["id"],
        })

    return {
        "action": "export_knowledge",
        "total": len(items),
        "since": since or None,
        "items": items,
        "timestamp": _ts(),
    }


# ── Agent 经验 ────────────────────────────────────────

@router.post("/agent/{agent_id}/learn", response_model=LearnResponse)
async def agent_learn(agent_id: str, req: LearnRequest):
    """Agent 提交个人经验，直接写入永恒。"""
    from yongheng.memory_service import write_memory

    if not req.content or len(req.content.strip()) < 10:
        raise HTTPException(400, "content 至少 10 个字符")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 服务端字段放最后，防止调用方通过 metadata 伪造身份/时间戳
        metadata = {
            **req.metadata,
            "agent_id": agent_id,
            "submitted_at": _ts(),
        }
        try:
            mem_result = await write_memory(
                conn,
                namespace=agent_id,
                content=req.content,
                mem_type=req.memory_type,
                source=req.source,
                metadata=metadata,
            )
            return LearnResponse(
                action="learn",
                agent_id=agent_id,
                namespace=agent_id,
                memory_id=mem_result["id"],
                status="stored",
                timestamp=_ts(),
            )
        except AppError as e:
            raise HTTPException(e.status, e.message)


@router.get("/agent/{agent_id}/insights")
async def agent_insights(agent_id: str, top_k: int = 10):
    """Agent 查看自身进化洞察。"""
    from yongheng.memory_service import search_memory

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await search_memory(
            conn, namespace=agent_id, query="", method="keyword",
            top_k=top_k,
        )
        memories = result.get("results", [])

    # 分类统计
    type_stats: dict = {}
    source_stats: dict = {}
    for m in memories:
        mt = m.get("memory_type", "unknown")
        type_stats[mt] = type_stats.get(mt, 0) + 1
        src = m.get("source", "unknown")
        source_stats[src] = source_stats.get(src, 0) + 1

    return {
        "action": "insights",
        "agent_id": agent_id,
        "namespace": agent_id,
        "total_memories": len(memories),
        "type_distribution": type_stats,
        "source_distribution": source_stats,
        "recent_memories": [
            {
                "id": m.get("id"), "type": m.get("memory_type"),
                "source": m.get("source"), "timestamp": str(m.get("timestamp", "")),
                "preview": (m.get("content", "") or "")[:120],
            }
            for m in memories[:10]
        ],
        "timestamp": _ts(),
    }


@router.post("/agent/{agent_id}/report-pitfall")
async def agent_report_pitfall(agent_id: str, req: ReportPitfallRequest):
    """Agent 上报踩坑。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        xz_id = await conn.fetchval(
            f"""INSERT INTO {SCHEMA}.xizhenji (title, description, severity, source, category, related_agent, tags)
                VALUES ($1, $2, $3, 'agent-report', 'agent_report', $4, $5) RETURNING id""",
            req.title, req.description, req.severity, agent_id, req.tags,
        )
    return {
        "action": "report_pitfall",
        "xizhenji_id": xz_id,
        "agent_id": agent_id,
        "status": "recorded",
        "timestamp": _ts(),
    }


# ── 踩坑记录 ──────────────────────────────────────────

@router.get("/xizhenji")
async def list_xizhenji(
    severity: str = Query(default=""),
    resolved: Optional[bool] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0),
):
    """获取踩坑记录列表。"""
    conditions = ["1=1"]
    params = []
    idx = 1

    if severity:
        conditions.append(f"severity = ${idx}")
        params.append(severity)
        idx += 1
    if resolved is not None:
        conditions.append(f"resolved = ${idx}")
        params.append(resolved)
        idx += 1

    where = " AND ".join(conditions)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.xizhenji WHERE {where} ORDER BY created_at DESC LIMIT {limit} OFFSET {offset}",
            *params,
        )
    return {
        "action": "list_xizhenji",
        "timestamp": _ts(),
        "entries": [dict(r) for r in rows],
        "total": len(rows),
    }


@router.post("/xizhenji")
async def create_xizhenji(req: XizhenjiCreate):
    """手动记录踩坑。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        xz_id = await conn.fetchval(
            f"""INSERT INTO {SCHEMA}.xizhenji (title, description, root_cause, solution, severity, source, category, related_agent, tags, learned_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
            req.title, req.description, req.root_cause, req.solution,
            req.severity, req.source, req.category, req.related_agent, req.tags,
            datetime.fromisoformat(req.learned_at) if req.learned_at else datetime.now(timezone.utc),
        )
    return {"action": "create_xizhenji", "xizhenji_id": xz_id, "timestamp": _ts()}


# ── 跨底座踩坑聚合 ──────────────────────────────────

@router.get("/xizhenji/aggregate")
async def aggregate_xizhenji(resolved: bool = False):
    """汇聚所有底座的踩坑记录（仅 management 可调）。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 底座可查看全局踩坑")

    return await _do_aggregate(resolved)


@router.get("/aggregate")
async def aggregate_alias(resolved: bool = False):
    """快捷别名: /v1/xixing/aggregate → /v1/xixing/xizhenji/aggregate"""
    return await aggregate_xizhenji(resolved)


async def _do_aggregate(resolved: bool = False):
    all_pitfalls = []
    server_summary = {}

    # 本地
    pool = await get_pool()
    async with pool.acquire() as conn:
        local_rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.xizhenji WHERE resolved = $1 ORDER BY created_at DESC",
            resolved,
        )
        local_pitfalls = [dict(r) for r in local_rows]
        all_pitfalls.append({
            "source": get_role(),
            "host": get_host(),
            "count": len(local_pitfalls),
            "pitfalls": local_pitfalls,
        })
        server_summary[f"{get_role()} ({get_host()})"] = len(local_pitfalls)

    # 远程 peer
    try:
        from huanyu.peers import get_engine
        peers = await get_engine().get_online_peers()
        for peer in peers:
            url = f"http://{peer['host']}:{peer['port']}/v1/xixing/xizhenji?resolved={'true' if resolved else 'false'}"
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        # P1 (R11): 远端 list_xizhenji 实际返回 "entries"，
                        # 原读 "results" → 恒空聚合。兼容两种 key。
                        peer_pitfalls = data.get("entries") or data.get("results", [])
                        all_pitfalls.append({
                            "source": f"{peer['name']}",
                            "host": peer["host"],
                            "count": len(peer_pitfalls),
                            "pitfalls": peer_pitfalls,
                        })
                        server_summary[peer["name"]] = len(peer_pitfalls)
            except Exception as e:
                server_summary[peer["name"]] = f"不可达: {e}"
    except Exception:
        pass

    total = sum(
        p["count"] if isinstance(p["count"], int) else 0
        for p in all_pitfalls
    )

    return {
        "action": "aggregate_xizhenji",
        "resolved": resolved,
        "total_unresolved" if not resolved else "total_resolved": total,
        "servers": server_summary,
        "pitfalls": all_pitfalls,
        "timestamp": _ts(),
    }


@router.patch("/xizhenji/{xizhenji_id}")
async def update_xizhenji(xizhenji_id: int, req: XizhenjiUpdate):
    """更新踩坑记录。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            f"SELECT id FROM {SCHEMA}.xizhenji WHERE id = $1", xizhenji_id
        )
        if not existing:
            raise HTTPException(404, f"踩坑记录不存在: {xizhenji_id}")
        updates = req.model_dump(exclude_none=True)
        if updates:
            set_clauses = [f"{k} = ${i+2}" for i, k in enumerate(updates)]
            values = list(updates.values())
            await conn.execute(
                f"UPDATE {SCHEMA}.xizhenji SET {', '.join(set_clauses)} WHERE id = $1",
                xizhenji_id, *values,
            )
    return {"action": "update_xizhenji", "xizhenji_id": xizhenji_id, "timestamp": _ts()}


# ── 竞品扫描 ──────────────────────────────────────────

@router.post("/scan", response_model=ScanResponse)
async def scan(req: ScanRequest = ScanRequest()):
    """触发竞品扫描。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可触发扫描")

    total_scanned = 0
    scan_results = []

    try:
        from .scanner import run_scan as do_scan
        result = await do_scan(deep=req.deep, since=req.since)
        total_scanned = result.get("total_scanned", 0)
        scan_results = [
            ScanResultItem(**r) for r in result.get("results", [])
        ]
    except Exception as e:
        raise HTTPException(500, f"扫描失败: {e}")

    return ScanResponse(
        action="scan",
        total_scanned=total_scanned,
        top_n=len(scan_results),
        results=scan_results,
        timestamp=_ts(),
    )


@router.get("/scan/results")
async def list_scan_results(
    date: str = Query(default=""),
    actionable_only: bool = False,
    limit: int = Query(default=50, le=200),
):
    """获取竞品扫描历史结果。"""
    conditions = ["1=1"]
    params = []
    idx = 1

    if date:
        conditions.append(f"scan_date = ${idx}")
        params.append(date)
        idx += 1
    if actionable_only:
        conditions.append(f"actionable = TRUE")

    where = " AND ".join(conditions)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.scan_results WHERE {where} ORDER BY scan_date DESC, score DESC LIMIT {limit}",
            *params,
        )
    return {
        "action": "list_scan_results",
        "timestamp": _ts(),
        "results": [dict(r) for r in rows],
        "total": len(rows),
    }


# ── 蒸馏 ──────────────────────────────────────────────

@router.post("/distill", response_model=DistillResponse)
async def distill(req: DistillRequest = DistillRequest()):
    """手动触发经验蒸馏。"""
    if not cc.is_management():
        raise HTTPException(403, "仅 management 角色可触发蒸馏")

    try:
        from .distiller import run_distillation
        result = await run_distillation(
            namespace=req.namespace,
            max_source_memories=req.max_source_memories,
            model=req.model,
        )
        return DistillResponse(
            action="distill",
            namespace=req.namespace,
            source_count=result.get("source_count", 0),
            produced_count=result.get("produced_count", 0),
            llm_model=result.get("llm_model"),
            token_used=result.get("token_used", 0),
            status=result.get("status", "completed"),
            timestamp=_ts(),
        )
    except Exception as e:
        raise HTTPException(500, f"蒸馏失败: {e}")


# ── 运行状态 ──────────────────────────────────────────

@router.get("/status")
async def system_status():
    """吸星运行状态和统计。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        sources_count = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.sources WHERE enabled = TRUE")
        knowledge_count = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_items")
        injected_count = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_items WHERE injected_to_yongheng = TRUE")
        xizhenji_count = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.xizhenji WHERE resolved = FALSE")
        last_collection = await conn.fetchrow(
            f"SELECT source_id, finished_at, status FROM {SCHEMA}.collection_runs ORDER BY id DESC LIMIT 1"
        )

    reports_dir = os.path.join(BASE_DIR, "reports")
    report_days = 0
    if os.path.isdir(reports_dir):
        report_days = len([d for d in os.listdir(reports_dir) if os.path.isdir(os.path.join(reports_dir, d))])

    return {
        "action": "status",
        "timestamp": _ts(),
        "base_os": {
            "role": get_role(),
            "management": cc.is_management(),
            "global_namespace": xcfg.get_global_namespace(),
        },
        "stats": {
            "sources_enabled": sources_count,
            "knowledge_items": knowledge_count,
            "injected_to_yongheng": injected_count,
            "unresolved_pitfalls": xizhenji_count,
            "report_days": report_days,
            "last_collection": {
                "source_id": last_collection["source_id"] if last_collection else None,
                "finished_at": last_collection["finished_at"].isoformat() if last_collection and last_collection["finished_at"] else None,
                "status": last_collection["status"] if last_collection else "none",
            },
        },
    }


@router.get("/capabilities")
async def get_capabilities():
    """返回本底座 9 维度自评分数，供从属服务器拉取对比。"""
    from .self_assess import self_assess
    from .scanner import CAPABILITY_DIMENSIONS
    scores = await self_assess()
    return {
        "peer_id": _get_peer_id(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dimensions": {
            dim_id: {
                "name": info["name"],
                "score": info["score"],
                "scope": CAPABILITY_DIMENSIONS.get(dim_id, {}).get("scope", "shared"),
            }
            for dim_id, info in scores.items()
        },
        "overall": round(
            sum(s["score"] for s in scores.values()) / max(len(scores), 1), 2
        ),
    }


def _get_peer_id() -> str:
    try:
        from huanyu.config import get_peer_id
        return get_peer_id()
    except Exception:
        return "unknown"


@router.get("/deploy-manifest")
async def get_deploy_manifest():
    """返回部署清单：当前 git commit + 变更文件列表，供从属服务器参考。"""
    import subprocess

    manifest = {
        "peer_id": _get_peer_id(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": None,
        "commit_message": "",
        "branch": "",
        "changed_files": [],
    }

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            manifest["git_commit"] = result.stdout.strip()
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            manifest["commit_message"] = result.stdout.strip()
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            manifest["branch"] = result.stdout.strip()
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            manifest["changed_files"] = [
                f for f in result.stdout.strip().split("\n") if f
            ]
    except Exception:
        pass

    return manifest


@router.get("/health")
async def health():
    return {"status": "ok", "module": "xixing", "version": "3.0.0"}


@router.post("/skills/evolve", response_model=EvolveResponse)
async def evolve_skills(req: EvolveRequest = EvolveRequest()):
    """手动触发 Skill 提案生成（仅 management 角色）"""

    if not cc.is_management():
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "仅 management 角色可触发 Skill 生成"},
        )
    try:
        pool = await get_pool()
        from .distiller import _generate_skill_proposals

        result = await _generate_skill_proposals(pool, full_scan=req.full_scan)
        return EvolveResponse(proposals=result, total=len(result), dry_run=req.dry_run)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "EVOLVE_FAILED", "message": f"Skill 提案生成失败: {e}"},
        )


# ── 模块间数据加工 ──────────────────────────────────────


class ProcessRequest(BaseModel):
    """加工请求体 — 其他模块请求吸星加工数据"""
    action: str  # classify / extract / quality / pattern_analysis / distill / cluster
    input: dict
    sync: bool = True
    callback_url: str = ""
    params: dict = {}


class ProcessResponse(BaseModel):
    """加工响应"""
    status: str
    result: dict = {}
    task_id: str = ""
    elapsed_ms: int = 0
    estimated_seconds: int = 0


@router.post("/process")
async def process_data(req: ProcessRequest):
    """同步/异步加工入口 — 其他模块请求吸星加工数据"""
    if req.sync:
        start = datetime.now(timezone.utc)
        result = await _process_sync(req.action, req.input, req.params)
        elapsed = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        return {"status": "ok", "result": result, "elapsed_ms": elapsed}
    else:
        # 2026-08-28 P0 修复（SSRF）：callback_url 请求方可控，先过 url_guard
        # （scheme 白名单+DNS 解析拒内网）再接受异步任务，不再裸 POST 任意地址
        if req.callback_url:
            from common.url_guard import check_external_url_async
            ok, reason = await check_external_url_async(req.callback_url)
            if not ok:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "BAD_CALLBACK", "message": f"callback_url 不合规: {reason}"},
                )
        task_id = f"xp_{uuid.uuid4().hex[:12]}"
        asyncio.create_task(_process_async(task_id, req.action, req.input, req.callback_url, req.params))
        return {"status": "accepted", "task_id": task_id, "estimated_seconds": _estimate_seconds(req.action)}


async def _process_sync(action: str, input_data: dict, params: dict) -> dict:
    """同步加工 — 直接处理返回"""
    if action == "classify":
        text = input_data.get("text", "")
        if not text:
            return {"category": "unknown", "confidence": 0.0}
        try:
            from xixing.classifier import classify_text
            return await classify_text(text)
        except Exception:
            return {"category": "unknown", "confidence": 0.0}

    elif action == "extract":
        text = input_data.get("text", "")
        if not text:
            return {"entities": []}
        try:
            from xixing.distiller import extract_entities
            return await extract_entities(text)
        except Exception:
            return {"entities": []}

    elif action == "quality":
        text = input_data.get("text", "")
        if not text:
            return {"score": 0, "issues": []}
        try:
            from xixing.quality_gate import evaluate_quality
            return await evaluate_quality(text)
        except Exception:
            return {"score": 0, "issues": []}

    return {"action": action, "status": "completed", "note": "sync processing done"}


async def _process_async(task_id: str, action: str, input_data: dict, callback_url: str, params: dict):
    """异步加工 — 处理后回调"""
    try:
        result = await _process_sync(action, input_data, params)
        if callback_url:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(callback_url, json={
                    "task_id": task_id,
                    "status": "completed",
                    "result": result,
                })
    except Exception as e:
        logger.error("异步加工失败 task=%s: %s", task_id, e)


def _estimate_seconds(action: str) -> int:
    """根据加工类型预估耗时（秒）"""
    estimates = {"classify": 5, "extract": 10, "quality": 8, "pattern_analysis": 120, "distill": 180, "cluster": 60}
    return estimates.get(action, 30)


# ── url.md 采集清单解析 ──────────────────────────────


class UrlmdParseRequest(BaseModel):
    """url.md 解析请求"""
    content: str         # url.md 文件全文
    agent_id: str = ""   # 来源 Agent ID


@router.post("/sources/parse-urlmd")
async def parse_urlmd(req: UrlmdParseRequest):
    """解析 url.md 内容并批量注册采集源

    url.md 是用户通过 IM 向 Agent 发送的采集清单文件，
    每行一个 URL，支持 @tags 标签和 P0/P1/P2 优先级标记。
    """
    from .urlmd_parser import parse_urlmd

    result = await parse_urlmd(content=req.content, agent_id=req.agent_id)
    return {**result, "timestamp": _ts()}


# ── 学习反馈追踪 ──────────────────────────────────────


class FeedbackSubmit(BaseModel):
    """经验反馈提交"""
    experience_id: str
    experience_type: str = "personal"
    source_agent: str
    feedback_agent: str
    feedback_type: str  # useful / useless / incorrect
    feedback_detail: str = ""
    task_id: str = ""


@router.post("/feedback")
async def submit_experience_feedback(req: FeedbackSubmit):
    """提交经验反馈 — 某 Agent 使用了另一 Agent 上报的经验后报告效果"""
    from .feedback import submit_feedback

    result = await submit_feedback(
        experience_id=req.experience_id,
        experience_type=req.experience_type,
        source_agent=req.source_agent,
        feedback_agent=req.feedback_agent,
        feedback_type=req.feedback_type,
        feedback_detail=req.feedback_detail,
        task_id=req.task_id,
    )
    return {**result, "timestamp": _ts()}


@router.get("/feedback/{experience_id}")
async def list_experience_feedback(experience_id: str, limit: int = 50):
    """查询某条经验收到的所有反馈"""
    from .feedback import get_feedback_for_experience

    items = await get_feedback_for_experience(experience_id, limit=limit)
    return {
        "experience_id": experience_id,
        "total": len(items),
        "feedback": items,
        "timestamp": _ts(),
    }


@router.get("/feedback/agent/{agent_id}/summary")
async def agent_feedback_summary(
    agent_id: str,
    as_source: bool = True,
    days: int = 30,
):
    """查询 Agent 的经验采纳统计

    as_source=true: 该 Agent 作为经验上报者收到的反馈
    as_source=false: 该 Agent 作为使用者给出的反馈
    """
    from .feedback import get_feedback_summary_for_agent

    result = await get_feedback_summary_for_agent(agent_id, as_source=as_source, days=days)
    return {
        "agent_id": agent_id,
        "as_source": as_source,
        "window_days": days,
        **result,
        "timestamp": _ts(),
    }
