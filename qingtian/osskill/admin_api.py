"""Skill 管理 API — 运行时管理 + 吊销管理端点

路由前缀: /api/v1/skills/admin
所有接口需要 management 角色。
"""

import json
import os
import tempfile

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from .api import require_management

router = APIRouter(
    prefix="/api/v1/skills/admin",
    tags=["技能管理"],
    dependencies=[Depends(require_management)],
)


# ── Pydantic 模型 ──


class BlacklistImportRequest(BaseModel):
    content: str  # 黑板名单 JSON 内容


class SkillActionResponse(BaseModel):
    skill_name: str
    action: str
    status: str
    message: str


# ── 运行时引用（由 main.py 初始化时注入） ──

_runtime_service = None
_revocation_service = None


def init_admin_api(runtime_service=None, revocation_service=None):
    """注入依赖（由 main.py 启动时调用）"""
    global _runtime_service, _revocation_service
    _runtime_service = runtime_service
    _revocation_service = revocation_service


def get_runtime_service():
    """获取全局 RuntimeService 实例（skill_updater 等后台任务用）。"""
    return _runtime_service


# ── 运行时管理 ──


@router.post("/runtime/{skill_name}/start")
async def admin_start_skill(skill_name: str, agent_id: str = ""):
    """启动 Skill 子进程"""
    if _runtime_service is None or _runtime_service._runtime is None:
        raise HTTPException(status_code=503, detail="XiheRuntime not initialized")
    try:
        await _runtime_service._runtime.launch_skill(skill_name, agent_id=agent_id)
        return SkillActionResponse(
            skill_name=skill_name, action="start",
            status="ok", message="Skill started",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])


@router.post("/runtime/{skill_name}/stop")
async def admin_stop_skill(skill_name: str, agent_id: str = ""):
    """停止 Skill 子进程"""
    if _runtime_service is None or _runtime_service._runtime is None:
        raise HTTPException(status_code=503, detail="XiheRuntime not initialized")
    try:
        # 停止 ≠ 卸载：uninstall 会执行 on_data_purge + 删除数据目录（purge_data=True 默认）
        await _runtime_service._runtime.stop_skill(skill_name, agent_id=agent_id)
        return SkillActionResponse(
            skill_name=skill_name, action="stop",
            status="ok", message="Skill stopped",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])


@router.post("/runtime/{skill_name}/restart")
async def admin_restart_skill(skill_name: str, agent_id: str = ""):
    """重启 Skill 子进程"""
    if _runtime_service is None or _runtime_service._runtime is None:
        raise HTTPException(status_code=503, detail="XiheRuntime not initialized")
    try:
        await _runtime_service._runtime.stop_skill(skill_name, agent_id=agent_id)
        await _runtime_service._runtime.launch_skill(skill_name, agent_id=agent_id)
        return SkillActionResponse(
            skill_name=skill_name, action="restart",
            status="ok", message="Skill restarted",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])


@router.get("/runtime")
async def admin_runtime_status():
    """运行时状态概览"""
    if _runtime_service is None or _runtime_service._runtime is None:
        return {"skills": [], "total": 0}
    skills = await _runtime_service._runtime.list_skills()
    return {
        "skills": skills,
        "total": len(skills),
        "max_processes": _runtime_service._runtime.config.max_processes,
    }


# ── 吊销管理 ──


@router.post("/blacklist/import")
async def admin_import_blacklist(body: BlacklistImportRequest):
    """导入黑板名单（JSON 字符串）"""
    if _revocation_service is None:
        raise HTTPException(status_code=503, detail="RevocationService not initialized")

    try:
        data = json.loads(body.content)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    # 写临时文件
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
    ) as f:
        json.dump(data, f)
        tmp_path = f.name

    # P2 (R11)：闭源 RevocationService 有 import_blacklist_file，开源
    # RevocationManager 早期没有（现已补齐同契约方法）。对注入对象做能力探测，
    # 缺失时降级 501 清晰报错而非 AttributeError 500。
    if not hasattr(_revocation_service, "import_blacklist_file"):
        raise HTTPException(
            status_code=501,
            detail={"code": "BLACKLIST_IMPORT_UNSUPPORTED",
                    "message": "当前 RevocationService 不支持黑板名单文件导入"},
        )
    try:
        count = _revocation_service.import_blacklist_file(tmp_path)
        return {"imported": count, "message": f"Imported {count} entries"}
    finally:
        os.unlink(tmp_path)


@router.get("/blacklist")
async def admin_list_blacklist():
    """查看黑板名单"""
    if _revocation_service is None:
        return {"blacklist": {}, "total": 0}
    entries = _revocation_service.get_blacklisted()
    return {"blacklist": entries, "total": len(entries)}


@router.get("/blacklist/{skill_name}")
async def admin_check_blacklist(skill_name: str):
    """检查指定 Skill 是否在黑名单中"""
    if _revocation_service is None:
        return {"skill_name": skill_name, "blacklisted": False}
    entry = _revocation_service.get_blacklist_entry(skill_name)
    return {
        "skill_name": skill_name,
        "blacklisted": entry is not None,
        "entry": entry,
    }


