"""检测管线 CLI — SAST + 沙箱 + 断言 一站式检测

用法:
  python -m osskill.detection.pipeline run skills/incoming/excel-generator/
  python -m osskill.detection.pipeline run-all
  python -m osskill.detection.pipeline report skills/incoming/excel-generator/
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .assertions import run_assertions
from .models import DetectionReport, Finding, SEV_BLOCKER, SEV_WARN, SEV_PASS
from .sandbox import run_sandbox
from .sast_md import scan_skill_md

logger = logging.getLogger("osskill.detection.pipeline")

# ── 根目录 ──────────────────────────────────────────

def _resolve_incoming_dir() -> str:
    """解析 incoming 目录 — 优先配置，再尝试多个相对路径"""
    try:
        from common.config import get as cfg_get
        val = cfg_get("skill.incoming_dir", "")
        if val:
            return os.path.abspath(val)
    except Exception:
        pass

    # 相对路径回退（尝试不同嵌套深度）
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "..", "..", "..", "..", "skills", "incoming"),
        os.path.join(base, "..", "..", "..", "skills", "incoming"),
    ]
    for c in candidates:
        abspath = os.path.abspath(c)
        if os.path.isdir(abspath):
            return abspath
    return os.path.abspath(candidates[0])

SKILLS_INCOMING = _resolve_incoming_dir()
"""incoming skill 目录（自动解析）"""


# ── 检测管线 ──────────────────────────────────────────


def detect_skill(skill_dir: str) -> DetectionReport:
    """对单个 Skill 运行完整检测管线

    Args:
        skill_dir: Skill 目录路径（含 SKILL.md + skill.json）

    Returns:
        DetectionReport
    """
    skill_dir = os.path.abspath(skill_dir)
    skill_name = os.path.basename(skill_dir)

    # 读取版本
    skill_json_path = os.path.join(skill_dir, "skill.json")
    version = ""
    if os.path.isfile(skill_json_path):
        try:
            with open(skill_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                skill_name = data.get("name", skill_name)
                version = data.get("version", "")
        except Exception:
            pass

    report = DetectionReport(
        skill_name=skill_name,
        skill_path=skill_dir,
        skill_version=version,
    )

    # ── Step 1: SAST ──
    logger.info("[%s] Step 1/3: SAST scan ...", skill_name)
    try:
        sast_result = scan_skill_md(skill_dir)
        report.sast = sast_result
    except Exception as e:
        logger.exception("[%s] SAST scan failed", skill_name)
        report.errors.append(f"SAST 异常: {e}")
        report.passed = False
        return report

    if not sast_result.passed:
        for f in sast_result.blockers:
            logger.warning("[%s] SAST BLOCKER: %s", skill_name, f.message)

    # ── Step 2: 沙箱 ──
    logger.info("[%s] Step 2/3: Sandbox execution ...", skill_name)
    sandbox_result = None
    try:
        sandbox_result = run_sandbox(skill_dir)
        report.sandbox = sandbox_result
        if not sandbox_result.passed:
            logger.warning("[%s] Sandbox: %s", skill_name, sandbox_result.stderr[:200])
    except Exception as e:
        logger.exception("[%s] Sandbox failed", skill_name)
        report.errors.append(f"沙箱异常: {e}")

    # ── Step 3: 断言 ──
    logger.info("[%s] Step 3/3: Assertions ...", skill_name)
    assertion_result = None
    try:
        assertion_result = run_assertions(skill_dir)
        report.assertions = assertion_result
    except Exception as e:
        logger.exception("[%s] Assertions failed", skill_name)
        report.errors.append(f"断言异常: {e}")

    # ── 综合判定 ──
    # SAST BLOCKER → 不通过
    # 沙箱/断言失败 → WARN 不阻断（可能因环境缺失）
    passed = True
    if sast_result and not sast_result.passed:
        passed = False
    report.passed = passed

    # 上报指标 — 仅在异步上下文（API）中创建任务，CLI 模式直接跳过
    try:
        import asyncio
        from .metrics import report_detection
        sandbox_ms = report.sandbox.duration_ms if report.sandbox else 0
        loop = asyncio.get_running_loop()
        loop.create_task(report_detection(
            skill=report.skill_name,
            passed=report.passed,
            duration_ms=sandbox_ms,
        ))
    except RuntimeError:
        pass  # 无运行中事件循环（CLI 模式），跳过指标上报

    logger.info(
        "[%s] Detection %s (SAST=%s, Sandbox=%s, Assertions=%s)",
        skill_name, "PASS" if passed else "FAIL",
        sast_result.passed if sast_result else "?",
        sandbox_result.passed if sandbox_result else "?",
        assertion_result.passed if assertion_result else "?",
    )

    return report


def save_report(report: DetectionReport):
    """保存检测报告到 Skill 目录"""
    report_path = os.path.join(report.skill_path, ".detection_result.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
    logger.info("Report saved to %s", report_path)


def load_report(skill_dir: str) -> Optional[dict]:
    """加载最近的检测报告"""
    report_path = os.path.join(skill_dir, ".detection_result.json")
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


# ── CLI ──────────────────────────────────────────────


def _print_safe(*args, **kwargs):
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        text = (text.replace("🔴", "[ERR]").replace("🟡", "[WARN]").replace("ℹ️", "[INFO]")
                     .replace("✅", "[OK]").replace("❌", "[FAIL]").replace("⏱", "[TIMEOUT]"))
        print(text, **kwargs)


def _print_report(report: DetectionReport):
    """打印人类可读的检测报告"""
    _print_safe(f"\n{'='*60}")
    _print_safe(f" 检测报告: {report.skill_name} v{report.skill_version}")
    _print_safe(f"{'='*60}")

    status = "✅ 通过" if report.passed else "❌ 未通过"
    _print_safe(f" 状态: {status}")
    _print_safe(f" 路径: {report.skill_path}")

    # SAST
    if report.sast:
        _print_safe(f"\n ── SAST 扫描 ──")
        _print_safe(f"    通过: {'是' if report.sast.passed else '否'}")
        for f in report.sast.findings:
            icon = "🔴" if f.severity == SEV_BLOCKER else "🟡" if f.severity == SEV_WARN else "ℹ️"
            _print_safe(f"    {icon} [{f.rule_id}] {f.message}")
            if f.line:
                _print_safe(f"        {f.file}:{f.line}")

    # 沙箱
    if report.sandbox:
        _print_safe(f"\n ── 沙箱执行 ──")
        if report.sandbox.passed:
            _print_safe(f"    ✅ 通过 ({report.sandbox.duration_ms}ms)")
        else:
            icon = "⏱" if report.sandbox.timeout else "❌"
            err_type = f" [{report.sandbox.error_type}]" if report.sandbox.error_type else ""
            _print_safe(f"    {icon}{err_type} 失败 ({report.sandbox.duration_ms}ms)")
            if report.sandbox.stderr:
                _print_safe(f"    stderr: {report.sandbox.stderr[:300]}")

    # 断言
    if report.assertions:
        _print_safe(f"\n ── 功能断言 ──")
        for case in report.assertions.cases:
            icon = "✅" if case.passed else "❌"
            _print_safe(f"    {icon} {case.name}")
            if case.error:
                _print_safe(f"       {case.error}")

    # 错误
    if report.errors:
        _print_safe(f"\n ── 异常 ──")
        for err in report.errors:
            _print_safe(f"    ❌ {err}")

    _print_safe(f"\n{'='*60}\n")


def cli_run(args: list[str]) -> int:
    """CLI: run 命令"""
    if not args:
        _print_safe("用法: python -m osskill.detection.pipeline run <skill_dir>")
        return 1

    skill_dir = os.path.abspath(args[0])
    if not os.path.isdir(skill_dir):
        _print_safe(f"目录不存在: {skill_dir}")
        return 1

    report = detect_skill(skill_dir)
    save_report(report)
    _print_report(report)
    return 0 if report.passed else 1


def cli_run_all() -> int:
    """CLI: run-all 命令 — 扫描所有 incoming Skill"""
    incoming = SKILLS_INCOMING
    if not os.path.isdir(incoming):
        _print_safe(f"incoming 目录不存在: {incoming}")
        return 1

    entries = sorted([
        os.path.join(incoming, d)
        for d in os.listdir(incoming)
        if os.path.isdir(os.path.join(incoming, d))
        and os.path.isfile(os.path.join(incoming, d, "SKILL.md"))
    ])

    if not entries:
        _print_safe("incoming 中未找到含 SKILL.md 的 Skill 目录")
        return 0

    _print_safe(f"发现 {len(entries)} 个 Skill，开始批量检测...")

    results = []
    for entry in entries:
        name = os.path.basename(entry)
        _print_safe(f"\n{'─'*40}")
        _print_safe(f"[{name}] 检测中...")
        report = detect_skill(entry)
        save_report(report)
        results.append(report)
        _print_report(report)

    # 汇总
    passed_count = sum(1 for r in results if r.passed)
    _print_safe(f"\n{'='*60}")
    _print_safe(f" 批量检测完成: {passed_count}/{len(results)} 通过")
    _print_safe(f"{'='*60}")
    for r in results:
        status = "✅" if r.passed else "❌"
        _print_safe(f"  {status} {r.skill_name}")

    return 0 if passed_count == len(results) else 1


def cli_report(args: list[str]):
    """CLI: report 命令 — 查看已保存的检测报告"""
    if not args:
        _print_safe("用法: python -m osskill.detection.pipeline report <skill_dir>")
        return

    skill_dir = os.path.abspath(args[0])
    data = load_report(skill_dir)
    if data is None:
        _print_safe(f"未找到检测报告，先运行检测: {skill_dir}")
        return

    _print_safe(json.dumps(data, indent=2, ensure_ascii=False))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
    )

    args = sys.argv[1:]
    if not args:
        _print_safe("用法:")
        _print_safe("  python -m osskill.detection.pipeline run <skill_dir>")
        _print_safe("  python -m osskill.detection.pipeline run-all")
        _print_safe("  python -m osskill.detection.pipeline report <skill_dir>")
        return

    cmd = args[0]
    rest = args[1:]

    if cmd == "run":
        sys.exit(cli_run(rest))
    elif cmd == "run-all":
        sys.exit(cli_run_all())
    elif cmd == "report":
        cli_report(rest)
    else:
        _print_safe(f"未知命令: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
