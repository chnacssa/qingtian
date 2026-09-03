"""执策多签交叉验证 — 一人执行，多人独立验收（§3.4.3 Layer 4）

当 Step 的 acceptance_criteria 标记 require_multisig=true 时：
  1. 执行 Agent 做完 Step 后正常 submit（作为第一票）
  2. 引擎在 submit 完成后检查是否需要多签
  3. 需要时自动创建 verification_tasks 等待其他 Agent 认领
  4. 验证 Agent 通过 claim-verify 认领 → 执行 agent-report 规则 → submit-verify 提交
  5. 所有验证者通过 → Step 标记为 multisig_verified
  6. 任一验证者不通过 → 标记为 multisig_failed → 通知创建者
"""
import json
import logging
from datetime import datetime, timezone
from common.db import get_pool
from . import config as cfg
from .checker import check_all
from .dispatcher import ws_notify

logger = logging.getLogger("zhice.multisig")
SCHEMA = cfg.get_schema_name()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def needs_multisig(acceptance_criteria: list[dict] | None) -> bool:
    """检查 acceptance_criteria 中是否有需要多签的规则。"""
    if not acceptance_criteria:
        return False
    for ac in acceptance_criteria:
        if ac.get("require_multisig"):
            return True
    return False


def get_multisig_count(acceptance_criteria: list[dict] | None) -> int:
    """获取需要的验证者总数（含执行者）。"""
    if not acceptance_criteria:
        return 1
    counts = [ac.get("multisig_count", 2) for ac in acceptance_criteria if ac.get("require_multisig")]
    return max(counts) if counts else 1


# ── verification_tasks 表操作 ────────────────────────────

async def create_verification_tasks(
    conn, step_id: int, task_id: int, required_count: int,
    step_title: str, step_index: int, acceptance_criteria: list[dict],
) -> list[int]:
    """为多签 Step 创建验证子任务，返回 verification_id 列表。"""
    vids = []
    for i in range(required_count - 1):  # -1 因为执行者自己算一票
        vid = await conn.fetchval(
            f"INSERT INTO {SCHEMA}.verifications "
            f"(task_id, step_id, rule_type, check_mode, rule_details, result, notes) "
            f"VALUES ($1, $2, 'all', 'multisig_pending', $3, 'pending', $4) "
            f"RETURNING verification_id",
            task_id, step_id,
            json.dumps(acceptance_criteria, ensure_ascii=False),
            json.dumps({"step_title": step_title, "step_index": step_index}),
        )
        vids.append(vid)
    logger.info("Created %d multisig verification tasks for step=%s", len(vids), step_id)
    return vids


async def count_multisig_passes(conn, step_id: int) -> tuple[int, int]:
    """统计多签通过/失败数。返回 (passed_count, failed_count)。"""
    row = await conn.fetchrow(
        f"SELECT "
        f"COUNT(*) FILTER (WHERE result = 'passed' AND check_mode = 'multisig_verified') AS passed, "
        f"COUNT(*) FILTER (WHERE result = 'failed' AND check_mode = 'multisig_verified') AS failed "
        f"FROM {SCHEMA}.verifications "
        f"WHERE step_id = $1 AND check_mode IN ('multisig_pending', 'multisig_verified', 'multisig_failed')",
        step_id,
    )
    return (row["passed"] or 0, row["failed"] or 0)


