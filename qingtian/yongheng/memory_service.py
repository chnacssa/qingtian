"""记忆服务 —— 存储 / 检索 / context / batch / export / status。"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg
import httpx

from . import config as cfg
from .models import AppError
from .embedding import embed_text, embedding_queue
from .high_value import keyword_scan, enqueue_llm_check, start_llm_worker
from .filter import should_store

logger = logging.getLogger("yongheng")

# 搜索上限，防止单次查询拉全表
MAX_SEARCH_LIMIT = 200
# 批量写入上限
MAX_BATCH_SIZE = 500

# metadata 过滤键名白名单：键会拼接进 SQL（metadata->>'key'），必须防止注入
_META_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_json_meta(meta) -> dict:
    """安全解析 metadata，兼容 asyncpg JSONB dict 和字符串。"""
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return meta
    if isinstance(meta, str):
        try:
            return json.loads(meta) if meta else {}
        except json.JSONDecodeError:
            return {}
    return {}

async def write_memory(conn: asyncpg.Connection, namespace: str, content: str,
                       mem_type: str = "episodic", source: str = "openclaw",
                       metadata: dict | None = None) -> dict:
    try:
        if not should_store(content):
            raise AppError("VALIDATION_ERROR", "content filtered", 400)
    except AppError:
        raise
    except Exception:
        logger.warning("Filter check failed for write_memory, failing open")
        # 过滤器异常时 fail-open：不过滤，允许写入

    meta = metadata or {}
    ts = datetime.now(timezone.utc)
    schema = cfg.get_schema_name()

    memory_id = await conn.fetchval(
        f"""INSERT INTO {schema}.memories (namespace, memory_type, content, source, timestamp, metadata, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW()) RETURNING id""",
        namespace, mem_type, content, source, ts, json.dumps(meta),
    )

    is_hv = keyword_scan(content)
    if is_hv:
        await conn.execute(
            f"UPDATE {schema}.memories SET memory_type = 'high_value', protected = TRUE WHERE id = $1",
            memory_id,
        )

    start_llm_worker()
    enqueue_llm_check(namespace, memory_id, content)
    await embedding_queue.enqueue(memory_id, content)

    return {
        "id": memory_id,
        "status": "stored",
        "high_value": is_hv or (mem_type == "high_value"),
        "timestamp": ts,
    }


async def delete_memory(conn: asyncpg.Connection, memory_id: int) -> dict:
    """按 id 删除记忆（embedding 向量随行删除）。返回 {"id", "status"}。"""
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"DELETE FROM {schema}.memories WHERE id = $1 RETURNING id",
        memory_id,
    )
    if not row:
        return {"id": memory_id, "status": "not_found"}
    return {"id": memory_id, "status": "deleted"}


async def batch_write(conn: asyncpg.Connection, namespace: str,
                      memories: list[dict]) -> tuple[list[dict], int, int]:
    if len(memories) > MAX_BATCH_SIZE:
        raise AppError("VALIDATION_ERROR", f"batch size {len(memories)} exceeds max {MAX_BATCH_SIZE}", 400)
    results = []
    stored = 0
    failed = 0
    for i, mem in enumerate(memories):
        try:
            r = await write_memory(
                conn, namespace,
                content=mem["content"],
                mem_type=mem.get("type", "episodic"),
                source=mem.get("source", "openclaw"),
                metadata=mem.get("metadata"),
            )
            results.append({"index": i, "id": r["id"], "status": "stored", "high_value": r["high_value"]})
            stored += 1
        except AppError as e:
            results.append({"index": i, "error": e.message})
            failed += 1
        except Exception as e:
            logger.error(f"Batch write error at index {i}: {e}")
            results.append({"index": i, "error": str(e)})
            failed += 1
    return results, stored, failed


def _row_to_dict(row) -> dict:
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    result = {
        "id": row["id"],
        "content": row["content"],
        "type": row.get("memory_type", "episodic"),
        "timestamp": row["timestamp"],
        "protected": row.get("protected", False),
        "search_hit_count": row.get("search_hit_count", 0),
        "metadata": metadata or {},
    }
    # 透传检索分（ts_rank / cosine 相似度），供时间衰减/RRF 排序使用
    for score_key in ("similarity", "rank"):
        if score_key in row:
            result["rrf_score"] = row[score_key]
    return result


def _append_metadata_filter(conditions: list[str], params: list[Any],
                            metadata_filter: dict, idx: int) -> int:
    """将 metadata 过滤条件拼入 SQL。键名会直接进 SQL（metadata->>'key'），须白名单校验。"""
    for key, value in metadata_filter.items():
        if not _META_KEY_RE.match(key):
            raise AppError("VALIDATION_ERROR", f"invalid metadata filter key: {key!r}", 400)
        conditions.append(f"metadata->>'{key}' = ${idx}")
        params.append(str(value))
        idx += 1
    return idx


async def _fts_search(conn: asyncpg.Connection, namespace: str, query: str,
                      top_k: int, date_from=None, date_to=None, type_filter=None,
                      metadata_filter: dict | None = None) -> list[dict]:
    schema = cfg.get_schema_name()
    conditions = [f"namespace = $1"]
    params: list[Any] = [namespace]
    idx = 2

    if date_from:
        conditions.append(f"timestamp >= ${idx}")
        params.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"timestamp <= ${idx}")
        params.append(date_to)
        idx += 1
    if type_filter:
        conditions.append(f"memory_type = ANY(${idx})")
        params.append(type_filter)
        idx += 1
    if metadata_filter:
        idx = _append_metadata_filter(conditions, params, metadata_filter, idx)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT id, content, memory_type, timestamp, protected, search_hit_count, metadata,
               ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', ${idx})) AS rank
        FROM {schema}.memories
        WHERE {where}
        ORDER BY rank DESC
        LIMIT {top_k}
    """
    params.append(query)
    rows = await conn.fetch(sql, *params)
    return [_row_to_dict(r) for r in rows]


