"""
羲和（Xihe / Agent Runtime Manager）— API 路由

Agent 运行时全生命周期管理端点：
  - 接管（adopt-self / adopt）
  - 状态查询（status / stats / health）
  - 生命周期控制（pause / resume / stop / restart）
  - 简报（briefing）
  - 状态上报（report-status）

所有端点使用 /v1/xihe 前缀，与寰宇目录 API（/v1/huanyu）分离。
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from zhenyue.auth import verify_admin_token

from . import agent_runtime as arm

router = APIRouter(prefix="/v1/xihe")


# ── 模型 ────────────────────────────────────────────────

class HealthCheckConfig(BaseModel):
    type: str = "process"                 # http / process / script
    endpoint: str = ""
    timeout_seconds: int = 5
    expected_status: int = 200


class AdoptSelfRequest(BaseModel):
    pid: int
    launch_command: str = ""
    cwd: str = ""
    env: dict = {}
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    version: str = ""


class AdoptRequest(BaseModel):
    agent_id: str
    pid: int
    launch_command: str = ""
    cwd: str = ""
    env: dict = {}
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)


class ReportStatusRequest(BaseModel):
    status: str = "started"               # started / running / stopping / error
    version: str = ""
    changes: dict = Field(default_factory=lambda: {
        "added": [], "modified": [], "fixed": [], "removed": [],
    })
    health: dict = {}


class BriefingResponse(BaseModel):
    agent_id: str
    state: str = "ready"
    modules: dict = Field(default_factory=dict)
    peers: dict = Field(default_factory=lambda: {"base_host": "", "base_port": 1996})
    capabilities: list = []
    skills: list = []
    timestamp: str = ""


# ── 辅助函数 ────────────────────────────────────────────

def _get_mgr():
    return arm.get_manager()


def _get_default_modules(agent_id: str) -> dict:
    """返回默认模块地址（注入简报和上下文使用）"""
    return {
        "memory": {"endpoint": "/v1/yongheng", "namespace": f"agent:{agent_id}"},
        "knowledge": {"endpoint": "/v1/huichuan"},
        "tasks": {"endpoint": "/v1/zhice", "assignment_mode": "push"},
        "billing": {"endpoint": "/v1/siku"},
        "inbox": {"endpoint": "/v1/huanyu"},
    }


# ── 接管 ─────────────────────────────────────────────────

@router.post("/agents/{agent_id}/adopt-self")
async def adopt_self(agent_id: str, req: AdoptSelfRequest):
    """Agent 主动投靠接管

    Agent 调用此接口向底座报告自身进程信息，
    底座开始全生命周期监控。
    """
    mgr = _get_mgr()
    result = await mgr.adopt_external(agent_id, req.model_dump())
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/agents/{agent_id}/adopt")
async def adopt_agent(agent_id: str, req: AdoptRequest,
                      _admin: str = Depends(verify_admin_token)):
    """管理员接管一个已注册的 Agent

    A2 (R11): 需要 X-Admin-Token（管理控制台）校验，未认证者无法接管。
    管理员指定 PID 后底座开始监控。
    """
    mgr = _get_mgr()
    result = await mgr.adopt_external(agent_id, req.model_dump())
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ── 状态查询 ─────────────────────────────────────────────

@router.get("/agents/{agent_id}/status")
async def agent_status(agent_id: str):
    """查询单个 Agent 的运行时状态"""
    mgr = _get_mgr()
    status = mgr.get_agent_status(agent_id)
    if not status:
        raise HTTPException(status_code=404, detail="Agent 未托管")
    return status


@router.get("/agents")
async def list_agents(
    status: Optional[str] = Query(None, description="按状态过滤"),
):
    """列出所有托管 Agent"""
    mgr = _get_mgr()
    agents = mgr.list_agents()
    if status:
        agents = [a for a in agents if a.get("status") == status]
    return {"agents": agents, "count": len(agents)}


@router.get("/stats")
async def xihe_stats(by_agent: bool = Query(False, description="按 Agent 明细返回")):
    """羲和运行统计"""
    mgr = _get_mgr()
    stats = await mgr._get_system_stats()
    if by_agent:
        agent_stats = []
        for agent_id in list(mgr._processes.keys()):
            ps = await mgr._get_process_stats(agent_id)
            if ps:
                agent_stats.append(ps)
        stats["agent_stats"] = agent_stats
    return stats


@router.get("/health")
async def xihe_health():
    """羲和健康检查"""
    mgr = _get_mgr()
    status_counts = {}
    for ap in mgr._processes.values():
        s = ap.status
        status_counts[s] = status_counts.get(s, 0) + 1

    fatal_count = status_counts.get("fatal", 0)
    healthy = fatal_count == 0

    return {
        "status": "ok" if healthy else "degraded",
        "managed_agents": len(mgr._processes),
        "status_counts": status_counts,
        "healthy": healthy,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── 生命周期控制 ────────────────────────────────────────

@router.post("/agents/{agent_id}/pause")
async def pause_agent(agent_id: str, _admin: str = Depends(verify_admin_token)):
    """暂停 Agent（用户主动暂停，底座停止监控）"""
    mgr = _get_mgr()
    ap = mgr._processes.get(agent_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Agent 未托管")
    if ap.status == "paused":
        return {"status": "ok", "message": f"Agent {agent_id} 已处于暂停状态"}

    old_status = ap.status
    ap.status = "paused"
    await mgr._update_process_db(agent_id, "paused")
    logger = arm.logger
    logger.info("[Xihe] 暂停 %s (之前状态=%s)", agent_id, old_status)
    return {"status": "ok", "agent_id": agent_id, "previous_status": old_status}


@router.post("/agents/{agent_id}/resume")
async def resume_agent(agent_id: str, _admin: str = Depends(verify_admin_token)):
    """恢复 Agent（用户恢复，底座重新接管）"""
    mgr = _get_mgr()
    ap = mgr._processes.get(agent_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Agent 未托管")
    if ap.status != "paused":
        return {"status": "ok", "message": f"Agent {agent_id} 未暂停，无需恢复"}

    # 检查进程是否仍存活
    pid_alive = False
    if ap.pid:
        try:
            import os
            os.kill(ap.pid, 0)
            pid_alive = True
        except (ProcessLookupError, OSError):
            pid_alive = False

    if pid_alive:
        ap.status = "running"
        ap._healthy_since = datetime.now(timezone.utc)
    else:
        ap.status = "stopped"
        ap.last_error = "resume: 进程已不存在"

    await mgr._update_process_db(agent_id, ap.status)
    logger = arm.logger
    logger.info("[Xihe] 恢复 %s (pid_alive=%s)", agent_id, pid_alive)
    return {
        "status": "ok",
        "agent_id": agent_id,
        "new_status": ap.status,
        "pid_alive": pid_alive,
    }


@router.post("/agents/{agent_id}/stop")
async def stop_agent(agent_id: str, _admin: str = Depends(verify_admin_token)):
    """停止 Agent（用户永久停止，从 agent_processes 移除）"""
    mgr = _get_mgr()
    ap = mgr._processes.get(agent_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Agent 未托管")

    # 如果进程由底座启动，先停止
    if ap.proc is not None:
        await mgr.stop_agent(agent_id)
    elif ap.pid:
        # 外部进程 — 发送信号
        try:
            import signal
            import os
            os.kill(ap.pid, signal.SIGTERM)
            ap.status = "stopped"
            ap.stopped_at = datetime.now(timezone.utc)
        except (ProcessLookupError, OSError):
            ap.status = "stopped"
            ap.stopped_at = datetime.now(timezone.utc)

    ap.status = "stopped"
    ap.pid = None
    ap.proc = None
    await mgr._update_process_db(agent_id, "stopped")
    logger = arm.logger
    logger.info("[Xihe] 停止 %s", agent_id)
    return {"status": "ok", "agent_id": agent_id}


@router.post("/agents/{agent_id}/restart")
async def restart_agent(agent_id: str, _admin: str = Depends(verify_admin_token),
                        force: bool = Query(False, description="绕过 fatal 检查强制重启")):
    """重启 Agent（适用于底座启动的进程）

    force=true: 绕过 fatal 检查，强制重启（设计文档 §3.3.4 方案 B）。
    """
    mgr = _get_mgr()
    ap = mgr._processes.get(agent_id)

    # force 模式：重置 fatal 状态再重启
    if force and ap and ap.status == "fatal":
        ap.status = "stopped"
        ap._consecutive_restarts = 0
        ap._backoff_index = 0
        ap.restart_count = 0
        ap.last_error = ""
        await mgr._update_process_db(agent_id, "stopped")

    ok = await mgr.restart_agent(agent_id)
    if not ok:
        raise HTTPException(status_code=500, detail=f"重启 {agent_id} 失败")
    return {"status": "ok", "agent_id": agent_id, "force": force}


# ── 简报 ─────────────────────────────────────────────────

@router.get("/agents/{agent_id}/briefing")
async def agent_briefing(agent_id: str):
    """Agent 入职简报

    Agent 通过此接口获取当前运行时环境信息：
    - 模块地址
    - 对端信息
    - 自身状态
    - 绑定的技能

    Agent 不需要记忆这些东西——每次请求的响应都附带上下文（通过 BusScheduler），
    briefing 是首次连接或上下文丢失时的初始化入口。
    """
    mgr = _get_mgr()
    ap = mgr._processes.get(agent_id)
    state = ap.status if ap else "unknown"

    return {
        "agent_id": agent_id,
        "state": state,
        "modules": _get_default_modules(agent_id),
        "peers": {
            "base_host": "localhost",
            "base_port": 1996,
        },
        "capabilities": [],
        "skills": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── 人工恢复 ─────────────────────────────────────────────

@router.post("/agents/{agent_id}/resume-from-fatal")
async def resume_from_fatal(agent_id: str, _admin: str = Depends(verify_admin_token)):
    """人工恢复 fatal 状态的 Agent

    设计文档 §3.3.4 — 三选一恢复手段的 A 方案（推荐）：
    重置 fatal 状态为 stopped，由 reconciliation 自动拉起。
    """
    mgr = _get_mgr()
    ap = mgr._processes.get(agent_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Agent 未托管")
    if ap.status != "fatal":
        return {"status": "ok", "agent_id": agent_id, "notice": "Agent 非 fatal 状态，无需恢复"}

    # 重置状态为 stopped → reconciliation 自动拉起
    ap.status = "stopped"
    ap._consecutive_restarts = 0
    ap._backoff_index = 0
    ap.restart_count = 0
    ap.last_error = ""
    await mgr._update_process_db(agent_id, "stopped")

    # 如果配置了 auto_start，立即拉起
    if ap.config.auto_start:
        ok = await mgr.start_agent(ap.config)
        if not ok:
            raise HTTPException(status_code=500, detail=f"拉起 {agent_id} 失败")

    logger = arm.logger
    logger.info("[Xihe] 人工恢复 %s 成功", agent_id)
    return {"status": "ok", "agent_id": agent_id, "new_status": ap.status}


# ── 状态上报 ─────────────────────────────────────────────

@router.post("/agents/{agent_id}/report-status")
async def report_status(agent_id: str, req: ReportStatusRequest):
    """Agent 向底座报告自身状态变更

    Agent 重启/升级后调用此接口通知底座：
    - 新版本号
    - 变更摘要
    - 当前健康状态
    """
    mgr = _get_mgr()
    ap = mgr._processes.get(agent_id)

    if req.status == "started":
        if ap:
            # 更新 PID（如果 Agent 重启后 PID 变了）
            pid = req.health.get("pid")
            if pid:
                ap.pid = pid
                ap.started_at = datetime.now(timezone.utc)
                ap.status = "running"
                ap._healthy_since = datetime.now(timezone.utc)
                await mgr._update_process_db(agent_id, "running", pid)

        logger = arm.logger
        logger.info(
            "[Xihe] Agent 上报状态 %s: status=%s version=%s changes=%s",
            agent_id, req.status, req.version,
            {k: len(v) for k, v in (req.changes or {}).items()},
        )

    return {
        "status": "ok",
        "agent_id": agent_id,
        "ack": True,
    }
