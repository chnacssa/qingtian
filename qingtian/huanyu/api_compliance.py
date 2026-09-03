"""
合规路由 — 对标 GB/Z 185 Part 2-7（社区版全开）

从 api_rest.py 迁移的合规接口。
"""

import json
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from . import directory as dirsvc
from . import messaging as msgsvc
from . import errors as qacp_err
from .models import (
    AgentResponse, RegisterAgentRequest, ResolveRequest,
    SendMessageRequest,
)

compliance_router = APIRouter(prefix="/v1/huanyu", tags=["Compliance"])

# agent_id 安全格式：UUID 或可读标识（字母、数字、下划线、连字符、点、冒号）
_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]+$")


def _validate_agent_id(agent_id: str) -> str:
    """校验 agent_id 格式，不合规则抛出 400。"""
    if not agent_id or len(agent_id) > 256:
        raise HTTPException(status_code=400, detail="agent_id 为空或过长")
    if not _SAFE_ID_PATTERN.match(agent_id):
        raise HTTPException(
            status_code=400,
            detail=f"agent_id 包含非法字符: {agent_id[:64]}",
        )
    return agent_id


@compliance_router.get("/health")
async def health():
    return {"status": "ok", "module": "huanyu"}


# ── Agent 目录 ────────────────────────────────────────

@compliance_router.post("/agents/register", response_model=AgentResponse)
async def register_agent(req: RegisterAgentRequest):
    try:
        agent = await dirsvc.register_agent(
            name=req.name, category=req.category,
            subcategory=req.subcategory, capabilities=req.capabilities,
            contact_info=req.contact_info, server_host=req.server_host,
            metadata=req.metadata,
            agent_id=req.agent_id,
            instance=req.instance,
            uscc=req.uscc,
            company_name=req.company_name,
        )
        return AgentResponse(**agent)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@compliance_router.get("/agents")
async def list_agents(
    category: str = Query(default=""),
    status: str = Query(default="active"),
    capability: str = Query(default="", description="能力标签筛选"),
    industry: str = Query(default="", description="ISIC 2 位行业码"),
    c_level_min: str = Query(default="", description="最低认证等级 (C0/C1/C2/C3)"),
    scale: str = Query(default="", description="企业规模 (micro/small/medium/large)"),
    format: str = Query(default="", description="返回格式: 空=自有格式, gbz=国标格式"),
):
    # review(2026-08-24 P1): 删除 api.py 被遮蔽的重复端点后，把其独有筛选参数
    # 与 gbz 输出格式移植到本（生效）版本
    agents = await dirsvc.discover_agents(
        category=category if category else None,
        capability=capability or None,
        industry=industry or None,
        c_level_min=c_level_min or None,
        scale=scale or None,
    )
    if format == "gbz":
        from .gbz_protocol import format_agent_for_gbz
        gbz_agents = [await format_agent_for_gbz(a) for a in agents]
        return {"status": "ok", "agents": gbz_agents, "count": len(gbz_agents)}
    return {"agents": agents}


@compliance_router.get("/agents/search")
async def search_agents(q: str = Query(..., min_length=1)):
    agents = await dirsvc.search_agents(q)
    return {"agents": agents}


@compliance_router.get("/agents/discover")
async def discover_agents(capability: str = Query(default=""), tag: str = Query(default="")):
    agents = await dirsvc.discover_agents(capability=capability, tag=tag)
    return {"agents": agents}


@compliance_router.post("/agents/resolve")
async def resolve_agent(req: ResolveRequest):
    agent = await dirsvc.resolve_agent(req.ain or req.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"agent": agent}


@compliance_router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, format: str = Query(default="", description="返回格式: 空=自有格式, gbz=国标格式")):
    _validate_agent_id(agent_id)
    agent = await dirsvc.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if format == "gbz":
        from .gbz_protocol import format_agent_for_gbz
        return {"status": "ok", "agent": await format_agent_for_gbz(agent)}
    return agent


@compliance_router.get("/agents/{agent_id}/description")
async def get_description(agent_id: str):
    _validate_agent_id(agent_id)
    agent = await dirsvc.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok", "agent_id": agent_id, "description": agent}


@compliance_router.post("/agents/{agent_id}/credential")
async def get_credential(agent_id: str):
    _validate_agent_id(agent_id)
    agent = await dirsvc.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"status": "ok", "agent_id": agent_id, "credential": {"public_key": agent.get("public_key", ""), "cert_fingerprint": agent.get("cert_fingerprint", "")}}


@compliance_router.post("/agents/{agent_id}/heartbeat")
async def heartbeat(agent_id: str):
    _validate_agent_id(agent_id)
    result = await dirsvc.heartbeat(agent_id)
    ok = result.get("status") == "ok" if isinstance(result, dict) else bool(result)
    return {"status": "ok" if ok else "not_found", "agent_id": agent_id}


@compliance_router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str):
    _validate_agent_id(agent_id)
    result = await dirsvc.soft_delete_agent(agent_id)
    # P2 (R11): 按真实结果判定——soft_delete_agent 返回 dict（恒为真值），
    # 此前 `if ok:` 恒真 → 删除不存在的 agent 也报 deleted。
    ok = result.get("deleted") if isinstance(result, dict) else bool(result)
    return {"status": "deleted" if ok else "not_found", "agent_id": agent_id}


