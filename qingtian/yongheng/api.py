"""
永恒 — REST API 路由
记忆 CRUD / 搜索 / 画像 / 轨迹 / Token / 整理 / 会话聚合
"""

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from common.db import get_pool

from . import memory_service, trajectory_service, profile_service, dreem_gate, token_service, hook_ingest
from .models import (
    AppError,
    WriteMemoryRequest, WriteMemoryResponse,
    BatchWriteRequest, BatchWriteResponse,
    SearchRequest, SearchResponse,
    ContextRequest, ContextResponse,
    UpdateMemoryStatusRequest, UpdateMemoryStatusResponse,
    ProfileResponse, UpdateProfileRequest,
    AddTrajectoryRequest, TrajectoryResponse,
    BatchMarkTrajectoryRequest,
    CreateTokenRequest, CreateTokenResponse,
    ValidateTokenRequest, ValidateTokenResponse,
    RevokeTokenRequest, RevokeTokenResponse,
    ConsolidateRequest, ConsolidateResponse,
    SessionStartRequest, SessionStartResponse,
    SessionEndRequest, SessionEndResponse,
    TransferRequest, TransferResponse,
    RecoverSessionRequest, RecoverSessionResponse,
    HookIngestRequest, HookIngestResponse,
)
from .auth import require_level, get_token_info
from . import config as cfg

logger = logging.getLogger("yongheng")

router = APIRouter(prefix="/v1/yongheng", tags=["永恒"])


# ── 限流辅助 ──────────────────────────────────────────

class RateLimiter:
    def __init__(self):
        self._windows: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str, max_requests: int, window_sec: int = 60) -> bool:
        now = time.time()
        window = self._windows[key]
        while window and window[0] < now - window_sec:
            window.pop(0)
        if len(window) >= max_requests:
            return False
        window.append(now)
        return True


_rate_limiter = RateLimiter()


def limit_for(prefix: str, limit_key: str) -> bool:
    limits_map = {
        "write": cfg.get_rate_limit_write(),
        "search": cfg.get_rate_limit_search(),
        "context": cfg.get_rate_limit_context(),
        "session_start": cfg.get_rate_limit_session_start(),
        "session_end": cfg.get_rate_limit_session_end(),
    }
    max_req = limits_map.get(prefix, 120)
    return _rate_limiter.check(f"{prefix}:{limit_key}", max_req)


async def get_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


# ── 记忆 ──────────────────────────────────────────────

@router.post("/memories", response_model=WriteMemoryResponse)
async def write_memory(req: WriteMemoryRequest,
                       token_info=require_level("namespace", "master", "admin"),
                       db=Depends(get_db)):
    if not limit_for("write", req.namespace):
        raise AppError("RATE_LIMITED", "write rate limit exceeded", 429)
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    return await memory_service.write_memory(
        db, req.namespace, req.content,
        mem_type=req.type, source=req.source, metadata=req.metadata,
    )


@router.post("/memories/batch", response_model=BatchWriteResponse)
async def batch_write(req: BatchWriteRequest,
                      token_info=require_level("namespace", "master", "admin"),
                      db=Depends(get_db)):
    if not limit_for("write", req.namespace):
        raise AppError("RATE_LIMITED", "write rate limit exceeded", 429)
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    mems = [m.model_dump() for m in req.memories]
    results, stored, failed = await memory_service.batch_write(db, req.namespace, mems)
    return {"results": results, "total": len(req.memories), "stored": stored, "failed": failed}


@router.post("/memories/search", response_model=SearchResponse)
async def search_memories(req: SearchRequest,
                          token_info=require_level("namespace", "master", "admin"),
                          db=Depends(get_db)):
    if not limit_for("search", req.namespace):
        raise AppError("RATE_LIMITED", "search rate limit exceeded", 429)
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    result = await memory_service.search_memory(
        db, req.namespace, req.query,
        method=req.method, top_k=req.top_k, offset=req.offset,
        budget_tokens=req.budget_tokens, filter_dict=req.filter,
        include_global=True,
    )
    return result


