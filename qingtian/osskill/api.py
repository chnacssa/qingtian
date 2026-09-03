"""
技能库 — 管理服 REST API

路由前缀: /api/v1/skills
所有 management 接口通过 Depends(require_management) 鉴权

审核状态转换规则:
  proposed    → submit-review  → in_review
  in_review   → approve        → active
  in_review   → reject         → rejected
  proposed    → reject         → rejected（快捷驳回）
  active      → deprecate      → deprecated
  deprecated  → archive        → archived
  rejected    → (终态，不可操作)
  archived    → (终态，不可操作)
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from common.db import get_pool
from common.config import is_management
from .database import (
    SCHEMA,
    get_skill_by_id,
    list_skills,
    add_review,
    update_status,
    get_agent_skills,
    bind_skill,
    unbind_skill,
)
from .deps import build_graph, CycleError
from huanyu.pubsub import publish

router = APIRouter(prefix="/api/v1/skills", tags=["技能库"])
logger = logging.getLogger(__name__)


# ── Pydantic 模型 ──────────────────────────────────


class ReviewRequest(BaseModel):
    comment: str = ""


class ApproveRequest(BaseModel):
    reviewer: str
    version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    comment: str = ""
    applicable_agents: list[str] = []


class RejectRequest(BaseModel):
    reviewer: str
    reason: str = ""
    review_stage: str = "value_assessment"


class DeprecateRequest(BaseModel):
    reason: str = ""
    replacement_id: Optional[int] = None


class VersionRequest(BaseModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    changelog: str = ""
    breaking_changes: list[str] = []


class BindRequest(BaseModel):
    config: dict = {}
    pinned_version: str = ""


# ── 依赖 ────────────────────────────────────────────


async def require_management():
    """FastAPI Depends: 校验 management 角色"""
    if not is_management():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "仅 management 角色可操作"},
        )


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


# ── 权限分析 ──────────────────────────────────

_PERMISSION_LEVELS = {
    "network": "L2",
    "network:outbound": "L2",
    "filesystem": "L2",
    "filesystem:config": "L3",
    "llm": "L2",
    "skills": "L2",
    "system": "L2",
    "identity": "L3",
    "lifecycle": "L3",
}

_PERMISSION_DESCRIPTIONS = {
    "network": "底座 API 内网调用",
    "network:outbound": "HTTP/HTTPS 出站",
    "filesystem": "Skill data 目录读写",
    "filesystem:config": "读取底座配置目录",
    "llm": "LLM 代理调用",
    "skills": "跨 Skill 调用",
    "system": "系统命令执行",
    "identity": "Agent 身份凭证",
    "lifecycle": "管理其他 Skill",
}


def _enrich_permission_info(skill: dict) -> dict:
    """为 Skill 详情补充权限分析信息"""
    skill = dict(skill)  # 避免修改原 dict
    permissions = skill.pop("permissions", None) or []
    sast_result = skill.pop("sast_result", None) or {}

    # 权限详情列表
    perm_details = []
    for p in permissions:
        perm_details.append({
            "permission": p,
            "level": _PERMISSION_LEVELS.get(p, "L1"),
            "description": _PERMISSION_DESCRIPTIONS.get(p, ""),
        })

    skill["permissions"] = {
        "declared": perm_details,
        "analysis": sast_result.get("sast") if isinstance(sast_result, dict) else None,
    }
    return skill


# ── 状态转换校验 ──────────────────────────────────

_VALID_TRANSITIONS = {
    "proposed": {"submit-review": "in_review", "reject": "rejected"},
    "in_review": {"approve": "active", "reject": "rejected"},
    "active": {"deprecate": "deprecated"},
    "deprecated": {"archive": "archived"},
}

_TERMINAL_STATES = {"rejected", "archived"}


def _check_transition(current_status: str, action: str) -> str:
    """校验状态转换，返回目标状态。非法时抛出 HTTPException。"""
    if current_status in _TERMINAL_STATES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "TERMINAL_STATE",
                "message": f"当前状态 {current_status} 为终态，不允许任何操作",
            },
        )
    transitions = _VALID_TRANSITIONS.get(current_status, {})
    target = transitions.get(action)
    if not target:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_TRANSITION",
                "message": f"当前状态 {current_status} 不允许执行 {action} 操作",
            },
        )
    return target


# ── 路由 ────────────────────────────────────────────


@router.get("")
async def api_list_skills(
    status: str = Query(default=""),
    category: str = Query(default=""),
    source: str = Query(default=""),
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """技能列表查询（任意角色可访问）"""
    skills, total = await list_skills(
        status=status, category=category, source=source,
        q=q, page=page, page_size=page_size,
    )
    return {"skills": skills, "total": total, "page": page, "page_size": page_size}


@router.get("/{skill_id}")
async def api_get_skill(skill_id: int):
    """技能详情（含版本、审核、绑定、权限分析）"""
    skill = await get_skill_by_id(skill_id)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
        )
    skill = _enrich_permission_info(skill)
    return {"skill": skill}


@router.get("/{skill_id}/bindings")
async def api_get_bindings(skill_id: int):
    """某个 Skill 的所有 Agent 绑定（反向查询）"""
    skill = await get_skill_by_id(skill_id)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
        )
    return {
        "skill_id": skill_id,
        "skill_name": skill["name"],
        "bindings": skill["bound_agents"],
    }


@router.get("/agents/{agent_id}")
async def api_get_agent_skills(agent_id: str, request: Request):
    """Agent 技能列表

    P1 (R11): 原实现无身份校验——任意人可枚举任意 agent 的技能/绑定配置（IDOR）。
    仅允许：本人查询（request.state 由网关注入）、admin 角色、management 部署。
    """
    req_agent = getattr(request.state, "agent_id", "") or ""
    req_role = getattr(request.state, "role", "") or ""
    if not (req_agent == agent_id or req_role == "admin" or is_management()):
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "仅可查询本人技能或需管理权限"},
        )
    skills = await get_agent_skills(agent_id)
    return {"agent_id": agent_id, "skills": skills}


@router.post("/{skill_id}/submit-review", dependencies=[Depends(require_management)])
async def api_submit_review(skill_id: int, body: ReviewRequest):
    """提交审核: proposed → in_review"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id, status FROM {SCHEMA}.skill_definitions WHERE id = $1",
            skill_id,
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
            )

        target = _check_transition(row["status"], "submit-review")

        await update_status(skill_id, target)
        await add_review(
            skill_id=skill_id, action="submit-review", reviewer="system",
            reason=body.comment, from_status=row["status"], to_status=target,
        )

    return {
        "id": skill_id,
        "status": target,
        "message": "已提交审核",
        "timestamp": _ts(),
    }


