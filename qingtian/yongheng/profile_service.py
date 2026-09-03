"""画像服务 —— Agent 画像读写 + learned 整理。"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

from .models import AppError
from . import config as cfg

logger = logging.getLogger("yongheng")


def _as_json(value: Any) -> Any:
    """asyncpg 读 jsonb：对象→dict、数组→list 已可直接用；仅字符串时再 json.loads。

    之前的 isinstance(value, dict/list) 单类型判断对 jsonb 数组（Python list）会误入
    json.loads 分支 → TypeError。统一判断 (dict, list) 兜住数组列。
    """
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _coerce_to_dict(value: Any) -> dict[str, Any]:
    """把 jsonb 值规整为 dict（traits/state 语义是对象）。

    设计意图：update_profile 顶层键合并、ProfileResponse.traits/state 声明 dict。
    线上历史数据可能因 jsonb `||` 混合拼接被写成数组 → 逐元素归并：
    dict 元素直接合并，JSON 字符串元素先解析再合并，其余忽略。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        merged: dict[str, Any] = {}
        for item in value:
            if isinstance(item, dict):
                merged.update(item)
            elif isinstance(item, str):
                try:
                    parsed = json.loads(item)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    merged.update(parsed)
        return merged
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass
    return {}


async def get_profile(conn: asyncpg.Connection, namespace: str) -> dict:
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(
        f"SELECT namespace, traits, learned, state, updated_at FROM {schema}.profiles WHERE namespace = $1",
        namespace,
    )

    parts = namespace.split(":", 1)
    agent_id = parts[1] if len(parts) > 1 else ""

    timeline_rows = await conn.fetch(
        f"SELECT target_date, timeline_entry, type FROM {schema}.digests "
        "WHERE namespace = $1 AND target_date >= CURRENT_DATE - INTERVAL '7 days' "
        "ORDER BY target_date DESC",
        namespace,
    )
    timeline = [
        {"date": str(r["target_date"]), "digest": r["timeline_entry"] or "", "type": r["type"] or "normal"}
        for r in timeline_rows
    ]

    if not row:
        return {
            "namespace": namespace,
            "agent_id": agent_id,
            "traits": {},
            "learned": [],
            "state": {},
            "timeline_index": timeline,
            "updated_at": datetime.now(timezone.utc),
        }

    traits = _coerce_to_dict(row["traits"])
    learned = _as_json(row["learned"])
    state = _coerce_to_dict(row["state"])

    return {
        "namespace": row["namespace"],
        "agent_id": agent_id,
        "traits": traits,
        "learned": learned,
        "state": state,
        "timeline_index": timeline,
        "updated_at": row["updated_at"],
    }


async def update_profile(conn: asyncpg.Connection, namespace: str,
                         traits: dict | None = None,
                         learned_add: list[dict] | None = None,
                         learned_override: list[dict] | None = None,
                         state: dict | None = None) -> dict:
    schema = cfg.get_schema_name()
    existing = await conn.fetchrow(
        f"SELECT namespace, traits, learned, state, updated_at FROM {schema}.profiles WHERE namespace = $1",
        namespace,
    )

    if not existing:
        await conn.execute(
            f"INSERT INTO {schema}.profiles (namespace, updated_at) VALUES ($1, NOW())",
            namespace,
        )

    if traits is not None:
        # 顶层键合并（|| jsonb 合并），避免 persona/recommender/reminder/daily_brief
        # 各写单键 traits 时互相清空对方数据。原子执行，无读改写竞态。
        # review(2026-08-15): traits 是 JSONB 列，直接传 dict（避免双重编码 + string||object 崩溃）
        await conn.execute(
            f"UPDATE {schema}.profiles SET traits = COALESCE(traits, '{{}}'::jsonb) || $1::jsonb, "
            "updated_at = NOW() WHERE namespace = $2",
            traits, namespace,
        )

    if learned_override is not None:
        # review(2026-08-15): learned 是 JSONB 列，直接传 list
        await conn.execute(
            f"UPDATE {schema}.profiles SET learned = $1::jsonb, updated_at = NOW() WHERE namespace = $2",
            learned_override, namespace,
        )
    elif learned_add is not None:
        now = datetime.now(timezone.utc).isoformat()
        for item in learned_add:
            item.setdefault("first_observed", now)
            item.setdefault("last_confirmed", now)
            item.setdefault("confidence", 0.5)
            item.setdefault("confirmations", 1)
            item.setdefault("contradictions", 0)

        existing_learned = []
        if existing:
            existing_learned = _as_json(existing["learned"])

        new_learned = existing_learned + learned_add
        # review(2026-08-15): learned 是 JSONB 列，直接传 list
        await conn.execute(
            f"UPDATE {schema}.profiles SET learned = $1::jsonb, updated_at = NOW() WHERE namespace = $2",
            new_learned, namespace,
        )

    if state is not None:
        # review(2026-08-15): state 是 JSONB 列，直接传 dict
        await conn.execute(
            f"UPDATE {schema}.profiles SET state = $1::jsonb, updated_at = NOW() WHERE namespace = $2",
            state, namespace,
        )

    return await get_profile(conn, namespace)


