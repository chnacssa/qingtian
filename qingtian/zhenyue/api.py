"""镇岳 — FastAPI 路由。

安全审计、Token 管理、审批工作流、隔离区、配置备份、守卫规则。
"""

import logging
import os
import secrets
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path as FsPath
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from common.db import get_pool

from . import config as zcfg
from . import token_service
from . import key_service
from .audit_service import (
    cleanup_old_audit_logs,
    verify_audit_chain,
    write_audit,
)
from .auth import auth_dependency, get_current_agent, verify_admin_token
from .encryptor import encryptor
from .guard import get_engine
from .models import (
    ApprovalResponse,
    AuditEntryRequest,
    AuditEntryResponse,
    AuditVerifyResponse,
    CreateTokenRequest,
    CreateTokenResponse,
    ErrorResponse,
    GenerateKeypairResponse,
    RevokeTokenRequest,
    ValidateTokenRequest,
    ValidateTokenResponse,
)
from .quarantine import (
    list_quarantine as quarantine_list,
    purge_expired,
    quarantine_file,
    restore_file,
)
from .backup import list_backups, backup_file
from .rate_limit import rate_limiter
from .scheduler import start as start_scheduler, stop as stop_scheduler

logger = logging.getLogger("zhenyue.api")

router = APIRouter(prefix="/v1/zhenyue", tags=["zhenyue"])


def _require_valid_uuid(value: str, detail: str = "Invalid UUID") -> None:
    """校验 UUID 路径参数，非法时返回 404（而非 SQL ::uuid 强转抛 500）。"""
    try:
        uuid_mod.UUID(value)
    except ValueError:
        raise HTTPException(status_code=404, detail=detail)


async def _resolve_audit_viewer(request: Request) -> dict:
    """解析审计详情查看者身份：X-Admin-Token → admin；否则走 Bearer/内部 IPC 认证。

    P2 (R11): 审计详情含 detail_enc 密文，查看者必须可信——admin 或已认证 agent，
    二者皆无时 401（原实现仅 verify_admin_token，无 agent 自助查看通道）。
    """
    try:
        admin = await verify_admin_token(request)
        return {"agent_id": admin, "role": "admin", "capabilities": ["admin"]}
    except HTTPException:
        pass
    return await auth_dependency(request)


def init_break_glass() -> None:
    """断网应急令牌初始化 — 启动时确保 break-glass token 文件存在。

    R11 (P1): main.py 调用 init_break_glass 但此前不存在 → ImportError 被
    except 吞掉 → 应急破窗功能整体缺失。现实现：启用时若 token 文件缺失则生成
    随机令牌（0o600 权限）。setup.py 已生成则跳过。
    """
    if not zcfg.get_break_glass_enabled():
        return
    token_path = zcfg.get_break_glass_token_path()
    try:
        path = FsPath(token_path)
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        os.chmod(path, 0o600)
        logger.info("Break-glass token initialized: %s", path)
    except Exception as e:
        logger.warning("Failed to init break-glass token: %s", e)


# ════════════════════════════════════════════════════════════════
# Token 管理
# ════════════════════════════════════════════════════════════════

