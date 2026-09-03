"""SAST 扫描器 — 针对 SKILL.md + skill.json 的安全合规检测

与 osskill/sast.py（Python 源码 AST 分析）互补：
  - sast.py → 分析 .py 文件的权限一致性
  - sast_md.py → 分析 SKILL.md + skill.json 的内容安全

规则清单:
  MD-001 — 隐藏指令（prompt injection 模式）
  MD-002 — 外部下载链接（curl | bash, wget, pip install git+）
  MD-003 — 数据上报 URL（可疑回调、埋点）
  MD-004 — 隐藏 Unicode（零宽字符、不可见字符）
  MD-005 — 权限越界（permissions 不合规）
  MD-006 — 协议/来源标注不完整
  MD-007 — 代码块中含危险系统调用
  MD-008 — SKILL.md 文件缺失
  MD-009 — skill.json 文件缺失或格式错误
"""

import json
import logging
import os
import re
import unicodedata
from pathlib import Path

from .models import Finding, SASTResult, SEV_BLOCKER, SEV_WARN, SEV_INFO

logger = logging.getLogger(__name__)

# ── 规则定义 ──────────────────────────────────────────

# MD-001: Prompt 注入 / 隐藏指令模式
INJECTION_PATTERNS = [
    re.compile(r"忽略\s*(上述|所有|前面).*指令", re.IGNORECASE),
    re.compile(r"ignore\s+(all\s+)?(above|previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"you\s+are\s+(now|not)\s+", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)", re.IGNORECASE),
    re.compile(r"忘记.*身份", re.IGNORECASE),
    re.compile(r"假装你", re.IGNORECASE),
]

# MD-002: 外部下载 / 远程执行
DOWNLOAD_PATTERNS = [
    re.compile(r"curl\s+.*\|\s*(bash|sh)\b", re.IGNORECASE),  # curl xxx | bash
    re.compile(r"wget\s+.*\|\s*(bash|sh)\b", re.IGNORECASE),  # wget xxx | bash
    re.compile(r"wget\s+.*-O\s*-", re.IGNORECASE),            # wget -O -
    re.compile(r"pip\s+install\s+git\+", re.IGNORECASE),      # pip install git+
    re.compile(r"npm\s+(install|i)\s+-g\s+", re.IGNORECASE),  # npm install -g
]

# MD-003: 可疑 URL / 数据上报
SUSPICIOUS_URL_PATTERNS = [
    re.compile(r"https?://[^\s]*?(heimdall|telemetry|tracker|analytics|beacon|pixel|collect)", re.IGNORECASE),
    re.compile(r"https?://[^\s]*?/\w{40,}", re.IGNORECASE),  # 长 token 在 URL 中
    re.compile(r"https?://(localhost|127\.0\.0\.1)(:\d+)?/api/(v\d+/)?(event|log|track)"),
]

# MD-004: 隐藏 Unicode
HIDDEN_UNICODE_RANGES = [
    (0x200B, 0x200F),   # 零宽空格/连字/不连字/从左到右/从右到左
    (0x2028, 0x2029),   # 行分隔符/段分隔符
    (0x202A, 0x202E),   # bidi 覆盖
    (0xFEFF, 0xFEFF),   # BOM / 零宽不换行空格
    (0x00AD, 0x00AD),   # 软连字符
    (0x2060, 0x2064),   # 字连接符/不可见运算符
]

# MD-005: 权限白名单（显式声明的合法值）
ALLOWED_PERMISSIONS = {"llm", "network", "filesystem", "system", "skills", "identity", "lifecycle", "network:outbound"}

# MD-007: 代码块中的危险系统调用（LLM 生成的代码含这些调用说明 Skill 设计有问题）
DANGEROUS_CODE_PATTERNS = [
    re.compile(r"os\.system\("),
    re.compile(r"subprocess\.(run|Popen|call)\("),
    re.compile(r"shutil\.rmtree\("),
    re.compile(r"eval\("),
    re.compile(r"exec\("),
    re.compile(r"pickle\.loads?"),
    re.compile(r"__import__\("),
]