@router.post("/memories/context", response_model=ContextResponse)
async def context_memories(req: ContextRequest,
                           token_info=require_level("namespace", "master", "admin"),
                           db=Depends(get_db)):
    if not limit_for("context", req.namespace):
        raise AppError("RATE_LIMITED", "context rate limit exceeded", 429)
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    return await memory_service.context_memory(db, req.namespace, req.context, top_k=req.top_k,
                                               include_global=True)


@router.patch("/memories/{memory_id}/status", response_model=UpdateMemoryStatusResponse)
async def update_memory_status(memory_id: int, req: UpdateMemoryStatusRequest,
                               token_info=require_level("namespace", "master", "admin"),
                               db=Depends(get_db)):
    if token_info.get("level") == "namespace":
        schema = cfg.get_schema_name()
        row = await db.fetchrow(f"SELECT namespace FROM {schema}.memories WHERE id = $1", memory_id)
        if not row:
            raise AppError("NOT_FOUND", "memory not found", 404)
        token_service.check_namespace_match(token_info["namespace"], row["namespace"])
    return await memory_service.update_memory_status(db, memory_id, req.review_status)


@router.get("/memories/export")
async def export_memories(namespace: str, date_from: str | None = None,
                          date_to: str | None = None, include_vectors: bool = False,
                          token_info=require_level("master", "admin"), db=Depends(get_db)):
    results = await memory_service.export_memories(
        db, namespace, date_from=date_from, date_to=date_to, include_vectors=include_vectors,
    )

    async def generate():
        for r in results:
            yield json.dumps(r, ensure_ascii=False) + "\n"

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f"attachment; filename=yongheng_{namespace}_export.ndjson"},
    )


@router.post("/memories/transfer", response_model=TransferResponse)
async def transfer_memories(req: TransferRequest,
                            token_info=require_level("master", "admin"),
                            db=Depends(get_db)):
    """迁移记忆：将 source namespace 的记忆复制/移动到 target namespace。

    Agent 迁移场景：
      mode=copy → 新 Agent 接管旧 Agent 记忆，旧记忆保留
      mode=move → 彻底迁移，旧记忆标记已完成
    """
    result = await memory_service.transfer_memories(
        db, req.source_namespace, req.target_namespace, mode=req.mode,
    )
    return TransferResponse(
        transferred=result["transferred"],
        source_namespace=result["source_ns"],
        target_namespace=result["target_ns"],
        mode=result["mode"],
        timestamp=datetime.now(timezone.utc),
    )


# ── 画像 ──────────────────────────────────────────────

@router.get("/profile", response_model=ProfileResponse)
async def get_profile(namespace: str = Query(...),
                      token_info=require_level("namespace", "master", "admin"),
                      db=Depends(get_db)):
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], namespace)
    return await profile_service.get_profile(db, namespace)


@router.put("/profile", response_model=ProfileResponse)
async def update_profile(req: UpdateProfileRequest,
                         token_info=require_level("namespace", "master", "admin"),
                         db=Depends(get_db)):
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    return await profile_service.update_profile(
        db, req.namespace,
        traits=req.traits,
        learned_add=req.learned_add,
        learned_override=req.learned_override,
        state=req.state,
    )


# ── 轨迹 ──────────────────────────────────────────────

@router.get("/trajectory", response_model=TrajectoryResponse)
async def get_trajectory(
    namespace: str = Query(...),
    date: str | None = Query(default=None),
    page_size: int = Query(default=20, ge=1, le=50),
    page_token: str | None = Query(default=None),
    token_info=require_level("namespace", "master", "admin"),
    db=Depends(get_db),
):
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], namespace)
    return await trajectory_service.get_trajectory(
        db, namespace, date_str=date, page_size=page_size, page_token=page_token,
    )


@router.post("/trajectory")
async def add_trajectory(req: AddTrajectoryRequest,
                         token_info=require_level("namespace", "master", "admin"),
                         db=Depends(get_db)):
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    action = {
        "time": req.time,
        "action": req.action,
        "detail": req.detail,
        "result": req.result,
    }
    return await trajectory_service.add_action(db, req.namespace, action)


