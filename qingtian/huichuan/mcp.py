"""汇川 MCP Server — Phase 7

FastMCP 服务，将 16 个汇川工具暴露为 MCP tools。
复用 FastAPI 端口 1996，通过 main.py 挂载。

Agent 通过 MCP 协议调用：
  - SSE:  GET  /mcp/sse
  - HTTP: POST /mcp/messages/

工具对应关系：
  search_entities/search_concepts/search_comparisons → search.py
  get_entity/get_concept/get_knowledge/get_index        → api.py
  ingest_text/ingest_file                                → ingest.py
  list_links/get_graph_neighborhood                     → knowledge_links SQL
  lint_report/auto_fix                                   → lint.py
  subscribe/unsubscribe                                  → 订阅 API
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from uuid import UUID

from common.db import get_pool
from huichuan.config import get_storage_base
from huichuan.database import SCHEMA
from huichuan.ingest import ingest_file as _huichuan_ingest_file
from huichuan.ingest import ingest_text as _huichuan_ingest_text
from huichuan.lint import auto_fix as _huichuan_auto_fix
from huichuan.lint import lint_report as _huichuan_lint_report
from huichuan.search import search_with_visibility, _visibility_filter

logger = logging.getLogger("huichuan.mcp")

# ── FastMCP 实例 ─────────────────────────────────────────

try:
    from fastmcp import FastMCP

    try:
        from fastmcp import Context  # noqa: F401 工具 ctx 注入用
    except ImportError:
        Context = None

    mcp = FastMCP(
        name="汇川知识引擎",
        description="ACSSA 智能体操作系统汇川知识管理中间件 — 企业级知识搜索/摄入/图谱/巡检",
        version="2.4",
    )
except ImportError:
    logger.warning("fastmcp not installed — MCP server unavailable. pip install fastmcp")
    mcp = None
    Context = None


# ═══════════════════════════════════════════════════════
# 工具实现
# ═══════════════════════════════════════════════════════

def _assert_storage_path(file_path: str) -> str | None:
    """校验 file_path 位于汇川存储根目录内，返回规范化路径；不在则返回 None。

    P1 (R?): ingest_file 的任意文件读防线 —— 只允许摄入存储目录内的文件。
    """
    storage_base = os.path.realpath(get_storage_base())
    real_path = os.path.realpath(file_path)
    if not real_path.startswith(storage_base + os.sep):
        return None
    return real_path


def _require_write_caller(ctx) -> tuple[bool, str]:
    """写类/巡检类工具身份门（9-1 修复日 P0，汇川 review 2026-08-28）。

    /mcp/ 此前在网关公开白名单且写类工具（ingest/auto_fix/subscribe/unsubscribe）
    零鉴权 + ingest 硬编码 visibility='enterprise' → 未认证企业级知识投毒原语。
    修复：写语义与全库巡检（枚举标题含 private）一律要求网关注入的可信身份
    （req.state.agent_id，Bearer 认证得出）；拿不到 → 拒绝（fail-closed）。
    读侧检索维持 public-only 降级（_resolve_mcp_caller 原语义）。
    """
    caller = _resolve_mcp_caller(ctx)
    if not caller:
        return False, ("未认证调用方：写类/巡检类 MCP 工具需携带有效 Bearer token"
                       "（经网关注入身份），请配置 MCP host 凭据后重试")
    return True, caller


def _resolve_mcp_caller(ctx) -> str | None:
    """从 MCP 传输层解析调用方 agent_id。

    FastMCP HTTP 传输下 ctx.request 为 starlette Request，网关中间件注入
    scope["state"]["agent_id"]（由 Bearer token 认证得出，不可客户端伪造）。
    拿不到（stdio/SSE、未认证、或 fastmcp 版本差异）→ None，调用方按
    public-only 处理（fail-closed：宁可检索不到 private，也不跨 agent 泄漏）。
    """
    try:
        if ctx is None:
            return None
        req = getattr(ctx, "request", None)
        if req is None:
            return None
        return getattr(req.state, "agent_id", "") or ""
    except Exception:
        return None


if mcp is not None:

    # ── 搜索类 ──────────────────────────────────────

    @mcp.tool()
    async def search_entities(query: str, domain: str = "", limit: int = 10, ctx: Context = None) -> dict:
        """搜索实体类型知识（供应商、产品、标准代号等）"""
        return await _search(entry_type="entity", query=query, domain=domain, limit=limit, ctx=ctx)

    @mcp.tool()
    async def search_concepts(query: str, domain: str = "", limit: int = 10, ctx: Context = None) -> dict:
        """搜索概念类型知识（绝缘等级、温升限值等）"""
        return await _search(entry_type="concept", query=query, domain=domain, limit=limit, ctx=ctx)

    @mcp.tool()
    async def search_comparisons(query: str, domain: str = "", limit: int = 10, ctx: Context = None) -> dict:
        """搜索对比类型知识（供应商报价对比等）"""
        return await _search(entry_type="comparison", query=query, domain=domain, limit=limit, ctx=ctx)

    @mcp.tool()
    async def get_index(domain: str = "") -> dict:
        """获取知识库索引概览"""
        return await _get_stats(domain=domain)

    # ── CRUD 类 ─────────────────────────────────────

    @mcp.tool()
    async def get_entity(knowledge_id: str, ctx: Context = None) -> dict:
        """获取单条知识全文（含可见性校验，跨 agent 不可读）"""
        return await _get_knowledge(knowledge_id, ctx=ctx)

    @mcp.tool()
    async def get_concept(knowledge_id: str, ctx: Context = None) -> dict:
        """获取概念类型知识全文（含可见性校验）"""
        return await _get_knowledge(knowledge_id, ctx=ctx)

    @mcp.tool()
    async def get_knowledge(knowledge_id: str, ctx: Context = None) -> dict:
        """获取单条知识全文（通用，含可见性校验）"""
        return await _get_knowledge(knowledge_id, ctx=ctx)

    # ── 摄入类 ─────────────────────────────────────

    @mcp.tool()
    async def ingest_text(text: str, source: str = "mcp", filename: str = "",
                          ctx: Context = None) -> dict:
        """文本摄入 — LLM 编译入库（需认证调用方）"""
        allowed, who = _require_write_caller(ctx)
        if not allowed:
            return {"error": who, "entries": 0}
        return await _ingest_text(text, source=source, filename=filename, caller=who)

    @mcp.tool()
    async def ingest_file(file_path: str, source: str = "mcp", ctx: Context = None) -> dict:
        """文件摄入 — 上传文件 → 解析 → LLM 编译入库（需认证调用方）。

        仅允许摄入汇川存储目录内的文件 —— 此前可读取服务器任意路径
        （/etc/passwd、config.yaml、私钥等），构成任意文件读原语。
        """
        allowed, who = _require_write_caller(ctx)
        if not allowed:
            return {"error": who, "entries": 0}
        real_path = _assert_storage_path(file_path)
        if real_path is None:
            return {"error": f"file outside allowed storage dir: {file_path}"}
        if not os.path.isfile(real_path):
            return {"error": f"file not found: {file_path}"}

        file_bytes = await asyncio.to_thread(lambda: open(real_path, "rb").read())
        filename = os.path.basename(real_path)

        pool = await get_pool()
        async with pool.acquire() as conn:
            return await _huichuan_ingest_file(conn, file_bytes, filename, source=source)

    @mcp.tool()
    async def ingest_huichuan_file(file_id: str, source: str = "mcp",
                                   ctx: Context = None) -> dict:
        """汇川文件摄入 — 按 file_id 从汇川存储导入（需认证调用方）。"""
        allowed, who = _require_write_caller(ctx)
        if not allowed:
            return {"error": who, "entries": 0}
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT storage_path, original_filename FROM {SCHEMA}.file_registry "
                f"WHERE file_id = $1::uuid AND status = 'active'",
                file_id,
            )
            if not row:
                return {"error": f"file not found in registry: {file_id}"}

            storage_path = row["storage_path"]
            if not os.path.isfile(storage_path):
                return {"error": f"file not found on disk: {storage_path}"}

            file_bytes = await asyncio.to_thread(
                lambda: open(storage_path, "rb").read()
            )
            filename = row["original_filename"] or os.path.basename(storage_path)

            return await _huichuan_ingest_file(conn, file_bytes, filename, source=source)

    # ── 图谱类 ─────────────────────────────────────

    @mcp.tool()
    async def list_links(knowledge_id: str) -> dict:
        """列出某知识的所有关联"""
        return await _get_links(knowledge_id)

    @mcp.tool()
    async def get_graph_neighborhood(knowledge_id: str, max_hops: int = 2) -> dict:
        """获取知识图谱 2-跳邻域"""
        return await _get_neighborhood(knowledge_id, max_hops)

    # ── 巡检类 ─────────────────────────────────────

    @mcp.tool()
    async def lint_report(ctx: Context = None) -> dict:
        """运行知识库巡检并返回报告（需认证调用方——枚举全库标题含 private）"""
        allowed, who = _require_write_caller(ctx)
        if not allowed:
            return {"error": who}
        return await _lint_report()

    @mcp.tool()
    async def auto_fix(categories: list[str] | None = None, ctx: Context = None) -> dict:
        """自动修复可自动处理的知识库问题（需认证调用方——批量删改）"""
        allowed, who = _require_write_caller(ctx)
        if not allowed:
            return {"error": who}
        return await _auto_fix(categories)

    # ── 订阅类 ─────────────────────────────────────

    @mcp.tool()
    async def subscribe(agent_id: str, domains: list[str] | None = None,
                        tags: list[str] | None = None, ctx: Context = None) -> dict:
        """订阅知识领域更新（需认证调用方，且只能为自己订阅）"""
        allowed, who = _require_write_caller(ctx)
        if not allowed:
            return {"error": who}
        if agent_id != who:
            return {"error": f"只能为调用方本人订阅（caller={who}），拒绝为 {agent_id} 操作"}
        return await _subscribe(agent_id, domains or [], tags or [])

    @mcp.tool()
    async def unsubscribe(subscription_id: str, ctx: Context = None) -> dict:
        """取消订阅（需认证调用方）"""
        allowed, who = _require_write_caller(ctx)
        if not allowed:
            return {"error": who}
        return await _unsubscribe(subscription_id, caller=who)


# ═══════════════════════════════════════════════════════
# 底层实现（复用现有模块）
# ═══════════════════════════════════════════════════════


async def _search(entry_type: str, query: str, domain: str, limit: int, ctx=None) -> dict:
    # P1 (R?): 检索叠加可见性过滤 —— 原实现 search_knowledge 无过滤，
    # MCP 调用方可跨 agent 读到全部（含 private）知识。
    agent_id = _resolve_mcp_caller(ctx)
    pool = await get_pool()
    async with pool.acquire() as conn:
        results, _total = await search_with_visibility(
            conn, query, agent_id=agent_id, domain=domain, limit=limit,
        )
    return {"results": results, "count": len(results), "query": query}


async def _get_knowledge(knowledge_id: str, ctx=None) -> dict:
    # P1 (R?): 读全文叠加可见性校验 —— 原实现按 ID 直接返回全文，
    # 任意调用方持 knowledge_id 即可跨 agent 读 private 全文。
    agent_id = _resolve_mcp_caller(ctx)
    pool = await get_pool()
    async with pool.acquire() as conn:
        vf_clause, vf_params = _visibility_filter(agent_id, start_index=2)
        row = await conn.fetchrow(
            f"SELECT knowledge_id, title, domain, tags, content, entry_type, "
            f"quality, status, created_at FROM {SCHEMA}.knowledge_entries "
            f"WHERE knowledge_id = $1 AND ({vf_clause})",
            UUID(knowledge_id), *vf_params,
        )
        if not row:
            return {"error": "not found or no access", "knowledge_id": knowledge_id}
        return {
            "knowledge_id": str(row["knowledge_id"]),
            "title": row["title"],
            "domain": row["domain"],
            "tags": row["tags"] or [],
            "content": row["content"],
            "entry_type": row.get("entry_type", "entity"),
            "quality": row.get("quality", 3),
            "status": row["status"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }


async def _get_stats(domain: str = "") -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if domain:
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries WHERE domain = $1", domain
            )
        else:
            total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.knowledge_entries")

        domains = await conn.fetch(
            f"SELECT domain, COUNT(*) as cnt FROM {SCHEMA}.knowledge_entries "
            f"GROUP BY domain ORDER BY cnt DESC"
        )
    return {
        "total_entries": total,
        "by_domain": {r["domain"]: r["cnt"] for r in domains},
    }


async def _ingest_text(text: str, source: str = "mcp", filename: str = "",
                      caller: str = "") -> dict:
    # 9-1：调用方身份随 source 落库溯源（此前匿名写入不可审计）
    if caller:
        source = f"{source}:{caller}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _huichuan_ingest_text(conn, text, source=source, original_filename=filename)


async def _get_links(knowledge_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT l.link_id, l.source_id, l.target_id, l.link_type, l.confidence "
            f"FROM {SCHEMA}.knowledge_links l "
            f"WHERE l.source_id = $1 OR l.target_id = $1 "
            f"ORDER BY l.created_at DESC LIMIT 100",
            UUID(knowledge_id),
        )
    return {
        "knowledge_id": knowledge_id,
        "links": [
            {
                "source_id": str(r["source_id"]),
                "target_id": str(r["target_id"]),
                "link_type": r["link_type"],
                "confidence": r["confidence"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


async def _get_neighborhood(knowledge_id: str, max_hops: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if max_hops == 1:
            rows = await conn.fetch(
                f"SELECT DISTINCT k.knowledge_id, k.title, k.domain, k.entry_type "
                f"FROM {SCHEMA}.knowledge_links l "
                f"JOIN {SCHEMA}.knowledge_entries k ON k.knowledge_id = l.target_id "
                f"WHERE l.source_id = $1 LIMIT 100",
                UUID(knowledge_id),
            )
        else:
            rows = await conn.fetch(
                f"""WITH one_hop AS (
                      SELECT target_id FROM {SCHEMA}.knowledge_links WHERE source_id = $1
                    ), two_hop AS (
                      SELECT kl2.target_id FROM {SCHEMA}.knowledge_links kl1
                      JOIN {SCHEMA}.knowledge_links kl2 ON kl1.target_id = kl2.source_id
                      WHERE kl1.source_id = $1
                    )
                    SELECT DISTINCT k.knowledge_id, k.title, k.domain, k.entry_type
                    FROM {SCHEMA}.knowledge_entries k
                    WHERE k.knowledge_id IN (TABLE one_hop)
                       OR k.knowledge_id IN (TABLE two_hop)
                    LIMIT 200""",
                UUID(knowledge_id),
            )
    return {
        "knowledge_id": knowledge_id,
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


async def _lint_report() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _huichuan_lint_report(conn)


async def _auto_fix(categories: list[str] | None = None) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _huichuan_auto_fix(conn, categories=categories)


async def _subscribe(agent_id: str, domains: list[str], tags: list[str]) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                f"INSERT INTO {SCHEMA}.subscriptions (agent_id, subscription_name, domains, tags) "
                f"VALUES ($1, $2, $3, $4) RETURNING *",
                agent_id, f"mcp_{agent_id}", domains, tags,
            )
            return {
                "subscription_id": str(row["subscription_id"]),
                "agent_id": row["agent_id"],
                "status": "created",
            }
        except Exception as e:
            return {"error": str(e)}


# ── 向后兼容：MCP_TOOLS 工具定义列表 ──────────────────────
# test_connector_lint.py 等引用此列表验证工具完整性

MCP_TOOLS: list[dict] = []  # 仅定义性存在,实际注册见上方 @mcp.tool()

if mcp is not None:
    MCP_TOOLS = [
        {"name": "search_entities",          "description": "搜索实体类型知识",         "parameters": {"query": "string", "domain": "string", "limit": "int"},   "handler": "huichuan.search"},
        {"name": "search_concepts",          "description": "搜索概念类型知识",         "parameters": {"query": "string", "domain": "string", "limit": "int"},   "handler": "huichuan.search"},
        {"name": "search_comparisons",       "description": "搜索对比类型知识",         "parameters": {"query": "string", "domain": "string", "limit": "int"},   "handler": "huichuan.search"},
        {"name": "get_index",                "description": "获取知识库索引概览",       "parameters": {"domain": "string"},                                       "handler": "huichuan.api"},
        {"name": "get_entity",               "description": "获取单条知识全文",         "parameters": {"knowledge_id": "string"},                                 "handler": "huichuan.api"},
        {"name": "get_concept",              "description": "获取概念类型知识全文",     "parameters": {"knowledge_id": "string"},                                 "handler": "huichuan.api"},
        {"name": "get_knowledge",            "description": "获取单条知识全文(通用)",   "parameters": {"knowledge_id": "string"},                                 "handler": "huichuan.api"},
        {"name": "ingest_text",              "description": "文本摄入—LLM编译入库",  "parameters": {"text": "string", "source": "string", "filename": "string"},"handler": "huichuan.ingest"},
        {"name": "ingest_url",              "description": "URL摄入—抓取网页→编译入库","parameters": {"url": "string"},                                          "handler": "huichuan.ingest"},
        {"name": "ingest_file",              "description": "文件摄入—解析→编译入库","parameters": {"file_path": "string", "source": "string"},                 "handler": "huichuan.ingest"},
        {"name": "list_links",               "description": "列出某知识的所有关联",     "parameters": {"knowledge_id": "string"},                                 "handler": "huichuan.graph"},
        {"name": "get_graph_neighborhood",   "description": "获取知识图谱2-跳邻域",   "parameters": {"knowledge_id": "string", "max_hops": "int"},              "handler": "huichuan.graph"},
        {"name": "lint_report",              "description": "运行知识库巡检并返回报告", "parameters": {},                                                         "handler": "huichuan.lint"},
        {"name": "auto_fix",                 "description": "自动修复知识库问题",       "parameters": {"categories": "list[str]"},                                "handler": "huichuan.lint"},
        {"name": "subscribe",                "description": "订阅知识领域更新",         "parameters": {"agent_id": "string", "domains": "list", "tags": "list"}, "handler": "huichuan.api"},
        {"name": "unsubscribe",              "description": "取消订阅",                "parameters": {"subscription_id": "string"},                              "handler": "huichuan.api"},
    ]
else:
    # fastmcp 未安装时仍提供工具定义供测试验证
    MCP_TOOLS = [
        {"name": "search_entities",       "description": "搜索实体类型知识",              "parameters": {}, "handler": "huichuan.search"},
        {"name": "search_concepts",       "description": "搜索概念类型知识",              "parameters": {}, "handler": "huichuan.search"},
        {"name": "search_comparisons",    "description": "搜索对比类型知识",              "parameters": {}, "handler": "huichuan.search"},
        {"name": "get_index",             "description": "获取知识库索引概览",            "parameters": {}, "handler": "huichuan.api"},
        {"name": "get_entity",            "description": "获取单条知识全文",              "parameters": {}, "handler": "huichuan.api"},
        {"name": "get_concept",           "description": "获取概念类型知识全文",          "parameters": {}, "handler": "huichuan.api"},
        {"name": "get_knowledge",         "description": "获取单条知识全文(通用)",        "parameters": {}, "handler": "huichuan.api"},
        {"name": "ingest_text",           "description": "文本摄入—LLM编译入库",       "parameters": {}, "handler": "huichuan.ingest"},
        {"name": "ingest_url",           "description": "URL摄入—抓取网页→编译入库",  "parameters": {}, "handler": "huichuan.ingest"},
        {"name": "ingest_file",           "description": "文件摄入—解析→编译入库",     "parameters": {}, "handler": "huichuan.ingest"},
        {"name": "list_links",            "description": "列出某知识的所有关联",          "parameters": {}, "handler": "huichuan.graph"},
        {"name": "get_graph_neighborhood","description": "获取知识图谱2-跳邻域",        "parameters": {}, "handler": "huichuan.graph"},
        {"name": "lint_report",           "description": "运行知识库巡检并返回报告",      "parameters": {}, "handler": "huichuan.lint"},
        {"name": "auto_fix",              "description": "自动修复知识库问题",            "parameters": {}, "handler": "huichuan.lint"},
        {"name": "subscribe",             "description": "订阅知识领域更新",              "parameters": {}, "handler": "huichuan.api"},
        {"name": "unsubscribe",           "description": "取消订阅",                     "parameters": {}, "handler": "huichuan.api"},
    ]


async def _unsubscribe(subscription_id: str, caller: str = "") -> dict:
    """取消订阅（9-1：caller 归属校验——只能删自己的订阅，防跨 agent 删订）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if caller:
            row = await conn.fetchrow(
                f"SELECT agent_id FROM {SCHEMA}.subscriptions "
                f"WHERE subscription_id = $1",
                UUID(subscription_id),
            )
            if row is None:
                return {"subscription_id": subscription_id, "status": "not_found"}
            if row["agent_id"] != caller:
                logger.warning(
                    "[trace] mcp unsubscribe reject 归属不符 caller=%s owner=%s",
                    caller, row["agent_id"])
                return {"subscription_id": subscription_id,
                        "error": "该订阅不属于调用方，拒绝删除"}
        await conn.execute(
            f"DELETE FROM {SCHEMA}.subscriptions WHERE subscription_id = $1",
            UUID(subscription_id),
        )
    return {"subscription_id": subscription_id, "status": "deleted"}
