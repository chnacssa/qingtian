"""
寰宇 — API 路由
Agent 注册/发现 + 消息收/发
"""

import logging
import uuid as _uuid
from datetime import datetime, timezone as _timezone
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from zhenyue.auth import verify_admin_token

from common.db import get_pool
from . import config as hcfg
from . import directory as dirsvc
from . import verification as verifysvc
from . import agent_runtime as arm
from .api_business import business_router


logger = logging.getLogger("huanyu.api")

router = APIRouter(prefix="/v1/huanyu")


# ---- 模型 ----
# review(2026-08-24 P1): RegisterRequest / SendMessageRequest 随重复端点删除一并清理


# ---- 目录 ----
# review(2026-08-24 P1 路由遮蔽修复): POST /register、GET /agents、GET /agents/search
# 与 api_compliance.py 重复注册——main.py 先 include compliance_router，先注册先匹配，
# 本文件同名端点全部是死代码且行为不一致（维护陷阱），已删除。
# gbz 输出格式参数已移植到 compliance 版 GET /agents。

@router.get("/agents/rating-summary")
async def rating_summary(agent_ids: str = Query("", description="逗号分隔的 agent_id 列表")):
    """批量查询 Agent 信誉评分"""
    ids = [a.strip() for a in agent_ids.split(",") if a.strip()] if agent_ids else []
    pool = await dirsvc.get_pool()
    async with pool.acquire() as conn:
        if ids:
            rows = await conn.fetch(
                f"SELECT agent_id, avg_score, total_ratings, unique_raters "
                f"FROM {hcfg.get_schema_name()}.agent_rating_summary WHERE agent_id = ANY($1) "
                f"ORDER BY avg_score DESC",
                ids,
            )
        else:
            rows = await conn.fetch(
                f"SELECT agent_id, avg_score, total_ratings, unique_raters "
                f"FROM {hcfg.get_schema_name()}.agent_rating_summary ORDER BY avg_score DESC LIMIT 100"
            )
    return {"status": "ok", "ratings": [dict(r) for r in rows], "count": len(rows)}


@router.get("/agents/identity/resolve")
async def resolve_agent_identity(
    channel: str = Query("", description="通道 (feishu/dingtalk/...)，可空"),
    channel_id: str = Query("", description="通道身份 open_id"),
):
    """通道身份 → 规范 agent 名（X 模型解析）。
    插件 resolveCanonicalAgentId 在 identityAliases 未命中时调本端点。
    未绑定/二义 → agent_id 为 null（调用方保持原值）。

    ⚠️ 路径刻意用三段 `/agents/identity/resolve`，避免被 main.py 先 include 的
    api_compliance `/agents/{agent_id}` 动态段抢占（2026-08-08 大师实测 404）。
    """
    from . import bindings as bindsvc
    agent_id = await bindsvc.resolve_agent(channel, channel_id)
    return {"status": "ok", "agent_id": agent_id}


# review(2026-08-24 P1 路由遮蔽修复): GET /agents/{agent_id}、POST /agents/{agent_id}/heartbeat
# 与 compliance 版重复（先注册先匹配，本文件版本是死代码），已删除；
# gbz 输出格式参数已移植到 compliance 版 GET /agents/{agent_id}。


class BindRequest(BaseModel):
    channel: str
    channel_id: str


@router.post("/agents/{agent_id}/bindings")
async def bind_agent_identity(agent_id: str, req: BindRequest):
    """绑定通道身份 → 规范 agent 名（账号绑定流程动态维护，非仓库硬编码）"""
    from . import bindings as bindsvc
    result = await bindsvc.bind_agent(agent_id, req.channel, req.channel_id)
    if result.get("status") == "error":
        raise HTTPException(409, result.get("error", "绑定失败"))
    return result


@router.get("/agents/{agent_id}/bindings")
async def list_agent_bindings(agent_id: str):
    """列出某 agent 的全部通道绑定"""
    from . import bindings as bindsvc
    return {"status": "ok", "bindings": await bindsvc.list_bindings(agent_id)}


@router.delete("/agents/{agent_id}/bindings/{channel}/{channel_id}")
async def unbind_agent_identity(agent_id: str, channel: str, channel_id: str):
    """解绑通道身份"""
    from . import bindings as bindsvc
    return await bindsvc.unbind_agent(agent_id, channel, channel_id)


# ---- 消息 ----
# review(2026-08-24 P1 路由遮蔽修复): POST /messages、GET /inbox/{agent_id}、
# POST /messages/{message_id}/read 与 compliance 版重复注册（compliance 先 include
# 先匹配，本文件版本是死代码且行为不一致），已删除。


# ---- 认证 (verification) ----

class UpgradeRequest(BaseModel):
    agent_id: str
    target_level: str  # C1/C2/C3
    uscc: str = ""
    company_name: str = ""
    country_code: str = "CN"