@router.post("/tokens", response_model=CreateTokenResponse)
async def create_token(
    req: CreateTokenRequest,
    _admin: str = Depends(verify_admin_token),
):
    """创建新 Token（需要管理员令牌）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await token_service.create_token(conn, agent_id=req.agent_id, role=req.role)
    return result


@router.post("/tokens/verify", response_model=ValidateTokenResponse)
async def verify_token(
    req: ValidateTokenRequest,
    request: Request,
):
    """验证 Token 是否有效。

    P2 (R11): 原实现完全无鉴权 → 可被当作无限枚举的 token 验证 oracle。
    现双层防护：
      1) 按客户端 IP 限流（防暴力枚举，超限 429）；
      2) 若调用方已认证（Bearer / 内部 IPC），普通 agent 仅能验证自己的
         token（防跨主体探测），平台（internal-ipc/admin/ops_admin）可验证任意。
    未认证调用方（门户登录校验用户 token 的合法场景）仍可用，但受 IP 限流约束。
    """
    client_ip = request.client.host if request.client else "unknown"
    if not await rate_limiter.check(f"tokens:verify:{client_ip}"):
        raise HTTPException(status_code=429, detail="Token 验证过于频繁，请稍后再试")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await token_service.validate_token(conn, req.token)

    if result is None:
        return ValidateTokenResponse(valid=False)

    # 已认证调用方 → 普通 agent 仅可验证自己的 token
    try:
        viewer = await auth_dependency(request)
    except HTTPException:
        viewer = None
    if viewer:
        caller = viewer.get("agent_id", "")
        role = viewer.get("role", "")
        is_platform = caller in ("internal-ipc",) or role in ("admin", "ops_admin")
        if not is_platform and caller != result["agent_id"]:
            raise HTTPException(status_code=403, detail="无权验证其他 Agent 的 Token")

    return ValidateTokenResponse(
        valid=True,
        agent_id=result["agent_id"],
        role=result["role"],
    )


@router.post("/tokens/{token_id}/revoke")
async def revoke_token(
    token_id: str,
    _admin: str = Depends(verify_admin_token),
):
    """撤销 Token（需要管理员令牌）。"""
    # token_id 可以是 token_hash 的前缀或原始 token
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 先尝试用完整 token 撤销
        revoked = await token_service.revoke_token(conn, token_id)

    if not revoked:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")

    return {"status": "ok", "message": "Token revoked"}


# ════════════════════════════════════════════════════════════════
# 审计日志
# ════════════════════════════════════════════════════════════════

@router.get("/audit/log")
async def query_audit_log(
    agent_id: Optional[str] = Query(None, description="按 Agent 筛选"),
    action: Optional[str] = Query(None, description="按动作筛选"),
    severity: Optional[str] = Query(None, description="按严重度筛选"),
    limit: int = Query(50, ge=1, le=1000, description="每页条数"),
    offset: int = Query(0, ge=0, description="偏移量"),
    _admin: str = Depends(verify_admin_token),
):
    """查询审计日志（分页）。"""
    schema = zcfg.get_schema_name()
    pool = await get_pool()

    conditions = []
    params = []
    idx = 1

    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1
    if action:
        conditions.append(f"action = ${idx}")
        params.append(action)
        idx += 1
    if severity:
        conditions.append(f"severity = ${idx}")
        params.append(severity)
        idx += 1

    where_clause = " AND ".join(conditions) if conditions else "TRUE"

    count_query = f"SELECT COUNT(*) FROM {schema}.audit_log WHERE {where_clause}"
    data_query = f"""
        SELECT audit_uid, created_at, agent_id, agent_role, action, target_type, target_id,
               severity, approval_status, hash, sign_key_id
        FROM {schema}.audit_log
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_query, *params)
        rows = await conn.fetch(data_query, *params, limit, offset)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


@router.get("/audit/log/{audit_uid}")
async def get_audit_entry(
    audit_uid: str,
    request: Request,
    viewer: dict = Depends(_resolve_audit_viewer),
):
    """获取单条审计日志详情。

    P2 (R11): 原实现原样返回含 detail_enc 密文、无解密出口。现：
      1) 查看者须可信（X-Admin-Token admin，或 Bearer/内部 IPC 认证 agent）；
      2) 仅本 agent 本人 / 平台（internal-ipc、admin、ops_admin）可见，他人 403；
      3) 用 encryptor 解密 detail_enc → detail 明文返回，密文不再外泄。
    """
    _require_valid_uuid(audit_uid, "Audit entry not found")
    schema = zcfg.get_schema_name()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {schema}.audit_log WHERE audit_uid = $1::uuid",
            audit_uid,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Audit entry not found")

    caller = viewer.get("agent_id", "")
    role = viewer.get("role", "")
    is_platform = caller in ("internal-ipc",) or role in ("admin", "ops_admin")
    if not is_platform and caller != row["agent_id"]:
        raise HTTPException(status_code=403, detail="无权查看其他 Agent 的审计详情")

    entry = dict(row)
    detail_enc = entry.pop("detail_enc", None) or ""
    if detail_enc:
        try:
            entry["detail"] = encryptor.decrypt(detail_enc)
        except Exception as e:
            # P2 (R11): 解密失败（如密钥轮换后旧密文不可解）不 500，回退脱敏视图
            logger.warning("审计详情解密失败 uid=%s: %s", audit_uid, e)
            entry["detail"] = None
    else:
        entry["detail"] = None
    return entry