@router.post("/{skill_id}/approve", dependencies=[Depends(require_management)])
async def api_approve_skill(skill_id: int, body: ApproveRequest):
    """批准: in_review → active"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id, status FROM {SCHEMA}.skill_definitions WHERE id = $1",
            skill_id,
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
            )

        target = _check_transition(row["status"], "approve")

        await update_status(
            skill_id, target,
            version=body.version,
            activated_at=datetime.now(timezone.utc),
            applicable_agents=body.applicable_agents,
        )

        # 首次 active 写入 skill_versions
        await conn.execute(
            f"""INSERT INTO {SCHEMA}.skill_versions (skill_id, version, changelog)
                VALUES ($1, $2, $3)
                ON CONFLICT (skill_id, version) DO NOTHING""",
            skill_id, body.version, body.comment or "审核通过",
        )

        await add_review(
            skill_id=skill_id, action="approve", reviewer=body.reviewer,
            reason=body.comment, from_status=row["status"], to_status=target,
        )

    return {
        "id": skill_id,
        "status": target,
        "version": body.version,
        "applicable_agents": body.applicable_agents,
        "message": "已批准",
        "timestamp": _ts(),
    }


@router.post("/{skill_id}/reject", dependencies=[Depends(require_management)])
async def api_reject_skill(skill_id: int, body: RejectRequest):
    """驳回: 任意非终态 → rejected"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id, status FROM {SCHEMA}.skill_definitions WHERE id = $1",
            skill_id,
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
            )

        target = _check_transition(row["status"], "reject")

        await update_status(
            skill_id, target,
            rejection_reason=body.reason,
        )
        await add_review(
            skill_id=skill_id, action="reject", reviewer=body.reviewer,
            reason=f"[{body.review_stage}] {body.reason}",
            from_status=row["status"], to_status=target,
        )

    return {
        "id": skill_id,
        "status": target,
        "message": "已驳回",
        "timestamp": _ts(),
    }


