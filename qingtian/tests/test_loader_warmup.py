"""loader.py warmup_skills 双形态识别测试。

破军 2026-08-10：warmup 硬套 {name}.{name} 约定，把嵌入式常驻型 Skill
（workflow，入口 skill.py，类不继承 Skill）误报 failed。修复后按 skill.json
entry 识别 embedded 型；模板/演示目录（examples/portal）跳过不计 failed。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from osskill.loader import SkillLoader, _classify_skill_dir

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_manifest(dir_path: Path, name: str, entry_file: str, entry_class: str) -> None:
    """写一个最小 skill.json（仅含 entry 声明）。"""
    data = {"name": name, "entry": {"file": entry_file, "class": entry_class}}
    (dir_path / "skill.json").write_text(json.dumps(data), encoding="utf-8")


def _no_skill_loader_load(monkeypatch):
    """让 SkillLoader.load 恒返回 None（模拟 {name}.{name} 形态匹配不上）。"""
    monkeypatch.setattr(SkillLoader, "load", lambda name, quiet=False: None)


class TestClassifyAgentSkill:
    def test_agent_skill_loaded(self, monkeypatch):
        """Agent 绑定型：SkillLoader.load 命中 → loaded。"""
        fake_cls = type("DummySkill", (object,), {})
        monkeypatch.setattr(SkillLoader, "load", lambda name, quiet=False: fake_cls)
        assert _classify_skill_dir("/tmp/scan", "bidding", "skills") == "loaded"


class TestClassifyEmbedded:
    def test_embedded_manifest_loaded(self, tmp_path, monkeypatch):
        """嵌入式常驻型：无 {name}.py 形态，但 skill.json 声明 entry → loaded。"""
        _no_skill_loader_load(monkeypatch)
        skill_dir = tmp_path / "workflow"
        skill_dir.mkdir()
        _write_manifest(skill_dir, "workflow", "skill.py", "WorkflowSkill")
        fake_mod = MagicMock()
        fake_mod.WorkflowSkill = object
        with patch("osskill.loader.importlib.import_module", return_value=fake_mod) as mi:
            assert _classify_skill_dir(str(tmp_path), "workflow", "skills") == "loaded"
        # 按 {import_base}.{entry}.{module_name} 构造入口模块
        mi.assert_called_once_with("skills.workflow.skill")

    def test_embedded_entry_import_fail(self, tmp_path, monkeypatch):
        """entry 模块导入失败 → failed。"""
        _no_skill_loader_load(monkeypatch)
        skill_dir = tmp_path / "workflow"
        skill_dir.mkdir()
        _write_manifest(skill_dir, "workflow", "skill.py", "WorkflowSkill")
        with patch("osskill.loader.importlib.import_module", side_effect=ImportError("boom")):
            assert _classify_skill_dir(str(tmp_path), "workflow", "skills") == "failed"

    def test_embedded_class_missing(self, tmp_path, monkeypatch):
        """entry 模块可导入但声明类不存在 → failed。"""
        _no_skill_loader_load(monkeypatch)
        skill_dir = tmp_path / "workflow"
        skill_dir.mkdir()
        _write_manifest(skill_dir, "workflow", "skill.py", "WorkflowSkill")
        fake_mod = MagicMock()
        del fake_mod.WorkflowSkill  # 类不存在
        with patch("osskill.loader.importlib.import_module", return_value=fake_mod):
            assert _classify_skill_dir(str(tmp_path), "workflow", "skills") == "failed"


class TestClassifySkip:
    def test_manifest_no_entry_skipped(self, tmp_path, monkeypatch):
        """skill.json 存在但无 entry.file/class → 非 Skill 目录，跳过。"""
        _no_skill_loader_load(monkeypatch)
        skill_dir = tmp_path / "examples"
        skill_dir.mkdir()
        (skill_dir / "skill.json").write_text(json.dumps({"name": "examples"}), encoding="utf-8")
        assert _classify_skill_dir(str(tmp_path), "examples", "skills") == "skipped"

    def test_no_manifest_skipped(self, tmp_path, monkeypatch):
        """既无 {name}.py 也无 skill.json → 跳过（examples/portal 场景）。"""
        _no_skill_loader_load(monkeypatch)
        assert _classify_skill_dir(str(tmp_path), "portal", "skills") == "skipped"


class TestWarmupRealTree:
    """对真实 skills/ 目录做集成校验（脚本路径下运行，仓库根已入 sys.path）。"""

    @classmethod
    def setup_class(cls):
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))

    def test_real_workflow_classified_loaded(self):
        """真实 skills/workflow/ → loaded（嵌入式常驻型，不再误报 failed）。"""
        assert _classify_skill_dir(str(_REPO_ROOT / "skills"), "workflow", "skills") == "loaded"

    def test_real_agent_skills_classified_loaded(self):
        """真实 Agent 绑定型 skill 全部 loaded。"""
        for name in ("bidding", "procurement", "sales", "work_secretary"):
            assert _classify_skill_dir(str(_REPO_ROOT / "skills"), name, "skills") == "loaded", name

    def test_real_impl_skills_classified_loaded(self):
        """真实 osskill/implementations/ 商业 skill（csv_analyzer 等）loaded。"""
        impl = str(_REPO_ROOT / "opensource" / "qingtian" / "osskill" / "implementations")
        for name in ("csv_analyzer", "document", "pdf_generator", "word_generator"):
            assert _classify_skill_dir(impl, name, "osskill.implementations") == "loaded", name