async def _vector_search(conn: asyncpg.Connection, namespace: str, query_vector: list[float] | None,
                         top_k: int, date_from=None, date_to=None, type_filter=None,
                         metadata_filter: dict | None = None) -> list[dict]:
    if query_vector is None:
        return []
    schema = cfg.get_schema_name()
    conditions = [
        f"namespace = $1",
        "embedding_status = 'done'",
        "embedding IS NOT NULL",
    ]
    params: list[Any] = [namespace]
    idx = 2

    if date_from:
        conditions.append(f"timestamp >= ${idx}")
        params.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"timestamp <= ${idx}")
        params.append(date_to)
        idx += 1
    if type_filter:
        conditions.append(f"memory_type = ANY(${idx})")
        params.append(type_filter)
        idx += 1
    if metadata_filter:
        idx = _append_metadata_filter(conditions, params, metadata_filter, idx)

    where = " AND ".join(conditions)
    vec_str = str(query_vector)
    sql = f"""
        SELECT id, content, memory_type, timestamp, protected, search_hit_count, metadata,
               1 - (embedding <=> ${idx}::vector) AS similarity
        FROM {schema}.memories
        WHERE {where}
        ORDER BY embedding <=> ${idx}::vector
        LIMIT {top_k}
    """
    params.append(vec_str)
    rows = await conn.fetch(sql, *params)
    return [_row_to_dict(r) for r in rows]


def _rrf_fusion(fts_results: list[dict], vec_results: list[dict], k: int = 60) -> list[dict]:
    scores: dict[int, float] = {}
    for rank, item in enumerate(fts_results):
        scores[item["id"]] = scores.get(item["id"], 0) + 1 / (k + rank + 1)
    for rank, item in enumerate(vec_results):
        scores[item["id"]] = scores.get(item["id"], 0) + 1 / (k + rank + 1)

    merged: dict[int, dict] = {}
    for item in fts_results + vec_results:
        merged[item["id"]] = item

    for mid, item in merged.items():
        item["rrf_score"] = scores.get(mid, 0)
    return list(merged.values())


