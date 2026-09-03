"""Dream Gate —— 记忆整理/压缩 + 健康检测。"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import asyncpg

from . import config as cfg
from .models import AppError
from .profile_service import consolidate_learned

logger = logging.getLogger("yongheng")


def _consume_task_exception(task: asyncio.Task) -> None:
    """消费 fire-and-forget 任务的异常，防止 "Task exception was never retrieved"。"""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("Background task failed: %s", exc)


async def check_trigger(conn: asyncpg.Connection, namespace: str) -> tuple[bool, str]:
    schema = cfg.get_schema_name()
    rows = await conn.fetch(
        f"SELECT content FROM {schema}.memories WHERE namespace = $1 AND memory_type = 'episodic' AND consolidated = FALSE",
        namespace,
    )
    total_chars = sum(len(r["content"]) for r in rows)
    estimated_tokens = total_chars // 2
    if estimated_tokens > cfg.get_consolidate_token_budget():
        return True, f"token_budget exceeded: {estimated_tokens} > {cfg.get_consolidate_token_budget()}"

    last_digest = await conn.fetchrow(
        f"SELECT created_at FROM {schema}.digests WHERE namespace = $1 ORDER BY created_at DESC LIMIT 1",
        namespace,
    )
    if last_digest:
        days_since = (datetime.now(timezone.utc) - last_digest["created_at"]).days
        if days_since > cfg.get_consolidate_min_days() and rows:
            return True, f"days_since_last: {days_since} > {cfg.get_consolidate_min_days()} with pending records"

    if len(rows) > 500:
        return True, f"record_count: {len(rows)} > 500"

    return False, "conditions not met"


async def _reset_hit_counts(conn: asyncpg.Connection, namespace: str) -> None:
    """超期记忆命中计数归零（仅限本 namespace）。

    P1-1（9-1 修复日）：补 namespace 谓词 —— 此前全表更新，任意 namespace
    token 调 /consolidate 即把所有 Agent 旧记忆命中数归零，经 memory_service
    时间衰减（hit<min 且非 protected → decay=0）放大为跨租户检索破坏。
    """
    schema = cfg.get_schema_name()
    await conn.execute(
        f"UPDATE {schema}.memories SET search_hit_count = 0 "
        "WHERE namespace = $2 AND search_hit_count > 0 AND "
        "EXTRACT(DAY FROM (NOW() - timestamp)) > $1",
        cfg.get_hit_exemption_reset_days(), namespace,
    )


async def consolidate(conn: asyncpg.Connection, namespace: str) -> dict:
    schema = cfg.get_schema_name()
    should_run, reason = await check_trigger(conn, namespace)
    if not should_run:
        return {"status": "skipped", "namespace": namespace, "reason": reason}

    records = await conn.fetch(
        f"SELECT id, content FROM {schema}.memories "
        "WHERE namespace = $1 AND memory_type = 'episodic' AND consolidated = FALSE "
        "ORDER BY timestamp LIMIT $2",
        namespace, cfg.get_consolidate_max_records(),
    )
    if not records:
        return {"status": "skipped", "namespace": namespace, "reason": "no records"}

    source_ids = [r["id"] for r in records]
    contents = [r["content"] for r in records]

    # LLM 调用在事务外执行（不持有连接等待远程响应），DB 写入统一包进事务保证原子性
    digest_text = await _llm_generate_digest(contents)
    extracted = await _llm_extract(contents)

    async with conn.transaction():
        digest_id = await conn.fetchval(
            f"INSERT INTO {schema}.digests (namespace, target_date, type, digest, source_records, record_count, timeline_entry) "
            "VALUES ($1, CURRENT_DATE, 'daily', $2, $3, $4, $5) "
            "ON CONFLICT (namespace, target_date, type) DO UPDATE SET "
            "digest = EXCLUDED.digest, source_records = EXCLUDED.source_records, "
            "record_count = EXCLUDED.record_count, timeline_entry = EXCLUDED.timeline_entry "
            "RETURNING id",
            namespace, digest_text, source_ids, len(source_ids), digest_text[:200],
        )

        mem_meta = json.dumps({"extracted": extracted})
        await conn.fetchval(
            f"INSERT INTO {schema}.memories (namespace, memory_type, content, metadata, protected) "
            "VALUES ($1, 'consolidated', $2, $3, TRUE) RETURNING id",
            namespace, digest_text, mem_meta,
        )

        await conn.execute(
            f"UPDATE {schema}.memories SET consolidated = TRUE, consolidated_to_id = $1 WHERE id = ANY($2)",
            digest_id, source_ids,
        )

        await consolidate_learned(conn, namespace)

        await _reset_hit_counts(conn, namespace)

    await _check_agent_health(conn, namespace)

    # 通过总线通知 Agent 记忆已压缩
    agent_id = namespace.replace("agent:", "", 1) if namespace.startswith("agent:") else namespace
    try:
        from common.bus import bus
        # P2 (R11): create_task fire-and-forget 需消费异常，否则任务抛错时出现
        # "Task exception was never retrieved" 告警
        task = asyncio.create_task(bus.publish(agent_id, {
            "type": "memory_consolidated",
            "source": "yongheng",
            "payload": {
                "status": "consolidated",
                "records_before": len(records),
                "records_after": 1,
                "digest_preview": digest_text[:200],
            },
        }))
        task.add_done_callback(_consume_task_exception)
    except Exception:
        pass

    return {
        "status": "consolidated",
        "namespace": namespace,
        "records_before": len(records),
        "records_after": 1,
        "digest_id": digest_id,
        "timeline_added": True,
    }


async def _llm_generate_digest(contents: list[str]) -> str:
    import httpx

    combined = "\n".join(f"- {c[:300]}" for c in contents[:100])
    prompt = (
        "以下是一个 Agent 今天的工作记录列表。请生成一段简洁的日终总结（200 字以内），"
        "涵盖主要工作内容、关键决策和产出。\n\n"
        f"{combined}"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{cfg.get_llm_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.get_llm_api_key()}"},
                json={
                    "model": cfg.get_llm_digest_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 500,
                    "temperature": 0.3,
                },
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.warning(f"Digest generation failed: {e}")
        return " | ".join(c[:100] for c in contents[:5])


async def _llm_extract(contents: list[str]) -> dict:
    import httpx

    combined = "\n".join(f"- {c[:500]}" for c in contents[:200])
    prompt = (
        "从以下工作记录中提取结构化信息。返回 JSON，包含四个数组：\n"
        "decisions: 决策列表\n"
        "facts: 关键事实列表\n"
        "risks: 风险列表\n"
        "entities: 涉及实体列表\n\n"
        f"{combined}"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{cfg.get_llm_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.get_llm_api_key()}"},
                json={
                    "model": cfg.get_llm_digest_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1000,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
                timeout=60,
            )
            resp.raise_for_status()
            return json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as e:
        logger.warning(f"LLM extract failed: {e}")
        return {"decisions": [], "facts": [], "risks": [], "entities": []}


async def _check_agent_health(conn: asyncpg.Connection, namespace: str):
    schema = cfg.get_schema_name()
    write_row = await conn.fetchrow(
        f"SELECT COUNT(*) as cnt FROM {schema}.memories "
        "WHERE namespace = $1 AND created_at > NOW() - INTERVAL '24 hours'",
        namespace,
    )
    context_row = await conn.fetchrow(
        f"SELECT COUNT(*) as cnt FROM {schema}.memories "
        "WHERE namespace = $1 AND search_hit_count > 0 AND timestamp > NOW() - INTERVAL '24 hours'",
        namespace,
    )

    write_active = write_row["cnt"] > 0 if write_row else False
    recall_active = context_row["cnt"] > 0 if context_row else False

    if not write_active:
        logger.warning(f"Agent {namespace} has no writes in last 24h")
    if not recall_active:
        logger.info(f"Agent {namespace} has no context calls in last 24h")
