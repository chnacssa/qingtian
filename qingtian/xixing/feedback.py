"""
吸星 — 学习反馈追踪

Agent 上报经验后，追踪该经验的后续使用情况，形成"传帮带"闭环：

  Agent A 上报经验 E
    └── 经验 E 入库 → 下游 Agent B 查询并应用
    └── Agent B 调 POST /v1/xixing/feedback 上报"有用/无用/错误"
    └── 反馈关联到经验 E
    └── 通知 Agent A："你的经验被采纳/需要修正"

"""

import logging

from common.db import get_pool
from . import config as xcfg

logger = logging.getLogger("xixing.feedback")

SCHEMA = xcfg.get_schema_name()


async def submit_feedback(
    experience_id: str,
    experience_type: str,
    source_agent: str,
    feedback_agent: str,
    feedback_type: str,
    feedback_detail: str = "",
    task_id: str = "",
) -> dict:
    """提交经验反馈。

    Returns:
        {"status": "ok", "feedback_id": int}
        或 {"status": "error", "error": "..."}
    """
    allowed = ("useful", "useless", "incorrect")
    if feedback_type not in allowed:
        return {"status": "error", "error": f"feedback_type must be one of {allowed}"}

    pool = await get_pool()
    async with pool.acquire() as conn:
        fid = await conn.fetchval(
            f"""INSERT INTO {SCHEMA}.experience_feedback
                (experience_id, experience_type, source_agent, feedback_agent,
                 feedback_type, feedback_detail, task_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id""",
            experience_id, experience_type, source_agent, feedback_agent,
            feedback_type, feedback_detail, task_id,
        )

    # 通知经验上报者（仅当上报者在线时；检查接收方 source_agent 的 active 状态）
    try:
        from common.bus import bus
        from huanyu.config import get_schema_name as hy_schema
        from common.db import get_pool as _pool
        _p = await _pool()
        async with _p.acquire() as _c:
            row = await _c.fetchval(
                f"SELECT 1 FROM {hy_schema()}.agents WHERE agent_id = $1 AND status = 'active'",
                source_agent,
            )
        if row:
            await bus.publish(source_agent, {
                "type": "experience_feedback",
                "source": "xixing",
                "payload": {
                    "experience_id": experience_id,
                    "feedback_type": feedback_type,
                    "feedback_agent": feedback_agent,
                    "feedback_detail": feedback_detail,
                    "task_id": task_id,
                },
            })
    except Exception:
        pass

    logger.info(
        "Feedback: %s → %s on %s (%s)",
        feedback_agent, feedback_type, experience_id, source_agent,
    )
    return {"status": "ok", "feedback_id": fid}


async def get_feedback_for_experience(
    experience_id: str,
    limit: int = 50,
) -> list[dict]:
    """查询某条经验收到的所有反馈"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.experience_feedback "
            "WHERE experience_id = $1 ORDER BY created_at DESC LIMIT $2",
            experience_id, limit,
        )
    return [dict(r) for r in rows]


async def get_feedback_summary_for_agent(
    agent_id: str,
    as_source: bool = True,
    days: int = 30,
) -> dict:
    """查询 Agent 的经验采纳统计

    Args:
        agent_id: 目标 Agent
        as_source: True=该 Agent 作为经验上报者收到的反馈；False=该 Agent 作为使用者给出的反馈
        days: 统计周期（天）

    Returns:
        {"total": int, "useful": int, "useless": int, "incorrect": int, "experiences": int}
    """
    pool = await get_pool()
    column = "source_agent" if as_source else "feedback_agent"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT feedback_type, COUNT(*) as cnt FROM {SCHEMA}.experience_feedback "
            f"WHERE {column} = $1 AND created_at > NOW() - make_interval(days => $2) "
            "GROUP BY feedback_type",
            agent_id, days,
        )
        exp_count = await conn.fetchval(
            f"SELECT COUNT(DISTINCT experience_id) FROM {SCHEMA}.experience_feedback "
            f"WHERE {column} = $1 AND created_at > NOW() - make_interval(days => $2)",
            agent_id, days,
        )

    counts = {"useful": 0, "useless": 0, "incorrect": 0}
    for row in rows:
        counts[row["feedback_type"]] = row["cnt"]

    return {
        "total": sum(counts.values()),
        **counts,
        "experiences": exp_count or 0,
    }
