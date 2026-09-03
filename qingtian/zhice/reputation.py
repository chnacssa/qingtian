"""执策 C-Level 信誉联动 — 抽查计数 + 自动降级/升级（§3.4.3）

连续抽查不通过 ≥ 3 次 → 调镇岳降级
连续抽查通过 ≥ 5 次 → 调镇岳升级（恢复信誉）
"""
import logging
import httpx
from . import config as cfg

logger = logging.getLogger("zhice.reputation")

_BASE_URL = cfg.get_zhenyue_base_url()

DOWNGRADE_FAILURE_THRESHOLD = 3   # 连续不通过多少次触发降级
UPGRADE_PASS_THRESHOLD = 5        # 连续通过多少次触发升级


async def _call_zhenyue(action: str, agent_id: str, reason: str) -> bool:
    """调用镇岳信誉调整 API，返回是否成功。"""
    url = f"{_BASE_URL}/v1/zhenyue/agents/{agent_id}/{action}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={"reason": reason},
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                logger.info("Zhenyue %s succeeded for agent=%s: %s", action, agent_id, reason)
                return True
            else:
                body = await resp.text()
                logger.warning("Zhenyue %s failed for agent=%s: HTTP %s %s",
                              action, agent_id, resp.status_code, body[:200])
                return False
    except Exception:
        logger.exception("Zhenyue %s error for agent=%s", action, agent_id)
        return False


async def record_reverify_result(
    conn, agent_id: str, step_id: int, task_id: int, passed: bool,
) -> dict:
    """记录一次抽查结果到 verifications 表。

    返回:
      {"action": "downgraded"|"upgraded"|"none", "consecutive": int}
    """
    schema = cfg.get_schema_name()

    # 写入 verifications 表标记此次抽查
    await conn.execute(
        f"INSERT INTO {schema}.verifications "
        f"(task_id, step_id, rule_type, check_mode, rule_details, result, "
        f"actual_value, verified_by) "
        f"VALUES ($1, $2, 'all', 'reverify', '{{}}', $3, $4, 'engine')",
        task_id, step_id,
        "passed" if passed else "failed",
        f"reverify_{'pass' if passed else 'fail'}",
    )

    # 统计最近连续抽查结果
    rows = await conn.fetch(
        f"SELECT result, verified_at FROM {schema}.verifications "
        f"WHERE check_mode = 'reverify' AND task_id IN ("
        f"  SELECT task_id FROM {schema}.steps WHERE assigned_agent = $1"
        f") "
        f"ORDER BY verified_at DESC LIMIT {max(DOWNGRADE_FAILURE_THRESHOLD, UPGRADE_PASS_THRESHOLD)}",
        agent_id,
    )

    consecutive_fails = 0
    consecutive_passes = 0
    for r in rows:
        if r["result"] == "failed":
            consecutive_fails += 1
            if consecutive_passes == 0:
                pass  # still counting fails
            else:
                break
        else:
            if consecutive_fails == 0:
                consecutive_passes += 1
            else:
                break

    # ── 降级判定 ──
    if not passed and consecutive_fails >= DOWNGRADE_FAILURE_THRESHOLD:
        ok = await _call_zhenyue("downgrade", agent_id,
                                 f"抽查连续失败 {consecutive_fails} 次，自动降级")
        return {"action": "downgraded" if ok else "downgrade_failed",
                "consecutive": consecutive_fails}

    # ── 升级判定 ──
    if passed and consecutive_passes >= UPGRADE_PASS_THRESHOLD:
        ok = await _call_zhenyue("upgrade", agent_id,
                                 f"抽查连续通过 {consecutive_passes} 次，自动恢复信誉")
        return {"action": "upgraded" if ok else "upgrade_failed",
                "consecutive": consecutive_passes}

    return {"action": "none", "consecutive": consecutive_fails if not passed else consecutive_passes}
