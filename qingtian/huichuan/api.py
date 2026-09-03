"""汇川 — FastAPI 路由"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os as _os
import shutil
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response

from common.db import get_pool
from yongheng.memory_service import delete_memory, search_memory, write_memory
from zhenyue import config as zcfg  # 镇岳 schema（本地 agent 安全登记）
from zhenyue.auth import verify_admin_token  # 平台管理令牌（restore 仅平台可调）
from zhenyue.token_service import authenticate

from . import config as kcfg
from .connector import run_connector
from .database import SCHEMA
from .errors import AppError, KnowledgeNotFoundError, VersionConflictError, VisibilityForbiddenError
from .import_export import batch_import, validate_entry
from .ingest import ingest_file, ingest_text
from .lint import auto_fix, lint_report
from .models import (
    ApiMetrics,
    BatchWriteRequest,
    BatchWriteResponse,
    BatchWriteResult,
    ImportReportResponse,
    ImportResultItem,
    IngestRequest,
    IngestResponse,
    IngestFileResponse,
    KnowledgeCreate,
    KnowledgeResponse,
    KnowledgeUpdate,
    MetricsResponse,
    RefineProcessResponse,
    RefineQueueItem,
    RefineQueueResponse,
    RefineSubmitRequest,
    RefinementMetrics,
    SearchMetrics,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    StatsResponse,
    StorageMetrics,
    SubscriptionCreate,
    SubscriptionResponse,
    SubscriptionUpdate,
    SyncMetrics,
    VectorSearchRequest,
    VectorSearchResponse,
    VectorSearchResultItem,
    VersionDetailResponse,
    VersionHistoryItem,
    VersionHistoryResponse,
)
from .refine import refine_batch
from .sanitizer import sanitize
from .search import search_with_visibility

logger = logging.getLogger("huichuan.api")

router = APIRouter(prefix="/v1/huichuan", tags=["汇川"])

ALLOWED_SORT_COLUMNS = {"created_at", "updated_at", "title", "quality", "version"}
ALLOWED_VISIBILITY = {"public", "enterprise", "private"}
ALLOWED_STATUS = {"draft", "active", "archived", "revoked"}

# 上传大小上限（MB）：2026-08-17 200→300；2026-08-31 波哥指示 300→500
# （客户投标文件实测超 200MB 常见）。
# 注意：当前实现仍是 file.read() 整读进内存，500MB 单请求峰值内存 ~500MB+，
# 大文件流式落盘 / 分块上传为后续优化方向（见 docs/信息流架构）。
_MAX_UPLOAD_MB = 500


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _safe_jsonb(val):
    """jsonb codec 兜底 — 兼容旧连接池返回字符串/列表的情况。

    PG metadata || jsonb 操作可能返回:
      - dict: 直接使用
      - str: JSON 字符串（旧连接池）
      - list: ['{}', '{"key":"val"}'] — || 拼接产生的 JSON 数组
    """
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(val, list):
        # PG metadata || jsonb 可能返回 JSON 数组
        result: dict = {}
        for item in val:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(item, dict):
                result.update(item)
        return result
    if isinstance(val, dict):
        return val
    return {}


def _row_to_response(row: dict) -> KnowledgeResponse:
    return KnowledgeResponse(
        knowledge_id=str(row["knowledge_id"]),
        title=row["title"],
        domain=row["domain"],
        tags=row["tags"] or [],
        visibility=row["visibility"],
        owner_agent=row.get("owner_agent"),
        authorized_agents=row.get("authorized_agents") or [],
        content=row["content"],
        source=row.get("source", "manual"),
        version=row.get("version", 1),
        valid_from=row.get("valid_from"),
        valid_until=row.get("valid_until"),
        metadata=_safe_jsonb(row.get("metadata")),
        entry_type=row.get("entry_type", "entity"),
        original_filename=row.get("original_filename"),
        original_storage_path=row.get("original_storage_path"),
        original_file_sha256=row.get("original_file_sha256"),
        quality=row.get("quality", 3),
        status=row.get("status", "active"),
        refined_at=row.get("refined_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ── 永恒同步辅助 ──────────────────────────────────────


async def _sync_to_yongheng(conn, knowledge_id: str, title: str, content: str,
                            domain: str, tags: list[str], visibility: str) -> bool:
    """将知识索引同步到永恒 (in-process, 非阻塞)。"""
    try:
        namespace = f"huichuan:index:{kcfg.get_deploy_env()}"
        metadata = {
            "knowledge_id": str(knowledge_id),
            "domain": domain,
            "title": title,
            "tags": tags,
            "visibility": visibility,
        }
        # 摘要取前 500 字，节省 embedding 开销
        summary = f"{title}\n{content[:500]}"
        result = await write_memory(
            conn,
            namespace=namespace,
            content=summary,
            mem_type="episodic",
            source="huichuan",
            metadata=metadata,
        )
        memory_id = result.get("id") if isinstance(result, dict) else result
        await conn.execute(
            f"UPDATE {SCHEMA}.knowledge_entries "
            f"SET metadata = metadata || $1::jsonb "
            f"WHERE knowledge_id = $2",
            json.dumps({"index_status": "synced", "index_memory_id": memory_id}),
            knowledge_id,
        )
        return True
    except Exception as e:
        logger.warning("YongHeng sync failed for %s: %s", knowledge_id, e)
        try:
            await conn.execute(
                f"UPDATE {SCHEMA}.knowledge_entries "
                f"SET metadata = metadata || $1::jsonb "
                f"WHERE knowledge_id = $2",
                json.dumps({"index_status": "pending_retry", "retry_at": datetime.now(timezone.utc).isoformat()}),
                knowledge_id,
            )
        except Exception:
            pass
        return False


async def _delete_from_yongheng(conn, knowledge_id: str) -> None:
    """从永恒删除已同步的知识索引 (非阻塞)。"""
    try:
        row = await conn.fetchrow(
            f"SELECT metadata FROM {SCHEMA}.knowledge_entries WHERE knowledge_id = $1",
            knowledge_id,
        )
        if not row:
            return
        meta = row["metadata"] or {}
        memory_id = meta.get("index_memory_id")
        if not memory_id:
            return
        await delete_memory(conn, memory_id)
    except Exception as e:
        logger.warning("YongHeng delete failed for %s: %s", knowledge_id, e)


# ── 可见性过滤 SQL 片段 ──────────────────────────────


def _visibility_filter(agent_id: str | None, start_index: int = 1) -> tuple[str, list]:
    """生成可见性过滤 SQL 片段和参数。agent_id=None 时仅返回 public。

    返回 (SQL片段, 参数列表)。SQL 使用 $start_index, $start_index+1 占位符。
    """
    if not agent_id:
        return ("visibility = 'public'", [])
    return (
        f"(visibility = 'public' "
        f"OR visibility = 'enterprise' "
        f"OR (visibility = 'private' AND owner_agent = ${start_index}) "
        f"OR (visibility = 'private' AND ${start_index + 1} = ANY(authorized_agents)))",
        [agent_id, agent_id],
    )


# ── 查询参数提取 ──────────────────────────────────────


async def _resolve_caller_agent(request: Request) -> str | None:
    """从请求上下文解析调用方 agent_id。

    优先级: 网关注入 state.agent_id > Bearer token (镇岳) > X-Agent-ID (仅 loopback)。

    P1-1 修正（2026-08-14）：IPC 代理（trusted Skill）以 admin Bearer token 认证、
    用 X-Agent-ID 头透传真实用户身份（与 gateway/middleware.py 同模式）。
    admin 凭据 + X-Agent-ID 并存时优先 X-Agent-ID——否则 private 经验检索会被
    admin 身份短路（工作秘书沉淀到汇川后"随时可查"断裂）。

    9-1 修复日 fail-closed（汇川 review P1-3/P1-4，agent_id 可伪造全局面）：
    - 新增 #0 网关注入（req.state.agent_id，Bearer 认证结果，不可客户端伪造）；
    - #1 Bearer 认证失败（无效 token/异常）不再静默降级信任 X-Agent-ID——
      带了凭据但无效 = 拒绝识别，防止伪造 X-Agent-ID 冒充；
    - #2/#3 X-Agent-ID/query 仅限 loopback 直连（羲和 IPC 代理/本地运维），
      远程调用方一律要求 Bearer/网关注入身份。
    """
    # 0. 网关注入（gateway/middleware 认证后写入，不可伪造）——最优先
    state_agent = getattr(getattr(request, "state", None), "agent_id", "")
    if state_agent:
        return state_agent

    # 1. Authorization Bearer (镇岳 token)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                identity = await authenticate(conn, token)
        except Exception:
            return None  # fail-closed：DB 异常不降级（原 pass 继续信任 X-Agent-ID）
        if identity:
            # admin 凭据 + X-Agent-ID → 用透传的真实用户身份
            if identity.get("role") == "admin":
                x_agent = request.headers.get("X-Agent-ID", "")
                if x_agent:
                    return x_agent
            return identity["agent_id"]
        # token 无效 → fail-closed：不再落到 #2/#3
        return None

    # 2/3. X-Agent-ID header / query param —— 仅 loopback 直连（IPC 代理/本地运维）
    client = getattr(request, "client", None)
    client_host = client.host if client else ""
    if client_host not in ("127.0.0.1", "::1"):
        return None
    agent_id = request.headers.get("X-Agent-ID", "")
    if agent_id:
        return agent_id
    return request.query_params.get("agent_id")


# ═══════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════


# ── Health ─────────────────────────────────────────────


@router.get("/health")
async def health():
    return {"status": "ok", "module": "huichuan", "version": "1.0.0"}


# ── 摄入 (Phase 3) ──────────────────────────────────────


@router.post("/ingest")
async def ingest_text_endpoint(req: IngestRequest):
    """文本摄入 — LLM 编译入库。

    认证: admin / dept_head（暂未强制，后续接镇岳）。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await ingest_text(
            conn,
            text=req.text,
            source=req.source,
            original_filename=req.original_filename or "",
            storage_path=req.storage_path or "",
            schema=SCHEMA,
        )

    if result.get("error"):
        raise HTTPException(400, result["error"])

    return result


@router.post("/ingest/file")
async def ingest_file_endpoint(
    file: UploadFile = File(...),
    source: str = Form("api"),
):
    """文件上传摄入 — 解析文本 → LLM 编译入库。

    支持格式: .txt, .md, .json, .csv, .pdf, .docx, .xlsx
    文件大小限制: {_MAX_UPLOAD_MB}MB
    """
    if not file.filename:
        raise HTTPException(400, "文件名不能为空")

    content = await file.read()
    if len(content) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {_MAX_UPLOAD_MB}MB 上限: {len(content)} 字节")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await ingest_file(
            conn,
            content,
            file.filename,
            source=source,
            schema=SCHEMA,
        )

    if result.get("error"):
        raise HTTPException(400, result["error"])

    return result


# ── CRUD ───────────────────────────────────────────────