@router.post("/audit/logs", response_model=AuditEntryResponse)
async def create_audit_entry(
    req: AuditEntryRequest,
    agent_info: dict = Depends(auth_dependency),
):
    """写入审计日志（供内部 Skill / Agent 通过 IPC 调用）。

    认证用 auth_dependency：loopback 来源（ipc-proxy）跳过 token 检查，
    Bearer Token 来源走 verify_agent_auth 校验。

    P1 (R11): 审计归属可伪造 —— 原实现直接用请求体 agent_id，不绑定认证身份，
    任意认证调用方可冒用任意 Agent 写审计。现非平台调用方（普通 agent token）
    强制使用认证身份 agent_id；平台方（internal-ipc / admin / ops_admin）保留
    以服务身份写他人动作审计的能力。
    """
    caller = agent_info.get("agent_id", "")
    role = agent_info.get("role", "")
    is_platform = caller in ("internal-ipc",) or role in ("admin", "ops_admin")

    data = req.model_dump()
    if not is_platform:
        data["agent_id"] = caller

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await write_audit(conn, data)
    return AuditEntryResponse(
        audit_uid=str(result["audit_uid"]),
        created_at=result["created_at"],
        agent_id=data["agent_id"],
        action=req.action,
        severity=req.severity,
        hash=result["hash"],
    )


@router.get("/audit/chain", response_model=AuditVerifyResponse)
async def verify_chain(
    _admin: str = Depends(verify_admin_token),
):
    """验证审计哈希链完整性。"""
    schema = zcfg.get_schema_name()
    pool = await get_pool()

    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT COUNT(*) FROM {schema}.audit_log")
        first = await conn.fetchval(
            f"SELECT audit_uid FROM {schema}.audit_log ORDER BY id ASC LIMIT 1"
        )
        last = await conn.fetchval(
            f"SELECT audit_uid FROM {schema}.audit_log ORDER BY id DESC LIMIT 1"
        )

    try:
        async with pool.acquire() as conn:
            await verify_audit_chain(conn)
        return AuditVerifyResponse(
            status="ok",
            total_records=total or 0,
            first_audit_uid=str(first) if first else None,
            last_audit_uid=str(last) if last else None,
        )
    except Exception as e:
        return AuditVerifyResponse(
            status="broken",
            total_records=total or 0,
            first_audit_uid=str(first) if first else None,
            last_audit_uid=str(last) if last else None,
            error=str(e),
        )


# ════════════════════════════════════════════════════════════════
# 审批
# ════════════════════════════════════════════════════════════════

