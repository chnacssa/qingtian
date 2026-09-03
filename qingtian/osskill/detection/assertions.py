"""功能断言引擎 — 验证 Skill 的元数据结构和代码完整性

每个 Skill 可配 skill_test.json 断言文件:
  skills/incoming/<skill_name>/skill_test.json

支持的断言类型:
  - not_error: 标记此用例不报错（占位，等沙箱结果）
  - import_check: 验证 Python 包可导入（如 "openpyxl"）
  - has_code_block: 验证 SKILL.md 有至少 N 个代码块
  - field_not_empty: 验证 skill.json 中某字段非空
  - entry_file_exists: 验证 entry.file 存在
"""

import json
import logging
import os
from pathlib import Path

from .models import AssertionResult, AssertionsResult

logger = logging.getLogger(__name__)


# ── 内置检查 ──────────────────────────────────────────


def _check_import(package: str) -> tuple[bool, str]:
    """检查 Python 包是否可导入"""
    try:
        __import__(package)
        return True, ""
    except ImportError:
        return False, f"包 '{package}' 未安装"


def _count_code_blocks(skill_dir: str) -> int:
    """统计 SKILL.md 中 python 代码块数量"""
    md_path = Path(skill_dir) / "SKILL.md"
    if not md_path.exists():
        return 0
    text = md_path.read_text(encoding="utf-8")
    count = 0
    in_code = False
    for line in text.split("\n"):
        if line.startswith("```"):
            if in_code:
                in_code = False
            else:
                lang = line[3:].strip()
                if lang == "python":
                    in_code = True
                    count += 1
    return count


def _check_field_not_empty(skill_dir: str, field_path: str) -> tuple[bool, str]:
    """检查 skill.json 中某字段非空

    field_path 支持点号路径，如 "author.name"、"source.platform"
    """
    json_path = Path(skill_dir) / "skill.json"
    if not json_path.exists():
        return False, "skill.json 不存在"

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"skill.json 解析失败: {e}"

    parts = field_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, f"字段 '{field_path}' 不存在"

    if current is None or (isinstance(current, str) and not current.strip()):
        return False, f"字段 '{field_path}' 为空"

    return True, ""


def _check_entry_file(skill_dir: str) -> tuple[bool, str]:
    """检查 entry.file 指向的文件是否存在"""
    json_path = Path(skill_dir) / "skill.json"
    if not json_path.exists():
        return False, "skill.json 不存在"

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"skill.json 解析失败: {e}"

    entry = data.get("entry", {})
    if not entry:
        return True, ""  # 无 entry 字段不报错（兼容旧格式）

    entry_file = entry.get("file", "")
    if not entry_file:
        return True, ""  # 无 file 字段不报错（兼容）

    target = Path(skill_dir) / entry_file
    if target.exists():
        return True, ""
    return False, f"entry.file 指向的文件不存在: {entry_file}"


# ── 加载测试用例 ──────────────────────────────────────


def load_test_cases(skill_dir: str) -> list[dict]:
    """加载 Skill 的测试断言"""
    for filename in ("skill_test.json", ".skill_test.json"):
        path = os.path.join(skill_dir, filename)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("cases", [])
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load test cases from %s: %s", path, e)
                return []
    return []


# ── 主入口 ──────────────────────────────────────────────


def run_assertions(skill_dir: str) -> AssertionsResult:
    """运行所有断言"""
    skill_dir = str(Path(skill_dir).resolve())
    results: list[AssertionResult] = []

    # ── 内置结构断言 ──
    skill_json = Path(skill_dir) / "skill.json"

    # skill.json 存在性
    if skill_json.exists():
        results.append(AssertionResult(name="struct:skill.json", passed=True))
        try:
            data = json.loads(skill_json.read_text(encoding="utf-8"))
        except Exception as e:
            results.append(AssertionResult(
                name="struct:parse", passed=False, error=f"JSON 解析失败: {e}",
            ))
            return AssertionsResult(passed=False, cases=results)

        # 必填字段
        for field in ("name", "display_name", "version"):
            if field in data and data[field]:
                results.append(AssertionResult(name=f"field:{field}", passed=True))
            else:
                results.append(AssertionResult(
                    name=f"field:{field}", passed=False, error=f"缺少或为空: {field}",
                ))

        # permissions 合法性
        perms = data.get("permissions", [])
        if isinstance(perms, list) and all(isinstance(p, str) for p in perms):
            results.append(AssertionResult(name="field:permissions", passed=True))
        else:
            results.append(AssertionResult(
                name="field:permissions", passed=False,
                error="permissions 必须是字符串列表",
            ))

        # entry.file 存在性
        ok, err = _check_entry_file(skill_dir)
        results.append(AssertionResult(name="struct:entry_file", passed=ok, error=err))

        # SKILL.md 存在性
        md_path = Path(skill_dir) / "SKILL.md"
        if md_path.exists():
            results.append(AssertionResult(name="struct:SKILL.md", passed=True))
            # 统计代码块
            block_count = _count_code_blocks(skill_dir)
            if block_count >= 1:
                results.append(AssertionResult(
                    name=f"struct:code_blocks", passed=True,
                ))
            else:
                results.append(AssertionResult(
                    name=f"struct:code_blocks", passed=False,
                    error="SKILL.md 中无 Python 代码块",
                ))
        else:
            results.append(AssertionResult(
                name="struct:SKILL.md", passed=False, error="SKILL.md 文件不存在",
            ))
    else:
        results.append(AssertionResult(
            name="struct:skill.json", passed=False, error="skill.json 文件不存在",
        ))
        return AssertionsResult(passed=False, cases=results)

    # ── 自定义断言 ──
    cases = load_test_cases(skill_dir)
    for case in cases:
        name = case.get("name", "unnamed")
        ass = case.get("assert", {})
        errors: list[str] = []

        # import_check: 验证包可导入
        import_target = ass.get("import_check", "")
        if import_target:
            ok, err = _check_import(import_target)
            if not ok:
                errors.append(err)

        # has_code_block: 验证代码块数量
        min_blocks = ass.get("has_code_block", 0)
        if min_blocks:
            actual = _count_code_blocks(skill_dir)
            if actual < min_blocks:
                errors.append(f"代码块数量不足: 需要 {min_blocks}, 实际 {actual}")

        # field_not_empty: 验证字段非空
        field_target = ass.get("field_not_empty", "")
        if field_target:
            ok, err = _check_field_not_empty(skill_dir, field_target)
            if not ok:
                errors.append(err)

        if errors:
            results.append(AssertionResult(
                name=name, passed=False, error="; ".join(errors),
            ))
        else:
            results.append(AssertionResult(name=name, passed=True))

    passed = all(r.passed for r in results)
    return AssertionsResult(passed=passed, cases=results)