def _apply_time_decay(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    recent_days = cfg.get_time_decay_recent_days()
    medium_days = cfg.get_time_decay_medium_days()
    recent_weight = cfg.get_time_decay_recent_weight()
    medium_weight = cfg.get_time_decay_medium_weight()
    min_hits = cfg.get_hit_exemption_min_hits()
    max_bonus = cfg.get_hit_exemption_max_bonus()

    for item in items:
        days = (now - item["timestamp"]).total_seconds() / 86400
        if days <= recent_days:
            decay = recent_weight
        elif days <= medium_days:
            decay = medium_weight
        else:
            if not item.get("protected") and item.get("search_hit_count", 0) < min_hits:
                decay = 0
            else:
                decay = medium_weight

        hit_bonus = min(item.get("search_hit_count", 0) * 0.01, max_bonus)
        item["final_score"] = item.get("rrf_score", 0) * decay + hit_bonus
        item["time_decay_weight"] = decay

    return [item for item in items if item.get("final_score", 0) > 0]


async def search_memory(conn: asyncpg.Connection, namespace: str, query: str,
                        method: str = "hybrid", top_k: int = 5, offset: int = 0,
                        budget_tokens: int = 2000, filter_dict: dict | None = None,
                        include_global: bool = True) -> dict:
    top_k = min(top_k, MAX_SEARCH_LIMIT)
    date_from = filter_dict.get("date_from") if filter_dict else None
    date_to = filter_dict.get("date_to") if filter_dict else None
    type_filter = filter_dict.get("type") if filter_dict else None
    metadata_filter = filter_dict.get("metadata") if filter_dict else None

    take = top_k * 2 if method in ("hybrid", "agentic") else top_k

    global_ns = None
    if include_global:
        from xixing import config as xcfg
        global_ns = xcfg.get_global_namespace()

    if method == "keyword":
        results = await _fts_search(conn, namespace, query, top_k, date_from, date_to, type_filter, metadata_filter)
        for r in results:
            r["final_score"] = r.get("rrf_score", 0)
            r["time_decay_weight"] = 1.0
        if include_global and namespace != global_ns:
            global_results = await _fts_search(conn, global_ns, query, top_k, date_from, date_to, type_filter, metadata_filter)
            for r in global_results:
                r["final_score"] = r.get("rrf_score", 0) * 0.7
                r["time_decay_weight"] = 1.0
            results = _merge_dedupe(results, global_results)
            results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        total_matched = len(results)
        # P2 (R11): 合并后可达 2×top_k，且未应用 offset/limit —— 与其他分支对齐，统一切片
        results = results[offset:offset + top_k]
    elif method == "hybrid":
        query_vector = await embed_text(query)
        fts_results = await _fts_search(conn, namespace, query, take, date_from, date_to, type_filter, metadata_filter)
        vec_results = await _vector_search(conn, namespace, query_vector, take, date_from, date_to, type_filter, metadata_filter)
        merged = _rrf_fusion(fts_results, vec_results, k=cfg.get_search_rrf_k())
        if include_global and namespace != global_ns:
            global_fts = await _fts_search(conn, global_ns, query, take, date_from, date_to, type_filter, metadata_filter)
            global_vec = await _vector_search(conn, global_ns, query_vector, take, date_from, date_to, type_filter, metadata_filter)
            global_merged = _rrf_fusion(global_fts, global_vec, k=cfg.get_search_rrf_k())
            for r in global_merged:
                r["rrf_score"] = r.get("rrf_score", 0) * 0.7
            merged = _merge_dedupe(merged, global_merged)
        results = _apply_time_decay(merged)
        results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        total_matched = len(results)
        results = results[offset:offset + top_k]
    elif method == "agentic":
        query_vector = await embed_text(query)
        fts = await _fts_search(conn, namespace, query, take, date_from, date_to, type_filter, metadata_filter)
        vec = await _vector_search(conn, namespace, query_vector, take, date_from, date_to, type_filter, metadata_filter)
        merged = _rrf_fusion(fts, vec, k=cfg.get_search_rrf_k())
        if include_global and namespace != global_ns:
            global_fts = await _fts_search(conn, global_ns, query, take, date_from, date_to, type_filter, metadata_filter)
            global_vec = await _vector_search(conn, global_ns, query_vector, take, date_from, date_to, type_filter, metadata_filter)
            global_merged = _rrf_fusion(global_fts, global_vec, k=cfg.get_search_rrf_k())
            for r in global_merged:
                r["rrf_score"] = r.get("rrf_score", 0) * 0.7
            merged = _merge_dedupe(merged, global_merged)
        results = _apply_time_decay(merged)
        results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
        results = results[:top_k]

        sub_queries = await _generate_sub_queries(query, results)
        if sub_queries:
            for sq in sub_queries[:3]:
                sq_vec = await embed_text(sq)
                sq_fts = await _fts_search(conn, namespace, sq, top_k, date_from, date_to, type_filter, metadata_filter)
                sq_vec_r = await _vector_search(conn, namespace, sq_vec, top_k, date_from, date_to, type_filter, metadata_filter)
                sq_merged = _rrf_fusion(sq_fts, sq_vec_r, k=cfg.get_search_rrf_k())
                if include_global and namespace != global_ns:
                    sq_global_fts = await _fts_search(conn, global_ns, sq, top_k, date_from, date_to, type_filter, metadata_filter)
                    sq_global_vec = await _vector_search(conn, global_ns, sq_vec, top_k, date_from, date_to, type_filter, metadata_filter)
                    sq_global_merged = _rrf_fusion(sq_global_fts, sq_global_vec, k=cfg.get_search_rrf_k())
                    for r in sq_global_merged:
                        r["rrf_score"] = r.get("rrf_score", 0) * 0.7
                    sq_merged = _merge_dedupe(sq_merged, sq_global_merged)
                results = _merge_dedupe(results, sq_merged)
            results.sort(key=lambda x: x.get("final_score", 0), reverse=True)

        total_matched = len(results)
        results = results[offset:offset + top_k]
    else:
        raise AppError("VALIDATION_ERROR", f"unknown method: {method}", 400)

    for r in results:
        r["score"] = r.get("final_score", 0)
        r["timestamp"] = r.get("timestamp", datetime.now(timezone.utc))

    hit_ids = [r["id"] for r in results[:top_k]]
    if hit_ids:
        schema = cfg.get_schema_name()
        await conn.execute(
            f"UPDATE {schema}.memories SET search_hit_count = search_hit_count + 1 WHERE id = ANY($1)",
            hit_ids,
        )

    total_tokens = sum(len(r["content"]) // 2 for r in results)

    return {
        "results": results,
        "total_matched": total_matched,
        "method": method,
        "total_tokens": total_tokens,
    }


def _audience_weight(metadata: dict | None, agent_profile: dict | None) -> float:
    """计算 target_audience 匹配权重。

    - targeted + 匹配 → 1.0（精准推送）
    - targeted + 不匹配 → 0.1（几乎不出现在结果中）
    - global → 0.7（所有人可得，但不优先）
    - 无 target_audience（legacy）→ 0.7（向后兼容）
    """
    if not agent_profile:
        return 0.7
    audience = None
    if metadata:
        audience = metadata.get("target_audience")
    if not isinstance(audience, dict):
        return 0.7  # legacy 记忆或无效格式

    scope = audience.get("scope", "global")
    if scope == "global":
        return 0.7

    if scope == "targeted":
        target_cats = audience.get("categories", [])
        target_caps = audience.get("capabilities", [])
        if not target_cats and not target_caps:
            return 0.7  # targeted 但没指定任何条件 → 降级为 global

        agent_cat = agent_profile.get("category", "")
        agent_caps = agent_profile.get("capabilities", [])

        cat_match = agent_cat in target_cats if target_cats else False
        cap_match = any(c in agent_caps for c in target_caps) if target_caps else False

        if cat_match or cap_match:
            return 1.0
        return 0.1

    return 0.7  # 未知 scope → 降级


async def context_memory(conn: asyncpg.Connection, namespace: str, context: str,
                         top_k: int = 10, agent_profile: dict | None = None,
                         include_global: bool = True) -> dict:
    top_k = min(top_k, MAX_SEARCH_LIMIT)
    vec = await embed_text(context)
    take = top_k * 3  # 多取一些，因为 audience 过滤可能筛掉不少
    vec_results = await _vector_search(conn, namespace, vec, take)
    if include_global:
        from xixing import config as xcfg
        global_ns = xcfg.get_global_namespace()
        if namespace != global_ns:
            global_results = await _vector_search(conn, global_ns, vec, take)
            for r in global_results:
                r["rrf_score"] = r.get("rrf_score", 0) * 0.7
            vec_results = _merge_dedupe(vec_results, global_results)
    results = _apply_time_decay(vec_results)

    # 能力分发：target_audience 匹配加权
    for r in results:
        audience_w = _audience_weight(r.get("metadata"), agent_profile)
        r["final_score"] = r.get("final_score", 0) * audience_w

    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    results = results[:top_k]

    hit_ids = [r["id"] for r in results]
    if hit_ids:
        schema = cfg.get_schema_name()
        await conn.execute(
            f"UPDATE {schema}.memories SET search_hit_count = search_hit_count + 1 WHERE id = ANY($1)",
            hit_ids,
        )

    total_tokens = sum(len(r["content"]) // 2 for r in results)
    for r in results:
        r["score"] = r.get("final_score", 0)
    return {
        "namespace": namespace,
        "context": context,
        "results": results,
        "method": "contextual",
        "total_tokens": total_tokens,
    }


async def _generate_sub_queries(query: str, preliminary_results: list[dict]) -> list[str]:
    if not preliminary_results:
        return []

    snippets = "\n".join(r["content"][:200] for r in preliminary_results[:5])
    prompt = (
        f"原始查询：{query}\n\n"
        f"初步检索结果摘要：\n{snippets}\n\n"
        "基于以上信息，生成 2-3 个更精确的子查询，用于进一步检索。"
        "每行一个查询，不要编号。"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{cfg.get_llm_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.get_llm_api_key()}"},
                json={
                    "model": cfg.get_llm_agentic_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.3,
                },
                timeout=20,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            return [line.strip() for line in text.strip().split("\n") if line.strip()][:3]
    except Exception as e:
        logger.warning(f"Sub-query generation failed: {e}")
        return []


def _merge_dedupe(existing: list[dict], new: list[dict]) -> list[dict]:
    by_id = {r["id"]: r for r in existing}
    for r in new:
        dup = by_id.get(r["id"])
        if dup is None:
            existing.append(r)
            by_id[r["id"]] = r
        else:
            dup["final_score"] = max(dup.get("final_score", 0), r.get("final_score", 0))
    return existing


async def update_memory_status(conn: asyncpg.Connection, memory_id: int,
                               review_status: str) -> dict:
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(f"SELECT namespace, review_status FROM {schema}.memories WHERE id = $1", memory_id)
    if not row:
        raise AppError("NOT_FOUND", "memory not found", 404)

    await conn.execute(
        f"UPDATE {schema}.memories SET review_status = $1 WHERE id = $2",
        review_status, memory_id,
    )
    return {
        "id": memory_id,
        "review_status": review_status,
        "updated_at": datetime.now(timezone.utc),
    }


async def export_memories(conn: asyncpg.Connection, namespace: str,
                          date_from: str | None = None, date_to: str | None = None,
                          include_vectors: bool = False) -> list[dict]:
    schema = cfg.get_schema_name()
    conditions = [f"namespace = $1"]
    params: list[Any] = [namespace]
    idx = 2

    if date_from:
        conditions.append(f"timestamp >= ${idx}")
        params.append(date_from)
        idx += 1
    if date_to:
        conditions.append(f"timestamp <= ${idx}")
        params.append(date_to)
        idx += 1

    where = " AND ".join(conditions)
    fields = "id, namespace, memory_type, content, timestamp, protected, source, metadata, review_status, search_hit_count"
    if include_vectors:
        fields += ", embedding::text"

    sql = f"SELECT {fields} FROM {schema}.memories WHERE {where} ORDER BY timestamp"
    rows = await conn.fetch(sql, *params)

    results = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("metadata"), str):
            item["metadata"] = json.loads(item["metadata"])
        item["timestamp"] = item["timestamp"].isoformat()
        results.append(item)
    return results


async def transfer_memories(conn: asyncpg.Connection, source_ns: str, target_ns: str,
                            mode: str = "copy") -> dict:
    """迁移记忆：将 source namespace 的记忆复制/移动到 target namespace。

    mode=copy: 保留原记忆，生成新副本到 target namespace
    mode=move: 原记忆标记为 consolidated（已迁移），生成副本到 target namespace
    """
    schema = cfg.get_schema_name()

    rows = await conn.fetch(
        f"""SELECT memory_type, content, source, timestamp, protected, metadata, keywords, embedding_status
            FROM {schema}.memories WHERE namespace = $1
            AND consolidated = FALSE ORDER BY timestamp""",
        source_ns,
    )

    if not rows:
        return {"transferred": 0, "source_ns": source_ns, "target_ns": target_ns, "mode": mode}

    count = 0
    async with conn.transaction():
        for row in rows:
            await conn.execute(
                f"""INSERT INTO {schema}.memories (namespace, memory_type, content, source, timestamp, protected, metadata, keywords, embedding_status)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                target_ns, row["memory_type"], row["content"],
                f"transfer:{source_ns}", row["timestamp"], row["protected"],
                json.dumps({"transferred_from": source_ns, "transferred_at": datetime.now(timezone.utc).isoformat(),
                            **(_safe_json_meta(row["metadata"]))}),
                row["keywords"], "pending",
            )
            count += 1

        if mode == "move" and count > 0:
            await conn.execute(
                f"UPDATE {schema}.memories SET consolidated = TRUE WHERE namespace = $1 AND consolidated = FALSE",
                source_ns,
            )

    return {"transferred": count, "source_ns": source_ns, "target_ns": target_ns, "mode": mode}


async def get_recent_memories(conn: asyncpg.Connection, namespace: str,
                               limit: int = 20, since: str | None = None) -> list[dict]:
    """获取最近记忆（崩溃恢复用）。"""
    schema = cfg.get_schema_name()
    conditions = [f"namespace = $1"]
    params: list[Any] = [namespace]
    idx = 2

    if since:
        conditions.append(f"timestamp >= ${idx}")
        params.append(since)
        idx += 1

    rows = await conn.fetch(
        f"""SELECT id, namespace, memory_type, content, source, timestamp, protected, metadata, review_status
            FROM {schema}.memories WHERE {' AND '.join(conditions)}
            ORDER BY timestamp DESC LIMIT {limit}""",
        *params,
    )

    results = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("metadata"), str):
            item["metadata"] = json.loads(item["metadata"])
        item["timestamp"] = item["timestamp"].isoformat()
        results.append(item)
    return results