# MD-006: 所需 metadata 字段
REQUIRED_SKILL_JSON_FIELDS = {
    "name", "display_name", "version", "description",
    "author", "source", "license_info", "entry", "permissions",
}
REQUIRED_AUTHOR_FIELDS = {"type", "name"}
REQUIRED_SOURCE_FIELDS = {"platform", "license"}
REQUIRED_LICENSE_FIELDS = {"type"}


# ── 扫描引擎 ──────────────────────────────────────────


def _extract_python_blocks(md_text: str) -> list[tuple[int, str]]:
    """从 Markdown 中提取所有 Python 代码块及其起始行号"""
    blocks = []
    lines = md_text.split("\n")
    in_code = False
    code_lines: list[str] = []
    start_line = 0

    for i, line in enumerate(lines):
        if line.startswith("```"):
            if in_code:
                in_code = False
                code = "\n".join(code_lines)
                blocks.append((start_line + 1, code))
                code_lines = []
            else:
                lang = line[3:].strip()
                if lang == "python":
                    start_line = i
                    in_code = True
        elif in_code:
            code_lines.append(line)

    return blocks


def _check_hidden_unicode(text: str) -> list[tuple[int, int, str]]:
    """检测隐藏 Unicode 字符，返回 [(位置, codepoint, 描述)]"""
    findings = []
    for i, ch in enumerate(text):
        cp = ord(ch)
        for start, end in HIDDEN_UNICODE_RANGES:
            if start <= cp <= end:
                name = unicodedata.name(ch, "UNKNOWN")
                findings.append((i, cp, f"隐藏字符 U+{cp:04X} ({name})"))
                break
    return findings


