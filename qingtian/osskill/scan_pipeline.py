"""Skill 提交扫描流水线 — 提交时自动运行 SAST 权限检查

流程:
  开发者提交 Skill → 签名验证 → SAST 权限检查 → 结果入库 → 提交至审核

与 skills_api.py（破军的市场模块）配合使用:
  from osskill.scan_pipeline import run_submission_scan
  result = await run_submission_scan(skill_dir)
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .sast import scan_directory, SASTReport, SASTFinding

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """完整扫描结果（SAST + 后续扩展）"""
    passed: bool
    sast_report: SASTReport
    scanned_at: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_api_dict(self) -> dict:
        """转为 API 返回格式"""
        summary = self.sast_report.summary
        return {
            "passed": self.passed,
            "scanned_at": self.scanned_at,
            "sast": summary,
            "errors": self.errors,
            "warnings": self.warnings,
        }


async def run_submission_scan(skill_dir: str) -> ScanResult:
    """Skill 提交时运行的完整扫描流水线

    扫描流程:
      1. SAST 权限一致性检查（当前）
      2. 签名验证（待扩展）
      3. 依赖安全检查（待扩展）

    Args:
        skill_dir: Skill 包目录（含 skill.json）

    Returns:
        ScanResult — passed=False 则提交应被拒绝
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "") + "Z"

    # ── Step 1: SAST 权限一致性检查 ──
    try:
        sast_report = await asyncio.to_thread(scan_directory, skill_dir)
    except Exception as e:
        logger.exception("SAST scan failed for %s", skill_dir)
        sast_report = SASTReport()
        sast_report.passed = False
        sast_report.findings.append(
            SASTFinding(
                rule_id="SAST-000",
                severity="🔴",
                permission="",
                message=f"SAST 扫描异常: {e}",
            )
        )

    result = ScanResult(
        passed=sast_report.passed,
        sast_report=sast_report,
        scanned_at=now,
        errors=[],
        warnings=[],
    )

    for finding in sast_report.findings:
        if finding.severity == "🔴":
            result.errors.append(
                f"[{finding.rule_id}] {finding.permission}: {finding.message}"
            )
        elif finding.severity == "🟡":
            result.warnings.append(
                f"[{finding.rule_id}] {finding.message}"
            )

    # ── Step 2: 签名验证（占位，待市场模块完成后扩展） ──
    # TODO: 验证 skill.json 中的 certificate 签名

    # ── Step 3: 依赖检查（占位） ──
    # TODO: 检查依赖版本兼容性

    return result


def save_scan_result(skill_name: str, scan_result: ScanResult) -> str:
    """将扫描结果保存到 skill 目录（持久化供审核页面读取）

    Returns:
        结果文件路径
    """
    skill_dir = scan_result.sast_report.skill_path
    result_path = os.path.join(skill_dir, ".sast_result.json")

    data = scan_result.to_api_dict()
    data["declared_permissions"] = scan_result.sast_report.declared_permissions
    data["detected_permissions"] = sorted(
        scan_result.sast_report.detected_permissions.keys()
    )

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.info("SAST result saved to %s", result_path)
    return result_path


def load_scan_result(skill_dir: str) -> Optional[dict]:
    """从 skill 目录加载最近的扫描结果（供审核页面读取）"""
    result_path = os.path.join(skill_dir, ".sast_result.json")
    if not os.path.isfile(result_path):
        return None
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load SAST result from %s: %s", result_path, e)
        return None
