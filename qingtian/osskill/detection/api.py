"""检测 + Skill 生命周期 API — 提交→检测→上架→安装→卸载

路由前缀: /api/v1/skills/detection
需 management 角色。
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..api import require_management
from .metrics import report_detection, report_install
from .pipeline import SKILLS_INCOMING, detect_skill, load_report, save_report

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/skills/detection",
    tags=["Skill 检测与生命周期"],
    dependencies=[Depends(require_management)],
)

# ── 配置 ──────────────────────────────────────────────

SKILLS_PACKAGES = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "skills", "packages")
)
"""已上架 Skill 目录"""


def _validate_path_safe(target: str, base_dir: str) -> str:
    """验证路径在 base_dir 范围内，防范路径遍历攻击"""
    real_target = os.path.realpath(target)
    real_base = os.path.realpath(base_dir)
    if not real_target.startswith(real_base + os.sep) and real_target != real_base:
        raise ValueError(f"Path traversal detected: {target} (resolved: {real_target})")
    return real_target


# ── 检测 API ──────────────────────────────────────────


@router.post("/run/{skill_name}")
async def api_detect_skill(skill_name: str):
    """运行指定 Skill 的检测管线"""
    skill_dir = os.path.join(SKILLS_INCOMING, skill_name)
    if not os.path.isdir(skill_dir):
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 不在 incoming 中")

    if not os.path.isfile(os.path.join(skill_dir, "SKILL.md")):
        raise HTTPException(status_code=400, detail=f"Skill '{skill_name}' 缺少 SKILL.md")

    try:
        report = detect_skill(skill_dir)
        save_report(report)

        # 上报指标
        await report_detection(
            skill=report.skill_name,
            passed=report.passed,
            duration_ms=report.sandbox.duration_ms if report.sandbox else 0,
        )

        return report.to_dict()
    except Exception as e:
        logger.exception("Detection failed for %s", skill_name)
        raise HTTPException(status_code=500, detail=str(e)[:500])


@router.get("/result/{skill_name}")
async def api_get_detection_result(skill_name: str):
    """查询 Skill 的最近检测结果"""
    skill_dir = os.path.join(SKILLS_INCOMING, skill_name)
    if not os.path.isdir(skill_dir):
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 不存在")

    result = load_report(skill_dir)
    if result is None:
        return {
            "skill_name": skill_name,
            "detected": False,
            "message": "该 Skill 尚未检测，请先 POST /api/v1/skills/detection/run/{name}",
        }

    return {"skill_name": skill_name, "detected": True, "result": result}


@router.post("/run-all")
async def api_detect_all():
    """批量检测 incoming 中所有未检测的 Skill"""
    if not os.path.isdir(SKILLS_INCOMING):
        raise HTTPException(status_code=404, detail="incoming 目录不存在")

    entries = sorted([
        d for d in os.listdir(SKILLS_INCOMING)
        if os.path.isdir(os.path.join(SKILLS_INCOMING, d))
        and os.path.isfile(os.path.join(SKILLS_INCOMING, d, "SKILL.md"))
    ])

    results = []
    for name in entries:
        skill_dir = os.path.join(SKILLS_INCOMING, name)
        try:
            report = detect_skill(skill_dir)
            save_report(report)
            await report_detection(
                skill=report.skill_name,
                passed=report.passed,
                duration_ms=report.sandbox.duration_ms if report.sandbox else 0,
            )
            results.append(report.to_dict())
        except Exception as e:
            logger.exception("Detection failed for %s", name)
            results.append({"skill_name": name, "passed": False, "error": str(e)[:200]})

    passed_count = sum(1 for r in results if r.get("passed"))
    return {
        "total": len(results),
        "passed": passed_count,
        "results": results,
    }


# ── 上架/安装 API ──────────────────────────────────────


@router.post("/publish/{skill_name}")
async def api_publish_skill(skill_name: str, force: bool = Query(default=False)):
    """上架 Skill — 检测通过后从 incoming 移入 packages"""
    incoming_dir = os.path.join(SKILLS_INCOMING, skill_name)
    if not os.path.isdir(incoming_dir):
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 不在 incoming 中")

    if not os.path.isfile(os.path.join(incoming_dir, "SKILL.md")):
        raise HTTPException(status_code=400, detail="缺少 SKILL.md")

    # 检查检测结果
    if not force:
        result = load_report(incoming_dir)
        if result is None:
            raise HTTPException(
                status_code=400,
                detail="该 Skill 尚未检测，请先运行检测。或传 force=true 强制上架",
            )
        if not result.get("passed"):
            raise HTTPException(
                status_code=400,
                detail="检测未通过，请先修复问题。或传 force=true 强制上架",
            )

    # 目标目录
    packages_dir = os.path.join(SKILLS_PACKAGES, skill_name)
    _validate_path_safe(packages_dir, SKILLS_PACKAGES)
    if os.path.isdir(packages_dir):
        shutil.rmtree(packages_dir)

    # 复制（保留检测报告等元数据）
    shutil.copytree(incoming_dir, packages_dir, dirs_exist_ok=True)

    logger.info("Skill '%s' published: %s → %s", skill_name, incoming_dir, packages_dir)

    return {
        "ok": True,
        "skill_name": skill_name,
        "action": "publish",
        "source": incoming_dir,
        "target": packages_dir,
        "force": force,
    }


@router.post("/install/{skill_name}")
async def api_install_skill(skill_name: str, agent_id: str = ""):
    """安装 Skill 到运行时"""
    packages_dir = os.path.join(SKILLS_PACKAGES, skill_name)
    if not os.path.isdir(packages_dir):
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 未上架")

    # 调用运行时启动
    from ..admin_api import _runtime_service
    if _runtime_service is None:
        raise HTTPException(status_code=503, detail="RuntimeService not initialized")

    try:
        await _runtime_service.launch_skill(skill_name, agent_id=agent_id)
        await report_install(skill_name)
        return {
            "ok": True,
            "skill_name": skill_name,
            "action": "install",
            "status": "installed",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])


@router.post("/uninstall/{skill_name}")
async def api_uninstall_skill(skill_name: str, agent_id: str = ""):
    """卸载 Skill 从运行时（不删除 packages 文件）"""
    from ..admin_api import _runtime_service
    if _runtime_service is None or _runtime_service._runtime is None:
        raise HTTPException(status_code=503, detail="XiheRuntime not initialized")

    try:
        await _runtime_service.uninstall(skill_name, agent_id=agent_id)
        return {
            "ok": True,
            "skill_name": skill_name,
            "action": "uninstall",
            "status": "uninstalled",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)[:500])


@router.delete("/{skill_name}")
async def api_delete_skill(skill_name: str):
    """从 incoming 和 packages 中删除 Skill"""
    deleted = []
    for base in (SKILLS_INCOMING, SKILLS_PACKAGES):
        target = os.path.join(base, skill_name)
        _validate_path_safe(target, base)
        if os.path.isdir(target):
            shutil.rmtree(target)
            deleted.append(target)

    if not deleted:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' 未找到")

    return {
        "ok": True,
        "skill_name": skill_name,
        "action": "delete",
        "deleted": deleted,
    }
