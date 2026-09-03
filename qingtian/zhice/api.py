"""执策 API — REST 端点

Phase 1 端点：
  POST   /v1/zhice/tasks                     — 创建任务（统一入口，支持 LLM 自动分解）
  GET    /v1/zhice/tasks                     — 查询任务列表
  GET    /v1/zhice/tasks/{task_id}           — 任务详情
  GET    /v1/zhice/tasks/{task_id}/next      — 获取下一步（原子分配）
  POST   /v1/zhice/tasks/{task_id}/cancel    — 取消任务
  POST   /v1/zhice/steps/{step_id}/start     — 确认开始执行
  POST   /v1/zhice/steps/{step_id}/heartbeat — 心跳（含 status_reason）
  POST   /v1/zhice/steps/{step_id}/submit    — 提交结果 + 触发检查
  POST   /v1/zhice/steps/{step_id}/issue     — 报告问题

创建任务三种模式：
  1. 传 steps → 使用调用方提供的步骤
  2. 传 workflow_id → 展开预定义 Workflow 模板
  3. 两者皆空 → LLM 自动分解（根据 title + description）
"""
import asyncio
import httpx
import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from common.db import get_pool
from . import config as cfg
from . import status_machine as sm
from . import runner
from . import checker
from . import dispatcher
from . import extractor
from zhenyue.auth import auth_dependency
from .models import (
    AppError,
    CreateTaskRequest,
    StartStepRequest,
    HeartbeatRequest,
    SubmitRequest,
    IssueRequest,
    AssignStepRequest,
    PauseTaskRequest,
    ReviewRequest,
    CreateWorkflowRequest,
    UpdateWorkflowRequest,
    RejectStepRequest,
    ConfirmStepRequest,
    CreatePolicyRequest,
    UpdatePolicyRequest,
    PolicyCheckRequest,
    TaskResponse,
    StepResponse,
    NextStepResponse,
    SubmitResponse,
    TaskListResponse,
)
from .multisig import (
    claim_verification,
    create_verification_tasks,
    get_multisig_count,
    needs_multisig,
    submit_verification,
)
from .policy_service import (
    create_policy as policy_create,
    delete_policy as policy_delete,
    get_policy as policy_get,
    list_policies as policy_list,
    update_policy as policy_update,
    policy_check,
)
from .reputation import record_reverify_result
from .signing import verify_signature
from .xixing_client import report_pitfall

logger = logging.getLogger("zhice.api")
router = APIRouter(prefix="/v1/zhice", tags=["执策"])
SCHEMA = cfg.get_schema_name()

# 已知 Agent ID 平台前缀（submit 权限校验时剥离后比较）
_AGENT_ID_PREFIXES = ("feishu:", "dingtalk:", "wechat:", "slack:", "discord:")

def _normalize_agent_id(aid: str) -> str:
    """剥离平台前缀后归一化比较。"""
    lowered = aid.strip().lower()
    for prefix in _AGENT_ID_PREFIXES:
        if lowered.startswith(prefix):
            return lowered[len(prefix):]
    return lowered


def _require_daemon_agent(auth: dict, claimed_agent_id: str) -> None:
    """daemon 消费端点归属断言（P0-1 9-2，与 Bearer 层配套使用）。

    端点先经 Depends(auth_dependency) 完成 fail-closed 认证（缺头/无效
    token → 401；内部 IPC → internal-ipc/admin），本助手再校验
    认证身份与请求自报 agent_id 一致（前缀归一化口径同 assigned 比对），
    防"持 A 的 token 自报 B 抢/交步骤"。admin 与 internal-ipc 放行
    （管理监控通道，与 ws 握手 verify_ws_connection 同口径）。
    """
    if auth.get("role") == "admin" or auth.get("agent_id") == "internal-ipc":
        return
    if _normalize_agent_id(auth.get("agent_id", "")) != _normalize_agent_id(claimed_agent_id):
        logger.warning(
            "[trace] daemon_auth fail: token_agent=%s claimed=%s",
            auth.get("agent_id", ""), claimed_agent_id,
        )
        raise HTTPException(
            403,
            f"认证身份与自报 agent_id 不符：token={auth.get('agent_id', '')}，claimed={claimed_agent_id}",
        )


def _effective_executor(step: dict, caller: str) -> str:
    """前缀归一化等效通过后，返回用于 DB 精确匹配的执行者值。

    R11 (P?): 调用方可为 "ou_abc" 而 assigned_agent 存为 "feishu:ou_abc"——
    归一化比较相等，但 status_machine 的 SQL 用原始值精确匹配（assigned_agent = $2）
    会 403/409。统一改用存库原始值，使前缀表示不一致的合法调用放行。
    """
    return step.get("assigned_agent") or caller


# ── 辅助 ──────────────────────────────────────────────────

async def _report_pitfall_async(agent_id: str, step: dict, task_id: int,
                                failed_rules: list[dict]):
    """fire-and-forget: 通过吸星 API 上报踩坑（失败不影响 submit 流程）"""
    try:
        ok = await report_pitfall(
            agent_id=agent_id,
            step_index=step["step_index"],
            step_title=step["title"],
            task_id=task_id,
            failed_rules=failed_rules,
        )
        if ok:
            logger.info(f"Step {step['step_id']} check failure → pitfall reported to xixing")
        else:
            logger.warning(f"Step {step['step_id']} pitfall API call returned non-ok")
    except Exception:
        logger.exception("pitfall 上报到吸星失败，不影响 submit 流程")


def _safe_jsonb(val):
    """jsonb 反序列化兜底 — 兼容旧连接池返回字符串的情况。"""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return None
    return val


async def _classify_gotcha(step_id: int, failure_reason: str, instruction: str):
    """后台 LLM 分类：真坑 or 环境偶然。真坑标记 gotcha_verified=true。"""
    if not failure_reason.strip():
        return
    api_key = cfg.get_llm_api_key()
    if not api_key:
        return
    prompt = (
        "判断以下失败是否是\"可泛化的踩坑经验\"（可在 workflow 中沉淀，帮助后续避开同样问题）：\n\n"
        f"失败原因: {failure_reason}\n"
        f"步骤指令: {instruction[:300]}\n\n"
        "如果失败原因是通用的指令缺陷/平台兼容/依赖冲突/参数错误等可在不同环境中复现的问题 → YES\n"
        "如果失败原因是临时的网络波动/DNS超时/磁盘满/环境特定配置/资源耗尽等偶然问题 → NO\n"
        "仅回复 YES 或 NO。"
    )
    try:
        model = cfg.get_llm_decompose_model()
        base_url = cfg.get_llm_base_url()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      # 2026-08-27: glm思考强制开启计入max_tokens,5必空 → 512
                      "max_tokens": 512, "temperature": 0},
            )
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
            if answer.startswith("YES"):
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(
                        f"UPDATE {SCHEMA}.steps SET outputs = COALESCE(outputs, '{{}}'::jsonb) || "
                        f"'{{\"gotcha_verified\": true}}'::jsonb WHERE step_id = $1",
                        step_id,
                    )
                logger.info("Step %s gotcha verified (real trap)", step_id)
            else:
                logger.info("Step %s gotcha skipped (transient/env)", step_id)
    except Exception:
        pass


def _is_reverify(step: dict) -> bool:
    """检查 Step 是否被标记为待抽查。"""
    outputs = step.get("outputs")
    if isinstance(outputs, dict):
        return bool(outputs.get("reverify_requested"))
    return False