@router.post("/{skill_id}/deprecate", dependencies=[Depends(require_management)])
async def api_deprecate_skill(skill_id: int, body: DeprecateRequest):
    """弃用: active → deprecated"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id, status FROM {SCHEMA}.skill_definitions WHERE id = $1",
            skill_id,
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
            )

        target = _check_transition(row["status"], "deprecate")

        await update_status(
            skill_id, target,
            deprecated_at=datetime.now(timezone.utc),
            replacement_id=body.replacement_id,
            rejection_reason=body.reason,
        )
        await add_review(
            skill_id=skill_id, action="deprecate", reviewer="system",
            reason=body.reason, from_status=row["status"], to_status=target,
        )

    return {
        "id": skill_id,
        "status": target,
        "message": "已标记弃用",
        "timestamp": _ts(),
    }


@router.post("/{skill_id}/archive", dependencies=[Depends(require_management)])
async def api_archive_skill(skill_id: int):
    """归档: deprecated → archived"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id, status FROM {SCHEMA}.skill_definitions WHERE id = $1",
            skill_id,
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
            )

        target = _check_transition(row["status"], "archive")

        await update_status(skill_id, target)
        await add_review(
            skill_id=skill_id, action="archive", reviewer="system",
            reason="归档", from_status=row["status"], to_status=target,
        )

    return {
        "id": skill_id,
        "status": target,
        "message": "已归档",
        "timestamp": _ts(),
    }


