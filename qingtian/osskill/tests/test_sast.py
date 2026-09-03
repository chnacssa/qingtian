"""SAST 权限一致性检查 — 测试"""

import os
import sys
import json
import tempfile
import pytest

# 确保能找到 osskill 包
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osskill.sast import (
    scan_directory,
    scan_file,
    cli_scan,
    _match_imports,
    _match_calls,
    _resolve_alias,
    _PermissionAnalyzer,
    SASTFinding,
    SASTReport,
)
from osskill.scan_pipeline import run_submission_scan, save_scan_result


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
TEST_SKILL_DIR = os.path.join(FIXTURES_DIR, "sast_test_skill")


# ── 单元测试 ─────────────────────────────────────


class TestUtilityFunctions:
    """测试工具函数"""

    def test_resolve_alias_no_alias(self):
        assert _resolve_alias("requests.get", {}) == "requests.get"

    def test_resolve_alias_with_alias(self):
        aliases = {"req": "requests"}
        assert _resolve_alias("req.get", aliases) == "requests.get"

    def test_resolve_alias_nested(self):
        aliases = {"pd": "pandas"}
        assert _resolve_alias("pd.DataFrame", aliases) == "pandas.DataFrame"

    def test_resolve_alias_self(self):
        aliases = {"np": "numpy"}
        assert _resolve_alias("np", aliases) == "numpy"

    def test_match_imports_exact(self):
        result = _match_imports({"requests", "os", "json"}, "network")
        assert any("requests" in r for r in result)

    def test_match_imports_submodule(self):
        result = _match_imports({"requests.auth"}, "network")
        assert any("requests.auth" in r for r in result)

    def test_match_imports_no_match(self):
        result = _match_imports({"os", "json"}, "network")
        assert result == []

    def test_match_calls_exact(self):
        calls = [("requests.get", 10), ("os.system", 20)]
        result = _match_calls(calls, "network", {})
        assert len(result) >= 1
        assert result[0][0] == "requests.get"

    def test_match_calls_no_match(self):
        calls = [("json.dumps", 10)]
        result = _match_calls(calls, "network", {})
        assert result == []


class TestPermissionAnalyzer:
    """测试 AST 分析器"""

    def test_detect_import(self):
        code = "import requests\nimport os"
        tree = __import__("ast").parse(code)
        analyzer = _PermissionAnalyzer()
        analyzer.visit(tree)
        assert "requests" in analyzer.imports
        assert "os" in analyzer.imports

    def test_detect_import_from(self):
        code = "from requests import get, post"
        tree = __import__("ast").parse(code)
        analyzer = _PermissionAnalyzer()
        analyzer.visit(tree)
        assert "requests" in analyzer.imports
        assert "requests.get" in analyzer.imports

    def test_detect_call(self):
        code = "requests.get('http://example.com')"
        tree = __import__("ast").parse(code)
        analyzer = _PermissionAnalyzer()
        analyzer.visit(tree)
        assert ("requests.get", 1) in analyzer.calls

    def test_detect_ctx_attribute(self):
        code = """async def f():
    result = await ctx.llm.chat([{}])
"""
        tree = __import__("ast").parse(code)
        analyzer = _PermissionAnalyzer()
        analyzer.visit(tree)
        attrs = [a for a, _ in analyzer.attribute_accesses]
        assert any("ctx.llm" in a for a in attrs)

    def test_detect_open_absolute_path(self):
        code = 'with open("/etc/passwd", "r") as f: pass'
        tree = __import__("ast").parse(code)
        analyzer = _PermissionAnalyzer()
        analyzer.visit(tree)
        assert len(analyzer.file_writes) > 0

    def test_detect_open_relative_path_skip(self):
        code = 'with open("data.json", "r") as f: pass'
        tree = __import__("ast").parse(code)
        analyzer = _PermissionAnalyzer()
        analyzer.visit(tree)
        # 相对路径不触发跨目录检测
        has_relative = False
        for path, _, _ in analyzer.file_writes:
            if path == "data.json":
                has_relative = True
        # 不触发
        assert not has_relative


# ── 集成测试 ─────────────────────────────────────