async def claim_verification(conn, verification_id: int, agent_id: str) -> dict | None:
    """验证 Agent 认领多签任务。返回 verification 信息或 None。

    P1 (R?): 防多签单票伪造击穿——
      1. 执行者不可自签通过（认领自己执行 Step 的多签任务 → 拒绝）
      2. 同一 Agent 对同一 Step 只算一票（已认领/已投 → 拒绝再次认领）
    """
    # 认领人不得是该 step 的执行者（JOIN steps 取 assigned_agent 比对）
    # 2026-08-28 P1 修复：assigned_agent 存裸 agent_id（runner 直接写 caller），
    # 此前比 f"agent:{agent_id}" 永不相等 → 防自签完全失效。归一化：两种形态都拦。
    row = await conn.fetchrow(
        f"SELECT v.*, s.assigned_agent AS step_executor "
        f"FROM {SCHEMA}.verifications v "
        f"JOIN {SCHEMA}.steps s ON v.step_id = s.step_id "
        f"WHERE v.verification_id = $1 AND v.check_mode = 'multisig_pending' "
        f"  AND (s.assigned_agent IS NULL "
        f"       OR (s.assigned_agent != $2 AND s.assigned_agent != $3)) "
        f"FOR UPDATE OF v",
        verification_id, agent_id, f"agent:{agent_id}",
    )
    if not row:
        return None

    # 同一 Agent 不得对同一 step 重复投票
    already = await conn.fetchval(
        f"SELECT 1 FROM {SCHEMA}.verifications "
        f"WHERE step_id = $1 AND verified_by = $2 "
        f"  AND check_mode IN ('multisig_claimed', 'multisig_verified', 'multisig_failed') "
        f"LIMIT 1",
        row["step_id"], f"agent:{agent_id}",
    )
    if already:
        return None

    await conn.execute(
        f"UPDATE {SCHEMA}.verifications SET check_mode = 'multisig_claimed', "
        f"verified_by = $2, verified_at = NOW() "
        f"WHERE verification_id = $1",
        verification_id, f"agent:{agent_id}",
    )
    rdetails = row["rule_details"]
    if isinstance(rdetails, str):
        rdetails = json.loads(rdetails)
    return {
        "verification_id": verification_id,
        "step_id": row["step_id"],
        "task_id": row["task_id"],
        "acceptance_criteria": rdetails,
        "notes": row["notes"],
    }


async def submit_verification(
    conn, verification_id: int, agent_id: str, check_results: dict,
) -> dict:
    """验证 Agent 提交多签验证结果。引擎比对后返回结果。"""
    row = await conn.fetchrow(
        f"SELECT * FROM {SCHEMA}.verifications "
        f"WHERE verification_id = $1 AND check_mode = 'multisig_claimed' "
        f"AND verified_by = $2",
        verification_id, f"agent:{agent_id}",
    )
    if not row:
        return {"success": False, "error": "验证任务不存在或未被你认领"}

    criteria = row["rule_details"]
    if isinstance(criteria, str):
        criteria = json.loads(criteria)
    elif not isinstance(criteria, list):
        criteria = []

    result = check_all(criteria, check_results)

    passed = result["passed"]
    await conn.execute(
        f"UPDATE {SCHEMA}.verifications SET "
        f"result = $2, actual_value = $3, check_mode = $4, "
        f"verified_at = NOW() "
        f"WHERE verification_id = $1",
        verification_id,
        "passed" if passed else "failed",
        json.dumps(check_results, ensure_ascii=False)[:500],
        "multisig_verified" if passed else "multisig_failed",
    )

    # 统计
    passes, fails = await count_multisig_passes(conn, row["step_id"])
    required = 0
    step = await conn.fetchrow(
        f"SELECT s.acceptance_criteria, s.task_id, s.title, s.step_index, s.assigned_agent "
        f"FROM {SCHEMA}.steps s WHERE s.step_id = $1",
        row["step_id"],
    )
    if step:
        ms_count = get_multisig_count(step["acceptance_criteria"] if isinstance(step["acceptance_criteria"], list) else
                                       json.loads(step["acceptance_criteria"]) if step["acceptance_criteria"] else None)
        required = ms_count - 1  # additional verifiers beyond executor

    # 所有验证者都提交了
    if passes + fails >= required:
        all_clear = fails == 0 and passes >= required
        if all_clear:
            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET outputs = COALESCE(outputs, '{{}}'::jsonb) || "
                f"'{{\"multisig_verified\": true, \"multisig_passes\": {passes}, \"multisig_required\": {required}}}'::jsonb "
                f"WHERE step_id = $1",
                row["step_id"],
            )
            logger.info("Multisig verified for step=%s: %d/%d passed", row["step_id"], passes + 1, required + 1)
        else:
            logger.warning("Multisig failed for step=%s: %d fails", row["step_id"], fails)
            task = await conn.fetchrow(
                f"SELECT created_by FROM {SCHEMA}.tasks WHERE task_id = $1", row["task_id"],
            )
            if task:
                await ws_notify(task["created_by"], "multisig_failed", {
                    "task_id": row["task_id"],
                    "step_id": row["step_id"],
                    "title": step["title"] if step else "",
                    "passes": passes + 1,
                    "fails": fails,
                    "required": required + 1,
                })

    return {
        "success": True,
        "verification_id": verification_id,
        "passed": passed,
        "verification_result": "passed" if passed else "failed",
        "multisig_progress": f"{passes + 1}/{required + 1}",
    }