@router.post("/verification/upgrade")
async def upgrade_c_level(req: UpgradeRequest):
    result = await verifysvc.upgrade_c_level(
        agent_id=req.agent_id,
        target_level=req.target_level,
        uscc=req.uscc,
        company_name=req.company_name,
        country_code=req.country_code,
    )
    if not result.get("success"):
        raise HTTPException(400, result.get("error", "upgrade failed"))
    return result


# ---- Agent 进程管理 (ARM) ----

@router.get("/agents/{agent_id}/runtime")
async def agent_runtime_status(agent_id: str):
    """查询 Agent 进程运行时状态"""
    mgr = arm.get_manager()
    status = mgr.get_agent_status(agent_id)
    if not status:
        raise HTTPException(404, "Agent 不在运行时管理中")
    return status


@router.get("/runtime/agents")
async def list_runtime_agents():
    """列出 ARM 管理的所有 Agent"""
    mgr = arm.get_manager()
    return {"agents": mgr.list_agents()}


class StartAgentRequest(BaseModel):
    executable: str = ""
    args: list[str] = []


@router.post("/runtime/agents/{agent_id}/start")
async def start_agent_process(agent_id: str, req: StartAgentRequest = None,
                              _admin: str = Depends(verify_admin_token)):
    """启动 Agent 进程（A2: 需 X-Admin-Token 管理校验）"""
    mgr = arm.get_manager()
    config = arm.AgentProcessConfig(
        agent_id=agent_id,
        executable=req.executable if req and req.executable else "python3",
        args=req.args if req else [],
    )
    ok = await mgr.start_agent(config)
    return {"status": "ok" if ok else "error", "agent_id": agent_id}


@router.post("/runtime/agents/{agent_id}/stop")
async def stop_agent_process(agent_id: str, _admin: str = Depends(verify_admin_token)):
    """停止 Agent 进程（A2: 需 X-Admin-Token 管理校验）"""
    mgr = arm.get_manager()
    ok = await mgr.stop_agent(agent_id)
    return {"status": "ok" if ok else "error", "agent_id": agent_id}


@router.post("/runtime/agents/{agent_id}/restart")
async def restart_agent_process(agent_id: str, _admin: str = Depends(verify_admin_token)):
    """重启 Agent 进程（A2: 需 X-Admin-Token 管理校验）"""
    mgr = arm.get_manager()
    ok = await mgr.restart_agent(agent_id)
    return {"status": "ok" if ok else "error", "agent_id": agent_id}


@router.post("/verification/webhook")
async def verification_webhook(
    payload: Dict,
    x_risk_signature: str = Header(default=""),
    x_risk_timestamp: str = Header(default=""),
):
    """接收 VP 风险事件推送（HMAC 验签 + 时间戳防重放）"""
    result = await verifysvc.handle_risk_event(
        payload, signature=x_risk_signature, timestamp=x_risk_timestamp
    )
    return result


# ============================================================
# 工作秘书 API — reminders + admin-messages + trajectory batch-mark
# ============================================================


def _get_agent_id(req: Request) -> str:
    """从认证中间件/header/query 获取 agent_id。

    白名单路径跳过认证中间件（req.state.agent_id 为空），Skill 子进程
    内部调用的请求不带 token → 需从 X-Agent-ID header 兜底。

    P1 (R11): X-Agent-ID header / ?agent_id= 是客户端可伪造的（读写他人提醒
    = IDOR），仅限内部 IPC 通道（loopback + X-Internal-Token，Skill 子进程
    经 xihe IPC 代理调用，agent_runtime.py 会带该令牌）才信任 —— 外部客户端
    直接调用一律 401。
    """
    aid = getattr(req.state, "agent_id", "") or ""
    if not aid:
        from common.ipc_auth import is_internal_ipc
        if is_internal_ipc(req):
            aid = req.headers.get("X-Agent-ID", "") or ""
            if not aid:
                aid = req.query_params.get("agent_id", "") or ""
    if not aid:
        raise HTTPException(401, "无法识别调用方 Agent 身份（内部通道请经 IPC 代理提供 X-Agent-ID）")
    return aid


class ReminderRequest(BaseModel):
    title: str = Field(..., max_length=200)
    body: str = Field(default="", max_length=2000)
    remind_at: str = Field(default="")  # ISO 时间戳或时钟时间如 "09:00"
    priority: str = Field(default="normal")  # low / normal / high
    type: str = Field(default="task")  # task / deadline / followup / custom