def _step_to_response(s: dict) -> dict:
    return {
        "step_id": s["step_id"],
        "step_index": s["step_index"],
        "title": s["title"],
        "instruction": s["instruction"],
        "status": s["status"],
        "status_reason": s.get("status_reason"),
        "assigned_agent": s.get("assigned_agent"),
        "depends_on": s.get("depends_on"),
        "acceptance_criteria": _safe_jsonb(s.get("acceptance_criteria")),
        "auto_retry": s.get("auto_retry", 0),
        "retries_left": s.get("auto_retry", 0),
        "timeout_minutes": s.get("timeout_minutes"),
        "summary": s.get("summary"),
        "outputs": s.get("outputs"),
        "started_at": s.get("started_at"),
        "completed_at": s.get("completed_at"),
        "created_at": s.get("created_at"),
        # v2 字段（全部可选，向后兼容）
        "quality_criteria": _safe_jsonb(s.get("quality_criteria")),
        "max_iterations": s.get("max_iterations", 3),
        "risk_level": s.get("risk_level", "low"),
        "confirmation_required": s.get("confirmation_required", False),
        "iteration_log": _safe_jsonb(s.get("iteration_log")),
    }


async def _build_task_response(task: dict, steps: list[dict]) -> dict:
    total = len(steps)
    done = sum(1 for s in steps if s["status"] in ("completed", "skipped"))
    failed = sum(1 for s in steps if s["status"] == "failed")
    return {
        "task_id": task["task_id"],
        "title": task["title"],
        "description": task["description"],
        "priority": task["priority"],
        "status": task["status"],
        "created_by": task["created_by"],
        "participants": task.get("participants") or [],
        "progress": task.get("progress", done * 100 // max(total, 1)),
        "total_steps": total,
        "completed_steps": done,
        "failed_steps": failed,
        "timeout_minutes": task.get("timeout_minutes"),
        "result": task.get("result"),
        "steps": [_step_to_response(s) for s in steps],
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


# ── Task 端点 ─────────────────────────────────────────────

@router.post("/tasks")
async def create_task(req: CreateTaskRequest):
    """统一入口：Steps 或 workflow_id 二选一（可共存，workflow 作底 steps 覆盖）"""
    # ── 行为规范检查（v1.10） ──
    policy_data = {
        "title": req.title,
        "description": req.description,
        "steps": [s.model_dump(exclude_none=True) for s in req.steps] if req.steps else [],
    }
    check = await policy_check(req.created_by, policy_data)
    if not check.get("allowed"):
        raise AppError(
            "POLICY_BLOCKED",
            check.get("message", "行为规范限制：任务不在该 Agent 服务范围内"),
            403,
        )

    # 校验 step_index 唯一
    if req.steps:
        indices = [s.step_index for s in req.steps]
        if len(indices) != len(set(indices)):
            raise AppError("VALIDATION_ERROR", "step_index 不能重复", 400)

    # 校验 acceptance_criteria type 合法
    for s in req.steps:
        for ac in (s.acceptance_criteria or []):
            if ac.type not in checker.VALID_CHECK_TYPES:
                raise AppError(
                    "VALIDATION_ERROR",
                    f"Step {s.step_index}: 不支持的检查规则类型 '{ac.type}'",
                    400,
                )

    # 按名称查 Workflow ID
    wf_id = req.workflow_id
    if req.source_workflow_name and not wf_id:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT workflow_id FROM {cfg.get_schema_name()}.workflows "
                f"WHERE name = $1 ORDER BY version DESC LIMIT 1",
                req.source_workflow_name,
            )
            if row:
                wf_id = row["workflow_id"]

    steps_dict = [s.model_dump(exclude_none=True) for s in req.steps]
    # v2: 提取 quality_criteria 传给 runner（可选）
    qc = None
    if hasattr(req, 'quality_criteria') and req.quality_criteria:
        qc = [c.model_dump() for c in req.quality_criteria]

    result = await runner.create_task(
        title=req.title,
        description=req.description,
        priority=req.priority,
        created_by=req.created_by,
        steps=steps_dict,
        acceptance_criteria=[a.model_dump() for a in req.acceptance_criteria] if req.acceptance_criteria else None,
        expected_outputs=req.expected_outputs,
        timeout_minutes=req.timeout_minutes,
        workflow_id=wf_id,
        workflow_version=req.workflow_version,
        quality_criteria=qc,
        auto_quality_confirm=getattr(req, 'auto_quality_confirm', False),
        skip_clarity=req.skip_clarity,
    )

    if not result.get("success"):
        if result.get("needs_clarification"):
            return {
                "status": "needs_clarification",
                "score": result.get("score", 0.5),
                "questions": result.get("questions", []),
            }
        raise AppError("VALIDATION_ERROR", result.get("error", "创建失败"), 400)

    task = result["task"]
    steps = result["steps"]
    response = await _build_task_response(task, steps)
    response["mode"] = result["mode"]
    response["trace_id"] = result.get("trace_id", "")
    return response


@router.get("/tasks")
async def list_tasks(
    status: str = Query(default=None),
    created_by: str = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: dict = Depends(auth_dependency),
):
    """查询任务列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = []
        params = []
        idx = 1

        if status:
            where.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if created_by:
            where.append(f"created_by = ${idx}")
            params.append(created_by)
            idx += 1

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, offset])
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.tasks{clause} "
            f"ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
            *params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.tasks{clause}",
            *params[:-2],
        )

        tasks = []
        for t in rows:
            steps = await sm.get_task_steps(conn, t["task_id"])
            tasks.append(await _build_task_response(dict(t), steps))

        return {"tasks": tasks, "total": total or 0, "limit": limit, "offset": offset}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int):
    """任务详情（含所有 Steps）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        task = await sm.get_task(conn, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        steps = await sm.get_task_steps(conn, task_id)
        return await _build_task_response(task, steps)


@router.get("/tasks/{task_id}/next")
async def get_next_step(task_id: int, agent_id: str = Query(default=""),
                        auth: dict = Depends(auth_dependency)):
    """Agent 查询下一步（原子分配 pending→assigned）

    P0-1 (9-2): daemon 消费端点 fail-closed 鉴权——Bearer（或内部 IPC）
    认证 + 自报 agent_id 归属校验。此前任何人自报 agent_id 即可原子抢占
    步骤拉走 instruction（配合 exec_type 缺省 shell 即远程 RCE 链入口）。
    """
    _require_daemon_agent(auth, agent_id)
    result = await runner.get_next_step(task_id, agent_id)
    if not result.get("found"):
        return NextStepResponse(
            task_id=task_id,
            task_status=result.get("task_status", "unknown"),
            current_step=None,
            progress=result.get("progress", ""),
            upcoming_steps=result.get("upcoming_steps", []),
        )
    return NextStepResponse(
        task_id=result["task_id"],
        task_status=result["task_status"],
        current_step=result["current_step"],
        progress=result.get("progress", ""),
        upcoming_steps=result.get("upcoming_steps", []),
    )


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: int):
    """取消任务"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            task = await sm.task_cancel(conn, task_id)
            if not task:
                raise HTTPException(400, "任务不存在或已处于终态")

            # 取消所有非终态 Step
            steps = await sm.get_task_steps(conn, task_id)
            for s in steps:
                if s["status"] not in ("completed", "failed", "skipped", "cancelled"):
                    await sm.step_cancel(conn, s["step_id"])
                    fresh = await sm.get_step(conn, s["step_id"])
                    if fresh:
                        asyncio.create_task(runner.step_hooks(dict(fresh), s.get("assigned_agent", ""), "cancelled"))

            # 跳过依赖被取消 Step 的 pending Step
            # P1 (R?): 原实现用取消前的旧快照 `steps` 取 cancelled 状态 → 恒为空，
            # 依赖跳过从不触发。重新读取最新状态后再计算。
            fresh_steps = await sm.get_task_steps(conn, task_id)
            cancelled_indices = [
                s["step_index"] for s in fresh_steps
                if s["status"] in ("cancelled",)
            ]
            for s in fresh_steps:
                if s["status"] == "pending" and s.get("depends_on"):
                    if any(d in cancelled_indices for d in s["depends_on"]):
                        await sm.step_skip(conn, s["step_id"])
                        fresh = await sm.get_step(conn, s["step_id"])
                        if fresh:
                            asyncio.create_task(runner.step_hooks(dict(fresh), s.get("assigned_agent", ""), "skipped"))

            # WS 推送通知所有被分配了 Step 的 Agent
            notified = set()
            for s in steps:
                agent = s.get("assigned_agent")
                if agent and agent not in notified and s["status"] not in ("completed", "failed", "skipped"):
                    notified.add(agent)
                    await dispatcher.ws_notify(agent, "cancelled", {
                        "task_id": task_id,
                        "title": task["title"],
                        "step_id": s["step_id"],
                        "step_index": s["step_index"],
                        "step_title": s["title"],
                    })

        return {"success": True, "task_id": task_id, "status": "cancelled"}


@router.post("/tasks/{task_id}/assign")
async def assign_step(task_id: int, req: AssignStepRequest):
    """将 Step 分派给指定 Agent（镇岳网关做能力检查，本层做目标 Agent 存在性校验）"""
    result = await dispatcher.assign_step(
        task_id=task_id,
        step_index=req.step_index,
        assigned_agent=req.assigned_agent,
        requested_by=req.requested_by,
    )
    if not result.get("success"):
        raise AppError("ASSIGN_FAILED", result["error"], 400)
    return result


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: int, req: PauseTaskRequest | None = None):
    """暂停任务"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        task = await sm.task_pause(conn, task_id, req.reason if req else "")
        if not task:
            raise HTTPException(400, "任务不存在或状态不是 running")
        return {"success": True, "task_id": task_id, "status": "paused"}


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: int):
    """恢复任务"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        task = await sm.task_resume(conn, task_id)
        if not task:
            raise HTTPException(400, "任务不存在或状态不是 paused")
        return {"success": True, "task_id": task_id, "status": "running"}


@router.get("/recover")
async def recover(agent_id: str = Query(default="")):
    """中断恢复 — 查询 Agent 所有未完成的 Steps"""
    if not agent_id:
        raise AppError("VALIDATION_ERROR", "agent_id 必填", 400)
    return await dispatcher.get_recovery_state(agent_id)


@router.post("/tasks/{task_id}/extract")
async def extract_workflow(task_id: int):
    """从已完成 Task 提取 Workflow 骨架"""
    result = await extractor.extract_workflow(task_id)
    if not result.get("success"):
        raise HTTPException(404, result.get("error", "提取失败"))
    return result


@router.post("/workflows/{workflow_id}/refine")
async def refine_workflow(workflow_id: int, min_completed: int = 2):
    """从多次执行数据中优化 Workflow（LLM 分析 → 自动生成新版本）"""
    result = await runner.refine_workflow(workflow_id, min_completed=min_completed)
    if not result.get("refined"):
        raise HTTPException(400, result.get("reason", "优化失败"))
    return result


@router.post("/workflows/cleanup")
async def cleanup_workflows(
    retention_days: int = 90,
    min_use_count: int = 0,
    auth: dict = Depends(auth_dependency),
):
    """清理过期/低使用 Workflow（需 admin）。

    删除 last_used_at < retention_days 且 use_count <= min_use_count 的 Workflow。
    默认: 90 天未使用且使用次数为 0 → 删除。返回清理统计。
    """
    if auth.get("role") not in ("admin", "ops_admin"):
        return JSONResponse(status_code=403, content={"error": "FORBIDDEN"})
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"DELETE FROM {SCHEMA}.workflows "
            f"WHERE (last_used_at IS NULL OR last_used_at < NOW() - make_interval(days => $2)) "
            f"AND use_count <= $1 "
            f"AND version = (SELECT MAX(v2.version) FROM {SCHEMA}.workflows v2 "
            f"              WHERE v2.name = {SCHEMA}.workflows.name)",
            min_use_count, retention_days,
        )
        count = int(result.split()[-1]) if result else 0
    return {"action": "cleanup", "deleted": count, "retention_days": retention_days}


# ── Workflow 模板 CRUD ─────────────────────────────────────


@router.get("/workflows")
async def list_workflows(
    name: str = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """查询 Workflow 模板列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = []
        params = []
        idx = 1

        if name:
            where.append(f"name ILIKE ${idx}")
            params.append(f"%{name}%")
            idx += 1

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, offset])
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.workflows{clause} "
            f"ORDER BY updated_at DESC LIMIT ${idx} OFFSET ${idx + 1}",
            *params,
        )
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.workflows{clause}",
            *params[:-2],
        )

        workflows = []
        for r in rows:
            d = dict(r)
            d["definition"] = d.get("definition")
            d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
            d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
            workflows.append(d)

        return {"workflows": workflows, "total": total or 0, "limit": limit, "offset": offset}