@router.post("", status_code=201)
@router.post("/", status_code=201)
async def create_knowledge(req: KnowledgeCreate, agent_id: str | None = Depends(_resolve_caller_agent)):
    """单条知识创建。"""
    if req.visibility not in ALLOWED_VISIBILITY:
        raise HTTPException(422, f"无效的可见性: {req.visibility}")
    if req.status not in ALLOWED_STATUS:
        raise HTTPException(422, f"无效的状态: {req.status}")

    max_size = kcfg.get_max_knowledge_size()
    if len(req.content) > max_size:
        raise HTTPException(
            422, f"内容长度 {len(req.content)} 超过上限 {max_size}"
        )

    # 准入校验（设计文档 §5.9）
    violation = validate_entry(req.title, req.content, req.domain)
    if violation:
        raise HTTPException(422, violation)

    owner = req.owner_agent or agent_id
    if req.visibility == "private" and not owner:
        raise HTTPException(400, "visibility=private 时必须指定 owner_agent")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.knowledge_entries
                (title, domain, tags, visibility, owner_agent, authorized_agents,
                 content, source, valid_from, valid_until, metadata,
                 entry_type, original_filename, original_storage_path, original_file_sha256,
                 quality, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,
                        $12,$13,$14,$15,
                        $16,$17)
                RETURNING *""",
            req.title, req.domain, req.tags, req.visibility,
            owner, req.authorized_agents,
            req.content, req.source, req.valid_from, req.valid_until,
            json.dumps(req.metadata or {}, ensure_ascii=False),
            req.entry_type, req.original_filename, req.original_storage_path,
            req.original_file_sha256,
            req.quality, req.status,
        )
        # 版本快照
        await conn.execute(
            f"INSERT INTO {SCHEMA}.knowledge_versions (knowledge_id, version, content, changed_by) "
            f"VALUES ($1, 1, $2, $3)",
            row["knowledge_id"], req.content, owner,
        )
        # 永恒同步 (非阻塞)
        await _sync_to_yongheng(
            conn, row["knowledge_id"], req.title, req.content,
            req.domain, req.tags, req.visibility,
        )

    return _row_to_response(dict(row))


# ── 批量写入 ──────────────────────────────────────────


@router.post("/batch-write")
async def batch_write(req: BatchWriteRequest, agent_id: str | None = Depends(_resolve_caller_agent)):
    """Agent 批量写入知识 (JSON)。"""
    stored = 0
    failed = 0
    results: list[BatchWriteResult] = []

    pool = await get_pool()
    async with pool.acquire() as conn:
        for i, entry in enumerate(req.entries):
            try:
                owner = entry.owner_agent or agent_id
                row = await conn.fetchrow(
                    f"""INSERT INTO {SCHEMA}.knowledge_entries
                        (title, domain, tags, visibility, owner_agent, authorized_agents,
                         content, source, valid_from, valid_until, metadata,
                         entry_type, original_filename, original_storage_path, original_file_sha256,
                         quality, status)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,
                                $12,$13,$14,$15,
                                $16,$17)
                        RETURNING *""",
                    entry.title, entry.domain, entry.tags, entry.visibility,
                    owner, entry.authorized_agents,
                    entry.content, entry.source, entry.valid_from, entry.valid_until,
                    json.dumps(entry.metadata, ensure_ascii=False) if entry.metadata else "{}",
                    getattr(entry, 'entry_type', 'entity'),
                    getattr(entry, 'original_filename', None),
                    getattr(entry, 'original_storage_path', None),
                    getattr(entry, 'original_file_sha256', None),
                    entry.quality, entry.status,
                )
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.knowledge_versions (knowledge_id, version, content, changed_by) "
                    f"VALUES ($1, 1, $2, $3)",
                    row["knowledge_id"], entry.content, owner,
                )
                await _sync_to_yongheng(
                    conn, row["knowledge_id"], entry.title, entry.content,
                    entry.domain, entry.tags, entry.visibility,
                )
                results.append(BatchWriteResult(
                    index=i, knowledge_id=str(row["knowledge_id"]),
                    title=entry.title, status="stored",
                ))
                stored += 1
            except Exception as e:
                results.append(BatchWriteResult(
                    index=i, title=entry.title, status="failed", error=str(e),
                ))
                failed += 1

    return BatchWriteResponse(
        total=len(req.entries), stored=stored, failed=failed,
        results=results, timestamp=_ts(),
    )


# ── 连接器 (Phase 4) ──────────────────────────────────


@router.post("/connector/{name}/run")
async def run_connector_endpoint(name: str, _admin: str = Depends(verify_admin_token)):
    """手动触发 ERP 连接器。

    认证: admin only（暂未强制，后续接镇岳）。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await run_connector(conn, name, schema=SCHEMA)

    if result.get("error"):
        raise HTTPException(400, result["error"])

    return result


# ── 巡检 (Phase 6) ──────────────────────────────────────


@router.get("/lint/report")
async def lint_report_endpoint(_admin: str = Depends(verify_admin_token)):
    """巡检报告 — 孤立/断链/矛盾/过期/衰减。

    认证: admin / dept_head。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        report = await lint_report(conn, schema=SCHEMA)

    return report


@router.post("/lint/auto-fix")
async def lint_auto_fix_endpoint(categories: list[str] | None = None, _admin: str = Depends(verify_admin_token)):
    """自动修复可自动处理的知识库问题。

    认证: admin only。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await auto_fix(conn, categories=categories, schema=SCHEMA)

    return result


# ── 晋升 (Phase 7) ──────────────────────────────────────


@router.post("/promote/{knowledge_id}")
async def promote_knowledge(knowledge_id: UUID, agent_id: str | None = Depends(_resolve_caller_agent), _admin: str = Depends(verify_admin_token)):
    """手动晋升私有知识到共享层。

    脱敏处理（PII + 内部备注），保留溯源链。
    认证: admin / dept_head / cfo。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT content, title, visibility, metadata "
                f"FROM {SCHEMA}.knowledge_entries WHERE knowledge_id = $1 "
                f"FOR UPDATE",
                knowledge_id,
            )
            if not row:
                raise KnowledgeNotFoundError(str(knowledge_id))

            # 脱敏：私有→共享
            content = sanitize(row["content"], level="private_to_shared")
            title = sanitize(row["title"], level="private_to_shared")[:256]

            # 更新 visibility + 追加溯源 metadata
            meta = _safe_jsonb(row.get("metadata"))
            meta["promoted_by"] = agent_id or "unknown"
            meta["promoted_at"] = _ts()
            meta["original_visibility"] = row["visibility"]

            await conn.execute(
                f"""UPDATE {SCHEMA}.knowledge_entries
                    SET visibility = 'enterprise',
                        content = $1,
                        title = $2,
                        metadata = $3::jsonb,
                        updated_at = NOW()
                    WHERE knowledge_id = $4""",
                content, title,
                json.dumps(meta, ensure_ascii=False),
                knowledge_id,
            )

    return {
        "action": "promote",
        "knowledge_id": str(knowledge_id),
        "visibility": "enterprise",
        "promoted_by": agent_id,
        "timestamp": _ts(),
    }


# ── 图谱查询 (Phase 5) ──────────────────────────────────


@router.get("/{knowledge_id}/links")
async def get_knowledge_links(knowledge_id: UUID):
    """查询某知识的所有关联（图谱 1-跳）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT l.link_id, l.source_id, l.target_id, l.link_type,
                       l.confidence, l.created_at
                FROM {SCHEMA}.knowledge_links l
                WHERE l.source_id = $1 OR l.target_id = $1
                ORDER BY l.created_at DESC LIMIT 100""",
            knowledge_id,
        )

    return {
        "knowledge_id": str(knowledge_id),
        "links": [
            {
                "link_id": r["link_id"],
                "source_id": str(r["source_id"]),
                "target_id": str(r["target_id"]),
                "link_type": r["link_type"],
                "confidence": r["confidence"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.get("/graph/{knowledge_id}/neighborhood")
async def get_graph_neighborhood(knowledge_id: UUID, max_hops: int = 2):
    """图谱 2-跳邻域查询。

    用 SQL CTE 实现：
      source_id → target_id
      UNION
      target_id → kl2.target_id
    """
    if max_hops not in (1, 2):
        raise HTTPException(400, "max_hops 仅支持 1 或 2")

    pool = await get_pool()
    async with pool.acquire() as conn:
        if max_hops == 1:
            rows = await conn.fetch(
                f"""SELECT DISTINCT k.knowledge_id, k.title, k.domain, k.entry_type
                    FROM {SCHEMA}.knowledge_links l
                    JOIN {SCHEMA}.knowledge_entries k
                      ON k.knowledge_id = l.target_id
                    WHERE l.source_id = $1
                    LIMIT 100""",
                knowledge_id,
            )
        else:
            rows = await conn.fetch(
                f"""WITH one_hop AS (
                      SELECT target_id FROM {SCHEMA}.knowledge_links
                      WHERE source_id = $1
                    ),
                    two_hop AS (
                      SELECT kl2.target_id
                      FROM {SCHEMA}.knowledge_links kl1
                      JOIN {SCHEMA}.knowledge_links kl2
                        ON kl1.target_id = kl2.source_id
                      WHERE kl1.source_id = $1
                    )
                    SELECT DISTINCT k.knowledge_id, k.title, k.domain, k.entry_type
                    FROM {SCHEMA}.knowledge_entries k
                    WHERE k.knowledge_id IN (TABLE one_hop)
                       OR k.knowledge_id IN (TABLE two_hop)
                    LIMIT 200""",
                knowledge_id,
            )

    return {
        "knowledge_id": str(knowledge_id),
        "max_hops": max_hops,
        "neighbors": [
            {
                "knowledge_id": str(r["knowledge_id"]),
                "title": r["title"],
                "domain": r["domain"],
                "entry_type": r.get("entry_type", "entity"),
            }
            for r in rows
        ],
        "total": len(rows),
    }


# ── 搜索 ──────────────────────────────────────────────


@router.get("/search")
async def search_knowledge_get(
    query: str = "",
    tags: str = Query(default=""),
    domain: str | None = None,
    sort_by: str = Query(default="updated_at"),
    sort_order: str = Query(default="desc"),
    limit: int = Query(default=20, le=200),
    offset: int = 0,
    include_expired: bool = False,
    agent_id: str | None = Depends(_resolve_caller_agent),
):
    """关键词/标签检索（GET 版），含可见性过滤。"""
    _tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    pool = await get_pool()
    async with pool.acquire() as conn:
        results, total = await search_with_visibility(
            conn,
            query=query,
            agent_id=agent_id,
            domain=domain or "",
            tags=_tags,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
            include_expired=include_expired,
            schema=SCHEMA,
        )

    return SearchResponse(
        results=[
            SearchResultItem(
                knowledge_id=r["knowledge_id"],
                title=r["title"],
                domain=r["domain"],
                tags=r["tags"],
                snippet=r["snippet"],
                visibility=r["visibility"],
                updated_at=r["updated_at"],
            )
            for r in results
        ],
        total=total,
    )


@router.post("/search")
async def search_knowledge(req: SearchRequest, agent_id: str | None = Depends(_resolve_caller_agent)):
    """关键词/标签检索，含可见性过滤。

    Phase 0: 委托 huichuan/search.py（pg_bigm + iLIKE 混合搜索）。
    """
    if req.sort_by not in ALLOWED_SORT_COLUMNS:
        raise HTTPException(422, f"不支持的排序字段: {req.sort_by}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        results, total = await search_with_visibility(
            conn,
            query=req.query,
            agent_id=agent_id,
            domain=req.domain or "",
            tags=req.tags,
            limit=req.limit,
            offset=req.offset,
            sort_by=req.sort_by,
            sort_order=req.sort_order,
            include_expired=req.include_expired,
            schema=SCHEMA,
        )

    return SearchResponse(
        results=[
            SearchResultItem(
                knowledge_id=r["knowledge_id"],
                title=r["title"],
                domain=r["domain"],
                tags=r["tags"],
                snippet=r["snippet"],
                visibility=r["visibility"],
                updated_at=r["updated_at"],
            )
            for r in results
        ],
        count=total,
        query=req.query,
        limit=req.limit,
        offset=req.offset,
    )


@router.post("/refine/trigger")
async def trigger_refinement(_admin: str = Depends(verify_admin_token)):
    """手动触发精炼管道（日常由 cron 每天 2:00 自动运行，此端点供手动调试）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await refine_batch(conn)
    return {"action": "refine", **result}