@router.post("/reminders")
async def create_reminder(req: ReminderRequest, request: Request):
    """创建提醒（agent_id 从认证上下文自动获取）"""
    agent_id = _get_agent_id(request)
    rid = _uuid.uuid4().hex[:16]
    now = datetime.now(_timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO zhenyue.agent_reminders
               (id, agent_id, title, body, remind_at, priority, type, status, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'pending',$8)""",
            rid, agent_id, req.title, req.body,
            req.remind_at or now.isoformat(),
            req.priority, req.type, now,
        )
    return {"id": rid, "status": "pending"}


@router.get("/reminders/pending")
async def get_pending_reminders(request: Request, limit: int = Query(default=20)):
    """获取提醒列表（pending 状态，按提醒时间排序）"""
    agent_id = _get_agent_id(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, title, body, remind_at, priority, type, status, created_at
               FROM zhenyue.agent_reminders
               WHERE agent_id=$1 AND status='pending'
               ORDER BY remind_at ASC LIMIT $2""",
            agent_id, limit,
        )
    return {"reminders": [dict(r) for r in rows]}


@router.put("/reminders/escalate")
async def escalate_overdue_reminders(request: Request):
    """批量升级超时提醒（供秘书 escalation_loop 用）。

    规则：
      - normal 超过 30min 未确认 → urgent
      - urgent 超过 15min 未确认 → critical
      - critical 超过 30min 未确认 → escalated 标记
      返回需要通知的 escalated 列表。

    review(2026-08-24 P1 死路由修复): 原先注册在 PUT /reminders/{reminder_id}
    之后，单段路径 "escalate" 被动态段先匹配且缺必填 status 参数 → 永远 422。
    现注册在动态段之前。
    """
    agent_id = _get_agent_id(request)
    pool = await get_pool()
    notified = []
    async with pool.acquire() as conn:
        # normal → urgent（30min 未确认）
        await conn.execute(
            "UPDATE zhenyue.agent_reminders SET priority='urgent', updated_at=NOW() "
            "WHERE agent_id=$1 AND status='delivered' AND priority='normal' "
            "AND updated_at <= NOW() - INTERVAL '30 minutes'",
            agent_id,
        )

        # urgent → critical（15min 未确认）
        await conn.execute(
            "UPDATE zhenyue.agent_reminders SET priority='critical', updated_at=NOW() "
            "WHERE agent_id=$1 AND status='delivered' AND priority='urgent' "
            "AND updated_at <= NOW() - INTERVAL '15 minutes'",
            agent_id,
        )

        # critical 超时 → 标记 escalated
        rows = await conn.fetch(
            "UPDATE zhenyue.agent_reminders SET escalated=TRUE, escalated_at=NOW(), updated_at=NOW() "
            "WHERE agent_id=$1 AND status='delivered' AND priority='critical' "
            "AND escalated=FALSE AND updated_at <= NOW() - INTERVAL '30 minutes' "
            "RETURNING id, title, body",
            agent_id,
        )
        notified = [dict(r) for r in rows]

    return {"escalated": notified}


@router.put("/reminders/{reminder_id}")
async def update_reminder(reminder_id: str, status: str = Query(...), request: Request = None):
    """更新提醒状态 (done / snooze / cancel)"""
    if status not in ("done", "snooze", "cancel"):
        raise HTTPException(400, "status 须为 done / snooze / cancel")
    agent_id = _get_agent_id(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE zhenyue.agent_reminders SET status=$1, updated_at=NOW() "
            "WHERE id=$2 AND agent_id=$3",
            status, reminder_id, agent_id,
        )
    return {"id": reminder_id, "status": status}


@router.delete("/reminders/{reminder_id}")
async def delete_reminder(reminder_id: str, request: Request):
    """删除提醒"""
    agent_id = _get_agent_id(request)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM zhenyue.agent_reminders WHERE id=$1 AND agent_id=$2",
            reminder_id, agent_id,
        )
    return {"id": reminder_id, "status": "deleted"}


# Admin message push via HTTP (workaround until full admin_message bus integration)
class AdminMessageRequest(BaseModel):
    level: str = Field(default="warning")  # critical / warning / info
    source: str = Field(default="system")
    title: str = Field(..., max_length=200)
    body: str = Field(default="", max_length=2000)
    dedup_key: str = Field(default="")


@router.post("/admin-messages")
async def send_admin_message(req: AdminMessageRequest):
    """发送管理员消息（供工作秘书等 Skill 通过 HTTP 调用）"""
    try:
        from common.admin_message import create_admin_bus, AdminMessage
        bus = create_admin_bus()
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        loop.create_task(bus.send(AdminMessage(
            level=req.level,
            source=req.source,
            title=req.title,
            body=req.body,
            dedup_key=req.dedup_key,
        )))
        return {"status": "queued"}
    except (RuntimeError, ImportError) as e:
        logger.warning("Admin message send failed: %s", e)
        raise HTTPException(500, "消息发送失败")


# 注册业务路由（询价/采购/谈判一站式）
router.include_router(business_router)