@router.post("/workflows")
async def create_workflow(req: CreateWorkflowRequest):
    """创建 Workflow 模板"""
    err = CreateWorkflowRequest.validate_definition(req.definition)
    if err:
        raise AppError("VALIDATION_ERROR", err, 400)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 查找同名最新 version
        latest = await conn.fetchval(
            f"SELECT COALESCE(MAX(version), 0) FROM {SCHEMA}.workflows WHERE name = $1",
            req.name,
        )
        version = latest + 1

        row = await conn.fetchrow(
            f"INSERT INTO {SCHEMA}.workflows "
            f"(name, description, version, definition, created_by) "
            f"VALUES ($1,$2,$3,$4,$5) RETURNING *",
            req.name,
            req.description,
            version,
            json.dumps(req.definition, ensure_ascii=False),
            req.created_by,
        )
        d = dict(row)
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None

        logger.info(f"Workflow created: {d['workflow_id']} '{req.name}' v{version} by {req.created_by}")
        return d


@router.put("/workflows/{workflow_id}")
async def update_workflow(workflow_id: int, req: UpdateWorkflowRequest):
    """更新 Workflow 模板（version+1）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.workflows WHERE workflow_id = $1",
            workflow_id,
        )
        if not existing:
            raise HTTPException(404, "Workflow 不存在")

        name = req.name or existing["name"]
        description = req.description if req.description else existing["description"]
        definition = req.definition if req.definition else existing["definition"]
        version = existing["version"] + 1

        row = await conn.fetchrow(
            f"INSERT INTO {SCHEMA}.workflows "
            f"(name, description, version, definition, created_by, source_task_id) "
            f"VALUES ($1,$2,$3,$4,$5,$6) RETURNING *",
            name,
            description,
            version,
            json.dumps(definition, ensure_ascii=False),
            existing["created_by"],
            existing.get("source_task_id"),
        )
        d = dict(row)
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None

        logger.info(f"Workflow updated: {workflow_id} → {d['workflow_id']} '{name}' v{version}")
        return d


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(workflow_id: int):
    """删除 Workflow 模板"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"DELETE FROM {SCHEMA}.workflows WHERE workflow_id = $1",
            workflow_id,
        )
        if result == "DELETE 0":
            raise HTTPException(404, "Workflow 不存在")

        logger.info(f"Workflow deleted: {workflow_id}")
        return {"success": True, "workflow_id": workflow_id}

