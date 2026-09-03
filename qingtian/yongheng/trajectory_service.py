"""轨迹服务 —— 读写行动轨迹。"""

import json
import uuid
from datetime import datetime, timezone

import asyncpg

from . import config as cfg


async def add_action(conn: asyncpg.Connection, namespace: str, action: dict) -> dict:
    if "agent_id" not in action or not action["agent_id"]:
        parts = namespace.split(":", 1)
        action["agent_id"] = parts[1] if len(parts) > 1 else ""

    # 稳定 action_id（供 recorder 批量标记 processed 去重用）
    action.setdefault("id", uuid.uuid4().hex[:16])

    # P2 (R11): 轨迹按天分区，统一用 UTC 日，与 memories.timestamp(UTC) 的"当天"口径一致
    today = datetime.now(timezone.utc).date()
    schema = cfg.get_schema_name()

    existing = await conn.fetchrow(
        f"SELECT id, actions FROM {schema}.trajectories WHERE namespace = $1 AND date = $2",
        namespace, today,
    )

    action_json = json.dumps(action)

    if existing:
        await conn.execute(
            f"UPDATE {schema}.trajectories SET actions = actions || $1::jsonb, updated_at = NOW() "
            "WHERE namespace = $2 AND date = $3",
            action_json, namespace, today,
        )
    else:
        await conn.execute(
            f"INSERT INTO {schema}.trajectories (namespace, date, actions) VALUES ($1, $2, $3::jsonb)",
            namespace, today, json.dumps([action]),
        )

    return {"status": "ok", "namespace": namespace, "date": str(today), "action": action}


async def get_trajectory(conn: asyncpg.Connection, namespace: str,
                         date_str: str | None = None,
                         page_size: int = 20, page_token: str | None = None) -> dict:
    if date_str is None:
        # P2 (R11): 统一 UTC 日，避免与 add_action 的写入日不一致
        target_date = datetime.now(timezone.utc).date()
    else:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date() if isinstance(date_str, str) else date_str

    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT actions, summary FROM {schema}.trajectories WHERE namespace = $1 AND date = $2",
        namespace, target_date,
    )

    if not row:
        return {
            "status": "ok",
            "namespace": namespace,
            "date": str(target_date),
            "actions": [],
            "summary": "",
            "next_page_token": None,
        }

    actions = row["actions"]
    if isinstance(actions, str):
        actions = json.loads(actions)

    actions.sort(key=lambda a: a.get("time", ""))

    start = int(page_token) if page_token else 0
    end = start + page_size
    page_actions = actions[start:end]
    next_token = str(end) if end < len(actions) else None

    return {
        "status": "ok",
        "namespace": namespace,
        "date": str(target_date),
        "actions": page_actions,
        "summary": row["summary"] or "",
        "next_page_token": next_token,
    }


async def mark_processed(conn: asyncpg.Connection, namespace: str,
                         date_str: str | None, action_ids: list[str]) -> None:
    """将指定 action_ids 的轨迹标记为已处理（recorder 去重）。

    在 trajectories.actions JSONB 数组内对匹配 id 的 action 追加 {"processed": true}。
    仅对 id 精确匹配的元素生效，未匹配项保持原样。
    """
    if not action_ids:
        return

    if date_str is None:
        # P2 (R11): 统一 UTC 日，与 add_action 的写入日一致
        target_date = datetime.now(timezone.utc).date()
    else:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()

    schema = cfg.get_schema_name()
    await conn.execute(
        f"""UPDATE {schema}.trajectories
            SET actions = (
                SELECT jsonb_agg(
                    CASE WHEN a->>'id' = ANY($3) THEN a || '{{"processed": true}}'::jsonb ELSE a END
                )
                FROM jsonb_array_elements(actions) AS a
            ), updated_at = NOW()
            WHERE namespace = $1 AND date = $2""",
        namespace, target_date, action_ids,
    )
