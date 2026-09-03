"""永恒 — Hook 事件摄入：OpenClaw 生命周期 hook → trajectory + memory 自动分流。

分层策略:
  - message/tool/llm 事件 → trajectory（时序日志，不建 embedding，不参与搜索）
  - agent_end / compact:after → memory（精炼后进 memories 表，可语义搜索）
  - 去重: agent_id + session_id + event + content_hash
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from . import config as cfg

logger = logging.getLogger("yongheng.hook_ingest")

# 路由规则: event_type → (write_target, mem_type)
# write_target: "trajectory" | "memory" | "both"
EVENT_ROUTES: dict[str, tuple[str, Optional[str], int]] = {
    "message:received":      ("trajectory", None, 0),
    "message:sent":          ("trajectory", None, 0),
    "message:transcribed":   ("trajectory", None, 0),
    "llm_input":             ("trajectory", None, 0),
    "llm_output":            ("trajectory", None, 0),
    "before_tool_call":      ("trajectory", None, 0),
    "tool:result":           ("trajectory", None, 0),
    "agent_end":             ("both", "episodic", 200),      # ≥200 字才写 memory
    "session:create":        ("trajectory", None, 0),
    "session:compact:after": ("memory", "consolidated", 100),
    "gateway:startup":       ("trajectory", None, 0),
    "agent:bootstrap":       ("trajectory", None, 0),
}
DEFAULT_TARGET = "trajectory"
DEFAULT_MIN_CHARS = 0


def _route(event_type: str) -> tuple[str, Optional[str], int]:
    return EVENT_ROUTES.get(event_type, (DEFAULT_TARGET, None, DEFAULT_MIN_CHARS))


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _dedup_key(event: dict) -> str:
    agent_id = event.get("agent_id", "unknown")
    session_id = event.get("session_id", "none")
    event_type = event.get("event", "unknown")
    content = event.get("content", "")[:200]
    return f"{agent_id}:{session_id}:{event_type}:{_content_hash(content)}"


async def _write_trajectory(conn: asyncpg.Connection, event: dict) -> str:
    """写入轨迹表（按天聚合 JSONB）。"""
    agent_id = event.get("agent_id", "unknown")
    namespace = event.get("namespace", f"agent:{agent_id}")
    # P2 (R11): 统一 UTC 日，与 trajectory_service.add_action 的写入日口径一致
    today = datetime.now(timezone.utc).date()

    action = {
        "id": uuid.uuid4().hex[:16],  # 稳定 action_id（供 recorder 批量标记 processed）
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "event": event.get("event", "unknown"),
        "content": event.get("content", "")[:500],
        "tool_name": event.get("tool_name", ""),
        "tool_result": event.get("tool_result", "")[:200] if event.get("tool_result") else "",
        "session_id": event.get("session_id", ""),
    }

    schema = cfg.get_schema_name()
    existing = await conn.fetchrow(
        f"SELECT id FROM {schema}.trajectories WHERE namespace = $1 AND date = $2",
        namespace, today,
    )

    if existing:
        await conn.execute(
            f"UPDATE {schema}.trajectories SET actions = actions || $1::jsonb, updated_at = NOW() "
            "WHERE namespace = $2 AND date = $3",
            json.dumps(action), namespace, today,
        )
    else:
        await conn.execute(
            f"INSERT INTO {schema}.trajectories (namespace, date, actions) VALUES ($1, $2, $3::jsonb)",
            namespace, today, json.dumps([action]),
        )

    return "trajectory"


async def _write_memory(conn: asyncpg.Connection, event: dict, mem_type: str) -> str:
    """写入记忆表（会触发 embedding 队列）。"""
    agent_id = event.get("agent_id", "unknown")
    namespace = event.get("namespace", f"agent:{agent_id}")
    content = event.get("content", "")

    from .memory_service import write_memory

    await write_memory(
        conn,
        namespace=namespace,
        content=content,
        mem_type=mem_type,
        source="openclaw-hook",
        metadata={
            "event": event.get("event", ""),
            "session_id": event.get("session_id", ""),
            "hook_captured": True,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return "memory"


async def ingest_hook_event(conn: asyncpg.Connection, event: dict) -> dict:
    """摄入单个 Hook 事件，自动分流到 trajectory / memory。

    Args:
        conn: asyncpg connection
        event: {"event": "message:received", "agent_id": "...",
                "session_id": "...", "content": "...", ...}

    Returns:
        {"status": "ok", "routed_to": ["trajectory"], "event": "message:received"}
    """
    event_type = event.get("event", "")
    target, mem_type, min_chars = _route(event_type)
    content = event.get("content", "")

    written: list[str] = []

    if target in ("trajectory", "both"):
        await _write_trajectory(conn, event)
        written.append("trajectory")

    if target in ("memory", "both") and mem_type:
        if len(content.strip()) >= min_chars:
            await _write_memory(conn, event, mem_type)
            written.append("memory")
        else:
            written.append(f"memory_skipped(<{min_chars}chars)")

    return {
        "status": "ok",
        "routed_to": written,
        "event": event_type,
        "agent_id": event.get("agent_id", "unknown"),
    }


async def ingest_batch(conn: asyncpg.Connection, events: list[dict]) -> dict:
    """批量摄入 Hook 事件，带去重。

    使用内存 set 做同批次去重（dedup_key），跨批次依赖 trajectory 表 UNIQUE 约束。
    """
    seen: set[str] = set()
    results: list[dict] = []
    trajectories = 0
    memories = 0
    skipped = 0

    for event in events:
        dkey = _dedup_key(event)
        if dkey in seen:
            skipped += 1
            results.append({"event": event.get("event", ""), "status": "duplicate"})
            continue
        seen.add(dkey)

        try:
            r = await ingest_hook_event(conn, event)
            results.append(r)
            if "trajectory" in r.get("routed_to", []):
                trajectories += 1
            if "memory" in r.get("routed_to", []):
                memories += 1
        except Exception as e:
            logger.error(f"Hook ingest failed for {event.get('event')}: {e}")
            results.append({"event": event.get("event", ""), "status": "error", "error": str(e)})

    return {
        "total": len(events),
        "trajectories": trajectories,
        "memories": memories,
        "skipped": skipped,
        "status": "completed",
        "results": results,
    }