@compliance_router.get("/categories")
async def list_categories():
    cats = await dirsvc.get_categories()
    return {"categories": cats}


# ── 消息 ────────────────────────────────────────

@compliance_router.post("/messages")
async def send_message(req: SendMessageRequest):
    try:
        result = await msgsvc.send_message(
            from_agent=req.from_agent, to_agent=req.to_agent,
            message_type=req.message_type, payload=req.payload,
            priority=req.priority, reply_to=req.reply_to,
            negotiation_id=req.negotiation_id,
            idempotency_key=req.idempotency_key or None,
        )
        return result
    except qacp_err.QACPError as e:
        raise HTTPException(status_code=e.status_code, detail=e.error_code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@compliance_router.get("/inbox/{agent_id}")
async def inbox(agent_id: str, limit: int = Query(default=20), offset: int = Query(default=0)):
    _validate_agent_id(agent_id)
    msgs = await msgsvc.get_inbox(agent_id, limit=limit, offset=offset)
    return {"messages": msgs}


@compliance_router.get("/inbox/{agent_id}/unread-count")
async def unread_count(agent_id: str):
    _validate_agent_id(agent_id)
    count = await msgsvc.get_unread_count(agent_id)
    return {"agent_id": agent_id, "unread_count": count}


@compliance_router.get("/conversation/{agent_a}/{agent_b}")
async def conversation(agent_a: str, agent_b: str, limit: int = 50):
    _validate_agent_id(agent_a)
    _validate_agent_id(agent_b)
    msgs = await msgsvc.get_conversation(agent_a, agent_b, limit=limit)
    return {"messages": msgs}


@compliance_router.post("/messages/{message_id}/read")
async def mark_read(message_id: str):
    await msgsvc.mark_read(message_id)
    return {"status": "ok"}


@compliance_router.post("/messages/batch-read")
async def batch_read(req: dict):
    await msgsvc.batch_mark_read(req.get("message_ids", []))
    return {"status": "ok"}


@compliance_router.post("/messages/{message_id}/archive")
async def archive_message(message_id: str):
    await msgsvc.archive_message(message_id)
    return {"status": "ok"}


@compliance_router.get("/messages/{message_id}/verify")
async def verify_message(message_id: str):
    ok = await msgsvc.verify_message_integrity(message_id)
    verified = ok.get("valid", False) if isinstance(ok, dict) else bool(ok)
    return {"message_id": message_id, "verified": verified}


# ── 工具 ────────────────────────────────────────

@compliance_router.get("/tools")
async def list_tools(agent_id: str = Query(default=""), q: str = Query(default="")):
    from .tool_registry import get_registry
    reg = get_registry()
    if agent_id:
        tools = reg.list_by_agent(agent_id)
    elif q:
        tools = reg.search(q)
    else:
        tools = reg.list_all()
    return {"tools": [t.model_dump() for t in tools]}


@compliance_router.get("/tools/{tool_id}")
async def get_tool(tool_id: str):
    from .tool_registry import get_registry
    tool = get_registry().get(tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool.model_dump()


# ── 会话 ────────────────────────────────────────

@compliance_router.post("/conversations")
async def create_conversation(req: dict):
    from .conversations import create_conversation as create_conv
    conv = await create_conv(
        agent_a=req.get("agent_a", ""), agent_b=req.get("agent_b", ""),
        topic=req.get("topic", ""),
    )
    return {"status": "ok", "conversation_id": conv.conversation_id}


@compliance_router.get("/conversations/{agent_id}")
async def list_conversations(agent_id: str):
    from .conversations import list_conversations as list_convs
    convs = await list_convs(agent_id)
    return {"agent_id": agent_id, "conversations": [c.model_dump() if hasattr(c, 'model_dump') else c for c in convs]}


@compliance_router.post("/conversations/{conv_id}/close")
async def close_conversation(conv_id: str, req: dict = None):
    """关闭会话 — 幂等，重复关闭仅更新 close_reason"""
    from .conversations import close_conversation as close_conv
    reason = (req or {}).get("reason", "")
    await close_conv(conv_id, reason)
    return {"status": "ok", "conversation_id": conv_id}


# ── GB/Z 185.2 映射 ────────────────────────────

@compliance_router.post("/gbz185/mappings")
async def create_gbz185_mapping(req: dict):
    """建立 AIN ↔ GB/Z 185.2 身份码映射"""
    ain = req.get("ain")
    gbz_id = req.get("gbz185_id")
    issuer = req.get("issuer", "")
    if not ain or not gbz_id:
        raise HTTPException(status_code=400, detail="ain and gbz185_id are required")
    from common.identity import get_identity_provider
    provider = get_identity_provider()
    ok = await provider.map_to_gbz(ain, gbz_id, issuer)
    return {"status": "ok" if ok else "error"}


@compliance_router.get("/gbz185/mappings/{ain}")
async def get_gbz185_mapping(ain: str):
    from common.identity import get_identity_provider
    provider = get_identity_provider()
    result = await provider.resolve(ain)
    return {"ain": ain, "gbz185_id": result.gbz_identity_code if result else "", "mapped": result.mapped if result else False}


# ── 合规检查 ────────────────────────────────────

@compliance_router.get("/compliance")
async def compliance_check():
    from .gbz185 import run_compliance_check
    report = await run_compliance_check()
    return report