async def consolidate_learned(conn: asyncpg.Connection, namespace: str):
    schema = cfg.get_schema_name()
    row = await conn.fetchrow(f"SELECT learned FROM {schema}.profiles WHERE namespace = $1", namespace)
    if not row:
        return

    learned = _as_json(row["learned"])
    if not learned:
        return

    changed = False
    filtered = [item for item in learned if item.get("confidence", 0) >= cfg.get_learned_min_confidence()]
    if len(filtered) != len(learned):
        changed = True
        learned = filtered

    deduped = _deduplicate_learned(learned)
    if len(deduped) != len(learned):
        changed = True
        learned = deduped

    if len(learned) > cfg.get_learned_max_items():
        learned = await _llm_merge_learned(learned)
        changed = True

    if changed:
        # review(2026-08-15): learned 是 JSONB 列，直接传 list
        await conn.execute(
            f"UPDATE {schema}.profiles SET learned = $1::jsonb, updated_at = NOW() WHERE namespace = $2",
            learned, namespace,
        )


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        return _levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,
                curr[j] + 1,
                prev[j] + (ca != cb),
            ))
        prev = curr
    return prev[-1]


def _deduplicate_learned(learned: list[dict]) -> list[dict]:
    threshold = cfg.get_learned_duplicate_threshold()
    result = []
    for item in learned:
        dup_idx = None
        item_pref = item.get("preference", "")
        for i, existing in enumerate(result):
            existing_pref = existing.get("preference", "")
            # P1-2（9-1 修复日）：编辑距离下界 = 长度差 —— 长度差已超阈值时
            # Levenshtein 必超阈值，跳过 O(n×m) DP。此前任意两条长 preference
            # 都做全量比对（同步跑在事件循环内），可挂起整个 1996 服务。
            if abs(len(item_pref) - len(existing_pref)) > threshold:
                continue
            if _levenshtein(item_pref, existing_pref) <= threshold:
                dup_idx = i
                break
        if dup_idx is not None:
            if item.get("confirmations", 0) > result[dup_idx].get("confirmations", 0):
                result[dup_idx] = item
        else:
            result.append(item)
    return result


async def _llm_merge_learned(learned: list[dict]) -> list[dict]:
    import httpx

    items_text = "\n".join(f"- {item.get('preference', '')} (confidence={item.get('confidence', 0)})" for item in learned)
    prompt = (
        "以下是 Agent 学到的用户偏好列表。请合并同类项，去除冗余，精简后输出。"
        "保留每条偏好的核心意思。输出 JSON 数组，每项含 preference 和 confidence 字段。\n\n"
        f"{items_text}"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{cfg.get_llm_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.get_llm_api_key()}"},
                json={
                    "model": cfg.get_llm_digest_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            merged = json.loads(text)
            if isinstance(merged, list):
                return merged
            if isinstance(merged, dict) and "preferences" in merged:
                return merged["preferences"]
            return learned
    except Exception as e:
        logger.warning(f"LLM merge learned failed: {e}")
        return learned