@router.post("/steps/{step_id}/start")
async def start_step(step_id: int, req: StartStepRequest,
                     auth: dict = Depends(auth_dependency)):
    """确认开始执行（校验 caller == assigned_agent）

    P0-1 (9-2): Bearer/IPC 认证 + 归属校验（assigned 比对是既有 R11 层）。
    """
    _require_daemon_agent(auth, req.agent_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        step = await sm.get_step(conn, step_id)
        if not step:
            raise HTTPException(404, "Step 不存在")

        if _normalize_agent_id(step["assigned_agent"]) != _normalize_agent_id(req.agent_id):
            raise AppError(
                "FORBIDDEN",
                f"无权执行此 Step：assigned_agent={step['assigned_agent']}，caller={req.agent_id}",
                403,
            )

        result = await sm.step_start(conn, step_id, _effective_executor(step, req.agent_id))
        if not result:
            raise AppError(
                "CONFLICT",
                f"Step {step_id} 状态为 '{step.get('status')}'，无法 start（需要 assigned）",
                409,
            )

        # fire-and-forget: 状态变更后重读确保 hook 数据最新（参照 heartbeat）
        fresh_step = await sm.get_step(conn, step_id)
        if fresh_step:
            asyncio.create_task(runner.step_hooks(dict(fresh_step), req.agent_id, "start"))

        return {
            "step_id": step_id,
            "status": "in_progress",
            "started_at": result.get("started_at"),
        }


@router.post("/steps/{step_id}/heartbeat")
async def heartbeat(step_id: int, req: HeartbeatRequest,
                    auth: dict = Depends(auth_dependency)):
    """心跳（含 status_reason + progress/outputs）

    P0-1 (9-2): Bearer/IPC 认证 + 归属校验（assigned 比对是既有 R11 层）。
    """
    _require_daemon_agent(auth, req.agent_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        step = await sm.get_step(conn, step_id)
        if not step:
            raise HTTPException(404, "Step 不存在")
        # R11 (P?): 前缀归一化等效 → DB 层用存储的 assigned_agent 精确匹配
        if _normalize_agent_id(step["assigned_agent"]) != _normalize_agent_id(req.agent_id):
            raise AppError(
                "FORBIDDEN",
                f"无权对此 Step 心跳：assigned_agent={step['assigned_agent']}，caller={req.agent_id}",
                403,
            )
        result = await sm.step_heartbeat(
            conn, step_id, _effective_executor(step, req.agent_id), req.status_reason,
        )
        if not result:
            raise HTTPException(404, "Step 不存在或状态不是 in_progress")

        trace_id = runner._get_trace(result.get("task_id", 0))
        logger.info(f"[trace={trace_id}] Step {step_id} heartbeat for task {result.get('task_id', '?')}")

        # 存储 progress + outputs（如果提供）
        if req.progress or req.status or req.outputs:
            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET outputs = COALESCE(outputs, '{{}}'::jsonb) || $2::jsonb, "
                f"updated_at = NOW() WHERE step_id = $1",
                step_id,
                json.dumps({
                    "heartbeat_progress": req.progress or "",
                    "heartbeat_status": req.status or "",
                    "heartbeat_outputs": req.outputs,
                }, ensure_ascii=False),
            )

        # fire-and-forget hook
        try:
            full_step = await sm.get_step(conn, step_id)
            if full_step:
                asyncio.create_task(runner.step_hooks(dict(full_step), req.agent_id, "heartbeat"))
        except Exception:
            pass

        return {
            "step_id": step_id,
            "status": result["status"],
            "status_reason": result["status_reason"],
            "last_heartbeat_at": result["last_heartbeat_at"],
        }


@router.post("/steps/{step_id}/submit")
async def submit_step(step_id: int, req: SubmitRequest,
                      auth: dict = Depends(auth_dependency)):
    """提交结果 + 触发引擎比对

    P0-1 (9-2): Bearer/IPC 认证 + 归属校验——提交是状态机推进面，
    此前自报他人 agent_id 可冒充完成/失败步骤。
    """
    _require_daemon_agent(auth, req.agent_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        step = await sm.get_step(conn, step_id)
        if not step:
            raise HTTPException(404, "Step 不存在")

        if _normalize_agent_id(step["assigned_agent"]) != _normalize_agent_id(req.agent_id):
            raise AppError(
                "FORBIDDEN",
                f"无权提交此 Step：assigned_agent={step['assigned_agent']}，caller={req.agent_id}",
                403,
            )

        # 幂等性检查
        trace_id = runner._get_trace(step["task_id"])
        if step.get("idempotency_key") == req.idempotency_key:
            logger.info(f"[trace={trace_id}] Step {step_id} 重复提交 idempotency_key={req.idempotency_key}")
            return SubmitResponse(
                step_id=step_id,
                status=step["status"],
                verification_result="duplicate",
                retries_left=step.get("auto_retry", 0),
            )

        # ── v2: iteration_log 收敛检查（放在入口，先检再执行）──
        step_has_criteria = bool(_safe_jsonb(step.get("quality_criteria")))
        has_iteration_log = bool(req.iteration_log)

        # Y3: Step 有 quality_criteria 但 submit 无 iteration_log — 旧 Agent 跳检告警
        if step_has_criteria and not has_iteration_log:
            logger.warning(
                f"[trace={trace_id}] Step {step_id} 有 quality_criteria 但 submit 无 "
                f"iteration_log，Agent {req.agent_id} 可能跳过了自检"
            )

        # Q1: iteration_log 最后一轮必须标记"全部通过"
        if has_iteration_log and len(req.iteration_log) > 0:
            last_round = req.iteration_log[-1]
            if last_round.get("self_check_result", "") != "全部通过":
                iteration_log_json = json.dumps(req.iteration_log, ensure_ascii=False)
                await conn.execute(
                    f"UPDATE {SCHEMA}.steps SET iteration_log = $2::jsonb "
                    f"WHERE step_id = $1",
                    step_id, iteration_log_json,
                )
                return SubmitResponse(
                    step_id=step_id,
                    status="rejected",
                    verification_result="self_check_incomplete",
                    retries_left=step.get("auto_retry", 0),
                    failed_rules=[{
                        "type": "self_check",
                        "expected": "全部通过",
                        "actual": last_round.get("self_check_result", "未知"),
                        "detail": f"自检 {len(req.iteration_log)}/{step.get('max_iterations', 3)} 轮未完成",
                    }],
                    retry_hint="请完成所有 quality_criteria 自检，确保最后一轮标记'全部通过'后再提交",
                )

        # 如果 Agent 报告执行失败
        if req.status == "failed":
            result = await sm.step_fail(conn, step_id, _effective_executor(step, req.agent_id), req.summary)
            if not result:
                raise AppError("CONFLICT", f"Step {step_id} 状态异常，无法标记 failed", 409)

            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET idempotency_key = $2 WHERE step_id = $1",
                step_id, req.idempotency_key,
            )

            # 记录踩坑: 失败原因 + 修复方法, 后台 LLM 分类真坑/环境偶然
            gotcha_data = {
                "retry_from_failure": True,
                "failure_reason": req.summary or "",
                "outputs_snapshot": req.outputs,
            }
            gotcha = json.dumps(gotcha_data, ensure_ascii=False)
            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET outputs = COALESCE(outputs, '{{}}'::jsonb) || $2::jsonb "
                f"WHERE step_id = $1", step_id, gotcha,
            )
            # 后台 LLM 分类: 真坑 or 环境偶然
            asyncio.create_task(_classify_gotcha(
                step_id, req.summary or "",
                step.get("instruction", "")[:500],
            ))

            # 自动重试
            if result.get("auto_retry", 0) > 0:
                retry = await sm.step_retry(conn, step_id)
                if retry:
                    asyncio.create_task(runner.step_hooks(dict(retry), req.agent_id, "retry"))
                    return SubmitResponse(
                        step_id=step_id,
                        status="retry",
                        verification_result="retrying",
                        retries_left=retry.get("auto_retry", 0),
                    )

            # Task 完结判定
            await runner.try_complete_task(step["task_id"], conn)

            # result 是 step_fail 返回的最新行
            asyncio.create_task(runner.step_hooks(dict(result), req.agent_id, "failed"))
            return SubmitResponse(
                step_id=step_id,
                status="failed",
                verification_result="agent_reported_failure",
                retries_left=0,
            )

        # ── v2: iteration_log 由事务保护段统一写入（Q1 拒绝路径和 success 事务路径），此处不留独立写入 ──

        # 运行检查
        criteria = _safe_jsonb(step.get("acceptance_criteria")) or []
        check_inputs = req.outputs if req.outputs else {}

        # 合并 outputs 和 check_results 到比对输入
        comparison_data = dict(check_inputs)
        if "check_results" in req.outputs:
            comparison_data.update(req.outputs["check_results"])

        # ── Ed25519 签名验证（§3.4.3） ──
        # R11 (P1): 原实现「缺 signature 即跳过」→ 验签可选空转（fail-open）。
        # 现凡有 check_results 必验：Agent 已注册公钥而缺签名 → 拒；绑定
        # step_id/task_id 防跨步骤重放。
        signature_valid = True
        signature_error = ""
        if "check_results" in req.outputs:
            signature_valid, signature_error = await verify_signature(
                req.agent_id, step_id, step["task_id"],
                req.outputs["check_results"], req.signature,
            )
            if not signature_valid:
                logger.warning(
                    "Signature verification failed for step=%s agent=%s: %s",
                    step_id, req.agent_id, signature_error,
                )

        check_result = checker.check_all(criteria, comparison_data)

        # ── 如果签名验证失败，整体标记为 rejected（协议错误，不消耗 auto_retry） ──
        if not signature_valid:
            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET idempotency_key = $2 WHERE step_id = $1",
                step_id, req.idempotency_key,
            )
            return SubmitResponse(
                step_id=step_id,
                status="rejected",
                verification_result="signature_invalid",
                retries_left=step.get("auto_retry", 0),
                failed_rules=[{"type": "signature", "error": signature_error}],
            )

        # 记录 verifications — 包含实际 rule_details + signature
        for i, r in enumerate(check_result["results"]):
            criterion = criteria[i] if i < len(criteria) else {}
            verdict = "needs_review" if r.get("error") == "needs_review" else ("passed" if r["passed"] else "failed")
            await conn.execute(
                f"INSERT INTO {SCHEMA}.verifications "
                f"(task_id, step_id, rule_type, check_mode, rule_details, result, "
                f"actual_value, signature, verified_by) "
                f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                step["task_id"], step_id,
                r["type"],
                "engine" if checker.is_engine_auto(r["type"]) else "agent_report",
                json.dumps(criterion, ensure_ascii=False),
                verdict,
                r.get("actual", "")[:500],
                req.signature or None,   # §3.4.3
                "engine",
            )

        # ── v2 Phase A: quality_criteria 引擎重检（不阻塞，仅告警）──
        qc_warnings = []
        qc_criteria_submit = _safe_jsonb(step.get("quality_criteria")) or []
        if qc_criteria_submit:
            qc_results = checker.check_quality_criteria(qc_criteria_submit, comparison_data)
            failed_qc = [r for r in qc_results if not r["passed"]]
            for r in failed_qc:
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.verifications "
                    f"(task_id, step_id, rule_type, check_mode, rule_details, result, "
                    f"actual_value, verified_by) "
                    f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    step["task_id"], step_id,
                    r["type"], "engine_recheck",
                    json.dumps(r["criterion"], ensure_ascii=False),
                    "failed", r["actual"], "engine",
                )
                qc_warnings.append(r)
            if failed_qc:
                logger.warning(
                    f"[trace={trace_id}] Step {step_id} quality_criteria 引擎重检 "
                    f"发现 {len(failed_qc)} 项未通过"
                )

        # ── manual_review 流程 — 挂起等人审，不 reject ──
        if check_result.get("has_manual_review"):
            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET status_reason = 'waiting_input', "
                f"updated_at = NOW() WHERE step_id = $1",
                step_id,
            )
            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET idempotency_key = $2 WHERE step_id = $1",
                step_id, req.idempotency_key,
            )
            fresh = await sm.get_step(conn, step_id)
            if fresh:
                asyncio.create_task(runner.step_hooks(dict(fresh), req.agent_id, "needs_review"))
            return SubmitResponse(
                step_id=step_id,
                status="in_progress",
                verification_result="needs_review",
                retries_left=step.get("auto_retry", 0),
                failed_rules=[],
                qc_warnings=qc_warnings,
            )

        if not check_result["passed"]:
            # 区分 schema 错误（协议错误，不消耗 auto_retry）与真实检查失败
            schema_only = check_result.get("has_schema_error", False) and all(
                fr.get("schema_error", False) for fr in check_result["failed_rules"]
            )

            if schema_only:
                # schema 校验失败：reject 但不消耗 auto_retry
                await sm.step_reject(conn, step_id)
                auto_retry = step.get("auto_retry", 0)
                if auto_retry > 0:
                    retry = await sm.step_retry(conn, step_id)
                    if retry:
                        # 恢复 auto_retry（step_retry 减了 1，加回去）
                        await conn.execute(
                            f"UPDATE {SCHEMA}.steps SET auto_retry = auto_retry + 1 "
                            f"WHERE step_id = $1", step_id,
                        )
                        status = "retry"
                        retries_left = retry.get("auto_retry", 0) + 1
                    else:
                        status = "rejected"
                        retries_left = auto_retry
                else:
                    status = "rejected"
                    retries_left = 0
            else:
                # 真实检查失败
                reject = await sm.step_reject(conn, step_id)
                auto_retry = reject.get("auto_retry", 0) if reject else 0

                if auto_retry > 0:
                    retry = await sm.step_retry(conn, step_id)
                    if retry:
                        status = "retry"
                        retries_left = retry.get("auto_retry", 0)
                    else:
                        status = "rejected"
                        retries_left = auto_retry
                else:
                    status = "failed"
                    retries_left = 0
                    # 原子 UPDATE：仅当状态仍在 rejected/timed_out 时置为 failed
                    await conn.execute(
                        f"UPDATE {SCHEMA}.steps SET status = 'failed', status_reason = NULL, "
                        f"completed_at = NOW(), updated_at = NOW() "
                        f"WHERE step_id = $1 AND status IN ('rejected', 'in_progress', 'timed_out')",
                        step_id,
                    )

                    # WS 推送：retry 耗尽通知 task 创建者
                    task = await sm.get_task(conn, step["task_id"])
                    if task:
                        await dispatcher.ws_notify(task["created_by"], "retry_exhausted", {
                            "task_id": step["task_id"],
                            "task_title": task["title"],
                            "step_id": step_id,
                            "step_index": step["step_index"],
                            "title": step["title"],
                            "reason": "检查不通过且 auto_retry 已耗尽",
                            "failed_rules": check_result["failed_rules"],
                        })

                    # 自动上报踩坑到吸星（§4.4）
                    asyncio.create_task(_report_pitfall_async(
                        req.agent_id, step, step["task_id"],
                        check_result["failed_rules"],
                    ))

            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET idempotency_key = $2 WHERE step_id = $1",
                step_id, req.idempotency_key,
            )

            # 信誉联动：如果这是抽查，记录失败
            if _is_reverify(step):
                rep = await record_reverify_result(
                    conn, req.agent_id, step_id, step["task_id"], passed=False,
                )
                logger.warning("Reverify failed for step=%s agent=%s → %s (x%s)",
                               step_id, req.agent_id, rep["action"], rep["consecutive"])

            await runner.try_complete_task(step["task_id"], conn)

            fresh = await sm.get_step(conn, step_id)
            if fresh:
                asyncio.create_task(runner.step_hooks(dict(fresh), req.agent_id, status))
            return SubmitResponse(
                step_id=step_id,
                status=status,
                verification_result="failed",
                retries_left=retries_left,
                failed_rules=check_result["failed_rules"],
                qc_warnings=qc_warnings,
            )

        # 检查通过 → completed
        # iteration_log 与 step_complete 在事务中，防止孤立 iteration_log
        async with conn.transaction():
            if has_iteration_log and len(req.iteration_log) > 0:
                iteration_log_json = json.dumps(req.iteration_log, ensure_ascii=False)
                await conn.execute(
                    f"UPDATE {SCHEMA}.steps SET iteration_log = $2::jsonb "
                    f"WHERE step_id = $1",
                    step_id, iteration_log_json,
                )

            result = await sm.step_complete(
                conn, step_id, _effective_executor(step, req.agent_id), req.summary,
                req.outputs,
            )
            if not result:
                raise AppError("CONFLICT", f"Step {step_id} 状态异常，无法标记 completed", 409)

            await conn.execute(
                f"UPDATE {SCHEMA}.steps SET idempotency_key = $2 WHERE step_id = $1",
                step_id, req.idempotency_key,
            )

            # Task 完结判定
            await runner.try_complete_task(step["task_id"], conn)

            # result 是 step_complete 返回的最新行
            asyncio.create_task(runner.step_hooks(dict(result), req.agent_id, "completed"))

            # 信誉联动：如果这是抽查，记录通过
            if _is_reverify(step):
                rep = await record_reverify_result(
                    conn, req.agent_id, step_id, step["task_id"], passed=True,
                )
                logger.info("Reverify passed for step=%s agent=%s → %s (x%s)",
                            step_id, req.agent_id, rep["action"], rep["consecutive"])

            # 多签交叉验证：如有 require_multisig 规则，创建验证子任务
            if needs_multisig(_safe_jsonb(step.get("acceptance_criteria"))):
                ms_count = get_multisig_count(step["acceptance_criteria"])
                vids = await create_verification_tasks(
                    conn, step_id, step["task_id"], ms_count,
                    step["title"], step["step_index"], step["acceptance_criteria"],
                )
                if vids:
                    logger.info("Multisig required for step=%s: %d verifiers needed", step_id, len(vids))

            return SubmitResponse(
                step_id=step_id,
                status="completed",
                verification_result="passed",
                retries_left=result.get("auto_retry", 0),
                qc_warnings=qc_warnings,
            )