@router.post("/blacklist/refresh")
async def admin_refresh_blacklist():
    """强制拉取最新黑板名单"""
    if _revocation_service is None:
        raise HTTPException(status_code=503, detail="RevocationService not initialized")
    try:
        revoked = await _revocation_service.poll_once()
        return {
            "refreshed": True,
            "revoked_count": len(revoked),
            "revoked_skills": [r.get("skill_name") for r in revoked],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])


# ── License 管理 ──


@router.post("/license/verify")
async def admin_verify_license(skill_name: str = Query(default=""),
                               license_data: dict = {}):
    """验证 Skill License"""
    try:
        from osskill_acssa.license_manager import verify_skill_license
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail={"code": "CLOSED_SOURCE_MISSING",
                    "message": "闭源 osskill-acssa 未安装，License 在线验证不可用"},
        )

    result = verify_skill_license(skill_name, license_data)
    return {
        "skill_name": skill_name,
        "valid": result.valid,
        "level": result.level,
        "license_type": result.license_type,
        "error": result.error,
    }


class GrantLicenseRequest(BaseModel):
    enterprise_id: str = ""
    days: int = 0          # 0=回退 manifest trial_days
    perpetual: bool = False


@router.post("/license/grant")
async def admin_grant_license(req: GrantLicenseRequest, skill_name: str = Query(default="")):
    """管理员签发 License：指定免费期/永久。

    - days>0: 试用 License，有效期 days 天
    - perpetual=true: 永久 License，expires=2099-12-31
    - 两者都未设: 回退 skill.json trial_days
    """
    if not skill_name:
        raise HTTPException(400, "缺少 skill_name")
    from .market_integration import _get_license_manager
    mgr = _get_license_manager()
    lic = mgr.grant_license(
        skill_name=skill_name,
        enterprise_id=req.enterprise_id,
        days=req.days,
        perpetual=req.perpetual,
    )
    return {"ok": True, "license": lic}


@router.post("/license/revoke")
async def admin_revoke_license(skill_name: str = Query(default="")):
    """管理员吊销 License（删除本地文件）。"""
    if not skill_name:
        raise HTTPException(400, "缺少 skill_name")
    from .market_integration import _get_license_manager
    mgr = _get_license_manager()
    ok = mgr.revoke_license(skill_name)
    return {"ok": ok, "skill_name": skill_name}


@router.get("/license/list")
async def admin_list_licenses():
    """管理员列出所有本地 License。"""
    from .market_integration import _get_license_manager
    return {"licenses": _get_license_manager().list_licenses()}


# ── 管理员消息收件箱 ──


class InboxQueryParams(BaseModel):
    page: int = 1
    page_size: int = 20
    level: str = ""  # critical | warning | info
    unread_only: bool = False


class MessageActionRequest(BaseModel):
    msg_ids: list[str] = []


@router.get("/messages")
async def admin_list_messages(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    level: str = Query(default=""),
    unread_only: bool = Query(default=False),
):
    """管理员收件箱 — 消息列表

    按时间降序排列，支持按级别筛选和仅未读。
    """
    from common.db import get_pool

    pool = await get_pool()
    conditions = ["1=1"]
    params = []
    idx = 1

    if level:
        conditions.append(f"level = ${idx}")
        params.append(level)
        idx += 1

    if unread_only:
        conditions.append("NOT read")

    where = " AND ".join(conditions)
    offset = (page - 1) * page_size

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM admin_messages WHERE {where}",
            *params,
        )
        rows = await conn.fetch(
            f"""SELECT id, msg_id, level, source, title, body, dedup_key,
                       count, read, archived, created_at
                FROM admin_messages
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
            *params,
            page_size,
            offset,
        )

    messages = []
    for r in rows:
        messages.append({
            "id": r["id"],
            "msg_id": r["msg_id"],
            "level": r["level"],
            "source": r["source"],
            "title": r["title"],
            "body": r["body"],
            "dedup_key": r["dedup_key"],
            "count": r["count"],
            "read": r["read"],
            "archived": r["archived"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else "",
        })

    return {
        "messages": messages,
        "total": total,
        "page": page,
        "page_size": page_size,
        "unread_count": sum(1 for m in messages if not m["read"]),
    }


@router.put("/messages/read")
async def admin_mark_read(body: MessageActionRequest):
    """标记消息已读"""
    if not body.msg_ids:
        return {"updated": 0}

    from common.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE admin_messages SET read = TRUE WHERE msg_id = ANY($1)",
            body.msg_ids,
        )
    updated = int(result.split()[-1]) if result else 0
    return {"updated": updated}


@router.put("/messages/archive")
async def admin_archive_messages(body: MessageActionRequest):
    """归档消息"""
    if not body.msg_ids:
        return {"updated": 0}

    from common.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE admin_messages SET archived = TRUE WHERE msg_id = ANY($1)",
            body.msg_ids,
        )
    updated = int(result.split()[-1]) if result else 0
    return {"updated": updated}


@router.get("/messages/unread-count")
async def admin_unread_count():
    """未读消息数（用于导航栏角标）"""
    from common.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM admin_messages WHERE NOT read AND NOT archived"
        )
    return {"unread_count": count or 0}
