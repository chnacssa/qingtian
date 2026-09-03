"""
运行时安全审计 — 羲和 ↔ 镇岳联动

为羲和提供事件上报与风险查询接口。
用于 Skill 进程的运行时行为监控与决策。

设计文档：docs/羲和-镇岳运行时安全审计-设计.md

用法:
    # 羲和上报事件
    await report_event(
        agent_id=child.agent_id,
        skill_name=child.skill_name,
        event_type="egress_anomaly",
        severity="high",
        detail={"pid": pid, "remote_addr": "..."},
    )

    # 羲和查询风险分
    score = await get_risk_score(
        agent_id=child.agent_id,
        skill_name=child.skill_name,
    )
    # {"score": 45, "events": 3, "recommendation": "monitor"}
"""

import json
import logging
from typing import Any

from common.db import get_pool
from . import config as cfg

logger = logging.getLogger("zhenyue.audit_runtime")

SCHEMA = cfg.get_schema_name()

# ── 风险等级分值 ──
SEVERITY_SCORES = {
    "critical": 50,
    "high": 30,
    "medium": 15,
    "low": 5,
}

MAX_RISK_SCORE = 100
RISK_WINDOW_DAYS = 7


async def ensure_table():
    """确保运行时审计事件表存在"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"""
            CREATE SCHEMA IF NOT EXISTS {SCHEMA};

            CREATE TABLE IF NOT EXISTS {SCHEMA}.runtime_audit_events (
                id          BIGSERIAL PRIMARY KEY,
                agent_id    VARCHAR(64) NOT NULL,
                skill_name  VARCHAR(64) NOT NULL,
                event_type  VARCHAR(64) NOT NULL,
                severity    VARCHAR(16) NOT NULL,
                detail      JSONB DEFAULT '{{}}',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_zt_rt_audit_lookup
                ON {SCHEMA}.runtime_audit_events (agent_id, skill_name, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_zt_rt_audit_cleanup
                ON {SCHEMA}.runtime_audit_events (created_at);
        """)


async def report_event(
    agent_id: str,
    skill_name: str,
    event_type: str,
    severity: str = "medium",
    detail: dict | None = None,
) -> dict:
    """上报运行时安全事件

    Args:
        agent_id: Agent ID
        skill_name: Skill 名称
        event_type: 事件类型
            egress_anomaly / mass_read / privilege_escalation / ...
        severity: critical / high / medium / low
        detail: 事件详情（PID、远程地址、字节数等）

    Returns:
        {"id": event_id, "created_at": iso_timestamp}
    """
    # P2 (R11): report_event 此前不调 ensure_table → 建表前首次上报即失败
    # （relation does not exist）。ensure_table 幂等（CREATE TABLE IF NOT EXISTS），
    # 每次上报前确保表存在，与 get_risk_score 保持同一致。
    await ensure_table()

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.runtime_audit_events
               (agent_id, skill_name, event_type, severity, detail)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id, created_at""",
            agent_id, skill_name, event_type, severity,
            json.dumps(detail or {}),
        )

    logger.info(
        "Runtime event recorded: %s/%s %s (severity=%s)",
        agent_id, skill_name, event_type, severity,
    )

    return {
        "id": row["id"],
        "created_at": row["created_at"].isoformat(),
    }


async def get_risk_score(
    agent_id: str,
    skill_name: str,
) -> dict:
    """查询 Skill 的风险评分

    基于近 7 天的事件计算：
    - 每个事件按 severity 加权（low=5, medium=15, high=30, critical=50）
    - 滑动窗口（7 天），无显式衰减
    - 上限 100 分

    Args:
        agent_id: Agent ID
        skill_name: Skill 名称

    Returns:
        {
            "score": 0-100,
            "events": 总事件数,
            "recommendation": "ok" | "monitor" | "warn" | "revoke"
        }
    """
    await ensure_table()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT severity, COUNT(*) as cnt
               FROM {SCHEMA}.runtime_audit_events
               WHERE agent_id = $1
                 AND skill_name = $2
                 AND created_at > NOW() - INTERVAL '{RISK_WINDOW_DAYS} days'
               GROUP BY severity""",
            agent_id, skill_name,
        )

    total_score = 0
    total_events = 0
    for row in rows:
        points = SEVERITY_SCORES.get(row["severity"], 5)
        total_score += points * row["cnt"]
        total_events += row["cnt"]

    score = min(total_score, MAX_RISK_SCORE)

    # 推荐决策
    if score > 80:
        recommendation = "revoke"
    elif score > 50:
        recommendation = "warn"
    elif score > 20:
        recommendation = "monitor"
    else:
        recommendation = "ok"

    return {
        "score": score,
        "events": total_events,
        "recommendation": recommendation,
    }