@router.post("/steps/{step_id}/issue")
async def report_issue(step_id: int, req: IssueRequest):
    """报告问题 — Step 保持 in_progress，status_reason → blocked"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        step = await sm.get_step(conn, step_id)
        if not step:
            raise HTTPException(404, "Step 不存在")

        if _normalize_agent_id(step["assigned_agent"]) != _normalize_agent_id(req.agent_id):
            raise AppError(
                "FORBIDDEN",
                f"无权报告此 Step 问题：assigned_agent={step['assigned_agent']}，caller={req.agent_id}",
                403,
            )

        if step["status"] != "in_progress":
            raise AppError(
                "CONFLICT",
                f"Step {step_id} 状态为 '{step['status']}'，只有 in_progress 才能报 issue",
                409,
            )

        # 更新 status_reason → blocked，记录 issue 信息到 outputs
        await conn.execute(
            f"UPDATE {SCHEMA}.steps SET status_reason = 'blocked', "
            f"outputs = COALESCE(outputs, '{{}}'::jsonb) || $2::jsonb, "
            f"updated_at = NOW() "
            f"WHERE step_id = $1",
            step_id,
            json.dumps({
                "issue": {
                    "issue_type": req.issue_type,
                    "description": req.description,
                    "severity": req.severity,
                    "reported_at": datetime.now(timezone.utc).isoformat(),
                }
            }, ensure_ascii=False),
        )

        logger.info(
            f"Step {step_id} issue reported: type={req.issue_type} "
            f"severity={req.severity} agent={req.agent_id}"
        )

        # hook: trajectory + audit_log（状态变更后重读）
        fresh = await sm.get_step(conn, step_id)
        if fresh:
            asyncio.create_task(runner.step_hooks(dict(fresh), req.agent_id, "blocked"))

        # WS 推送通知 task 创建者
        task = await sm.get_task(conn, step["task_id"])
        if task:
            await dispatcher.ws_notify(task["created_by"], "issue_reported", {
                "task_id": step["task_id"],
                "step_id": step_id,
                "step_index": step["step_index"],
                "title": step["title"],
                "issue_type": req.issue_type,
                "description": req.description,
                "reported_by": req.agent_id,
            })

        return {
            "step_id": step_id,
            "status": "in_progress",
            "status_reason": "blocked",
            "issue_type": req.issue_type,
            "severity": req.severity,
        }


@router.post("/steps/{step_id}/review")
async def review_step(step_id: int, req: ReviewRequest):
    """Reviewer 审核 manual_review 类型的检查规则"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 校验 verification 存在
        verification = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.verifications WHERE verification_id = $1",
            req.verification_id,
        )
        if not verification:
            raise HTTPException(404, "Verification 记录不存在")

        if verification["step_id"] != step_id:
            raise AppError("VALIDATION_ERROR", "Verification 不属于此 Step", 400)

        if verification["rule_type"] != "manual_review":
            raise AppError("VALIDATION_ERROR", "只有 manual_review 类型需要人工审核", 400)

        # 更新 verification 结果
        result = "passed" if req.decision == "approved" else "failed"
        await conn.execute(
            f"UPDATE {SCHEMA}.verifications SET result = $2, notes = $3, "
            f"verified_by = 'human' WHERE verification_id = $1",
            req.verification_id, result, req.notes,
        )

        logger.info(
            f"Step {step_id} manual_review {req.decision}: "
            f"verification_id={req.verification_id}"
        )

        # R11 (P?): manual_review 卡死修复 —— 所有人工审核处理完后按存储的
        # verifications 汇总判定：全通过 → completed；任一失败 → reject/retry。
        # 原实现只把 status_reason 改回 executing，Step 永不 completed，卡死任务。
        step = await sm.get_step(conn, step_id)
        if step and step["status"] == "in_progress":
            pending_reviews = await conn.fetchval(
                f"SELECT COUNT(*) FROM {SCHEMA}.verifications "
                f"WHERE step_id = $1 AND rule_type = 'manual_review' "
                f"AND result NOT IN ('passed', 'failed')",
                step_id,
            )
            if not pending_reviews:
                ver = await conn.fetch(
                    f"SELECT rule_type, check_mode, result FROM {SCHEMA}.verifications "
                    f"WHERE step_id = $1",
                    step_id,
                )
                formal = [r for r in ver if r["rule_type"] != "manual_review"
                          and r["check_mode"] in ("engine", "agent_report")]
                manual = [r for r in ver if r["rule_type"] == "manual_review"]
                any_failed = (
                    any(r["result"] == "failed" for r in formal)
                    or any(r["result"] == "failed" for r in manual)
                )

                if not any_failed:
                    # 全部通过 → completed
                    done = await sm.step_complete(
                        conn, step_id,
                        _effective_executor(step, step.get("assigned_agent") or ""),
                        step.get("summary") or "人工复核通过",
                        _safe_jsonb(step.get("outputs")) or {},
                    )
                    if done:
                        await runner.try_complete_task(step["task_id"], conn)
                        asyncio.create_task(runner.step_hooks(
                            dict(done), step.get("assigned_agent") or "", "completed"))
                else:
                    # 任一失败 → reject（消耗 auto_retry 走重试/失败）
                    reject = await sm.step_reject(conn, step_id)
                    auto_retry = reject.get("auto_retry", 0) if reject else 0
                    if auto_retry > 0:
                        await sm.step_retry(conn, step_id)
                    else:
                        await conn.execute(
                            f"UPDATE {SCHEMA}.steps SET status = 'failed', status_reason = NULL, "
                            f"completed_at = NOW(), updated_at = NOW() "
                            f"WHERE step_id = $1 AND status IN ('rejected', 'in_progress', 'timed_out')",
                            step_id,
                        )
                    await runner.try_complete_task(step["task_id"], conn)

        return {
            "verification_id": req.verification_id,
            "step_id": step_id,
            "decision": req.decision,
            "result": result,
        }


