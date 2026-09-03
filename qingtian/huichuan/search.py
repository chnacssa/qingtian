"""汇川中文搜索适配 — pg_bigm + iLIKE 兜底

Phase 0 核心模块。pg_bigm 提供 2-gram 中日韩分词，
在不支持中文分词的 PostgreSQL simple 词典上实现中文全文搜索。

降级策略:
  - 运行时检测 pg_bigm 扩展是否安装
  - 已安装: pg_bigm =% + iLIKE 双通道（=% 是 pg_bigm 相似搜索操作符）
  - 未安装: 纯 iLIKE（不崩溃，不报错）

边界约束:
  - query 长度 > 100 截断 (防止 GIN 索引膨胀)
  - limit max 200 (防止返回过多)
  - 空 query 返回空列表 (不报错)
  - pg_bigm 不可用时 iLIKE 兜底 (不崩溃)
"""

from __future__ import annotations

import logging

logger = logging.getLogger("huichuan.search")

# ── 边界常量 ────────────────────────────────────────────

MAX_QUERY_LENGTH = 100       # 搜索 query 最大字符数
MAX_LIMIT = 200               # 单次搜索返回上限
DEFAULT_LIMIT = 20            # 默认返回条数

# ── pg_bigm 运行时检测 ──────────────────────────────────

_bigm_cache: bool | None = None  # None=未检测, True=可用, False=不可用


async def _has_pg_bigm(conn) -> bool:
    """检测 pg_bigm 扩展是否已安装（结果缓存）。"""
    global _bigm_cache
    if _bigm_cache is not None:
        return _bigm_cache
    try:
        row = await conn.fetchrow(
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_bigm'"
        )
        _bigm_cache = row is not None
    except Exception:
        _bigm_cache = False
    if not _bigm_cache:
        logger.info("pg_bigm not installed, falling back to iLIKE-only search")
    return _bigm_cache


def _build_search_clause(idx: int, use_bigm: bool, query_len: int = 100) -> tuple[str, int, bool]:
    """构建搜索 SQL 子句。

    返回 (SQL子句, 下一个参数索引, 是否实际用了bigm)。
    短词（≤2字符）: pg_bigm =% 对 2-gram 匹配弱，仅用 ILIKE 避免噪声。
    """
    is_short = query_len <= 2
    if use_bigm and not is_short:
        return (
            f"(title ILIKE ${idx} OR content ILIKE ${idx} "
            f"OR title =% ${idx + 1} OR content =% ${idx + 1})",
            idx + 2,
            True,
        )
    else:
        return (
            f"(title ILIKE ${idx} OR content ILIKE ${idx})",
            idx + 1,
            False,
        )


async def search_knowledge(
    conn,
    query: str,
    domain: str = "",
    tags: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    schema: str = "huichuan",
) -> list[dict]:
    """混合搜索: pg_bigm 优先, iLIKE 兜底。

    搜索策略（三层降级）:
      1. pg_bigm =% 操作符 — 2-gram 相似匹配（中日韩分词）
      2. ILIKE — 精确子串匹配（兜底，pg_bigm 未安装时也能工作）
      3. ts_rank — 相关性排序（simple 词典，对中文无效但对英数有效）

    Args:
        conn: asyncpg connection
        query: 搜索关键词
        domain: 领域过滤（空字符串 = 不限）
        tags: 标签过滤（None = 不限）
        limit: 返回上限（max 200）
        offset: 分页偏移
        schema: 数据库 schema 名

    Returns:
        list[dict]: 搜索结果，每项含 knowledge_id, title, domain, tags,
                    visibility, snippet, rank, updated_at
    """
    if not query or not query.strip():
        return []

    query = query.strip()[:MAX_QUERY_LENGTH]
    limit = max(1, min(limit, MAX_LIMIT))

    # 运行时检测 pg_bigm 是否可用
    use_bigm = await _has_pg_bigm(conn)

    conditions: list[str] = ["status = 'active'"]
    params: list = []
    idx = 1

    # ── 搜索条件：pg_bigm + iLIKE 双通道（或纯 iLIKE）─
    ilike_pattern = f"%{query}%"
    search_clause, new_idx, use_bigm_actual = _build_search_clause(idx, use_bigm, len(query))
    conditions.append(search_clause)
    params.append(ilike_pattern)  # iLIKE pattern
    if use_bigm_actual:
        params.append(query)       # pg_bigm query
    idx = new_idx

    # ── 领域过滤 ────────────────────────────────────────
    if domain:
        conditions.append(f"domain = ${idx}")
        params.append(domain)
        idx += 1

    # ── 标签过滤 ────────────────────────────────────────
    if tags:
        conditions.append(f"tags && ${idx}")
        params.append(tags)
        idx += 1

    where = " AND ".join(conditions)

    # ── ts_rank 排序（simple 词典，对英数有效）──────────
    rank_idx = idx
    params.append(query)  # plainto_tsquery 的 query 参数
    idx += 1

    # limit / offset
    params.extend([limit, offset])

    rows = await conn.fetch(
        f"SELECT knowledge_id, title, domain, tags, visibility, "
        f"LEFT(content, 200) AS snippet, "
        f"ts_rank(to_tsvector('simple', title || ' ' || content), "
        f"plainto_tsquery('simple', ${rank_idx})) AS rank, "
        f"updated_at "
        f"FROM {schema}.knowledge_entries "
        f"WHERE {where} "
        f"ORDER BY rank DESC "
        f"LIMIT ${idx} OFFSET ${idx + 1}",
        *params,
    )

    results = []
    for r in rows:
        results.append({
            "knowledge_id": str(r["knowledge_id"]),
            "title": r["title"],
            "domain": r["domain"],
            "tags": r["tags"] or [],
            "visibility": r["visibility"],
            "snippet": r["snippet"] or "",
            "rank": r["rank"],
            "updated_at": r["updated_at"],
        })

    logger.debug("Search '%s' returned %d results", query, len(results))
    return results


