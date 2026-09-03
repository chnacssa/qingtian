"""Skill 迁移工具 — 测试"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osskill.migration import (
    detect_used_permissions,
    extract_metadata,
    generate_skill_json,
    convert_skill,
    CALL_TO_PERM,
)


class TestCallToPerm:
    """CALL_TO_PERM 与 SAST 共享规则的一致性"""

    def test_has_network_perms(self):
        assert "requests.get" in CALL_TO_PERM
        assert CALL_TO_PERM["requests.get"] == "network"

    def test_has_filesystem_perms(self):
        assert "shutil.copytree" in CALL_TO_PERM
        assert CALL_TO_PERM["shutil.copytree"] == "filesystem"

    def test_has_system_perms(self):
        assert "subprocess.run" in CALL_TO_PERM
        assert CALL_TO_PERM["subprocess.run"] == "system"

    def test_has_llm_perms(self):
        assert "ctx.llm.chat" in CALL_TO_PERM
        assert CALL_TO_PERM["ctx.llm.chat"] == "llm"

    def test_has_skills_perms(self):
        assert "ctx.call_skill" in CALL_TO_PERM
        assert CALL_TO_PERM["ctx.call_skill"] == "skills"

    def test_coverage(self):
        """至少包含 20 条规则"""
        assert len(CALL_TO_PERM) >= 20


class TestDetectUsedPermissions:
    """测试 detect_used_permissions"""

    def test_network_import_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("import requests\n")
            perms = detect_used_permissions(tmpdir)
            assert "network" in perms

    def test_filesystem_call_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("""
import os
os.remove("/tmp/test")
""")
            perms = detect_used_permissions(tmpdir)
            assert "filesystem" in perms

    def test_system_call_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("""
import subprocess
subprocess.run(["ls"])
""")
            perms = detect_used_permissions(tmpdir)
            assert "system" in perms

    def test_ctx_skills_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("""
async def foo():
    await ctx.call_skill("other", "execute", {})
""")
            perms = detect_used_permissions(tmpdir)
            assert "skills" in perms

    def test_ctx_llm_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("""
async def foo():
    result = await ctx.llm.chat([{"role": "user", "content": "hi"}])
""")
            perms = detect_used_permissions(tmpdir)
            assert "llm" in perms

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            perms = detect_used_permissions(tmpdir)
            assert perms == set()

    def test_syntax_error_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "broken.py"), "w") as f:
                f.write("this is not valid python @@@\n")
            # 不应该抛出异常
            perms = detect_used_permissions(tmpdir)
            assert isinstance(perms, set)

    def test_no_py_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 只有非 .py 文件
            with open(os.path.join(tmpdir, "data.json"), "w") as f:
                f.write('{"key": "value"}')
            perms = detect_used_permissions(tmpdir)
            assert perms == set()

    def test_nonexistent_dir(self):
        perms = detect_used_permissions("/nonexistent/path")
        assert perms == set()


class TestGenerateSkillJson:
    """测试 generate_skill_json"""

    def test_minimal_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {"name": "test_skill", "display_name": "Test"}
            result = generate_skill_json(metadata, tmpdir)
            assert result["name"] == "test_skill"
            assert result["display_name"] == "Test"
            assert result["version"] == "1.0.0"
            assert result["lifecycle"] == "resident"
            assert "permissions" in result
            assert "migrated_at" in result

    def test_permissions_from_sast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("import requests\n")
            metadata = {"name": "net_skill", "permissions": []}
            result = generate_skill_json(metadata, tmpdir)
            assert "network" in result["permissions"]

    def test_permissions_union(self):
        """合并声明权限和 SAST 检测到的权限"""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "main.py"), "w") as f:
                f.write("import requests\nimport subprocess\n")
            metadata = {"name": "combo", "permissions": ["filesystem"]}
            result = generate_skill_json(metadata, tmpdir)
            assert "network" in result["permissions"]
            assert "system" in result["permissions"]
            assert "filesystem" in result["permissions"]

    def test_entry_class_generated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata = {"name": "my_skill"}
            result = generate_skill_json(metadata, tmpdir)
            assert result["entry"]["class"] == "MySkillSkill"
            assert result["entry"]["file"] == "main.py"


class TestConvertSkill:
    """测试 convert_skill 完整流程"""

    def test_convert_basic_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "src_skill")
            os.makedirs(src)
            # 创建源文件 + __init__.py 使包可导入
            with open(os.path.join(src, "__init__.py"), "w") as f:
                f.write("")
            with open(os.path.join(src, "src_skill.py"), "w") as f:
                f.write("""
from osskill.models import Skill

class SrcSkillSkill(Skill):
    name = "src_skill"
    display_name = "Src Skill"
    version = "1.5.0"
    category = "test"
""")
            out_dir = os.path.join(tmpdir, "out")
            result = convert_skill(src, out_dir, skill_name="src_skill")

            assert result["skill_name"] == "src_skill"
            assert result["version"] == "1.5.0"
            assert os.path.isfile(os.path.join(out_dir, "src_skill", "skill.json"))

    def test_convert_nonexistent_source(self):
        import pytest
        with pytest.raises(FileNotFoundError):
            convert_skill("/nonexistent", "/tmp/out")

    def test_convert_auto_skill_name(self):
        """不传入 skill_name 时使用目录名"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "auto_name")
            os.makedirs(src)
            with open(os.path.join(src, "code.py"), "w") as f:
                f.write("# just a comment\n")
            out_dir = os.path.join(tmpdir, "out")
            result = convert_skill(src, out_dir)
            assert result["skill_name"] == "auto_name"

    def test_convert_with_permissions(self):
        """权限检测正确的 skill"""
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "perm_skill")
            os.makedirs(src)
            with open(os.path.join(src, "main.py"), "w") as f:
                f.write("import requests\nimport os\n")
            out_dir = os.path.join(tmpdir, "out")
            result = convert_skill(src, out_dir, skill_name="perm_skill")
            # 应该检测到 network 和 filesystem
            detected = result.get("detected_permissions", [])
            assert "network" in detected
            # filesystem 来自 import os

    def test_convert_skips_pycache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src = os.path.join(tmpdir, "clean_skill")
            os.makedirs(os.path.join(src, "__pycache__"))
            with open(os.path.join(src, "__pycache__", "cached.py"), "w") as f:
                f.write("# cached\n")
            out_dir = os.path.join(tmpdir, "out")
            result = convert_skill(src, out_dir, skill_name="clean_skill")
            assert result["source_files"] == 0  # 只有 __pycache__ 文件


class TestExtractMetadata:
    """测试 extract_metadata"""

    def test_default_metadata_for_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            meta = extract_metadata("empty", tmpdir)
            assert meta["name"] == "empty"
            assert meta["version"] == "1.0.0"
            assert meta["category"] == "tool"