@router.post("/approvals")
async def create_approval(
    request_type: str = Query(..., description="审批类型: delete_file/config_change/privilege_escalation"),
    target_id: str = Query(..., description="目标 ID"),
    reason: str = Query(..., description="审批原因"),
    reviewers: str = Query("", description="审批人列表（逗号分隔）"),
    agent_id: str = Depends(get_current_agent),
):
    """创建审批请求。"""
    if request_type not in ("delete_file", "config_change", "privilege_escalation"):
        raise HTTPException(status_code=400, detail=f"Invalid request_type: {request_type}")

    reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()] or [agent_id]

    schema = zcfg.get_schema_name()
    pool = await get_pool()
    async with pool.acquire() as conn:
        approval_id = await conn.fetchval(
            f"INSERT INTO {schema}.approval_requests "
            f"(request_type, requester_id, target_id, reason, reviewers) "
            f"VALUES ($1, $2, $3, $4, $5) RETURNING approval_id",
            request_type, agent_id, target_id, reason, reviewer_list,
        )

    return {
        "approval_id": str(approval_id),
        "status": "pending",
        "request_type": request_type,
        "requester_id": agent_id,
    }


@router.get("/approvals")
async def list_approvals(
    status: Optional[str] = Query(None, description="按状态筛选"),
    requester_id: Optional[str] = Query(None, description="按请求者筛选"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: str = Depends(verify_admin_token),
):
    """列出审批请求。"""
    schema = zcfg.get_schema_name()
    pool = await get_pool()

    conditions = []
    params = []
    idx = 1

    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1
    if requester_id:
        conditions.append(f"requester_id = ${idx}")
        params.append(requester_id)
        idx += 1

    where_clause = " AND ".join(conditions) if conditions else "TRUE"

    count_query = f"SELECT COUNT(*) FROM {schema}.approval_requests WHERE {where_clause}"
    data_query = f"""
        SELECT * FROM {schema}.approval_requests
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """

    async with pool.acquire() as conn:
        total = await conn.fetchval(count_query, *params)
        rows = await conn.fetch(data_query, *params, limit, offset)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: str = Path(..., description="审批 ID"),
    decision: str = Query(..., description="approved 或 rejected"),
    _admin: str = Depends(verify_admin_token),
):
    """批准或拒绝审批请求。"""
    if decision not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'rejected'")
    _require_valid_uuid(approval_id, "Approval request not found or already decided")

    schema = zcfg.get_schema_name()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE {schema}.approval_requests "
            f"SET status = $1, decided_by = 'admin:console', decided_at = NOW() "
            f"WHERE approval_id = $2::uuid AND status = 'pending' "
            f"RETURNING approval_id, status",
            decision, approval_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Approval request not found or already decided")

    return {"approval_id": str(row["approval_id"]), "status": row["status"]}


# ════════════════════════════════════════════════════════════════
# 隔离区
# ════════════════════════════════════════════════════════════════

@router.get("/quarantine")
async def list_quarantine_files(
    agent_id: Optional[str] = Query(None, description="按 Agent 筛选"),
    status: str = Query("quarantined", description="状态筛选"),
    _admin: str = Depends(verify_admin_token),
):
    """列出隔离区文件。"""
    items = await quarantine_list(agent_id=agent_id or "", status=status)
    return {"items": items, "total": len(items)}


@router.post("/quarantine/{quarantine_id}/restore")
async def restore_quarantine_file(
    quarantine_id: str,
    _admin: str = Depends(verify_admin_token),
):
    """从隔离区恢复文件。"""
    _require_valid_uuid(quarantine_id, "Quarantine record not found or already purged")
    result = await restore_file(quarantine_id)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.delete("/quarantine/{quarantine_id}")
async def delete_quarantine_file(
    quarantine_id: str,
    _admin: str = Depends(verify_admin_token),
):
    """从隔离区永久删除文件（purge）。"""
    _require_valid_uuid(quarantine_id, "Quarantine record not found or already purged")
    schema = zcfg.get_schema_name()
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT quarantine_path FROM {schema}.quarantine "
            f"WHERE quarantine_id = $1::uuid AND status = 'quarantined'",
            quarantine_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Quarantine record not found or already purged")

    quarantine_path = row["quarantine_path"]
    if os.path.exists(quarantine_path):
        try:
            os.remove(quarantine_path)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")

    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {schema}.quarantine SET status = 'purged' "
            f"WHERE quarantine_id = $1::uuid",
            quarantine_id,
        )

    return {"status": "ok", "message": "File purged from quarantine"}


