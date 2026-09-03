"""SAST 权限一致性检查 — 静态分析 Skill 代码中的权限使用情况

通过 Python AST 分析检测 Skill 代码实际使用的 API 调用，
与 skill.json 声明的 permissions 字段对比，生成一致性报告。

规则:
  SAST-001 — 检测到 network 调用但未声明 network 或 network:outbound
  SAST-002 — 检测到 subprocess/os.system 但未声明 system
  SAST-003 — 检测到跨目录文件操作但未声明 filesystem
  SAST-004 — 检测到 ctx.llm 但未声明 llm
  SAST-005 — 检测到 ctx.call_skill 但未声明 skills
  SAST-006 — 声明了权限但代码未使用（过度声明 → 🟡 警告）
  SAST-007 — 检测到身份凭证操作但未声明 identity
  SAST-008 — 检测到生命周期操作但未声明 lifecycle
"""

import ast
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 安全限制 ──────────────────────────────────────────

MAX_SCAN_FILES = 1000
"""单次 SAST 扫描的最大 .py 文件数，超限跳过并告警，防 OOM（🔴 B6）"""

# ── 权限规则（从共享模块导入） ────────────────────────

from common.permission_rules import _PERMISSION_RULES


# ── 结果类型 ──────────────────────────────────────────


@dataclass
class SASTFinding:
    """单个检测结果"""
    rule_id: str  # SAST-001 ~ SAST-008
    severity: str  # 🔴 error / 🟡 warning / ℹ️ info
    permission: str  # 涉及的权限名（如 "network"）
    message: str  # 人类可读的描述
    line: int = 0  # 代码行号
    code: str = ""  # 相关代码片段


@dataclass
class SASTReport:
    """SAST 扫描报告"""
    skill_name: str = ""
    skill_path: str = ""
    declared_permissions: list[str] = field(default_factory=list)
    detected_permissions: dict[str, list[str]] = field(default_factory=dict)
    """{permission: [evidence_str, ...]}"""
    findings: list[SASTFinding] = field(default_factory=list)
    passed: bool = True  # True = 通过, False = 有必须修复的问题

    @property
    def summary(self) -> dict:
        """简洁的摘要字典，供 API 返回"""
        return {
            "skill_name": self.skill_name,
            "passed": self.passed,
            "declared": sorted(self.declared_permissions),
            "detected": sorted(self.detected_permissions.keys()),
            "issues": len(self.findings),
            "errors": [
                {"rule_id": f.rule_id, "permission": f.permission, "message": f.message}
                for f in self.findings if f.severity == "🔴"
            ],
            "warnings": [
                {"rule_id": f.rule_id, "permission": f.permission, "message": f.message}
                for f in self.findings if f.severity == "🟡"
            ],
        }


# ── AST 分析器 ──────────────────────────────────────


class _PermissionAnalyzer(ast.NodeVisitor):
    """AST 访问器，收集代码中使用的权限相关调用"""

    def __init__(self):
        self.imports: set[str] = set()  # import 的模块名
        self.aliases: dict[str, str] = {}  # alias → 原始模块名 例: pd → pandas
        self.calls: list[tuple[str, int]] = []  # (完整调用名, 行号)
        self.attribute_accesses: list[tuple[str, int]] = []  # (属性链, 行号)
        self.file_writes: list[tuple[str, int, str]] = []  # (文件名/模式, 行号, 完整调用)
        self.current_module = ""

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.name
            self.imports.add(name)
            if alias.asname:
                self.aliases[alias.asname] = name
            # 记录子模块（如 os.path → os）
            parts = name.split(".")
            if parts[0] != name:
                self.imports.add(parts[0])

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            self.imports.add(node.module)
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                self.imports.add(full)
                if alias.asname:
                    self.aliases[alias.asname] = full
                else:
                    self.aliases[alias.name] = full

    def visit_Call(self, node: ast.Call):
        """检测函数调用"""
        call_str = _get_call_str(node.func)
        if call_str:
            self.calls.append((call_str, node.lineno or 0))

        # 检测 open() 的参数是否可能是跨目录路径
        if call_str == "open" or call_str.endswith(".open"):
            self._check_open_args(node)

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        """检测 ctx.xxx 属性访问"""
        attr_chain = _get_attr_chain(node)
        if attr_chain and attr_chain.startswith("ctx."):
            self.attribute_accesses.append((attr_chain, node.lineno or 0))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript):
        """检测 os.environ 等下标访问"""
        # os.environ.get("VAR") 在子进程环境下属于正常操作，不触发文件系统
        self.generic_visit(node)

    def _check_open_args(self, node: ast.Call):
        """分析 open() 的参数，判断是否涉及跨目录操作"""
        if not node.args:
            return
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            path = first_arg.value
            # 相对路径不触发（如 open("data.json")）
            if path.startswith("/") or path.startswith("..") or "~" in path:
                self.file_writes.append((path, node.lineno or 0, f"open({path!r})"))