class TestScanDirectory:
    """测试目录扫描"""

    def test_scan_test_skill(self):
        """sast_test_skill 声明了 network+llm，代码还用了 filesystem+system+skills"""
        report = scan_directory(TEST_SKILL_DIR)

        assert report.skill_name == "sast_test_skill"
        assert "network" in report.declared_permissions
        assert "llm" in report.declared_permissions

        # 应检测到 filesystem / system / skills 等额外权限在代码中使用
        detected = report.detected_permissions
        assert "filesystem" in detected, f"应检测到 filesystem 使用，实际: {detected}"
        assert "system" in detected, f"应检测到 system 使用，实际: {detected}"
        assert "skills" in detected, f"应检测到 skills 使用，实际: {detected}"
        assert "network" in detected, f"应检测到 network 使用，实际: {detected}"
        assert "llm" in detected, f"应检测到 llm 使用，实际: {detected}"

        # 应生成 🔴 error（未声明但使用了）
        errors = [f for f in report.findings if f.severity == "🔴"]
        assert len(errors) >= 2, f"应有至少 2 个错误（filesystem+system）, 实际: {[e.permission for e in errors]}"
        error_perms = {e.permission for e in errors}
        assert "filesystem" in error_perms or "system" in error_perms

        # network 和 llm 已声明 → 应该是 ℹ️ 级别
        info_findings = [f for f in report.findings
                         if f.severity == "ℹ️" and f.permission in ("network", "llm")]
        assert len(info_findings) >= 2

        # 整体未通过
        assert not report.passed

    def test_scan_nonexistent_directory(self):
        """不存在目录应返回错误"""
        report = scan_directory("/nonexistent/path")
        assert not report.passed

    def test_scan_no_py_files(self):
        """空目录（无 .py 文件）应失败"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建 skill.json 但没有 .py 文件
            with open(os.path.join(tmpdir, "skill.json"), "w") as f:
                json.dump({"name": "empty_skill", "permissions": []}, f)
            report = scan_directory(tmpdir)
            assert not report.passed


class TestScanPipeline:
    """测试扫描流水线"""

    @pytest.mark.asyncio
    async def test_submission_scan(self):
        """完整的提交扫描流程"""
        result = await run_submission_scan(TEST_SKILL_DIR)

        assert not result.passed  # 因为权限不一致
        assert len(result.errors) > 0
        assert "SAST" in result.errors[0] or result.errors[0]

        api_dict = result.to_api_dict()
        assert "passed" in api_dict
        assert "sast" in api_dict
        assert api_dict["sast"]["skill_name"] == "sast_test_skill"

    @pytest.mark.asyncio
    async def test_save_and_load_result(self):
        """保存和加载扫描结果"""
        result = await run_submission_scan(TEST_SKILL_DIR)
        path = save_scan_result("sast_test_skill", result)
        assert os.path.isfile(path)

        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["sast"]["skill_name"] == "sast_test_skill"
        assert loaded["passed"] is False

        os.remove(path)


# ── CLI 测试 ─────────────────────────────────────


class TestCLI:
    """测试 CLI 入口"""

    def test_cli_with_directory(self):
        """CLI 扫描目录"""
        exit_code = cli_scan(TEST_SKILL_DIR)
        assert exit_code == 1  # 有错误

    def test_cli_with_skill_json(self):
        """CLI 扫描 skill.json 文件"""
        skill_json = os.path.join(TEST_SKILL_DIR, "skill.json")
        exit_code = cli_scan(skill_json)
        assert exit_code == 1  # 有错误

    def test_cli_invalid_path(self):
        """无效路径返回 1"""
        exit_code = cli_scan("/nonexistent")
        assert exit_code == 1


# ── 边缘场景测试 ─────────────────────────────────


class TestEdgeCases:
    """边缘场景"""

    def test_empty_permissions(self):
        """permissions: [] 的 Skill，代码无外部调用 → 应通过"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "skill.json"), "w") as f:
                json.dump({"name": "l1_skill", "permissions": []}, f)
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write('"""L1 Skill pure compute"""\n')
                f.write('from osskill.models import Skill\n')
                f.write('class L1Skill(Skill):\n')
                f.write('    async def execute(self, params):\n')
                f.write('        return {"ok": True}\n')

            report = scan_directory(tmpdir)
            errors = [f for f in report.findings if f.severity == "🔴"]
            assert len(errors) == 0

    def test_ctx_only_logger(self):
        """仅使用 ctx.logger（L1）→ 不需声明任何权限"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "skill.json"), "w") as f:
                json.dump({"name": "logger_skill", "permissions": []}, f)
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write('"""Only L1 capability"""\n')
                f.write('from osskill.models import Skill\n')
                f.write('class LoggerSkill(Skill):\n')
                f.write('    async def execute(self, params):\n')
                f.write('        self.ctx.logger.info("hello")\n')
                f.write('        return {"ok": True}\n')

            report = scan_directory(tmpdir)
            errors = [f for f in report.findings if f.severity == "🔴"]
            assert len(errors) == 0

    def test_import_alias_detection(self):
        """别名导入应正确解析"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "skill.json"), "w") as f:
                json.dump({"name": "alias_skill", "permissions": []}, f)
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("import requests as req\n")
                f.write('req.get("http://example.com")\n')

            report = scan_directory(tmpdir)
            assert "network" in report.detected_permissions

    def test_subprocess_variants(self):
        """subprocess 多种调用方式检测"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "skill.json"), "w") as f:
                json.dump({"name": "sub_skill", "permissions": []}, f)
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("import subprocess\n")
                f.write('subprocess.Popen(["ls"])\n')
                f.write('subprocess.check_call(["echo", "hi"])\n')

            report = scan_directory(tmpdir)
            assert "system" in report.detected_permissions