@router.post("/{skill_id}/versions", dependencies=[Depends(require_management)])
async def api_register_version(skill_id: int, body: VersionRequest):
    """注册新版本"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id FROM {SCHEMA}.skill_definitions WHERE id = $1",
            skill_id,
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
            )

        await conn.execute(
            f"""INSERT INTO {SCHEMA}.skill_versions
                (skill_id, version, changelog, breaking_changes)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (skill_id, version) DO UPDATE SET
                    changelog = EXCLUDED.changelog,
                    breaking_changes = EXCLUDED.breaking_changes""",
            skill_id, body.version, body.changelog, body.breaking_changes,
        )

    return {
        "id": skill_id,
        "version": body.version,
        "message": "版本注册成功",
        "timestamp": _ts(),
    }


@router.post(
    "/agents/{agent_id}/bind/{skill_id}",
    dependencies=[Depends(require_management)],
)
async def api_bind_skill(agent_id: str, skill_id: int, body: BindRequest | None = None):
    """Agent 绑定 Skill"""
    body = body or BindRequest()
    # 先检查 Skill 是否存在及 applicable_agents
    skill = await get_skill_by_id(skill_id)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
        )

    aa = skill.get("applicable_agents", [])
    if aa and agent_id not in aa:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AGENT_NOT_APPLICABLE",
                "message": f"该 Skill 不适用于此 Agent（applicable_agents: {aa}）",
            },
        )

    created = await bind_skill(
        agent_id, skill_id,
        config=body.config,
        pinned_version=body.pinned_version,
    )
    if not created:
        raise HTTPException(
            status_code=409,
            detail={"code": "ALREADY_BOUND", "message": "Agent 已绑定该 Skill"},
        )

    # 发布 SKILL_BIND_CHANGED 事件
    try:
        await publish("huanyu:skill_bind_changed", {
            "agent_id": agent_id,
            "action": "bind",
            "skill_id": skill_id,
            "skill_name": skill["name"],
        })
    except Exception as e:
        logger.warning("skill_bind_changed publish failed (bind): %s", e)

    return {
        "agent_id": agent_id,
        "skill_id": skill_id,
        "is_active": True,
        "message": "绑定成功",
        "timestamp": _ts(),
    }


@router.delete(
    "/agents/{agent_id}/bind/{skill_id}",
    dependencies=[Depends(require_management)],
)
async def api_unbind_skill(agent_id: str, skill_id: int):
    """Agent 解绑 Skill"""
    skill = await get_skill_by_id(skill_id)
    if skill is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SKILL_NOT_FOUND", "message": "Skill not found"},
        )

    deleted = await unbind_skill(agent_id, skill_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "BINDING_NOT_FOUND",
                "message": f"Agent {agent_id} 未绑定 Skill {skill_id}",
            },
        )

    # 发布 SKILL_BIND_CHANGED 事件
    try:
        await publish("huanyu:skill_bind_changed", {
            "agent_id": agent_id,
            "action": "unbind",
            "skill_id": skill_id,
            "skill_name": skill["name"],
        })
    except Exception as e:
        logger.warning("skill_bind_changed publish failed (unbind): %s", e)

    return {
        "agent_id": agent_id,
        "skill_id": skill_id,
        "message": "已解绑",
        "timestamp": _ts(),
    }


# ── 运行时管理端点（R3 新增） ─────────────────────────


@router.post(
    "/runtime/{skill_name}/start",
    dependencies=[Depends(require_management)],
)
async def api_runtime_start(skill_name: str, agent_id: str = ""):
    """启动 Skill 子进程"""
    runtime = getattr(api_runtime_start, "_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "RUNTIME_NOT_READY", "message": "XiheRuntime 未初始化"},
        )
    try:
        handle = await runtime.launch_skill(skill_name, agent_id=agent_id)
        return {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "status": "started",
            "message": f"Skill '{skill_name}' 已启动",
            "timestamp": _ts(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "START_FAILED", "message": str(e)[:500]},
        )


@router.post(
    "/runtime/{skill_name}/stop",
    dependencies=[Depends(require_management)],
)
async def api_runtime_stop(skill_name: str, agent_id: str = ""):
    """停止 Skill 子进程"""
    runtime = getattr(api_runtime_stop, "_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "RUNTIME_NOT_READY", "message": "XiheRuntime 未初始化"},
        )
    try:
        await runtime.stop_skill(skill_name, agent_id=agent_id)
        return {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "status": "stopped",
            "message": f"Skill '{skill_name}' 已停止",
            "timestamp": _ts(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "STOP_FAILED", "message": str(e)[:500]},
        )


@router.post(
    "/runtime/{skill_name}/restart",
    dependencies=[Depends(require_management)],
)
async def api_runtime_restart(skill_name: str, agent_id: str = ""):
    """重启 Skill 子进程"""
    runtime = getattr(api_runtime_restart, "_runtime", None)
    if runtime is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "RUNTIME_NOT_READY", "message": "XiheRuntime 未初始化"},
        )
    try:
        await runtime.stop_skill(skill_name, agent_id=agent_id)
        handle = await runtime.launch_skill(skill_name, agent_id=agent_id)
        return {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "status": "restarted",
            "message": f"Skill '{skill_name}' 已重启",
            "timestamp": _ts(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={"code": "RESTART_FAILED", "message": str(e)[:500]},
        )


@router.get("/runtime/stats")
async def api_runtime_stats():
    """运行时统计（所有运行中的 Skill）"""
    runtime = getattr(api_runtime_stats, "_runtime", None)
    if runtime is None:
        return {"skills": [], "total": 0, "max_processes": 0}
    skills = await runtime.list_skills()
    config = runtime.config
    return {
        "skills": skills,
        "total": len(skills),
        "max_processes": config.max_processes,
    }


# ── 使用统计 API（Phase 3） ─────────────────────────────


@router.get("/usage-stats")
async def api_usage_stats(
    skill_name: str = Query(default=""),
    agent_id: str = Query(default=""),
    days: int = Query(default=7, ge=1, le=365),
):
    """查询 Skill 使用统计

    Args:
        skill_name: 按 Skill 名筛选（可选）
        agent_id: 按 Agent 筛选（可选）
        days: 最近 N 天（默认 7，最长 365）
    """
    pool = await get_pool()
    conditions = ["stat_date >= $1"]
    # P2 (R11): monitor.flush_to_db 改存 UTC 日期，此处筛选也统一 UTC，
    # 避免本地日期 vs UTC 存储跨时区相差一天导致统计窗口错位。
    params = [datetime.now(timezone.utc).date() - timedelta(days=days)]
    idx = 2

    if skill_name:
        conditions.append(f"skill_name = ${idx}")
        params.append(skill_name)
        idx += 1
    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT skill_name, agent_id,
                       SUM(invoke_count)::int AS total_invoke,
                       SUM(success_count)::int AS total_success,
                       CASE WHEN SUM(invoke_count) > 0
                           THEN ROUND(
                               SUM(success_count)::numeric / SUM(invoke_count), 4)
                           ELSE 0
                       END AS success_rate,
                       ROUND(AVG(avg_latency_ms))::int AS avg_latency_ms,
                       MAX(stat_date) AS last_stat_date
                FROM {SCHEMA}.skill_usage_stats
                WHERE {where}
                GROUP BY skill_name, agent_id
                ORDER BY total_invoke DESC""",
            *params,
        )

    stats = []
    for r in rows:
        stats.append({
            "skill_name": r["skill_name"],
            "agent_id": r["agent_id"],
            "total_invoke": r["total_invoke"],
            "total_success": r["total_success"],
            "success_rate": float(r["success_rate"]),
            "avg_latency_ms": r["avg_latency_ms"],
            "last_stat_date": r["last_stat_date"].isoformat()
            if r["last_stat_date"] else "",
        })

    # 汇总行
    total_invoke = sum(s["total_invoke"] for s in stats)
    total_success = sum(s["total_success"] for s in stats)

    return {
        "stats": stats,
        "total": len(stats),
        "summary": {
            "total_invoke": total_invoke,
            "total_success": total_success,
            "overall_success_rate": round(total_success / total_invoke, 4)
            if total_invoke > 0 else 0,
            "days": days,
        },
    }


