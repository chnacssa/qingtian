"""
吸星 — 踩坑自动记录

触发源：
  - API 异常处理中调用 capture(exception, context)
  - Agent 通过 API 主动上报
  - 从 zhenyue audit_log 中检测 critical 事件自动录入

可选 LLM 分析：根因定位 + 修复建议
"""

import json
import logging
import os
import traceback
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from . import config as cfg

logger = logging.getLogger("xixing.xizhenji")

SCHEMA = cfg.get_schema_name()


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符：%/_ 及转义符本身，防止 audit_uid 等含通配符时误匹配。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def capture(
    exception: Exception,
    context: dict | None = None,
    severity: str = "medium",
    related_agent: str | None = None,
    tags: list[str] | None = None,
) -> int:
    """自动捕获异常并记录为踩坑。"""
    if not cfg.get_xizhenji_auto_capture():
        return 0

    ctx = context or {}
    title = f"{type(exception).__name__}: {str(exception)[:100]}"
    description = (
        f"## 异常\n{type(exception).__name__}: {exception}\n\n"
        f"## 上下文\n{json.dumps(ctx, ensure_ascii=False, indent=2)}\n\n"
        f"## 堆栈\n```\n{''.join(traceback.format_tb(exception.__traceback__))}\n```"
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        xz_id = await conn.fetchval(
            f"""INSERT INTO {SCHEMA}.xizhenji (title, description, severity, source, category, related_agent, tags, learned_at)
                VALUES ($1, $2, $3, 'auto-capture', 'auto_capture', $4, $5, NOW()) RETURNING id""",
            title, description[:8000], severity, related_agent, tags or [],
        )

    # 严重事件触发 LLM 分析
    if severity in ("high", "critical") and cfg.get_xizhenji_llm_severity() in ("high", "critical"):
        await _llm_analyze(xz_id, title, description)

    return xz_id


async def _llm_analyze(xz_id: int, title: str, description: str):
    """LLM 根因分析 + 修复建议。"""
    import httpx

    from common.config import default_llm_model as cfg_default_llm_model

    api_key = cfg.get_deepseek_key()  # 语义=当前主 LLM key（2026-08-27 切智谱后 ZHIPU 优先）
    if not api_key:
        return

    prompt = (
        "分析以下系统异常，给出根因和修复建议。仅回复 JSON：\n"
        '{"root_cause": "根因分析", "solution": "修复建议"}\n\n'
        f"## 标题\n{title}\n\n"
        f"## 详情\n{description[:3000]}\n"
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{cfg.get_deepseek_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    # 2026-08-27：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序（智谱优先）
                    "model": cfg_default_llm_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    # 2026-08-27 切智谱：去 response_format（glm json_object 兼容性不保，
                    # 容错靠下方 json.loads 的 try）；max_tokens 500→4096（glm 思考强制
                    # 开启且计入额度，小预算思考没完正文即空）；超时 45→60 同步放宽。
                    "max_tokens": 4096,
                    "temperature": 0.3,
                },
            )
            if resp.is_success:
                result = resp.json()
                analysis = json.loads(result["choices"][0]["message"]["content"])
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        f"UPDATE {SCHEMA}.xizhenji SET root_cause=$1, solution=$2 WHERE id=$3",
                        analysis.get("root_cause", ""), analysis.get("solution", ""), xz_id,
                    )
    except Exception as e:
        logger.warning("LLM analysis failed for xizhenji #%s: %s", xz_id, e)


async def detect_from_audit_log(days: int = 1) -> int:
    """从 zhenyue audit_log 中检测 critical 事件，自动录入踩坑。"""
    try:
        from zhenyue import config as zcfg
        zt_schema = zcfg.get_schema_name()

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT audit_uid, agent_id, action, severity, created_at
                    FROM {zt_schema}.audit_log
                    WHERE severity = 'critical'
                    AND action NOT LIKE 'tool:%'
                    AND created_at > NOW() - INTERVAL '1 day' * $1
                    ORDER BY created_at DESC""",
                days,
            )

            captured = 0
            for row in rows:
                # P2 (R11): audit_uid 含 %/_ 时按 LIKE 通配符展开会误匹配，
                # 转义后精确匹配该 uid 文本。
                audit_uid = str(row["audit_uid"])
                existing = await conn.fetchval(
                    f"SELECT id FROM {SCHEMA}.xizhenji WHERE description LIKE $1 ESCAPE '\\'",
                    f"%{_escape_like(audit_uid)}%",
                )
                if existing:
                    continue

                await conn.execute(
                    f"""INSERT INTO {SCHEMA}.xizhenji (title, description, severity, source, category, related_agent, tags)
                        VALUES ($1, $2, 'critical', 'audit-log', 'zhenyue_block', $3, $4)""",
                    f"Critical: {row['action']}",
                    f"审计事件 {row['audit_uid']}: agent={row['agent_id']} action={row['action']} at={row['created_at']}",
                    row["agent_id"],
                    [row["action"]],
                )
                captured += 1

            return captured
    except Exception as e:
        logger.error("Failed to detect from audit log: %s", e)
        return 0