@router.post("/steps/{step_id}/reject")
async def reject_step(step_id: int, req: RejectStepRequest | None = None):
    """创建者手动打回 Step（重置 auto_retry，给一次重做机会）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            step = await sm.get_step(conn, step_id)
            if not step:
                raise HTTPException(404, "Step 不存在")

            if step["status"] not in ("failed", "rejected", "timed_out"):
                raise AppError(
                    "CONFLICT",
                    f"Step {step_id} 状态为 '{step['status']}'，只有 failed/rejected/timed_out 才能打回",
                    409,
                )

            reset_count = req.reset_retry if req and req.reset_retry else 1
            result = await sm.step_reject_reset(conn, step_id, reset_count)
            if not result:
                raise AppError("CONFLICT", f"Step {step_id} 打回失败", 409)

            reason = req.reason if req and req.reason else "创建者手动打回"

            logger.info(f"Step {step_id} manually rejected by creator: {reason}, "
                        f"auto_retry reset to {reset_count}")

            # v2: 精准反馈
            failed_rules = req.failed_rules if req and req.failed_rules else []
            retry_hint = req.retry_hint if req and req.retry_hint else ""

            if step.get("assigned_agent"):
                await dispatcher.ws_notify(step["assigned_agent"], "rejected", {
                    "task_id": step["task_id"],
                    "step_id": step_id,
                    "step_index": step["step_index"],
                    "title": step["title"],
                    "reason": reason,
                    "retries_reset": reset_count,
                    "failed_rules": failed_rules,
                    "retry_hint": retry_hint,
                })

            return {
                "step_id": step_id,
                "status": "pending",
                "auto_retry": reset_count,
                "reason": reason,
                "failed_rules": failed_rules,
                "retry_hint": retry_hint,
            }


@router.post("/steps/{step_id}/confirm")
async def confirm_step(step_id: int, req: ConfirmStepRequest):
    """v2 Phase 3: 确认高风险 Step，允许执行"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        step = await sm.get_step(conn, step_id)
        if not step:
            raise HTTPException(404, "Step 不存在")
        if not step.get("confirmation_required"):
            raise AppError(
                "CONFLICT",
                f"Step {step_id} 未标记为高风险，无需确认",
                409,
            )
        if step.get("confirmed_by"):
            raise AppError(
                "CONFLICT",
                f"Step {step_id} 已被 {step['confirmed_by']} 确认",
                409,
            )

        # Task 必须在 running/pending 状态
        task = await sm.get_task(conn, step["task_id"])
        if task and task["status"] not in ("running", "pending"):
            raise AppError(
                "CONFLICT",
                f"Task {step['task_id']} 状态为 '{task['status']}'，只有 running/pending 才能确认 Step",
                409,
            )

        result = await sm.step_confirm(conn, step_id, req.confirmed_by)
        if not result:
            raise AppError("CONFLICT", f"Step {step_id} 确认失败", 409)

        # WS 通知 assigned_agent
        if result.get("assigned_agent"):
            await dispatcher.ws_notify(result["assigned_agent"], "confirmed", {
                "task_id": step["task_id"],
                "step_id": step_id,
                "step_index": step["step_index"],
                "title": step["title"],
                "confirmed_by": req.confirmed_by,
                "notes": req.notes or "",
            })

        return {
            "step_id": step_id,
            "confirmed_by": req.confirmed_by,
            "confirmed_at": result.get("confirmed_at"),
            "status": result["status"],
        }