@router.post("/vector-search")
async def vector_search(req: VectorSearchRequest, agent_id: str | None = Depends(_resolve_caller_agent)):
    """语义检索，委托永恒。永恒不可用时降级为本地关键词搜索。"""
    try:
        deploy_env = kcfg.get_deploy_env()
        namespace = f"huichuan:index:{deploy_env}"
        filter_dict = {}
        if req.domain:
            filter_dict["domain"] = req.domain
        if req.tags:
            filter_dict["tags"] = req.tags

        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await search_memory(
                conn, namespace, req.query,
                method="hybrid", top_k=req.top_k,
            )
    except Exception as e:
        logger.warning("Vector search delegated to YongHeng failed: %s, falling back to local", e)
        # Fallback: 本地关键词搜索（直调 search_with_visibility，不绕端点函数）
        pool = await get_pool()
        async with pool.acquire() as conn:
            results, count = await search_with_visibility(
                conn,
                query=req.query,
                agent_id=agent_id,
                domain=req.domain or "",
                tags=req.tags,
                limit=req.top_k,
                schema=SCHEMA,
            )
        return VectorSearchResponse(
            results=[
                VectorSearchResultItem(
                    knowledge_id=r["knowledge_id"],
                    title=r["title"],
                    domain=r["domain"],
                    tags=r["tags"],
                    snippet=r["snippet"],
                    similarity=0.0,
                    visibility=r["visibility"],
                    updated_at=r["updated_at"],
                )
                for r in results
            ],
            count=count,
            delegated_to="huichuan_local",
        )

    # 解析永恒返回结果
    items = result.get("items") or result.get("results") or []
    memory_ids = [item.get("id") for item in items if item.get("id")]
    similarities = {
        item.get("id"): item.get("score", 0.0)
        for item in items if item.get("id")
    }

    if not memory_ids:
        return VectorSearchResponse(results=[], count=0)

    # 根据 memory_id 批量查 knowledge_entries
    pool2 = await get_pool()
    async with pool2.acquire() as conn:
        vf_clause, vf_params = _visibility_filter(agent_id, start_index=len(memory_ids) + 1)
        # 从 metadata.index_memory_id 匹配
        placeholders = ", ".join(f"${i+1}" for i in range(len(memory_ids)))
        rows = await conn.fetch(
            f"SELECT knowledge_id, title, domain, tags, visibility, content, updated_at "
            f"FROM {SCHEMA}.knowledge_entries "
            f"WHERE metadata->>'index_memory_id' IN ({placeholders}) AND ({vf_clause})",
            *memory_ids, *vf_params,
        )

    results = [
        VectorSearchResultItem(
            knowledge_id=str(r["knowledge_id"]),
            title=r["title"],
            domain=r["domain"],
            tags=r["tags"] or [],
            snippet=r["content"][:200] if r["content"] else "",
            similarity=similarities.get(
                (r.get("metadata") or {}).get("index_memory_id", ""), 0.0,
            ),
            visibility=r["visibility"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    results.sort(key=lambda x: x.similarity, reverse=True)

    return VectorSearchResponse(results=results, count=len(results))


# ── 订阅 ──────────────────────────────────────────────


@router.get("/subscriptions")
async def list_subscriptions(agent_id: str | None = Depends(_resolve_caller_agent)):
    """查看订阅列表。可按 agent_id 过滤。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_id:
            rows = await conn.fetch(
                f"SELECT subscription_id, agent_id, subscription_name, domains, tags, "
                f"active, created_at, updated_at "
                f"FROM {SCHEMA}.subscriptions WHERE agent_id = $1 ORDER BY created_at DESC",
                agent_id,
            )
        else:
            rows = await conn.fetch(
                f"SELECT subscription_id, agent_id, subscription_name, domains, tags, "
                f"active, created_at, updated_at "
                f"FROM {SCHEMA}.subscriptions ORDER BY created_at DESC",
            )

    return {
        "subscriptions": [
            SubscriptionResponse(
                subscription_id=str(r["subscription_id"]),
                agent_id=r["agent_id"],
                subscription_name=r["subscription_name"],
                domains=r["domains"] or [],
                tags=r["tags"] or [],
                active=r["active"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/subscribe", status_code=201)
async def create_subscription(req: SubscriptionCreate):
    """注册订阅。同一 agent 可创建多条（按 subscription_name 区分）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.subscriptions
                    (agent_id, subscription_name, domains, tags)
                    VALUES ($1, $2, $3, $4) RETURNING *""",
                req.agent_id, req.subscription_name, req.domains, req.tags,
            )
        except UniqueViolationError:
            raise HTTPException(409, f"订阅已存在: {req.agent_id}/{req.subscription_name}")

    return SubscriptionResponse(
        subscription_id=str(row["subscription_id"]),
        agent_id=row["agent_id"],
        subscription_name=row["subscription_name"],
        domains=row["domains"] or [],
        tags=row["tags"] or [],
        active=row["active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.put("/subscribe/{subscription_id}")
async def update_subscription(subscription_id: UUID, req: SubscriptionUpdate):
    """更新订阅（domains/tags/active/subscription_name）。"""
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "至少提供一个要更新的字段")

    set_clauses = []
    params: list = []
    idx = 1
    for key, value in updates.items():
        set_clauses.append(f"{key} = ${idx}")
        params.append(value)
        idx += 1
    set_clauses.append("updated_at = NOW()")
    params.append(subscription_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE {SCHEMA}.subscriptions "
            f"SET {', '.join(set_clauses)} WHERE subscription_id = ${idx} RETURNING *",
            *params,
        )
        if not row:
            raise HTTPException(404, f"订阅不存在: {subscription_id}")

    return SubscriptionResponse(
        subscription_id=str(row["subscription_id"]),
        agent_id=row["agent_id"],
        subscription_name=row["subscription_name"],
        domains=row["domains"] or [],
        tags=row["tags"] or [],
        active=row["active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.delete("/subscribe/{subscription_id}")
async def delete_subscription(subscription_id: UUID):
    """删除订阅。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"DELETE FROM {SCHEMA}.subscriptions WHERE subscription_id = $1",
            subscription_id,
        )
        if result == "DELETE 0":
            raise HTTPException(404, f"订阅不存在: {subscription_id}")

    return {"action": "delete_subscription", "subscription_id": subscription_id, "timestamp": _ts()}


# ── 批量导入 ──────────────────────────────────────────


@router.post("/batch-import")
async def batch_import_files(
    request: Request,
    files: list[UploadFile] = File(...),
    domain: str = Form("general"),
    visibility: str = Form("public"),
    auto_confirm: bool = Form(False),
    _admin: str = Depends(verify_admin_token),
):
    """批量导入文件（设计文档 §4.1）。multipart/form-data，支持 .json/.md/.csv/.txt。"""
    if len(files) > 100:
        raise HTTPException(400, f"单次最多 100 个文件，收到 {len(files)}")

    file_contents: list[tuple[str, str]] = []
    for f in files:
        try:
            content = (await f.read()).decode("utf-8", errors="replace")
        except Exception as e:
            raise HTTPException(422, f"文件读取失败 {f.filename}: {e}")
        file_contents.append((f.filename or "unknown", content))

    # P2 (R11): 去重按调用方归属范围（无法解析时 None = 全局 NULL owner 范围）
    caller = await _resolve_caller_agent(request)
    result = await batch_import(file_contents, domain=domain, visibility=visibility,
                                auto_confirm=auto_confirm, owner_agent=caller)

    if "error" in result:
        raise HTTPException(400, result["error"])

    return ImportReportResponse(
        status=result.get("status", "completed"),
        total_files=result.get("total_files", len(files)),
        total_items=result.get("total_items", 0),
        created=result.get("created", 0),
        updated=result.get("updated", 0),
        skipped=result.get("skipped", 0),
        conflicted=result.get("conflicted", 0),
        failed=result.get("failed", 0),
        results=[
            ImportResultItem(**r) for r in result.get("results", [])
        ],
        timestamp=result.get("timestamp", _ts()),
    )


# ── 恢复 ──────────────────────────────────────────────


@router.post("/{knowledge_id}/restore")
async def restore_knowledge(knowledge_id: UUID, _admin: str = Depends(verify_admin_token)):
    """恢复已撤回的知识（设计文档 §7.4）。status=revoked → active。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT status, original_storage_path "
            f"FROM {SCHEMA}.knowledge_entries WHERE knowledge_id = $1",
            knowledge_id,
        )
        if not row:
            raise KnowledgeNotFoundError(knowledge_id)
        if row["status"] != "revoked":
            raise HTTPException(400, f"只能恢复 status=revoked 的知识，当前状态: {row['status']}")

        await conn.execute(
            f"UPDATE {SCHEMA}.knowledge_entries SET status='active', updated_at=NOW() "
            f"WHERE knowledge_id = $1",
            knowledge_id,
        )

        # 同步 file_registry：恢复时减 revoked 计数
        storage_path = row.get("original_storage_path")
        if storage_path:
            await conn.execute(
                f"UPDATE {SCHEMA}.file_registry "
                f"SET entries_revoked = GREATEST(0, entries_revoked - 1), "
                f"    status = 'active', updated_at = NOW() "
                f"WHERE storage_path = $1",
                storage_path,
            )

    return {"action": "restore_huichuan", "knowledge_id": str(knowledge_id), "timestamp": _ts()}


@router.get("/abstract/{agent_id}")
async def get_abstract(agent_id: str, include_expired: bool = False,
                       caller: str | None = Depends(_resolve_caller_agent)):
    """Agent 拉取摘要清单：合并所有 active 订阅的 domains + tags，返回匹配知识的标题+摘要。

    P1 (R?): 越权 —— 此前任何人可指定任意 agent_id 拉取该 agent 的私密摘要
    （标题 + 120 字摘要）。现要求调用方身份与目标 agent 一致。
    """
    if not caller or caller != agent_id:
        raise HTTPException(403, "无权读取该 Agent 的摘要")
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 获取该 Agent 的所有 active 订阅
        subs = await conn.fetch(
            f"SELECT domains, tags FROM {SCHEMA}.subscriptions "
            f"WHERE agent_id = $1 AND active = TRUE",
            agent_id,
        )
        if not subs:
            return {"agent_id": agent_id, "abstract": [], "count": 0}

        # 合并 domains 和 tags
        all_domains: set[str] = set()
        all_tags: set[str] = set()
        for s in subs:
            for d in (s["domains"] or []):
                all_domains.add(d)
            for t in (s["tags"] or []):
                all_tags.add(t)

        conditions = [f"status = 'active'"]
        params: list = []
        idx = 1

        if not include_expired:
            conditions.append(f"(valid_until IS NULL OR valid_until >= CURRENT_DATE)")

        if all_domains:
            placeholders = ", ".join(f"${i}" for i in range(idx, idx + len(all_domains)))
            conditions.append(f"domain IN ({placeholders})")
            params.extend(all_domains)
            idx += len(all_domains)

        if all_tags:
            conditions.append(f"tags && ${idx}")
            params.append(list(all_tags))
            idx += 1

        vf_clause, vf_params = _visibility_filter(agent_id, start_index=idx)
        conditions.append(f"({vf_clause})")
        params.extend(vf_params)
        idx += len(vf_params)

        where = " AND ".join(conditions)

        max_tokens = kcfg.get_abstract_max_tokens()
        # 粗略估算：~4 chars/token，每条约 200 char
        limit = max(5, max_tokens * 4 // 200)

        params.append(limit)
        rows = await conn.fetch(
            f"SELECT knowledge_id, title, domain, tags, content, visibility, "
            f"valid_until, updated_at "
            f"FROM {SCHEMA}.knowledge_entries "
            f"WHERE {where} "
            f"ORDER BY updated_at DESC LIMIT ${idx}",
            *params,
        )

    # 即将到期提醒
    today = date.today()
    abstract = []
    for r in rows:
        item = {
            "knowledge_id": str(r["knowledge_id"]),
            "title": r["title"],
            "domain": r["domain"],
            "tags": r["tags"] or [],
            "snippet": r["content"][:120] if r["content"] else "",
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        if r["valid_until"] and isinstance(r["valid_until"], date):
            days_left = (r["valid_until"] - today).days
            if 0 <= days_left <= 7:
                item["expiring_soon"] = True
                item["days_left"] = days_left
        abstract.append(item)

    return {"agent_id": agent_id, "abstract": abstract, "count": len(abstract)}


# ── Redis 限流辅助 ────────────────────────────────────


_redis_client = None


async def _get_redis():
    """懒加载 Redis 客户端（复用 huanyu/peers.py 模式）。"""
    global _redis_client
    if _redis_client is None:
        try:
            import redis.asyncio as aioredis
            _redis_client = aioredis.from_url(kcfg.get_redis_url())
        except Exception as e:
            logger.warning("Redis unavailable, rate limiting disabled: %s", e)
            return None
    return _redis_client


async def _check_refine_rate_limit(agent_id: str) -> bool:
    """检查精炼提交限流：10 次/小时/agent（设计文档 §7.2）。

    Returns: True if allowed, False if rate limited.
    Redis 不可用时 fail-open（允许通过）。
    """
    redis = await _get_redis()
    if redis is None:
        return True  # fail-open

    key = f"huichuan:refine:ratelimit:{agent_id}"
    try:
        current = await redis.get(key)
        if current is None:
            await redis.set(key, 1, ex=3600)
            return True
        count = int(current)
        if count >= 10:
            return False
        await redis.incr(key)
        return True
    except Exception as e:
        logger.warning("Redis rate limit check failed: %s", e)
        return True  # fail-open


# ── 精炼 ──────────────────────────────────────────────


@router.post("/refine")
async def submit_refine(req: RefineSubmitRequest, agent_id: str | None = Depends(_resolve_caller_agent)):
    """提交经验到精炼队列。含 Redis 限流（10次/小时/agent）。"""
    submitter = agent_id or "anonymous"

    # Redis 限流
    if not await _check_refine_rate_limit(submitter):
        raise HTTPException(429, f"提交过于频繁，每 Agent 每小时最多 10 条。请稍后重试。")

    # 基本校验
    content = req.observation.strip()
    if len(content) < 50:
        raise HTTPException(422, f"内容过短（{len(content)} 字符），最少 50 字符")
    if len(content) > 5000:
        raise HTTPException(422, f"内容过长（{len(content)} 字符），最多 5000 字符")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 检查队列上限
        pending = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.refinement_queue WHERE status = 'pending'"
        )
        if pending >= 500:
            raise HTTPException(503, "精炼队列繁忙，请稍后重试")

        row = await conn.fetchrow(
            f"INSERT INTO {SCHEMA}.refinement_queue (submitter, domain, raw_experience) "
            f"VALUES ($1, $2, $3) RETURNING *",
            submitter, req.domain, content,
        )

    return {
        "action": "submit_refine",
        "refine_id": str(row["id"]),
        "status": row["status"],
        "timestamp": _ts(),
    }


@router.get("/refine/queue")
async def get_refine_queue(status: str | None = None, _admin: str = Depends(verify_admin_token)):
    """查看精炼队列。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                f"SELECT id, submitter, domain, confidence, status, created_at "
                f"FROM {SCHEMA}.refinement_queue WHERE status = $1 "
                f"ORDER BY created_at DESC LIMIT 200",
                status,
            )
        else:
            rows = await conn.fetch(
                f"SELECT id, submitter, domain, confidence, status, created_at "
                f"FROM {SCHEMA}.refinement_queue "
                f"ORDER BY created_at DESC LIMIT 200",
            )

    return RefineQueueResponse(
        total=len(rows),
        items=[
            RefineQueueItem(
                id=str(r["id"]),
                submitter=r["submitter"],
                domain=r.get("domain"),
                confidence=r.get("confidence", 3),
                status=r["status"],
                created_at=r["created_at"],
            )
            for r in rows
        ],
        timestamp=_ts(),
    )


@router.post("/refine/process")
async def process_refine(batch_size: int | None = None, _admin: str = Depends(verify_admin_token)):
    """手动触发精炼管道（设计文档 §7.2）。"""
    if batch_size is None:
        batch_size = kcfg.get_refine_batch_size()

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await refine_batch(conn, limit=batch_size)

    return RefineProcessResponse(
        processed=result.get("processed", 0),
        accepted=result.get("accepted", 0),
        rejected=result.get("rejected", 0),
        duration_ms=result.get("duration_ms", 0),
        timestamp=_ts(),
    )


@router.get("/metrics")
async def get_metrics(_admin: str = Depends(verify_admin_token)):
    """运行时可观测性指标（设计文档 §11.8）。Phase 2 简化版。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Storage
        total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries")

        domain_rows = await conn.fetch(
            f"SELECT domain, COUNT(*) as cnt FROM {SCHEMA}.knowledge_entries "
            f"GROUP BY domain ORDER BY cnt DESC"
        )
        by_domain = {r["domain"]: r["cnt"] for r in domain_rows}

        expired_not_cleaned = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries "
            f"WHERE valid_until < CURRENT_DATE "
            f"AND status NOT IN ('archived', 'revoked')"
        )

        # Refinement
        queue_pending = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.refinement_queue WHERE status = 'pending'"
        )

        processed_24h = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.refinement_queue "
            f"WHERE processed_at >= NOW() - INTERVAL '24 hours'"
        )

        approved_24h = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.refinement_queue "
            f"WHERE status = 'approved' AND processed_at >= NOW() - INTERVAL '24 hours'"
        )

        avg_confidence_row = await conn.fetchval(
            f"SELECT AVG(confidence) FROM {SCHEMA}.refinement_queue "
            f"WHERE processed_at >= NOW() - INTERVAL '24 hours'"
        )

        # Sync
        sync_backlog = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries "
            f"WHERE metadata->>'index_status' = 'pending_retry'"
        )

        retry_exhausted = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries "
            f"WHERE metadata->>'index_status' = 'pending_retry' "
            f"AND (metadata->>'retry_count')::int >= 3"
        )

    return MetricsResponse(
        storage=StorageMetrics(
            total_entries=total or 0,
            by_domain=by_domain,
            expired_not_cleaned=expired_not_cleaned or 0,
        ),
        refinement=RefinementMetrics(
            queue_pending=queue_pending or 0,
            processed_24h=processed_24h or 0,
            success_rate_24h=round(approved_24h / max(processed_24h, 1), 2) if processed_24h else 0.0,
            avg_confidence=round(avg_confidence_row, 1) if avg_confidence_row else 0.0,
        ),
        sync=SyncMetrics(
            yongheng_backlog=sync_backlog or 0,
            yongheng_retry_exhausted_24h=retry_exhausted or 0,
        ),
    )


# ── 统计 ──────────────────────────────────────────────


@router.get("/stats")
async def get_stats(_admin: str = Depends(verify_admin_token)):
    """汇川统计信息。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries")

        domain_rows = await conn.fetch(
            f"SELECT domain, COUNT(*) as cnt FROM {SCHEMA}.knowledge_entries "
            f"GROUP BY domain ORDER BY cnt DESC"
        )
        by_domain = {r["domain"]: r["cnt"] for r in domain_rows}

        status_rows = await conn.fetch(
            f"SELECT status, COUNT(*) as cnt FROM {SCHEMA}.knowledge_entries "
            f"GROUP BY status"
        )
        by_status = {r["status"]: r["cnt"] for r in status_rows}

        vis_rows = await conn.fetch(
            f"SELECT visibility, COUNT(*) as cnt FROM {SCHEMA}.knowledge_entries "
            f"GROUP BY visibility"
        )
        by_visibility = {r["visibility"]: r["cnt"] for r in vis_rows}

        pending = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.refinement_queue WHERE status = 'pending'"
        )

    return StatsResponse(
        total_entries=total,
        by_domain=by_domain,
        by_status=by_status,
        by_visibility=by_visibility,
        pending_refinement=pending or 0,
        timestamp=_ts(),
    )