# ── 依赖拓扑 API（Phase 3） ────────────────────────────


@router.get("/dependency-graph")
async def api_dependency_graph():
    """依赖拓扑图（从 DB 中 active 状态的 Skill 构建）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, name, version, knowledge_deps, tool_deps, model_deps, status "
            f"FROM {SCHEMA}.skill_definitions WHERE status = 'active'")
        all_skills = await conn.fetch(
            f"SELECT id, name, version, knowledge_deps, tool_deps, model_deps, status "
            f"FROM {SCHEMA}.skill_definitions")

    # 构建依赖图（skill_definitions 无 skill→skill 依赖列，
    # 从知识/工具/模型依赖中取指向真实 Skill 的边，避免缺节点被误报成环）
    all_names = {r["name"] for r in all_skills}
    skills_dict = {}
    for row in rows:
        deps = {}
        for dep in (row.get("knowledge_deps") or []):
            if dep in all_names:
                deps[dep] = "*"
        for dep in (row.get("tool_deps") or []):
            if dep in all_names:
                deps[dep] = "*"
        model_dep = row.get("model_deps") or ""
        if model_dep in all_names:
            deps[model_dep] = "*"
        skills_dict[row["name"]] = {
            "version": row["version"],
            "deps": deps,
            "skill_id": row["id"],
        }

    graph = build_graph(skills_dict)

    # 拓扑排序
    try:
        topo_order = graph.topo_sort()
        has_cycle = False
        cycle_path = None
    except CycleError:
        # 有循环时回退到 detect_cycle
        topo_order = list(skills_dict.keys())
        has_cycle = True
        cycle_path = graph.detect_cycle()

    # 所有 Skill 汇总（含非 active）
    all_names = {}
    for row in all_skills:
        all_names[row["name"]] = {
            "skill_id": row["id"],
            "version": row["version"],
            "status": row["status"],
        }

    return {
        "node_count": len(skills_dict),
        "topological_order": topo_order,
        "dependencies": {
            name: {
                "deps": list(graph.get_dependencies(name)),
                "dependents": graph.get_dependents(name),
            }
            for name in skills_dict
        },
        "nodes": all_names,
        "has_cycle": has_cycle,
        "cycle_path": cycle_path,
    }