@router.post("/steps/{step_id}/reverify")
async def reverify_step(step_id: int):
    """创建者对已完成 Step 发起抽查（§3.4.3）。

    引擎通知 Agent 重新执行 agent-report 规则，Agent 通过正常 submit 提交新结果。
    引擎比对后通过 reputation 模块追踪连续失败/通过次数，触发 C-Level 自动调整。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        step = await sm.get_step(conn, step_id)
        if not step:
            raise HTTPException(404, "Step 不存在")

        if step["status"] != "completed":
            raise AppError(
                "CONFLICT",
                f"Step {step_id} 状态为 '{step['status']}'，只有 completed 才能抽查",
                409,
            )

        # 标记 step 为待抽查
        # P1 (R?): 原实现 step 保持 completed——Agent 重交后 step_complete 要求
        # status='in_progress' 恒 409 → reverify 只能记失败，信誉只能降级。
        # 现置回 in_progress + 重置 started_at（给足新超时窗口，防看门狗按旧
        # started_at 秒超时），重交才能走完成/拒绝路径，信誉可通过重验恢复。
        await conn.execute(
            f"UPDATE {SCHEMA}.steps SET status = 'in_progress', "
            f"status_reason = 'reverifying', started_at = NOW(), "
            f"outputs = COALESCE(outputs, '{{}}'::jsonb) || "
            f"'{{\"reverify_requested\": true, \"reverify_requested_at\": \"{_now_iso()}\"}}'::jsonb, "
            f"updated_at = NOW() WHERE step_id = $1",
            step_id,
        )

        # 通知 Agent 重新验证
        agent = step.get("assigned_agent")
        if agent:
            await dispatcher.ws_notify(agent, "reverify_requested", {
                "task_id": step["task_id"],
                "step_id": step_id,
                "step_index": step["step_index"],
                "title": step["title"],
                "reason": "创建者发起抽查，请重新执行 agent-report 检查规则并提交结果",
            })

        fresh = await sm.get_step(conn, step_id)
        if fresh:
            asyncio.create_task(runner.step_hooks(dict(fresh), agent or "", "reverify_requested"))

        logger.info("Reverify requested for step=%s by creator", step_id)
        return {
            "step_id": step_id,
            "status": "reverify_requested",
            "agent_notified": bool(agent),
        }


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── 多签交叉验证（§3.4.3 Layer 4）───────────────────

@router.get("/multisig/pending")
async def list_pending_multisig(agent_id: str = Query(default="")):
    """查询待认领的多签验证任务。agent_id 可选——限定只查特定 Step 的 assigned_agent 非自身的任务。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT v.verification_id, v.step_id, v.task_id, v.rule_details, v.notes, "
            f"s.title, s.step_index, s.assigned_agent "
            f"FROM {SCHEMA}.verifications v "
            f"JOIN {SCHEMA}.steps s ON v.step_id = s.step_id "
            f"WHERE v.check_mode = 'multisig_pending' "
            f"AND ('' = $1 OR s.assigned_agent != $1) "
            f"ORDER BY v.verified_at LIMIT 50",
            agent_id or "",
        )
    tasks = []
    for r in rows:
        tasks.append({
            "verification_id": r["verification_id"],
            "step_id": r["step_id"],
            "task_id": r["task_id"],
            "step_title": r["title"],
            "step_index": r["step_index"],
            "executor": r["assigned_agent"],
            "acceptance_criteria": (r["rule_details"] if isinstance(r["rule_details"], list)
                                   else json.loads(r["rule_details"]) if isinstance(r["rule_details"], str)
                                   else None),
            "notes": r["notes"],
        })
    return {"tasks": tasks, "total": len(tasks)}