def _get_call_str(node: ast.AST) -> Optional[str]:
    """将 AST 调用节点转为字符串表示，如 a.b.c() → 'a.b.c'"""
    if isinstance(node, ast.Attribute):
        base = _get_call_str(node.value)
        if base:
            return f"{base}.{node.attr}"
        return None
    elif isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Call):
        return _get_call_str(node.func)
    return None


def _get_attr_chain(node: ast.AST) -> Optional[str]:
    """将 AST 属性访问节点转为字符串链，如 ctx.llm.chat"""
    if isinstance(node, ast.Attribute):
        base = _get_attr_chain(node.value)
        if base:
            return f"{base}.{node.attr}"
        return node.attr
    elif isinstance(node, ast.Name):
        return node.id
    return None


# ── 扫描引擎 ──────────────────────────────────────


def _match_imports(imports: set[str], permission: str) -> list[str]:
    """检测导入的模块是否匹配某权限的 imports 模式"""
    rules = _PERMISSION_RULES.get(permission, {})
    required_imports = rules.get("imports", set())
    if not required_imports:
        return []

    evidence = []
    for imp in imports:
        for req in required_imports:
            if imp == req or imp.startswith(req + "."):
                evidence.append(f"import {imp}")
    return evidence


def _match_calls(
    calls: list[tuple[str, int]],
    permission: str,
    aliases: dict[str, str],
) -> list[tuple[str, int]]:
    """检测函数调用是否匹配某权限的 calls 模式

    处理别名：如果用户 import requests as req，则 req.get → requests.get
    """
    rules = _PERMISSION_RULES.get(permission, {})
    required_calls = rules.get("calls", set())
    if not required_calls:
        return []

    evidence: list[tuple[str, int]] = []
    for call, line in calls:
        # 尝试解析别名
        resolved = _resolve_alias(call, aliases)

        # 匹配精确调用
        if resolved in required_calls or call in required_calls:
            evidence.append((call if call == resolved else f"{call} ({resolved})", line))
            continue

        # 匹配前缀（如 subprocess.run → 匹配 subprocess.*）
        for req in required_calls:
            if req.endswith(".*") and resolved.startswith(req[:-1]):
                evidence.append((call, line))
                break
            # 模糊匹配：ctx.llm.xxx → 匹配 ctx.llm
            if resolved.startswith(req) and len(resolved) > len(req):
                evidence.append((call, line))
                break

    return evidence


def _match_patterns(
    source: str,
    permission: str,
) -> list[tuple[str, int]]:
    """正则表达式补充检测（覆盖 AST 无法捕捉的模式）"""
    rules = _PERMISSION_RULES.get(permission, {})
    patterns = rules.get("patterns", [])
    if not patterns:
        return []

    evidence: list[tuple[str, int]] = []
    for pattern in patterns:
        for match in re.finditer(pattern, source):
            line = source[:match.start()].count("\n") + 1
            evidence.append((match.group(), line))
    return evidence


def _resolve_alias(call: str, aliases: dict[str, str]) -> str:
    """将别名解析为原始模块名

    Example:
        aliases = {"pd": "pandas", "req": "requests"}
        _resolve_alias("req.get", aliases) → "requests.get"
    """
    parts = call.split(".")
    if not parts:
        return call
    first = parts[0]
    if first in aliases:
        rest = ".".join(parts[1:])
        if rest:
            return f"{aliases[first]}.{rest}"
        return aliases[first]
    return call


# ── 主扫描函数 ──────────────────────────────────────


def scan_file(file_path: str) -> SASTReport:
    """扫描单个 Skill 文件，返回 SAST 报告

    Args:
        file_path: skill.json 的路径（同一目录下的 .py 文件会被自动扫描）

    Returns:
        SASTReport 对象
    """
    skill_dir = os.path.dirname(os.path.abspath(file_path))
    return scan_directory(skill_dir)