# ── 版本历史 ──────────────────────────────────────────


@router.get("/{knowledge_id}/versions")
async def list_versions(knowledge_id: UUID):
    """列出知识的所有版本（不含 content）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            f"SELECT 1 FROM {SCHEMA}.knowledge_entries WHERE knowledge_id = $1",
            knowledge_id,
        )
        if not exists:
            raise KnowledgeNotFoundError(knowledge_id)

        rows = await conn.fetch(
            f"SELECT version_id, knowledge_id, version, changed_by, created_at "
            f"FROM {SCHEMA}.knowledge_versions "
            f"WHERE knowledge_id = $1 ORDER BY version DESC",
            knowledge_id,
        )

    return VersionHistoryResponse(
        knowledge_id=knowledge_id,
        versions=[
            VersionHistoryItem(
                version_id=str(r["version_id"]),
                knowledge_id=str(r["knowledge_id"]),
                version=r["version"],
                changed_by=r.get("changed_by"),
                created_at=r["created_at"],
            )
            for r in rows
        ],
        total=len(rows),
    )


@router.get("/{knowledge_id}/versions/{version_id}")
async def get_version_detail(knowledge_id: UUID, version_id: UUID):
    """获取指定版本的 content 快照。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.knowledge_versions WHERE version_id = $1",
            version_id,
        )
        if not row:
            raise HTTPException(404, f"版本不存在: {version_id}")

    return VersionDetailResponse(
        version_id=str(row["version_id"]),
        knowledge_id=str(row["knowledge_id"]),
        version=row["version"],
        content=row["content"],
        changed_by=row.get("changed_by"),
        created_at=row["created_at"],
    )