@router.post("/trajectory/batch-mark")
async def batch_mark_trajectory(req: BatchMarkTrajectoryRequest,
                                token_info=require_level("namespace", "master", "admin"),
                                db=Depends(get_db)):
    """批量标记轨迹动作为已处理（recorder 去重）。

    在 trajectories.actions JSONB 数组内对匹配 id 的 action 追加 processed 标记，
    供 recorder 跳过已处理项，避免重复摘录。
    """
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    await trajectory_service.mark_processed(db, req.namespace, req.date, req.action_ids)
    return {
        "status": "ok",
        "namespace": req.namespace,
        "marked": len(req.action_ids),
    }


# ── Token ─────────────────────────────────────────────

@router.post("/token/create", response_model=CreateTokenResponse)
async def create_token(req: CreateTokenRequest,
                       token_info=require_level("admin"),
                       db=Depends(get_db)):
    return await token_service.create_token(db, req.namespace, req.level,
                                            created_by=token_info.get("namespace", ""))


@router.post("/token/validate", response_model=ValidateTokenResponse)
async def validate_token(req: ValidateTokenRequest,
                         token_info=require_level("admin"),
                         db=Depends(get_db)):
    return await token_service.validate_token(db, req.token)


@router.post("/token/revoke", response_model=RevokeTokenResponse)
async def revoke_token(req: RevokeTokenRequest,
                       token_info=require_level("admin"),
                       db=Depends(get_db)):
    return await token_service.revoke_token(db, req.token)


# ── 整理 ──────────────────────────────────────────────

@router.post("/consolidate", response_model=ConsolidateResponse)
async def trigger_consolidate(req: ConsolidateRequest,
                              token_info=require_level("namespace", "master", "admin"),
                              db=Depends(get_db)):
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)
    return await dreem_gate.consolidate(db, req.namespace)


# ── 会话聚合 ──────────────────────────────────────────

@router.post("/session/start", response_model=SessionStartResponse)
async def session_start(req: SessionStartRequest,
                        token_info=require_level("namespace", "master", "admin"),
                        db=Depends(get_db)):
    if not limit_for("session_start", req.namespace):
        raise AppError("RATE_LIMITED", "session start rate limit exceeded", 429)
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)

    # 能力分发：从寰宇查 agent 的 category + capabilities，传入 context 搜索做加权匹配
    agent_profile = None
    if req.agent_id:
        try:
            from huanyu.config import get_schema_name as hy_schema
            pool = await get_pool()
            async with pool.acquire() as hconn:
                agent_row = await hconn.fetchrow(
                    f"SELECT category, capabilities FROM {hy_schema()}.agents WHERE agent_id = $1",
                    req.agent_id,
                )
                if agent_row:
                    caps = agent_row["capabilities"]
                    if isinstance(caps, str):
                        caps = json.loads(caps)
                    agent_profile = {
                        "agent_id": req.agent_id,
                        "category": agent_row["category"],
                        "capabilities": caps or [],
                    }
        except Exception:
            logger.warning("Agent profile lookup failed for %s, degrading to plain search", req.agent_id)

    context_results = []
    if req.context:
        ctx = await memory_service.context_memory(
            db, req.namespace, req.context, top_k=req.top_k,
            agent_profile=agent_profile,
            include_global=True,
        )
        context_results = ctx["results"]

    profile = await profile_service.get_profile(db, req.namespace)
    traj = await trajectory_service.get_trajectory(db, req.namespace, date_str=None, page_size=20)

    return {
        "namespace": req.namespace,
        "context_results": context_results,
        "profile": profile,
        "trajectory": {
            "status": traj["status"],
            "namespace": traj["namespace"],
            "date": traj["date"],
            "actions": traj["actions"],
            "summary": traj["summary"],
        },
    }