# ════════════════════════════════════════════════════════════════
# 配置备份
# ════════════════════════════════════════════════════════════════

@router.get("/backups/{filename:path}")
async def get_backups(
    filename: str,
    _admin: str = Depends(verify_admin_token),
):
    """列出某文件的所有备份版本。"""
    filename = os.path.basename(filename)  # 防御路径穿越（{filename:path} 可含 ../）
    backups = list_backups(filename)
    return {"filename": filename, "backups": backups}


# ════════════════════════════════════════════════════════════════
# 守卫规则
# ════════════════════════════════════════════════════════════════

@router.get("/guard/rules")
async def list_guard_rules(
    _admin: str = Depends(verify_admin_token),
):
    """列出所有守卫规则。"""
    engine = get_engine()
    rules = await engine.list_rules()
    return {"rules": rules, "total": len(rules)}


@router.post("/guard/rules")
async def add_guard_rule(
    name: str = Query(..., description="规则名称"),
    rule_type: str = Query(..., description="allow / deny / audit"),
    match_pattern: str = Query(..., description="匹配模式（glob）"),
    priority: int = Query(0, description="优先级（越高越先匹配）"),
    description: str = Query("", description="规则描述"),
    _admin: str = Depends(verify_admin_token),
):
    """添加守卫规则。"""
    engine = get_engine()
    try:
        result = await engine.add_rule(
            name=name,
            rule_type=rule_type,
            match_pattern=match_pattern,
            priority=priority,
            description=description,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.delete("/guard/rules/{rule_id}")
async def delete_guard_rule(
    rule_id: str,
    _admin: str = Depends(verify_admin_token),
):
    """删除守卫规则。"""
    _require_valid_uuid(rule_id, "Rule not found")
    engine = get_engine()
    deleted = await engine.delete_rule(rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"status": "ok", "message": f"Rule {rule_id} deleted"}


@router.post("/guard/check")
async def check_guard_rule(
    agent_id: str = Query(..., description="Agent ID"),
    action: str = Query(..., description="请求动作"),
    target: str = Query(..., description="请求目标"),
    _admin: str = Depends(verify_admin_token),
):
    """测试请求是否被守卫规则允许。"""
    engine = get_engine()
    result = await engine.check(agent_id=agent_id, action=action, target=target)
    return result


# ════════════════════════════════════════════════════════════════
# 密钥管理
# ════════════════════════════════════════════════════════════════

@router.get("/agents/{agent_id}/public-key")
async def get_agent_public_key(agent_id: str):
    """获取 Agent 活跃公钥（验签用）——公钥按 Ed25519 设计是公开信息，不做鉴权。

    review(2026-08-16): zhice/signing.py verify_signature 调用本路由，此前不存在
    → 智采 Agent 提交签名后引擎验签恒 404 失败（Ed25519 验签功能整体不可用）。
    公钥非敏感（公开分享正是公钥用途），验签方据此验证 Agent 签名，故免 token。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        key = await key_service.get_public_key(conn, agent_id=agent_id)
    if key is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 无活跃公钥（需先调 POST /v1/zhenyue/keys?agent_id=... 生成）")
    return key


@router.get("/agents/{agent_id}/private-key")
async def get_agent_private_key(
    agent_id: str,
    agent_info: dict = Depends(auth_dependency),
):
    """获取 Agent 私钥（check_results 签名用）。

    R11 (P?): key_service.get_private_key 此前无路由，Agent 无法获取私钥签名；
    且私钥敏感，不能像公钥一样免鉴权。仅 Agent 本人 / 平台（internal-ipc 或
    admin / ops_admin 角色）可读取。
    """
    caller = agent_info.get("agent_id", "")
    role = agent_info.get("role", "")
    is_platform = caller in ("internal-ipc",) or role in ("admin", "ops_admin")
    if not is_platform and caller != agent_id:
        raise HTTPException(status_code=403, detail="无权访问其他 Agent 的私钥")

    pool = await get_pool()
    async with pool.acquire() as conn:
        private_key = await key_service.get_private_key(conn, agent_id=agent_id)
    if private_key is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} 无活跃私钥（需先调 POST /v1/zhenyue/keys?agent_id=... 生成）")
    return {"agent_id": agent_id, "private_key": private_key}


@router.get("/keys")
async def list_agent_keys(
    agent_id: Optional[str] = Query(None, description="按 Agent 筛选"),
    status: Optional[str] = Query(None, description="按状态筛选"),
    _admin: str = Depends(verify_admin_token),
):
    """列出 Agent 密钥。"""
    schema = zcfg.get_schema_name()
    pool = await get_pool()

    conditions = []
    params = []
    idx = 1

    if agent_id:
        conditions.append(f"agent_id = ${idx}")
        params.append(agent_id)
        idx += 1
    if status:
        conditions.append(f"status = ${idx}")
        params.append(status)
        idx += 1

    where_clause = " AND ".join(conditions) if conditions else "TRUE"

    query = f"""
        SELECT key_id, agent_id, public_key, algorithm, status, created_at, revoked_at
        FROM {schema}.agent_keys
        WHERE {where_clause}
        ORDER BY created_at DESC
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return {"keys": [dict(r) for r in rows], "total": len(rows)}


@router.post("/keys", response_model=GenerateKeypairResponse)
async def generate_key(
    agent_id: str = Query(..., description="Agent ID"),
    _admin: str = Depends(verify_admin_token),
):
    """为 Agent 生成新密钥对。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await key_service.generate_keypair(conn, agent_id=agent_id)
    return result


@router.delete("/keys/{key_id}")
async def revoke_key(
    key_id: int,
    _admin: str = Depends(verify_admin_token),
):
    """撤销密钥。"""
    schema = zcfg.get_schema_name()
    pool = await get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE {schema}.agent_keys SET status = 'revoked', revoked_at = NOW() "
            f"WHERE key_id = $1 AND status = 'active' RETURNING key_id",
            key_id,
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Key not found or already revoked")

    return {"status": "ok", "message": f"Key {key_id} revoked"}


# ════════════════════════════════════════════════════════════════
# 调度器管理
# ════════════════════════════════════════════════════════════════

@router.post("/scheduler/start")
async def scheduler_start(
    _admin: str = Depends(verify_admin_token),
):
    """启动后台调度器。"""
    # P2 (R11): start/stop 为 async，此前未 await → 创建 task 未执行即被丢弃，
    # 调度器启停实际无操作。现 await 完成启动（start 幂等：已运行则直接返回）。
    await start_scheduler()
    return {"status": "ok", "message": "Scheduler started"}


@router.post("/scheduler/stop")
async def scheduler_stop(
    _admin: str = Depends(verify_admin_token),
):
    """停止后台调度器。"""
    # P2 (R11): stop 为 async 此前未 await → 只置位 _running 却未取消后台 task，
    # 调度循环仍在跑。现 await 完成取消与回收。
    await stop_scheduler()
    return {"status": "ok", "message": "Scheduler stopped"}


# ════════════════════════════════════════════════════════════════
# 健康检查 & 速率限制状态
# ════════════════════════════════════════════════════════════════

@router.get("/health")
async def health():
    """镇岳模块健康检查。"""
    return {
        "module": "zhenyue",
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/rate-limit/{key}")
async def get_rate_limit_status(
    key: str,
    _admin: str = Depends(verify_admin_token),
):
    """查询指定 key 的速率限制状态。"""
    remaining = await rate_limiter.remaining(key)
    return {
        "key": key,
        "limit": rate_limiter.limit,
        "window_seconds": rate_limiter.window,
        "remaining": remaining,
    }