# ═══════════════════════════════════════════════════════
# 文件图片 API（Phase 1+）
# ═══════════════════════════════════════════════════════


@router.get("/files/images")
async def list_all_file_images(
    file_id: str | None = None,
    source_type: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """列出文件图片（支持按 file_id 和 source_type 过滤）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions: list[str] = []
        params: list = []
        idx = 1

        if file_id:
            conditions.append(f"fi.file_id = ${idx}::uuid")
            params.append(file_id)
            idx += 1
        if source_type:
            conditions.append(f"fi.source_type = ${idx}")
            params.append(source_type)
            idx += 1

        where = " AND ".join(conditions) if conditions else "TRUE"
        params.extend([limit, offset])

        rows = await conn.fetch(
            f"""SELECT fi.image_id, fi.file_id, fi.source_type, fi.source_sheet,
                       fi.page_num, fi.image_index, fi.image_format,
                       fi.image_size, fi.image_sha256, fi.storage_path,
                       fi.width, fi.height, fi.context_before, fi.context_after,
                       fi.created_at,
                       fr.original_filename
                FROM {SCHEMA}.file_images fi
                LEFT JOIN {SCHEMA}.file_registry fr ON fi.file_id = fr.file_id
                WHERE {where}
                ORDER BY fi.created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
            *params,
        )

    return {
        "images": [
            {
                "image_id": str(r["image_id"]),
                "file_id": str(r["file_id"]),
                "source_type": r["source_type"],
                "source_sheet": r["source_sheet"] or "",
                "page_num": r["page_num"],
                "image_index": r["image_index"],
                "image_format": r["image_format"],
                "image_size": r["image_size"],
                "image_sha256": r["image_sha256"],
                "storage_path": r["storage_path"],
                "width": r["width"],
                "height": r["height"],
                "context_before": r["context_before"] or "",
                "context_after": r["context_after"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "original_filename": r["original_filename"] or "",
            }
            for r in rows
        ],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
    }


@router.get("/images/{image_id}")
async def get_image_info(image_id: UUID):
    """获取单张图片信息。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT fi.*, fr.original_filename
                FROM {SCHEMA}.file_images fi
                LEFT JOIN {SCHEMA}.file_registry fr ON fi.file_id = fr.file_id
                WHERE fi.image_id = $1""",
            image_id,
        )
        if not row:
            raise HTTPException(404, f"图片不存在: {image_id}")

    return {
        "image_id": str(row["image_id"]),
        "file_id": str(row["file_id"]),
        "source_type": row["source_type"],
        "source_sheet": row["source_sheet"] or "",
        "page_num": row["page_num"],
        "image_index": row["image_index"],
        "image_format": row["image_format"],
        "image_size": row["image_size"],
        "image_sha256": row["image_sha256"],
        "storage_path": row["storage_path"],
        "width": row["width"],
        "height": row["height"],
        "context_before": row["context_before"] or "",
        "context_after": row["context_after"] or "",
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "original_filename": row["original_filename"] or "",
    }


@router.get("/images/{image_id}/download")
async def download_image(image_id: UUID):
    """下载图片原始字节。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT storage_path, image_format FROM {SCHEMA}.file_images WHERE image_id = $1",
            image_id,
        )
        if not row:
            raise HTTPException(404, f"图片不存在: {image_id}")

    path = row["storage_path"]
    if not os.path.isfile(path):
        raise HTTPException(404, f"图片文件已丢失: {path}")

    data = await asyncio.to_thread(lambda: open(path, "rb").read())

    content_type_map = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
        "svg": "image/svg+xml", "tiff": "image/tiff",
    }
    fmt = row["image_format"]
    media_type = content_type_map.get(fmt, "application/octet-stream")
    return Response(content=data, media_type=media_type)


@router.post("/files/{storage_path:path}/reprocess")
async def reprocess_file(storage_path: str, _admin: str = Depends(verify_admin_token)):
    """重新处理已入库的文件（重新提取文本 + LLM 编译 + 图片提取）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        registry_row = await conn.fetchrow(
            f"SELECT original_filename, file_id "
            f"FROM {SCHEMA}.file_registry WHERE storage_path = $1",
            storage_path,
        )
        if not registry_row:
            raise HTTPException(404, f"文件不存在: {storage_path}")

        # P1-7（9-1 修复日）：路径遏制（纵深防御）—— storage_path 来自 URL，
        # 此前 registry 命中后直接 open()。校验路径必须落在存储区或回收区
        # 根内（防 registry 被污染后变成任意文件读取通道）。
        if not (_is_path_within(_FILE_STORAGE_BASE, storage_path)
                or _is_path_within(_FILE_RECYCLE_BASE, storage_path)):
            logger.error("[trace] reprocess path_escape_refused path=%r", storage_path)
            raise HTTPException(400, f"存储路径异常，拒绝处理: {storage_path}")

        if not _os.path.isfile(storage_path):
            raise HTTPException(404, f"文件已丢失: {storage_path}")

        file_bytes = await asyncio.to_thread(
            lambda: open(storage_path, "rb").read()
        )

        result = await ingest_file(
            conn,
            file_bytes,
            registry_row["original_filename"] or "unknown",
            source=f"reprocess:{registry_row.get('file_id', '')}",
            schema=SCHEMA,
        )

        # 更新 file_registry 状态
        await conn.execute(
            f"UPDATE {SCHEMA}.file_registry "
            f"SET status = 'active', updated_at = NOW() "
            f"WHERE storage_path = $1",
            storage_path,
        )

    return {
        "action": "reprocess",
        "storage_path": storage_path,
        "original_filename": registry_row["original_filename"],
        "entries": result.get("entries", 0),
        "images_registered": result.get("images_registered", 0),
        "xlsx_sheets": result.get("xlsx_sheets", 0),
        "ingested_at": result.get("ingested_at", _ts()),
    }


@router.post("/files/reprocess-future")
async def reprocess_future_files(_admin: str = Depends(verify_admin_token)):
    """重新处理所有标记为 future_processable 的 metadata_only 文件（如图片）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT storage_path, original_filename, metadata
                FROM {SCHEMA}.file_registry
                WHERE status = 'metadata_only'
                AND metadata->>'future_processable' = 'true'
                ORDER BY updated_at DESC
                LIMIT 50"""
        )

        if not rows:
            return {"action": "reprocess_future", "total": 0, "results": []}

        results = []
        for r in rows:
            try:
                if not _os.path.isfile(r["storage_path"]):
                    results.append({
                        "storage_path": r["storage_path"],
                        "status": "skipped",
                        "reason": "file_missing",
                    })
                    continue

                file_bytes = await asyncio.to_thread(
                    lambda: open(r["storage_path"], "rb").read()
                )

                result = await ingest_file(
                    conn,
                    file_bytes,
                    r["original_filename"] or "unknown",
                    source="reprocess:future",
                    schema=SCHEMA,
                )
                results.append({
                    "storage_path": r["storage_path"],
                    "status": "completed",
                    "entries": result.get("entries", 0),
                    "images_registered": result.get("images_registered", 0),
                })
            except Exception as e:
                logger.warning("Reprocess failed for %s: %s", r["storage_path"], e)
                results.append({
                    "storage_path": r["storage_path"],
                    "status": "failed",
                    "error": str(e)[:200],
                })

    return {"action": "reprocess_future", "total": len(results), "results": results}


# ═══════════════════════════════════════════════════════
# 简单上传页面（内部用，不用域名）
# ═══════════════════════════════════════════════════════