@router.post("/session/end", response_model=SessionEndResponse)
async def session_end(req: SessionEndRequest,
                      token_info=require_level("namespace", "master", "admin"),
                      db=Depends(get_db)):
    if not limit_for("session_end", req.namespace):
        raise AppError("RATE_LIMITED", "session end rate limit exceeded", 429)
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)

    mem = await memory_service.write_memory(
        db, req.namespace, req.summary,
        mem_type="consolidated", source="openclaw",
    )

    profile_updated = False
    if req.state:
        await profile_service.update_profile(db, req.namespace, state=req.state)
        profile_updated = True

    today_traj = await trajectory_service.get_trajectory(db, req.namespace)
    has_end_action = any(
        a.get("action") == "会话结束" for a in today_traj.get("actions", [])
    )
    if not has_end_action:
        now = datetime.now(timezone.utc)
        await trajectory_service.add_action(db, req.namespace, {
            "time": now.strftime("%H:%M:%S"),
            "action": "会话结束",
            "detail": req.summary,
            "result": "已完成",
        })

    return {
        "memory_id": mem["id"],
        "memory_status": mem["status"],
        "profile_updated": profile_updated,
        "timestamp": datetime.now(timezone.utc),
    }


# ── 会话恢复 ──────────────────────────────────────────

@router.post("/session/recover", response_model=RecoverSessionResponse)
async def recover_session(req: RecoverSessionRequest,
                          token_info=require_level("namespace", "master", "admin"),
                          db=Depends(get_db)):
    """Agent 崩溃后恢复：获取最近记忆 + 画像 + 最后会话。

    用于 Agent 进程崩溃/重启后快速找回上下文。
    """
    if token_info.get("level") == "namespace":
        token_service.check_namespace_match(token_info["namespace"], req.namespace)

    recent = await memory_service.get_recent_memories(
        db, req.namespace, limit=20, since=req.since,
    )

    profile = None
    try:
        profile = await profile_service.get_profile(db, req.namespace)
    except Exception:
        pass

    last_session = None
    for m in recent:
        if m.get("memory_type") == "consolidated":
            last_session = {
                "memory_id": m["id"],
                "content": m["content"],
                "timestamp": m["timestamp"],
            }
            break

    return RecoverSessionResponse(
        namespace=req.namespace,
        recent_memories=recent,
        total_recovered=len(recent),
        profile=profile,
        last_session=last_session,
        timestamp=datetime.now(timezone.utc),
    )


# ── Hook 摄入（OpenClaw 生命周期 → 永恒自动分流）─────────


@router.post("/hooks/ingest", response_model=HookIngestResponse)
async def hooks_ingest(req: HookIngestRequest,
                       token_info=require_level("namespace", "master", "admin"),
                       db=Depends(get_db)):
    """OpenClaw 生命周期 Hook 统一摄入端点。

    自动分流规则：
      - message:*/llm:*/tool:* → trajectory（时序日志，不建 embedding）
      - agent_end → trajectory + memory（≥200字时写 episodic 记忆）
      - session:compact:after → memory（consolidated 类型）

    P1 (R11): 原实现缺 namespace 归属校验——namespace 级 token 可指定任意
    event.namespace 跨 namespace 写入记忆。补校验（与其他写入端点一致）。

    P1-3（9-1 修复日）：补 limit_for 写限流 —— 同模块 /memories、/memories/batch
    均有 60/min 写限流，唯本端点无（且位于网关公开白名单直通面后，泄漏 token
    可不限速批量写 trajectory+memory 并触发 embedding/LLM 成本型 DoS）。
    """
    # 限流键用 token namespace（master/admin 按自身 namespace 收敛）
    if not limit_for("write", token_info.get("namespace", req.namespace)):
        raise HTTPException(
            status_code=429,
            detail="写入频率超限，请稍后重试",
        )
    if token_info.get("level") == "namespace":
        tok_ns = token_info.get("namespace", "")
        for e in req.events:
            ns = (e.namespace or "").strip()
            if ns and ns != tok_ns:
                raise HTTPException(
                    status_code=403,
                    detail=f"事件 namespace={ns} 与 token namespace={tok_ns} 不符",
                )
    events = [e.model_dump() for e in req.events]
    return await hook_ingest.ingest_batch(db, events)


# ── 健康检查 ──────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "module": "yongheng", "version": "2.0.0"}