def scan_directory(skill_dir: str) -> SASTReport:
    """扫描整个 Skill 目录

    Args:
        skill_dir: 包含 skill.json 和 .py 文件的 Skill 目录

    Returns:
        SASTReport 对象
    """
    skill_dir = os.path.abspath(skill_dir)
    report = SASTReport()
    report.skill_path = skill_dir

    # ── 1. 读取 skill.json ──
    manifest_path = os.path.join(skill_dir, "skill.json")
    if not os.path.isfile(manifest_path):
        report.findings.append(SASTFinding(
            rule_id="SAST-000",
            severity="🔴",
            permission="",
            message=f"未找到 skill.json: {manifest_path}",
        ))
        report.passed = False
        return report

    with open(manifest_path, "r", encoding="utf-8") as f:
        try:
            manifest = json.load(f)
        except json.JSONDecodeError as e:
            report.findings.append(SASTFinding(
                rule_id="SAST-000",
                severity="🔴",
                permission="",
                message=f"skill.json 解析失败: {e}",
            ))
            report.passed = False
            return report

    report.skill_name = manifest.get("name", "")
    report.declared_permissions = manifest.get("permissions", [])
    declared_set = set(report.declared_permissions)

    # ── 2. 扫描所有 .py 文件 ──
    all_imports: set[str] = set()
    all_aliases: dict[str, str] = {}  # P2 (R11): 聚合各文件的 alias → 原始模块名
    all_calls: list[tuple[str, int]] = []
    all_attrs: list[tuple[str, int]] = []
    all_source = ""

    py_files = sorted([
        os.path.join(skill_dir, f)
        for f in os.listdir(skill_dir)
        if f.endswith(".py")
    ])

    if not py_files:
        report.findings.append(SASTFinding(
            rule_id="SAST-000",
            severity="🔴",
            permission="",
            message="未找到 .py 文件",
        ))
        report.passed = False
        return report

    # B6：限制扫描文件数，防 OOM
    if len(py_files) > MAX_SCAN_FILES:
        logger.warning(
            "SAST scan truncated: %d .py files found, limit is %d",
            len(py_files), MAX_SCAN_FILES,
        )
        report.findings.append(SASTFinding(
            rule_id="SAST-099",
            severity="🟡",
            permission="",
            message=f"扫描截断：{len(py_files)} 个 .py 文件超过限制 {MAX_SCAN_FILES}",
        ))
        py_files = py_files[:MAX_SCAN_FILES]

    for py_file in py_files:
        rel_path = os.path.relpath(py_file, skill_dir)
        with open(py_file, "r", encoding="utf-8") as f:
            source = f.read()
        all_source += f"\n# {rel_path}\n" + source

        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError as e:
            report.findings.append(SASTFinding(
                rule_id="SAST-000",
                severity="🔴",
                permission="",
                message=f"{rel_path}: 语法错误 — {e}",
                line=e.lineno or 0,
            ))
            report.passed = False
            continue

        analyzer = _PermissionAnalyzer()
        analyzer.visit(tree)
        all_imports.update(analyzer.imports)
        all_aliases.update(analyzer.aliases)  # P2 (R11)
        all_calls.extend((c, ln) for c, ln in analyzer.calls)
        all_attrs.extend((a, ln) for a, ln in analyzer.attribute_accesses)

        # 将属性访问也加入 calls（ct.llm → 当作 call 处理）
        for attr, ln in analyzer.attribute_accesses:
            all_calls.append((attr, ln))

    # ── 3. 检测每个权限的使用情况 ──
    detected: dict[str, list[str]] = {}

    for perm in sorted(_PERMISSION_RULES.keys()):
        rules = _PERMISSION_RULES[perm]
        evidence: list[str] = []

        # 方法 1: 检测 import
        for imp in sorted(all_imports):
            for req in sorted(rules.get("imports", set())):
                if imp == req or imp.startswith(req + "."):
                    evidence.append(f"import {imp}")

        # 方法 2: 检测调用
        # P2 (R11): 原传空 dict → 别名不解析（import requests as req; req.get 漏报）。
        # 现传聚合后的真实 alias 表；确无 alias（空表）时 _resolve_alias 原样返回，
        # 属正确行为而非漏报。
        for call, line in all_calls:
            resolved = _resolve_alias(call, all_aliases)
            for req in rules.get("calls", set()):
                if resolved == req or resolved.startswith(req + "."):
                    evidence.append(f"L{line}: {call}")
                    break
                # ctx 属性访问：ctx.llm → 匹配 ctx.llm
                if resolved.startswith(req) or call.startswith(req):
                    evidence.append(f"L{line}: {call}")
                    break

        # 方法 3: 正则匹配（AST 兜底）
        for pattern in rules.get("patterns", []):
            for match in re.finditer(pattern, all_source):
                line = all_source[:match.start()].count("\n") + 1
                ctx = all_source[:match.start()].rfind("\n")
                snippet = all_source[ctx + 1: ctx + 1 + 60].strip()
                evidence.append(f"L{line}: {snippet}")

        if evidence:
            detected[perm] = evidence[:10]  # 最多 10 条证据

    report.detected_permissions = detected

    # ── 4. 生成 findings ──

    for perm, evidence_list in sorted(detected.items()):
        rules = _PERMISSION_RULES[perm]
        if perm not in declared_set:
            # 代码用了但没声明 → 🔴 error（新 Skill）或 🟡 warning（现有 Skill）
            # 默认按新 Skill 严格处理，调用方可降级
            report.findings.append(SASTFinding(
                rule_id=_rule_id_for(perm, "missing"),
                severity="🔴",
                permission=perm,
                message=(
                    f"代码使用了「{perm}」权限（{rules['description']}），"
                    f"但 skill.json 未声明。"
                    f"请在 permissions 中添加「{perm}」"
                ),
                code="\n".join(evidence_list[:3]),
            ))
        else:
            # 声明且使用了 → ✅ 无问题
            report.findings.append(SASTFinding(
                rule_id=_rule_id_for(perm, "match"),
                severity="ℹ️",
                permission=perm,
                message=f"权限「{perm}」已声明，代码中检测到使用",
                code="\n".join(evidence_list[:1]),
            ))

    # SAST-006: 声明了但未检测到使用 → 🟡 警告
    for perm in sorted(declared_set):
        if perm not in _PERMISSION_RULES:
            # 未知权限声明 → 🟡 但不阻塞
            report.findings.append(SASTFinding(
                rule_id="SAST-006",
                severity="🟡",
                permission=perm,
                message=f"声明了未知权限「{perm}」，不在白名单中",
            ))
        elif perm not in detected:
            report.findings.append(SASTFinding(
                rule_id="SAST-006",
                severity="🟡",
                permission=perm,
                message=f"权限「{perm}」已声明但代码中未检测到使用（过度声明）",
            ))

    # ── 5. 判定是否通过 ──
    # 🔴 = 未通过（新 Skill），🟡 = 警告不阻塞
    has_errors = any(f.severity == "🔴" for f in report.findings)
    report.passed = not has_errors

    return report