_UPLOAD_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>汇川文件中心</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--text:#1a1a2e;--sub:#6b7280;--primary:#4f46e5;--primary-h:#4338ca;--green:#10b981;--red:#ef4444;--border:#e5e7eb;--radius:8px}
*{margin:0;padding:0;box-sizing:border-box}
body{font:14px/1.6 -apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.topbar{background:var(--card);border-bottom:1px solid var(--border);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.topbar h1{font-size:18px;color:var(--primary)}
.topbar select{padding:6px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;background:#fff}
.container{max-width:760px;margin:0 auto;padding:24px}
.card{background:var(--card);border-radius:var(--radius);box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:16px}
.card h2{font-size:16px;margin-bottom:16px}
.dropzone{border:2px dashed var(--border);border-radius:12px;padding:40px;text-align:center;cursor:pointer;transition:all .2s}
.dropzone:hover,.dragover{border-color:var(--primary);background:rgba(79,70,229,.04)}
.dropzone .icon{font-size:36px;margin-bottom:8px}
.dropzone p{color:var(--sub);font-size:13px}
.progress-bar{height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin-top:12px}
.progress-bar .fill{height:100%;background:var(--primary);transition:width .3s;width:0;border-radius:3px}
.hidden{display:none!important}
.btn{padding:8px 20px;border:none;border-radius:6px;font-size:14px;cursor:pointer;font-weight:500}
.btn-primary{background:var(--primary);color:#fff}.btn-primary:hover{background:var(--primary-h)}
.file-list{list-style:none;margin-top:12px}
.file-list li{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);font-size:13px;gap:8px}
.file-list .name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.file-list .id{font-family:monospace;font-size:11px;color:var(--primary);cursor:pointer;padding:0 8px}
.file-list .actions{display:flex;gap:6px;flex-shrink:0}
.file-list .actions button{font-size:11px;padding:3px 8px;border:1px solid var(--border);border-radius:4px;cursor:pointer;background:var(--card)}
.file-list .actions button:hover{border-color:var(--primary)}
.toast{position:fixed;top:16px;right:16px;padding:10px 16px;border-radius:var(--radius);color:#fff;font-size:13px;z-index:999;box-shadow:0 4px 12px rgba(0,0,0,.15);animation:slideIn .25s}
.toast.success{background:var(--green)}.toast.error{background:var(--red)}
@keyframes slideIn{from{transform:translateX(100%);opacity:0}to{transform:translateX(0);opacity:1}}
</style>
</head>
<body>
<header class="topbar"><h1>汇川文件中心</h1>
  <div style="display:flex;gap:12px;align-items:center">
    <select id="agentSelect"><option value="">加载中...</option></select>
    <span style="font-size:12px;color:var(--sub)" id="fileCount"></span>
  </div>
</header>
<main class="container">
  <div class="card">
    <h2>上传文件</h2>
    <div class="dropzone" id="dropzone">
      <div class="icon">📁</div><p>拖拽文件到此处，或点击选择</p>
      <p style="font-size:11px;color:#999;margin-top:4px">支持所有格式，最大 500MB，可批量上传</p>
      <input type="file" id="fileInput" multiple style="display:none">
    </div>
    <div class="progress-bar hidden" id="progWrap"><div class="fill" id="progBar"></div></div>
    <p style="font-size:12px;color:var(--sub);margin-top:4px" id="progText"></p>
  </div>
  <div class="card">
    <h2>已上传的文件</h2>
    <ul class="file-list" id="fileList"><li style="color:var(--sub)">选择部门后自动加载...</li></ul>
  </div>
</main>
<script>
let AID=localStorage.getItem('huichuan_agent')||'';
async function loadAgents(){
  const sel=document.getElementById('agentSelect');
  try{
    const r=await fetch('/v1/huichuan/agents');if(!r.ok)throw Error();
    const d=await r.json();
    if(!d.agents||!d.agents.length)throw Error();
    sel.innerHTML=d.agents.map(a=>'<option value="'+a.agent_id+'">'+a.name+'</option>').join('');
  }catch(e){
    sel.innerHTML='<option value="sales">销售</option><option value="procurement">采购</option>';
  }
  if(AID){sel.value=AID;}
  else{AID=sel.value||'sales';localStorage.setItem('huichuan_agent',AID);}
  loadFiles();
}
document.getElementById('agentSelect').addEventListener('change',e=>{AID=e.target.value;localStorage.setItem('huichuan_agent',AID);loadFiles();});
loadAgents();
document.getElementById('dropzone').addEventListener('click',()=>document.getElementById('fileInput').click());
['dragover','dragleave','drop'].forEach(ev=>document.getElementById('dropzone').addEventListener(ev,e=>{e.preventDefault();if(ev!=='drop')e.target.classList[ev==='dragover'?'add':'remove']('dragover');}));
document.getElementById('dropzone').addEventListener('drop',e=>{e.target.classList.remove('dragover');handleFiles(e.dataTransfer.files);});
document.getElementById('fileInput').addEventListener('change',e=>handleFiles(e.target.files));
document.getElementById('fileList').addEventListener('click',e=>{
  const btn=e.target.closest('button[data-action]');if(!btn)return;
  const li=btn.closest('li');const id=li.dataset.fid;const name=li.dataset.filename;
  if(btn.dataset.action==='download'){window.open('/v1/huichuan/files/'+id+'/download','_blank');return}
  if(btn.dataset.action==='delete'){if(!confirm('确认删除 '+name+'？'))return;fetch('/v1/huichuan/files/'+id,{method:'DELETE'}).then(r=>{if(!r.ok)throw Error();loadFiles();toast('已删除','success');}).catch(e=>toast('删除失败','error'));return}
  if(btn.dataset.action==='copy'){(function(){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(id).then(function(){toast('已复制: '+id,'success');}).catch(fallbackCopy);
  }else{fallbackCopy();}
  function fallbackCopy(){
    var ta=document.createElement('textarea');
    ta.value=id;ta.style.position='fixed';ta.style.left='-9999px';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');toast('已复制: '+id,'success');}catch(e){toast('复制失败','error');}
    document.body.removeChild(ta);
  }
})();}
});

async function handleFiles(files){for(const f of files)await upload(f);loadFiles();}
async function upload(file){
  const bar=document.getElementById('progBar');const wrap=document.getElementById('progWrap');const txt=document.getElementById('progText');
  wrap.classList.remove('hidden');bar.style.width='0%';txt.textContent=file.name+' 上传中...';
  const fd=new FormData();fd.append('file',file);
  try{
    const r=await fetch('/v1/huichuan/files/upload/'+encodeURIComponent(AID),{method:'POST',body:fd});
    if(!r.ok)throw new Error(await r.text());
    bar.style.width='100%';txt.textContent=file.name+' 完成';
    toast(file.name+' 上传成功','success');
  }catch(e){txt.textContent=file.name+' 失败';toast(file.name+': '+e.message,'error')}
  setTimeout(()=>{bar.style.width='0';wrap.classList.add('hidden');if(!txt.textContent.includes('失败'))txt.textContent=''},2000);
}
async function loadFiles(){
  const el=document.getElementById('fileList');
  try{
    const r=await fetch('/v1/huichuan/files/search?agent_id='+encodeURIComponent(AID));
    if(!r.ok)throw Error();const d=await r.json();
    document.getElementById('fileCount').textContent=d.files.length+' 个文件';
    if(!d.files.length){el.innerHTML='<li style="color:var(--sub)">暂无文件</li>';return}
    el.innerHTML=d.files.map(f=>'<li data-fid="'+f.file_id+'" data-filename="'+escapeHtml(f.filename)+'"><span class="name">'+escapeHtml(f.filename)+' <span class="id">'+f.file_id+'</span></span><span class="actions"><span style="font-size:11px;color:var(--sub)">'+fmtSize(f.size)+'</span><a href="/v1/huichuan/files/' + f.file_id + '/download" target="_blank" style="padding:3px 8px;border:1px solid var(--border);border-radius:4px;background:var(--card);color:var(--primary);text-decoration:none;font-size:11px;cursor:pointer">下载</a><button data-action="delete">删除</button><button data-action="copy">复制 ID</button></span></li>').join('');
  }catch(e){el.innerHTML='<li style="color:var(--sub)">加载失败</li>'}
}
function escapeHtml(s){const d=document.createElement('div');d.appendChild(document.createTextNode(s));return d.innerHTML}
function toast(m,t){const e=document.createElement('div');e.className='toast '+t;e.textContent=m;document.body.appendChild(e);setTimeout(()=>e.remove(),2500)}
function fmtSize(b){return b<1024*1024?(b/1024).toFixed(1)+'KB':(b/1024/1024).toFixed(1)+'MB'}
</script>
</body>
</html>"""


@router.get("/files", response_class=HTMLResponse)
@router.get("/files/", response_class=HTMLResponse)
async def agent_files_page(request: Request):
    """简单文件上传页面（内部用，浏览器打开即可上传）。"""
    _check_file_token(request)
    return HTMLResponse(_UPLOAD_PAGE_HTML)


@router.get("/agents")
async def list_agents_for_file_center():
    """返回本地活跃 Agent 列表，供文件页面下拉框使用。

    数据源为镇岳（zhenyue）的 agents 登记表（本地安全登记，status='active' 为活跃），
    而非 huanyu.agents（跨底座通信目录，可能含其他底座的 Agent）。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT agent_id, name FROM {zcfg.get_schema_name()}.agents "
            f"WHERE status = 'active' ORDER BY name"
        )
    return {"agents": [{"agent_id": r["agent_id"], "name": r["name"]} for r in rows]}


# ═══════════════════════════════════════════════════════
# Agent 文件管理 API — 上传/下载/搜索（供 Agent 拉取文件）
# ═══════════════════════════════════════════════════════

# 存储根目录（与 ingest.py 保持一致）
_FILE_STORAGE_BASE = _os.environ.get(
    "QINGTIAN_HUICHUAN_STORAGE",
    "/opt/qingtian/huichuan/storage",
)
# 文件回收区根目录（30 天冷静期：软删文件集中存放，mirror storage 相对结构，purge 后真删）
_FILE_RECYCLE_BASE = _os.environ.get(
    "QINGTIAN_HUICHUAN_RECYCLE",
    "/opt/qingtian/huichuan/recycle",
)
# 文件访问令牌验证（HUICHUAN_FILE_TOKEN 环境变量）


def _is_path_within(base: str, path: str) -> bool:
    """路径包含性校验：path 必须在 base 目录内（防 ../ 逃逸）。P2 (R11)

    commonpath 相等即包含；不同盘符/驱动器（Windows）抛 ValueError → 拒绝。
    """
    try:
        base_abs = _os.path.abspath(base)
        path_abs = _os.path.abspath(path)
        return _os.path.commonpath([base_abs, path_abs]) == base_abs
    except ValueError:
        return False


def _check_file_token(request: Request):
    """检查文件 API 的访问令牌。

    环境变量 HUICHUAN_FILE_TOKEN 为空 = 不限制（内网默认）。
    设了之后，上传/下载/搜索都需要带 ?token=xxx 或 Authorization: Bearer xxx。
    """
    token = _os.environ.get("HUICHUAN_FILE_TOKEN", "").strip()
    if not token:
        return True
    # URL 参数 token
    if request.query_params.get("token") == token:
        return True
    # Authorization header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == token:
        return True
    raise HTTPException(403, "需要有效的 token 才能访问文件服务")


@router.post("/files/upload/{agent_id}")
async def upload_agent_file(
    request: Request,
    agent_id: str,
    file: UploadFile = File(...),
    share_with: str = Query(default="", description="共享给其他 agent（逗号分隔的 agent_id 列表）"),
):
    """上传原始文件到指定 Agent 的汇川存储（不触发 LLM 知识编译）。

    文件保存到 Agent 专属目录 Layer 1 文件系统，记录到 file_registry，
    metadata.owner_agent 标记所属 Agent。支持通过 share_with 参数共享给其他 Agent。

    支持所有格式，最大 {_MAX_UPLOAD_MB}MB。
    """
    _check_file_token(request)
    filename = file.filename or ""
    logger.info("[trace] upload entry agent=%s filename=%s share_with=%s",
                agent_id, filename, share_with)
    if not filename:
        raise HTTPException(400, "文件名不能为空")

    content = await file.read()
    if len(content) > _MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {_MAX_UPLOAD_MB}MB 上限: {len(content)} 字节")

    ext = _os.path.splitext(file.filename)[1].lower()
    today = date.today()

    # Agent 分区存储：agents/{agent_id}/YYYY/MM/
    storage_dir = _os.path.join(
        _FILE_STORAGE_BASE,
        "agents", agent_id,
        str(today.year), f"{today.month:02d}",
    )
    await asyncio.to_thread(_os.makedirs, storage_dir, exist_ok=True)

    file_id = uuid.uuid4().hex
    storage_path = _os.path.join(storage_dir, f"{file_id}{ext}")
    await asyncio.to_thread(lambda: open(storage_path, "wb").write(content))
    logger.info("[trace] upload stored agent=%s file_id=%s path=%s size=%d",
                agent_id, file_id, storage_path, len(content))

    file_sha256 = hashlib.sha256(content).hexdigest()
    file_size = len(content)

    # 处理共享列表
    authorized = []
    if share_with:
        authorized = [a.strip() for a in share_with.split(",") if a.strip()]

    # 入库 file_registry（大师 2026-08-10 幽灵文件：入库失败必须回滚已写盘文件，
    # 否则产生「有文件无记录」的孤儿/幽灵 file_id，用户拿到 download_url 却 404）
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            metadata = {
                "owner_agent": agent_id,
                "upload_source": "agent_file_api",
            }
            if authorized:
                metadata["authorized_agents"] = authorized
            await conn.execute(
                f"""INSERT INTO {SCHEMA}.file_registry
                    (file_id, storage_path, original_filename, file_sha256, file_size,
                     status, entries_total, metadata)
                    VALUES ($1::uuid, $2, $3, $4, $5, 'active', 0, $6)
                    ON CONFLICT (storage_path) DO UPDATE SET
                        original_filename = EXCLUDED.original_filename,
                        file_size = EXCLUDED.file_size,
                        updated_at = NOW()""",
                file_id,
                storage_path,
                file.filename,
                file_sha256,
                file_size,
                metadata,
            )
    except Exception:
        # 回滚：删除已写盘文件，避免孤儿文件；异常继续向上抛（调用方不会拿到幽灵 file_id）
        try:
            if _os.path.exists(storage_path):
                _os.remove(storage_path)
                logger.warning("[trace] upload registry insert fail — rollback file_id=%s path=%s",
                               file_id, storage_path)
        except OSError as _re:
            logger.warning("[trace] upload registry insert fail + rollback rm error=%s", _re)
        raise

    logger.info("Agent file uploaded: id=%s name=%s agent=%s size=%d",
                file_id, file.filename, agent_id, file_size)

    return {
        "ok": True,
        "file_id": file_id,
        "filename": file.filename,
        "size": file_size,
        "sha256": file_sha256,
        "agent_id": agent_id,
    }


@router.get("/files/{file_id}/download")
async def download_agent_file(
    request: Request,
    file_id: str,
    agent_id: str = Query(default="", description="请求的 agent 身份，用于权限校验"),
):
    """下载 Agent 上传的原始文件。

    校验调用方是否有权访问此文件（owner_agent 匹配或 authorized_agents 包含该 agent_id）。
    提供 agent_id 参数时按该身份校验；缺省 agent_id 时从请求上下文解析真实调用方
    （Bearer 镇岳 token / X-Agent-ID），无法解析则 fail-closed 拒绝（P2 R11）。
    """
    _check_file_token(request)
    logger.info("[trace] download entry file_id=%s agent_id=%s", file_id, agent_id)

    # P2 (R11): 缺省 agent_id 时不再向后兼容地跳过归属校验 —— 改为从请求上下文解析
    # 真实调用方身份；解析不到则 fail-closed 拒绝下载。
    caller = agent_id.strip()
    if not caller:
        caller = (await _resolve_caller_agent(request)) or ""
    if not caller:
        logger.warning("[trace] download auth deny file_id=%s — 无法识别调用方身份", file_id)
        raise HTTPException(403, "无法识别调用方 agent 身份，拒绝下载")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT storage_path, original_filename, file_size, metadata "
            f"FROM {SCHEMA}.file_registry WHERE file_id = $1::uuid",
            file_id,
        )
        if not row:
            logger.warning("[trace] download miss file_id=%s — DB row not found", file_id)
            raise HTTPException(404, f"文件不存在: {file_id}")

        # 归属校验（显式 agent_id 与解析出的调用方走同一逻辑）
        metadata = row.get("metadata") or {}
        owner = metadata.get("owner_agent", "") or ""
        authorized = metadata.get("authorized_agents", []) or []
        # 防御：authorized_agents 非列表时降级为空列表
        if not isinstance(authorized, list):
            authorized = []
        if owner == caller:
            logger.info("[trace] download auth pass file_id=%s agent=%s (owner match)", file_id, caller)
        elif caller in authorized:
            logger.info("[trace] download auth pass file_id=%s agent=%s (shared)", file_id, caller)
        else:
            logger.warning("[trace] download auth deny file_id=%s agent=%s owner=%s authorized=%s",
                           file_id, caller, owner, authorized)
            raise HTTPException(403, f"Agent '{caller}' 无权访问此文件")

        storage_path = row["storage_path"]
        if not _os.path.isfile(storage_path):
            logger.warning("[trace] download miss file_id=%s — file lost path=%s", file_id, storage_path)
            raise HTTPException(404, f"文件已丢失: {storage_path}")

        data = await asyncio.to_thread(lambda: open(storage_path, "rb").read())
        logger.info("[trace] download done file_id=%s agent=%s size=%d", file_id, agent_id, len(data))

    filename = row["original_filename"] or file_id
    media_type = "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(filename, safe="")},
    )


