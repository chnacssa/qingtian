"""检测管线 — 数据模型"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ── 严重度 ──────────────────────────────────────────────

SEV_BLOCKER = "BLOCKER"
SEV_WARN = "WARN"
SEV_INFO = "INFO"
SEV_PASS = "PASS"


# ── SAST 发现 ──────────────────────────────────────────


@dataclass
class Finding:
    """单个检测发现"""
    rule_id: str
    severity: str  # BLOCKER / WARN / INFO
    message: str
    file: str = ""
    line: int = 0
    snippet: str = ""


# ── SAST 结果 ──────────────────────────────────────────


@dataclass
class SASTResult:
    """SKILL.md SAST 扫描结果"""
    passed: bool
    findings: list[Finding] = field(default_factory=list)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEV_BLOCKER]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == SEV_WARN]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "blockers": len(self.blockers),
            "warnings": len(self.warnings),
            "findings": [
                {"rule_id": f.rule_id, "severity": f.severity,
                 "message": f.message, "file": f.file,
                 "line": f.line, "snippet": f.snippet}
                for f in self.findings
            ],
        }


# ── 沙箱结果 ──────────────────────────────────────────


@dataclass
class SandboxResult:
    """沙箱执行结果"""
    passed: bool
    stdout: str = ""
    stderr: str = ""
    timeout: bool = False
    exit_code: int = -1
    duration_ms: int = 0
    error_type: str = ""
    """错误分类: DEPENDENCY / INPUT_FILE / TIMEOUT / SYNTAX / RUNTIME / 空字符串表示无错误"""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "timeout": self.timeout,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "error_type": self.error_type,
            "stdout": self.stdout[:500],
            "stderr": self.stderr[:500],
        }


# ── 断言结果 ──────────────────────────────────────────


@dataclass
class AssertionResult:
    """功能断言结果"""
    name: str
    passed: bool
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "error": self.error,
        }


@dataclass
class AssertionsResult:
    """全部断言结果"""
    passed: bool
    cases: list[AssertionResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "cases": [c.to_dict() for c in self.cases],
        }


# ── 完整检测报告 ──────────────────────────────────────


@dataclass
class DetectionReport:
    """Skill 检测完整报告"""
    skill_name: str
    skill_path: str
    skill_version: str = ""
    detected_at: str = ""
    passed: bool = False
    sast: Optional[SASTResult] = None
    sandbox: Optional[SandboxResult] = None
    assertions: Optional[AssertionsResult] = None
    errors: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = datetime.now(timezone.utc).isoformat() + "Z"

    def to_dict(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "skill_path": self.skill_path,
            "skill_version": self.skill_version,
            "detected_at": self.detected_at,
            "passed": self.passed,
            "sast": self.sast.to_dict() if self.sast else None,
            "sandbox": self.sandbox.to_dict() if self.sandbox else None,
            "assertions": self.assertions.to_dict() if self.assertions else None,
            "errors": self.errors,
        }