def _rule_id_for(perm: str, issue: str) -> str:
    """根据权限和问题类型生成 SAST 规则 ID"""
    mapping = {
        ("network", "missing"): "SAST-001",
        ("network:outbound", "missing"): "SAST-001",
        ("system", "missing"): "SAST-002",
        ("filesystem", "missing"): "SAST-003",
        ("llm", "missing"): "SAST-004",
        ("skills", "missing"): "SAST-005",
        ("identity", "missing"): "SAST-007",
        ("lifecycle", "missing"): "SAST-008",
    }
    return mapping.get((perm, issue), "SAST-099")


# ── CLI 入口 ────────────────────────────────────────


def _print_safe(*args, **kwargs):
    """安全打印（处理 Windows GBK 终端无法输出 emoji 的问题）"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        # 替换无法编码的 Unicode 字符
        text = text.replace("🔴", "[ERR]").replace("🟡", "[WARN]").replace("ℹ️", "[INFO]")
        text = text.replace("✅", "[OK]").replace("❌", "[FAIL]")
        print(text, **kwargs)


def cli_scan(path: str) -> int:
    """CLI 调用入口

    Args:
        path: skill.json 路径或含 skill.json 的目录路径

    Returns:
        0 = 通过, 1 = 有错误
    """
    path = os.path.abspath(path)
    if os.path.isfile(path) and path.endswith(".json"):
        report = scan_file(path)
    elif os.path.isdir(path):
        report = scan_directory(path)
    else:
        _print_safe("[SAST] 无效路径:", path)
        return 1

    # 输出报告
    _print_safe(f"\n{'='*60}")
    _print_safe(" SAST 权限一致性检查报告")
    _print_safe(f"{'='*60}")
    _print_safe(f" Skill: {report.skill_name or '未知'}")
    _print_safe(f" 路径:  {report.skill_path}")
    _print_safe(f" 声明:  {report.declared_permissions or '[]'}")
    _print_safe(f" 检测:  {sorted(report.detected_permissions.keys()) or '无'}")

    status_icon = "通过" if report.passed else "未通过"
    _print_safe(f" 状态:  {status_icon}")
    _print_safe("")

    for finding in report.findings:
        if finding.severity == "ℹ️":
            continue  # 匹配的权限不显示，减少噪音
        line_str = f" L{finding.line}" if finding.line else ""
        _print_safe(f"  [{finding.rule_id}]{line_str} {finding.message}")
        if finding.code:
            for code_line in finding.code.split("\n")[:2]:
                _print_safe(f"         {code_line}")

    error_count = sum(1 for f in report.findings if f.severity == "🔴")
    warning_count = sum(1 for f in report.findings if f.severity == "🟡")
    _print_safe(f"\n{'='*60}")
    _print_safe(f" Summary: {error_count} errors, {warning_count} warnings")
    _print_safe(f"{'='*60}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    sys.exit(cli_scan(path))