# ── 分享下载（签名链接，限时） ─────────────────────────

def _get_share_secret() -> str:
    """分享签名密钥：优先专属密钥，缺省复用文件 API token。

    两者都未配置 → 返回空串，分享功能禁用（不启用任何默认密钥，防伪造）。
    """
    return (_os.environ.get("HUICHUAN_SHARE_SECRET", "").strip()
            or _os.environ.get("HUICHUAN_FILE_TOKEN", "").strip())


def _share_token(file_id: str, expires_ts: int) -> str:
    """生成签名 token：`<file_id>.<expires_ts>.<hmac>`，URL 安全。

    未配置分享密钥时抛 503（分享功能禁用）。
    """
    import hashlib
    import hmac
    secret = _get_share_secret()
    if not secret:
        raise HTTPException(
            503,
            "分享功能未启用：未配置 HUICHUAN_SHARE_SECRET 或 HUICHUAN_FILE_TOKEN",
        )
    payload = f"{file_id}.{expires_ts}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _parse_share_token(token: str) -> str | None:
    """校验签名 + 有效期，返回 file_id；非法/过期/未启用 → None。"""
    import hashlib
    import hmac
    import time
    secret = _get_share_secret()
    if not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    file_id, expires_ts, sig = parts
    try:
        expires = int(expires_ts)
    except ValueError:
        return None
    if expires < int(time.time()):
        return None
    payload = f"{file_id}.{expires_ts}"
    expect = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(expect, sig):
        return None
    return file_id


def _public_base() -> str:
    """对外下载链接的基础地址（生产需配 QINGTIAN_PUBLIC_URL）。"""
    return _os.environ.get("QINGTIAN_PUBLIC_URL", "").strip().rstrip("/")


@router.post("/files/{file_id}/share")
async def create_share_link(
    request: Request,
    file_id: str,
    ttl_minutes: int = Query(default=1440, ge=5, le=43200),
):
    """为 Agent 文件生成限时签名下载链接（默认 24h）。

    返回 {token, url, expires_at}。url 由 QINGTIAN_PUBLIC_URL 拼出；
    未配置时返回相对路径，由调用方决定如何对外暴露。
    """
    _check_file_token(request)
    import time
    logger.info("[trace] share create file_id=%s ttl=%dmin", file_id, ttl_minutes)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT original_filename, metadata FROM {SCHEMA}.file_registry "
            f"WHERE file_id = $1::uuid",
            file_id,
        )
        if not row:
            logger.warning("[trace] share miss file_id=%s — DB row not found", file_id)
            raise HTTPException(404, f"文件不存在: {file_id}")

        # P1-6（9-1 修复日）：属主校验 —— 分享=数据外泄通道，此前任何持
        # FILE_TOKEN 者可为任意 Agent 文件签发限时外链。口径同 delete：
        # fail-closed 解析调用方，非 owner 且非 authorized → 403。
        caller = await _resolve_caller_agent(request)
        if not caller:
            logger.warning("[trace] share auth fail_closed file_id=%s — 调用方身份不可解析", file_id)
            raise HTTPException(403, "无法解析调用方身份，拒绝生成分享链接（fail-closed）")
        meta = _safe_jsonb(row.get("metadata"))
        owner = (meta or {}).get("owner_agent", "")
        authorized = (meta or {}).get("authorized_agents") or []
        if caller != owner and caller not in authorized:
            logger.warning("[trace] share auth deny file_id=%s caller=%s owner=%s",
                           file_id, caller, owner)
            raise HTTPException(403, f"无权分享该文件（owner={owner}）")

    expires = int(time.time()) + ttl_minutes * 60
    token = _share_token(file_id, expires)
    public_base = _public_base()
    rel = f"/v1/huichuan/files/s/{token}"
    return {
        "ok": True,
        "token": token,
        "url": f"{public_base}{rel}" if public_base else rel,
        "expires_at": datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
        "filename": row["original_filename"],
    }


@router.get("/files/s/{token}")
async def download_share_link(token: str):
    """签名链接下载（限时，凭 token 即下载，不额外鉴权）。"""
    file_id = _parse_share_token(token)
    if not file_id:
        raise HTTPException(410, "下载链接无效或已过期")

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT storage_path, original_filename FROM {SCHEMA}.file_registry "
            f"WHERE file_id = $1::uuid",
            file_id,
        )
        if not row or not row["storage_path"] or not _os.path.isfile(row["storage_path"]):
            raise HTTPException(404, f"文件不存在: {file_id}")

        data = await asyncio.to_thread(lambda: open(row["storage_path"], "rb").read())

    filename = row["original_filename"] or file_id
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(filename, safe="")},
    )


@router.delete("/files/{file_id}")
async def delete_agent_file(request: Request, file_id: str):
    """删除 Agent 上传的文件（30 天冷静期软删）。

    不再物理删 + DELETE 记录：文件移入集中回收区（mirror storage 相对结构），
    file_registry 置 status='deleted' + purge_at=+30 天，由 huichuan cron
    purge_expired_files 到期真删；平台侧 restore 端点可在冷静期内恢复误删。
    门户列表按 status='active' 过滤，软删后自动不可见。
    """
    _check_file_token(request)
    logger.info("[trace] delete entry file_id=%s", file_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT storage_path, status, metadata FROM {SCHEMA}.file_registry "
            f"WHERE file_id = $1::uuid",
            file_id,
        )
        if not row:
            logger.warning("[trace] delete miss file_id=%s — DB row not found", file_id)
            raise HTTPException(404, f"文件不存在: {file_id}")

        if row["status"] == "deleted":
            logger.info("[trace] delete already_deleted file_id=%s — 已在回收区", file_id)
            return {"ok": True, "file_id": file_id, "deleted": True}

        # P1-6（9-1 修复日）：属主校验 —— 此前仅凭 FILE_TOKEN（内网未配 token
        # 时任何人）即可软删任意 Agent 文件。口径对齐 download（P2 R11）：
        # 从请求上下文解析调用方（网关注入 Bearer / loopback X-Agent-ID），
        # 解析不到 fail-closed 拒绝；非 owner 且非 authorized → 403。
        caller = await _resolve_caller_agent(request)
        if not caller:
            logger.warning("[trace] delete auth fail_closed file_id=%s — 调用方身份不可解析", file_id)
            raise HTTPException(403, "无法解析调用方身份，拒绝删除（fail-closed）")
        meta = _safe_jsonb(row.get("metadata"))
        owner = (meta or {}).get("owner_agent", "")
        authorized = (meta or {}).get("authorized_agents") or []
        if caller != owner and caller not in authorized:
            logger.warning("[trace] delete auth deny file_id=%s caller=%s owner=%s",
                           file_id, caller, owner)
            raise HTTPException(403, f"无权删除该文件（owner={owner}）")

        storage_path = row["storage_path"]

        # P2 (R11): 防路径逃逸 —— commonpath 包含性校验 + 拒绝 `..` 相对片段。
        # 否则 relpath() 对 storage 根之外的路径会产出 `..` 序列，把回收目标拼出回收区。
        if not storage_path or not _is_path_within(_FILE_STORAGE_BASE, storage_path):
            logger.error("[trace] delete path_escape_refused file_id=%s path=%r",
                         file_id, storage_path)
            raise HTTPException(400, f"文件存储路径异常，拒绝删除: {storage_path}")
        rel = _os.path.relpath(storage_path, _FILE_STORAGE_BASE)
        if rel == ".." or rel.startswith(".." + _os.sep):
            logger.error("[trace] delete relpath_escape_refused file_id=%s rel=%r", file_id, rel)
            raise HTTPException(400, f"文件存储路径异常，拒绝删除: {storage_path}")

        # 回收区 mirror storage 相对结构：recycle/<storage 相对路径>，恢复时前缀替换即可还原
        recycle_path = _os.path.join(_FILE_RECYCLE_BASE, rel)
        await asyncio.to_thread(_os.makedirs, _os.path.dirname(recycle_path), exist_ok=True)
        if storage_path and _os.path.isfile(storage_path):
            await asyncio.to_thread(shutil.move, storage_path, recycle_path)
        else:
            logger.warning("[trace] delete storage_missing file_id=%s path=%r — 物理文件缺失，仅软删记录",
                           file_id, storage_path)

        await conn.execute(
            f"UPDATE {SCHEMA}.file_registry "
            f"SET status='deleted', deleted_at=NOW(), "
            f"purge_at=NOW() + INTERVAL '30 days', storage_path=$2, updated_at=NOW() "
            f"WHERE file_id=$1::uuid",
            file_id, recycle_path,
        )

    logger.info("[trace] delete soft-done file_id=%s → recycle=%s", file_id, recycle_path)
    return {"ok": True, "file_id": file_id, "deleted": True}