def scan_skill_md(skill_dir: str) -> SASTResult:
    """扫描 SKILL.md + skill.json 目录，返回 SAST 结果

    Args:
        skill_dir: 包含 SKILL.md 和 skill.json 的目录

    Returns:
        SASTResult — passed=False 表示有必须修复的问题
    """
    skill_dir = os.path.abspath(skill_dir)
    findings: list[Finding] = []

    # ── 1. 检查文件存在性 ──
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    skill_json_path = os.path.join(skill_dir, "skill.json")

    if not os.path.isfile(skill_md_path):
        findings.append(Finding(
            rule_id="MD-008", severity=SEV_BLOCKER,
            message="缺少 SKILL.md 文件",
            file="SKILL.md",
        ))

    if not os.path.isfile(skill_json_path):
        findings.append(Finding(
            rule_id="MD-009", severity=SEV_BLOCKER,
            message="缺少 skill.json 文件",
            file="skill.json",
        ))

    # 缺少关键文件，直接返回
    if not os.path.isfile(skill_md_path) or not os.path.isfile(skill_json_path):
        return SASTResult(passed=False, findings=findings)

    # ── 2. 读取 SKILL.md ──
    with open(skill_md_path, "r", encoding="utf-8") as f:
        md_content = f.read()

    # MD-004: 隐藏 Unicode
    hidden_chars = _check_hidden_unicode(md_content)
    for pos, cp, desc in hidden_chars:
        line = md_content[:pos].count("\n") + 1
        findings.append(Finding(
            rule_id="MD-004", severity=SEV_BLOCKER,
            message=desc,
            file="SKILL.md", line=line,
            snippet=md_content[max(0, pos-10):pos+10],
        ))

    # MD-001: Prompt 注入模式
    for pattern in INJECTION_PATTERNS:
        for match in pattern.finditer(md_content):
            line = md_content[:match.start()].count("\n") + 1
            findings.append(Finding(
                rule_id="MD-001", severity=SEV_BLOCKER,
                message=f"检测到可能的注入指令: {match.group()[:60]}",
                file="SKILL.md", line=line,
                snippet=match.group()[:100],
            ))

    # MD-002: 外部下载
    for pattern in DOWNLOAD_PATTERNS:
        for match in pattern.finditer(md_content):
            line = md_content[:match.start()].count("\n") + 1
            findings.append(Finding(
                rule_id="MD-002", severity=SEV_BLOCKER,
                message=f"外部下载/执行: {match.group()[:80]}",
                file="SKILL.md", line=line,
                snippet=match.group()[:100],
            ))

    # MD-003: 可疑 URL
    for pattern in SUSPICIOUS_URL_PATTERNS:
        for match in pattern.finditer(md_content):
            line = md_content[:match.start()].count("\n") + 1
            findings.append(Finding(
                rule_id="MD-003", severity=SEV_BLOCKER,
                message=f"可疑 URL: {match.group()[:80]}",
                file="SKILL.md", line=line,
                snippet=match.group()[:100],
            ))

    # MD-007: 代码块中危险调用（仅检测 ```python 代码块内的调用）
    code_blocks = _extract_python_blocks(md_content)
    for (block_start_line, block_code) in code_blocks:
        for pattern in DANGEROUS_CODE_PATTERNS:
            for match in pattern.finditer(block_code):
                line = block_start_line + block_code[:match.start()].count("\n")
                findings.append(Finding(
                    rule_id="MD-007", severity=SEV_WARN,
                    message=f"代码块中含危险调用: {match.group()}",
                    file="SKILL.md", line=line,
                    snippet=match.group()[:100],
                ))

    # ── 3. 读取 skill.json ──
    with open(skill_json_path, "r", encoding="utf-8") as f:
        try:
            manifest = json.load(f)
        except json.JSONDecodeError as e:
            findings.append(Finding(
                rule_id="MD-009", severity=SEV_BLOCKER,
                message=f"skill.json JSON 解析失败: {e}",
                file="skill.json",
            ))
            return SASTResult(passed=False, findings=findings)

    # MD-006: 字段完整性
    missing_fields = REQUIRED_SKILL_JSON_FIELDS - set(manifest.keys())
    if missing_fields:
        findings.append(Finding(
            rule_id="MD-006", severity=SEV_WARN,
            message=f"skill.json 缺少字段: {', '.join(sorted(missing_fields))}",
            file="skill.json",
        ))

    # author 子字段
    author = manifest.get("author", {})
    if isinstance(author, dict):
        missing_author = REQUIRED_AUTHOR_FIELDS - set(author.keys())
        if missing_author:
            findings.append(Finding(
                rule_id="MD-006", severity=SEV_WARN,
                message=f"author 缺少字段: {', '.join(sorted(missing_author))}",
                file="skill.json",
            ))

    # source 子字段
    source = manifest.get("source", {})
    if isinstance(source, dict):
        missing_source = REQUIRED_SOURCE_FIELDS - set(source.keys())
        if missing_source:
            findings.append(Finding(
                rule_id="MD-006", severity=SEV_WARN,
                message=f"source 缺少字段: {', '.join(sorted(missing_source))}",
                file="skill.json",
            ))

    # license_info 子字段
    license_info = manifest.get("license_info", {})
    if isinstance(license_info, dict):
        missing_license = REQUIRED_LICENSE_FIELDS - set(license_info.keys())
        if missing_license:
            findings.append(Finding(
                rule_id="MD-006", severity=SEV_WARN,
                message=f"license_info 缺少字段: {', '.join(sorted(missing_license))}",
                file="skill.json",
            ))

    # MD-005: 权限检查
    declared_perms = set(manifest.get("permissions", []))
    unknown_perms = declared_perms - ALLOWED_PERMISSIONS
    if unknown_perms:
        findings.append(Finding(
            rule_id="MD-005", severity=SEV_BLOCKER,
            message=f"声明了未知权限: {', '.join(sorted(unknown_perms))}",
            file="skill.json",
        ))

    # ── 4. 提取 Skill 名称 ──
    name = manifest.get("name", Path(skill_dir).name)

    passed = not any(f.severity == SEV_BLOCKER for f in findings)
    result = SASTResult(passed=passed, findings=findings)

    logger.info(
        "SAST scan [%s]: %s (passed=%s, %d blockers, %d warnings)",
        name, skill_dir, passed,
        len(result.blockers), len(result.warnings),
    )
    return result