@router.post("/multisig/{verification_id}/claim")
async def claim_multisig(verification_id: int, agent_id: str = Query(default="")):
    """验证 Agent 认领多签任务。"""
    if not agent_id:
        raise AppError("VALIDATION_ERROR", "agent_id 必填", 400)
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await claim_verification(conn, verification_id, agent_id)
    if not result:
        raise HTTPException(404, "多签任务不存在或已被认领")
    return result


@router.post("/multisig/{verification_id}/submit")
async def submit_multisig(verification_id: int, req: SubmitRequest):
    """验证 Agent 提交多签验证结果（复用 SubmitRequest，outputs 含 check_results）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await submit_verification(
            conn, verification_id, req.agent_id,
            req.outputs.get("check_results", {}),
        )
    if not result.get("success"):
        raise AppError("CONFLICT", result.get("error", "提交失败"), 409)
    return result


# ── 行为规范（v1.10）───────────────────────────────────


@router.get("/policies")
async def list_policies(auth: dict = Depends(auth_dependency)):
    """列出所有行为规范策略（需认证）。"""
    policies = await policy_list()
    return {"policies": policies, "total": len(policies)}


@router.post("/policies")
async def create_policy(req: CreatePolicyRequest, auth: dict = Depends(auth_dependency)):
    """创建行为规范策略（需 admin/ops_admin）。"""
    if auth.get("role") not in ("admin", "ops_admin"):
        return JSONResponse(status_code=403, content={"error": "FORBIDDEN"})
    rule = req.rule.model_dump(exclude_none=True)
    result = await policy_create(
        name=req.name, policy_type=req.policy_type, rule=rule,
        action=req.action, created_by=auth.get("agent_id", req.created_by),
        agent_id=req.agent_id or "", category=req.category or "",
        reject_message=req.reject_message, priority=req.priority,
    )
    return result


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: int, auth: dict = Depends(auth_dependency)):
    """查看单条策略。"""
    p = await policy_get(policy_id)
    if not p:
        raise HTTPException(404, "策略不存在")
    return p


@router.put("/policies/{policy_id}")
async def update_policy(policy_id: int, req: UpdatePolicyRequest, auth: dict = Depends(auth_dependency)):
    """编辑行为规范策略（需 admin/ops_admin）。"""
    if auth.get("role") not in ("admin", "ops_admin"):
        return JSONResponse(status_code=403, content={"error": "FORBIDDEN"})
    updates = {}
    if req.name:
        updates["name"] = req.name
    if req.policy_type:
        updates["policy_type"] = req.policy_type
    if req.action:
        updates["action"] = req.action
    if req.reject_message:
        updates["reject_message"] = req.reject_message
    if req.priority >= 0:
        updates["priority"] = req.priority
    if req.enabled is not None:
        updates["enabled"] = req.enabled
    if req.agent_id:
        updates["agent_id"] = req.agent_id
    if req.category:
        updates["category"] = req.category
    result = await policy_update(policy_id, updates)
    if not result:
        raise HTTPException(404, "策略不存在")
    return result


@router.delete("/policies/{policy_id}")
async def delete_policy(policy_id: int, auth: dict = Depends(auth_dependency)):
    """删除行为规范策略（需 admin/ops_admin）。"""
    if auth.get("role") not in ("admin", "ops_admin"):
        return JSONResponse(status_code=403, content={"error": "FORBIDDEN"})
    ok = await policy_delete(policy_id)
    if not ok:
        raise HTTPException(404, "策略不存在")
    return {"status": "deleted", "policy_id": policy_id}


@router.post("/policies/check")
async def check_policy(req: PolicyCheckRequest):
    """测试策略匹配。"""
    result = await policy_check(req.agent_id, {
        "title": req.title,
        "description": req.description,
        "steps": req.steps,
    })
    return result


# ── 健康检查 ──────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "module": "zhice", "version": "1.9.0"}


# ── Phase C: 质量可观测性 ──────────────────────────────────

@router.get("/tasks/{task_id}/quality-stats")
async def task_quality_stats(task_id: int):
    """返回单 Task 的质量统计。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        task = await sm.get_task(conn, task_id)
        if not task:
            raise HTTPException(404, "Task 不存在")

        steps = await sm.get_task_steps(conn, task_id)
        qs = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.task_quality_stats WHERE task_id = $1",
            task_id,
        )

        step_iterations = []
        total_iterations = 0
        has_qc = False
        for s in steps:
            it_log = _safe_jsonb(s.get("iteration_log"))
            it_count = len(it_log) if it_log else 0
            if it_count:
                total_iterations += it_count
            if s.get("quality_criteria"):
                has_qc = True
            step_iterations.append({
                "step_id": s["step_id"],
                "step_index": s["step_index"],
                "title": s["title"],
                "iteration_count": it_count,
                "max_iterations": s.get("max_iterations", 3),
            })

        return {
            "task_id": task_id,
            "title": task["title"],
            "has_quality_criteria": has_qc,
            "iteration_count": total_iterations,
            "max_iterations": max((s.get("max_iterations") or 3) for s in steps) if steps else 0,
            "engine_recheck_fails": qs["engine_recheck_fails"] if qs else 0,
            "failure_patterns": _safe_jsonb(qs.get("failure_patterns")) if qs else None,
            "step_iterations": step_iterations,
            "created_at": qs["created_at"] if qs else None,
        }


@router.get("/quality/trends")
async def quality_trends(workflow_id: int = None, limit: int = 20):
    """跨 Task 质量趋势。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = ""
        params = [limit]
        if workflow_id:
            where = "WHERE workflow_id = $2"
            params.append(workflow_id)

        rows = await conn.fetch(
            f"SELECT tqs.*, t.title, t.completed_at FROM {SCHEMA}.task_quality_stats tqs "
            f"JOIN {SCHEMA}.tasks t ON t.task_id = tqs.task_id "
            f"{where} ORDER BY tqs.created_at DESC LIMIT $1",
            *params,
        )

        trends = []
        total_tasks = len(rows)
        qc_count = sum(1 for r in rows if r["has_quality_criteria"])
        total_iterations = sum(r["iteration_count"] for r in rows)
        total_recheck_fails = sum(r["engine_recheck_fails"] for r in rows)

        # failure pattern 聚合
        all_patterns = {}
        for r in rows:
            pats = _safe_jsonb(r.get("failure_patterns"))
            if pats:
                for p in pats:
                    d = p.get("detail", "")[:60]
                    if d:
                        all_patterns[d] = all_patterns.get(d, 0) + 1

        top_patterns = [
            {"pattern": k, "count": v}
            for k, v in sorted(all_patterns.items(), key=lambda x: -x[1])[:5]
        ]

        for r in rows:
            trends.append({
                "task_id": r["task_id"],
                "title": r["title"],
                "has_quality_criteria": r["has_quality_criteria"],
                "iteration_count": r["iteration_count"],
                "engine_recheck_fails": r["engine_recheck_fails"],
                "completed_at": r["completed_at"],
            })

        return {
            "trends": trends,
            "summary": {
                "total_tasks": total_tasks,
                "qc_coverage": round(qc_count / total_tasks, 2) if total_tasks else 0,
                "avg_iterations": round(total_iterations / total_tasks, 1) if total_tasks else 0,
                "total_recheck_fails": total_recheck_fails,
                "top_failure_patterns": top_patterns,
            },
        }