@router.post("/files/{file_id}/restore")
async def restore_agent_file(request: Request, file_id: str, _admin: str = Depends(verify_admin_token)):
    """平台侧恢复误删文件（30 天冷静期内）。

    仅平台/运维可调用（X-Admin-Token + ZHENYUE_ADMIN_TOKEN，见 verify_admin_token）；
    门户用户无此权限、无回收站入口。文件从回收区移回原 storage 路径，status 回 active。
    """
    logger.info("[trace] restore entry file_id=%s", file_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT storage_path FROM {SCHEMA}.file_registry "
            f"WHERE file_id = $1::uuid AND status='deleted'",
            file_id,
        )
        if not row:
            logger.warning("[trace] restore miss file_id=%s — 不在回收区", file_id)
            raise HTTPException(404, f"回收区不存在该文件: {file_id}")

        recycle_path = row["storage_path"]
        if recycle_path and recycle_path.startswith(_FILE_RECYCLE_BASE):
            original_path = recycle_path.replace(_FILE_RECYCLE_BASE, _FILE_STORAGE_BASE)
        else:
            # 异常路径（不在回收区下）：不动文件，仅置 active（防路径注入）
            original_path = recycle_path
            logger.warning("[trace] restore path_not_in_recycle file_id=%s path=%r", file_id, recycle_path)

        await asyncio.to_thread(_os.makedirs, _os.path.dirname(original_path), exist_ok=True)
        if recycle_path and _os.path.isfile(recycle_path):
            await asyncio.to_thread(shutil.move, recycle_path, original_path)
        else:
            logger.warning("[trace] restore file_missing file_id=%s recycle_path=%r", file_id, recycle_path)

        await conn.execute(
            f"UPDATE {SCHEMA}.file_registry "
            f"SET status='active', deleted_at=NULL, purge_at=NULL, storage_path=$2, updated_at=NOW() "
            f"WHERE file_id=$1::uuid",
            file_id, original_path,
        )

    logger.info("[trace] restore done file_id=%s → %s", file_id, original_path)
    return {"ok": True, "file_id": file_id, "restored": True}


@router.get("/files/search")
async def search_agent_files(
    request: Request,
    q: str = Query(default=""),
    agent_id: str = Query(default=""),
    limit: int = Query(default=20, le=100),
):
    """搜索 Agent 上传的文件（按文件名模糊匹配 + Agent 过滤）。"""
    _check_file_token(request)
    logger.info("[trace] search entry q=%s agent=%s limit=%d", q, agent_id, limit)
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions: list[str] = ["status = 'active'"]
        params: list = []
        idx = 1

        # Agent 隔离：owner_agent 匹配 或 authorized_agents 包含该 agent
        if agent_id:
            conditions.append(
                f"(metadata->>'owner_agent' = ${idx} "
                f"OR metadata->'authorized_agents' ? ${idx})"
            )
            params.append(agent_id)
            idx += 1

        if q:
            conditions.append(f"original_filename ILIKE ${idx}")
            params.append(f"%{q}%")
            idx += 1

        where = " AND ".join(conditions)
        params.append(limit)

        rows = await conn.fetch(
            f"""SELECT file_id, storage_path, original_filename, file_size,
                       file_sha256, metadata, created_at
                FROM {SCHEMA}.file_registry
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${idx}""",
            *params,
        )

    logger.info("[trace] search done q=%s agent=%s found=%d", q, agent_id, len(rows))

    return {
        "files": [
            {
                "file_id": str(r["file_id"]),
                "filename": r["original_filename"] or "",
                "size": r["file_size"],
                "sha256": r["file_sha256"],
                "agent_id": (r["metadata"] or {}).get("owner_agent", ""),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


# ── 单条知识 CRUD（/{knowledge_id}）— 定义在所有具名路由之后，避免 UUID 路径参数拦截 /files 等具名路由 ──


@router.get("/{knowledge_id}")
async def get_knowledge(knowledge_id: UUID, agent_id: str | None = Depends(_resolve_caller_agent)):
    """获取单条知识全文，含可见性校验。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        vf_clause, vf_params = _visibility_filter(agent_id, start_index=2)
        row = await conn.fetchrow(
            f"SELECT knowledge_id, title, domain, tags, visibility, owner_agent, "
            f"authorized_agents, content, source, version, valid_from, valid_until, "
            f"metadata, entry_type, original_filename, original_storage_path, "
            f"original_file_sha256, quality, status, refined_at, created_at, updated_at "
            f"FROM {SCHEMA}.knowledge_entries "
            f"WHERE knowledge_id = $1 AND ({vf_clause})",
            knowledge_id, *vf_params,
        )
        if not row:
            exists = await conn.fetchval(
                f"SELECT 1 FROM {SCHEMA}.knowledge_entries WHERE knowledge_id = $1",
                knowledge_id,
            )
            if exists:
                raise VisibilityForbiddenError(knowledge_id)
            raise KnowledgeNotFoundError(knowledge_id)

    return _row_to_response(dict(row))


@router.put("/{knowledge_id}")
async def update_knowledge(knowledge_id: UUID, req: KnowledgeUpdate,
                           agent_id: str | None = Depends(_resolve_caller_agent)):
    """更新知识。乐观锁：请求必须带 version 字段，版本不匹配返回 409。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 需全字段：updates 为空时直接返回 current，乐观锁需 version
        current = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.knowledge_entries WHERE knowledge_id = $1",
            knowledge_id,
        )
        if not current:
            raise KnowledgeNotFoundError(knowledge_id)

        if req.version != current["version"]:
            raise VersionConflictError(current["version"])

        updates = req.model_dump(exclude_none=True)
        updates.pop("version", None)

        if not updates:
            return _row_to_response(dict(current))

        # 列名白名单：防止非预期字段注入 SQL
        _ALLOWED_UPDATE_COLUMNS = frozenset({
            "title", "domain", "content", "tags", "visibility",
            "owner_agent", "authorized_agents", "source",
            "valid_from", "valid_until", "metadata",
            "entry_type", "original_filename", "original_storage_path",
            "original_file_sha256", "quality", "status",
        })
        for key in updates:
            if key not in _ALLOWED_UPDATE_COLUMNS:
                raise HTTPException(422, f"不允许更新的字段: {key}")

        new_version = current["version"] + 1
        set_clauses = []
        params = []
        idx = 1
        for key, value in updates.items():
            set_clauses.append(f"{key} = ${idx}")
            params.append(value)
            idx += 1
        set_clauses.append(f"version = ${idx}")
        params.append(new_version)
        idx += 1
        set_clauses.append(f"updated_at = NOW()")

        old_version = current["version"]
        params.append(knowledge_id)
        where = f"knowledge_id = ${idx} AND version = ${idx + 1}"
        params.append(old_version)

        row = await conn.fetchrow(
            f"UPDATE {SCHEMA}.knowledge_entries "
            f"SET {', '.join(set_clauses)} "
            f"WHERE {where} RETURNING *",
            *params,
        )
        if not row:
            raise VersionConflictError(old_version)

        await conn.execute(
            f"INSERT INTO {SCHEMA}.knowledge_versions (knowledge_id, version, content, changed_by) "
            f"VALUES ($1, $2, $3, $4)",
            knowledge_id, new_version, row["content"], agent_id,
        )
        if "content" in updates or "title" in updates:
            await _sync_to_yongheng(
                conn, str(knowledge_id), row["title"], row["content"],
                row["domain"], row["tags"], row["visibility"],
            )

    return _row_to_response(dict(row))


@router.delete("/{knowledge_id}")
async def delete_knowledge(knowledge_id: UUID,
                           agent_id: str | None = Depends(_resolve_caller_agent)):
    """软删除知识：status → revoked，30 天后物理删除。

    权限：私有条目仅 owner 可删；admin 可删任意条目（镇岳拦截）。
    关联处理：
      - 断开所有 knowledge_links（此条目作为 source 或 target 的链接失效）
      - 从永恒索引移除
      - 记录 revoked_at / revoked_by 到 metadata，供 cron 30 天后物理删除
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"SELECT visibility, owner_agent, metadata, original_storage_path "
                f"FROM {SCHEMA}.knowledge_entries "
                f"WHERE knowledge_id = $1 FOR UPDATE",
                knowledge_id,
            )
            if not row:
                raise KnowledgeNotFoundError(str(knowledge_id))

            if row["visibility"] == "private":
                if not agent_id or row["owner_agent"] != agent_id:
                    raise VisibilityForbiddenError(str(knowledge_id))

            revoked_at_iso = _ts()
            meta = _safe_jsonb(row.get("metadata"))
            meta["revoked_at"] = revoked_at_iso
            meta["revoked_by"] = agent_id or "unknown"

            await conn.execute(
                f"UPDATE {SCHEMA}.knowledge_entries "
                f"SET status = 'revoked', metadata = $1::jsonb, updated_at = NOW() "
                f"WHERE knowledge_id = $2",
                json.dumps(meta, ensure_ascii=False),
                knowledge_id,
            )

            await conn.execute(
                f"DELETE FROM {SCHEMA}.knowledge_links "
                f"WHERE source_id = $1 OR target_id = $1",
                knowledge_id,
            )

            storage_path = row.get("original_storage_path")
            if storage_path:
                await conn.execute(
                    f"UPDATE {SCHEMA}.file_registry "
                    f"SET entries_revoked = entries_revoked + 1, "
                    f"    updated_at = NOW() "
                    f"WHERE storage_path = $1",
                    storage_path,
                )
                active_count = await conn.fetchval(
                    f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries "
                    f"WHERE original_storage_path = $1 AND status != 'revoked'",
                    storage_path,
                )
                if active_count == 0:
                    await conn.execute(
                        f"UPDATE {SCHEMA}.file_registry "
                        f"SET status = 'revoked', updated_at = NOW() "
                        f"WHERE storage_path = $1",
                        storage_path,
                    )

        await _delete_from_yongheng(conn, str(knowledge_id))

    return {"action": "revoke_huichuan", "knowledge_id": str(knowledge_id),
            "status": "revoked", "revoked_at": revoked_at_iso,
            "retain_until": (datetime.now(timezone.utc) +
                             timedelta(days=30)).isoformat(),
            "timestamp": _ts()}