async def search_with_visibility(
    conn,
    query: str,
    agent_id: str | None = None,
    domain: str = "",
    tags: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    sort_by: str = "rank",
    sort_order: str = "DESC",
    include_expired: bool = False,
    schema: str = "huichuan",
) -> tuple[list[dict], int]:
    """带可见性过滤的搜索（供 API 端点直接调用）。

    在 search_knowledge 基础上叠加:
      - 可见性过滤（public / enterprise / private + owner/authorized）
      - 过期过滤
      - 自定义排序
      - 返回总数

    Args:
        conn: asyncpg connection
        query: 搜索关键词
        agent_id: 调用方 agent_id（None = 仅 public）
        domain: 领域过滤
        tags: 标签过滤
        limit: 返回上限
        offset: 分页偏移
        sort_by: 排序字段（rank / created_at / updated_at / title / quality）
        sort_order: ASC / DESC
        include_expired: 是否包含过期知识
        schema: 数据库 schema 名

    Returns:
        (results, total_count)
    """
    if not query or not query.strip():
        return [], 0

    query = query.strip()[:MAX_QUERY_LENGTH]
    limit = max(1, min(limit, MAX_LIMIT))

    # 白名单校验排序字段
    allowed_sort = {"rank", "created_at", "updated_at", "title", "quality", "version"}
    if sort_by not in allowed_sort:
        sort_by = "rank"
    order_dir = "DESC" if sort_order.upper() == "DESC" else "ASC"

    # 运行时检测 pg_bigm 是否可用
    use_bigm = await _has_pg_bigm(conn)

    conditions: list[str] = ["status = 'active'"]
    query_params: list = []  # WHERE 子句专用参数（不含 ts_rank/limit/offset）
    idx = 1

    # ── 搜索条件 ────────────────────────────────────────
    ilike_pattern = f"%{query}%"
    search_clause, new_idx, use_bigm_actual = _build_search_clause(idx, use_bigm, len(query))
    conditions.append(search_clause)
    query_params.append(ilike_pattern)
    if use_bigm_actual:
        query_params.append(query)
    idx = new_idx

    # ── 过期过滤 ────────────────────────────────────────
    if not include_expired:
        conditions.append(f"(valid_until IS NULL OR valid_until >= CURRENT_DATE)")

    # ── 领域过滤 ────────────────────────────────────────
    if domain:
        conditions.append(f"domain = ${idx}")
        query_params.append(domain)
        idx += 1

    # ── 标签过滤 ────────────────────────────────────────
    if tags:
        conditions.append(f"tags && ${idx}")
        query_params.append(tags)
        idx += 1

    # ── 可见性过滤 ──────────────────────────────────────
    vf_clause, vf_params = _visibility_filter(agent_id, start_index=idx)
    conditions.append(f"({vf_clause})")
    query_params.extend(vf_params)
    idx += len(vf_params)

    where = " AND ".join(conditions)

    # ── 执行查询 ────────────────────────────────────────
    # 先查总数（仅传 WHERE 子句参数，不含 ts_rank/limit/offset）
    count_row = await conn.fetchrow(
        f"SELECT COUNT(*) FROM {schema}.knowledge_entries WHERE {where}",
        *query_params,
    )
    total = count_row["count"] if count_row else 0

    # ── ts_rank + limit/offset ───────────────────────────
    rank_idx = idx
    full_params = list(query_params)
    full_params.append(query)  # ts_rank query
    idx += 1

    # 排序子句
    if sort_by == "rank":
        order_clause = f"rank DESC"
    else:
        order_clause = f"{sort_by} {order_dir}"

    full_params.extend([limit, offset])

    rows = await conn.fetch(
        f"SELECT knowledge_id, title, domain, tags, visibility, "
        f"LEFT(content, 200) AS snippet, "
        f"ts_rank(to_tsvector('simple', title || ' ' || content), "
        f"plainto_tsquery('simple', ${rank_idx})) AS rank, "
        f"updated_at "
        f"FROM {schema}.knowledge_entries "
        f"WHERE {where} "
        f"ORDER BY {order_clause} "
        f"LIMIT ${idx} OFFSET ${idx + 1}",
        *full_params,
    )

    results = []
    for r in rows:
        results.append({
            "knowledge_id": str(r["knowledge_id"]),
            "title": r["title"],
            "domain": r["domain"],
            "tags": r["tags"] or [],
            "visibility": r["visibility"],
            "snippet": r["snippet"] or "",
            "rank": r["rank"],
            "updated_at": r["updated_at"],
        })

    return results, total


def _visibility_filter(agent_id: str | None, start_index: int = 1) -> tuple[str, list]:
    """生成可见性过滤 SQL 片段和参数。

    agent_id=None 时仅返回 public。

    Returns:
        (SQL片段, 参数列表)。SQL 使用 $start_index, $start_index+1 占位符。
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
