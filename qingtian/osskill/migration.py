"""Skill 迁移工具 — 旧格式到新包格式转换

用途：
1. 将吸星自动进化的旧式 Skill（纯 Python 类，无 skill.json）转换为新式包格式
2. 自动分析权限声明（集成 SAST）
3. 批量迁移整个 implementations 目录
4. 数据库 schema 迁移兼容检测

用法:
    python -m osskill.migration analyze <skill_name> [source_dir]
    python -m osskill.migration convert <skill_name> [source_dir] [output_dir]
    python -m osskill.migration batch-convert [implementations_dir] [output_root]
    python -m osskill.migration check-schema
"""

import argparse
import asyncio
import ast
import importlib
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .database import SCHEMA

logger = logging.getLogger("osskill.migration")

# ── 检测规则：从共享模块导入，与 SAST 保持一致 ──────────

from common.permission_rules import CALL_TO_PERM

MAX_SCAN_FILES = 1000
"""单次 SAST / 迁移扫描的最大文件数，超限跳过并告警，防 OOM（🔴 B6）"""


def detect_used_permissions(source_dir: str) -> set[str]:
    """AST 扫描源码目录，检测实际使用的权限

    Args:
        source_dir: Skill 源码目录

    Returns:
        检测到的权限集合
    """
    used: set[str] = set()
    src = Path(source_dir)
    if not src.is_dir():
        return used

    num_scanned = 0
    for py_file in sorted(src.rglob("*.py")):
        if num_scanned >= MAX_SCAN_FILES:
            logger.warning(
                "SAST scan truncated at %d files in %s (MAX_SCAN_FILES limit)",
                MAX_SCAN_FILES, source_dir,
            )
            break
        num_scanned += 1
        try:
            tree = ast.parse(
                py_file.read_text(encoding="utf-8", errors="replace"),
            )
        except SyntaxError as e:
            logger.warning("Syntax error in %s: %s", py_file, e)
            continue

        for node in ast.walk(tree):
            # 属性链调用: obj.attr.attr(...)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                parts = []
                cur = node.func
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                    name = ".".join(reversed(parts))
                    if name in CALL_TO_PERM:
                        used.add(CALL_TO_PERM[name])

            # 直接函数调用: eval(...) 等
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in CALL_TO_PERM:
                    used.add(CALL_TO_PERM[node.func.id])

            # import 检测: import requests → network
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("requests", "httpx", "aiohttp", "urllib",
                                       "socket", "websockets"):
                        used.add("network")
                    elif alias.name in ("subprocess", "pickle", "marshal", "yaml"):
                        used.add("system")
                    elif alias.name in ("os", "shutil", "pathlib"):
                        used.add("filesystem")

            # from import: from requests import post → network
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in ("requests", "httpx", "aiohttp", "urllib",
                              "socket", "websockets"):
                    used.add("network")
                elif module in ("subprocess", "pickle", "marshal", "yaml"):
                    used.add("system")
                elif module in ("os", "shutil", "pathlib"):
                    used.add("filesystem")
                elif module.startswith("osskill") or module.startswith("xihe"):
                    # 框架自身调用不计入
                    pass

    return used


# ── 元数据提取 ──────────────────────────

def extract_metadata(skill_name: str, source_dir: str) -> dict:
    """从 Skill 源码提取元数据（通过 import + 类属性反射）

    Args:
        skill_name: Skill 名称
        source_dir: 源码目录

    Returns:
        元数据字典
    """
    metadata = {
        "name": skill_name,
        "display_name": skill_name,
        "description": "",
        "version": "1.0.0",
        "category": "tool",
        "entry": {"class": _to_pascal(skill_name) + "Skill", "file": "main.py"},
        "permissions": [],
        "resources": {
            "cpu": "low",
            "memory_mb": 128,
            "api_calls_per_minute": 20,
        },
        "dependencies": {
            "qingtian": ">=2.0.0",
            "skills": {},
        },
        "knowledge_deps": [],
        "tool_deps": [],
    }

    # 尝试导入获取类属性
    _added_path = os.path.dirname(source_dir)
    sys.path.insert(0, _added_path)
    try:
        module_name = os.path.basename(source_dir)
        # Try multiple naming patterns
        for try_name in (module_name, skill_name, skill_name.lower()):
            try:
                mod = importlib.import_module(f"{try_name}.{try_name}")
                break
            except (ImportError, ModuleNotFoundError):
                try:
                    mod = importlib.import_module(try_name)
                    break
                except (ImportError, ModuleNotFoundError):
                    continue
        else:
            logger.warning(
                "Could not import skill '%s' from %s, using defaults",
                skill_name, source_dir,
            )
            return metadata

        # 查找 Skill 子类
        from osskill.models import Skill
        skill_cls = None
        for name in dir(mod):
            obj = getattr(mod, name)
            if isinstance(obj, type) and issubclass(obj, Skill) and obj is not Skill:
                skill_cls = obj
                break

        if skill_cls is None:
            logger.warning(
                "No Skill subclass found in %s, using defaults", module_name,
            )
            return metadata

        # 提取类属性
        for attr in ("name", "display_name", "description", "version", "category"):
            val = getattr(skill_cls, attr, None)
            if val:
                metadata[attr] = val

        metadata["entry"]["class"] = skill_cls.__name__

        # 提取依赖
        kd = getattr(skill_cls, "knowledge_deps", None)
        if kd:
            metadata["knowledge_deps"] = kd
        td = getattr(skill_cls, "tool_deps", None)
        if td:
            metadata["tool_deps"] = td

    except Exception as e:
        logger.warning("Failed to extract metadata from '%s': %s", source_dir, e)
    finally:
        # B7: 清理 sys.path，防止永久污染
        try:
            sys.path.remove(_added_path)
        except (ValueError, AttributeError):
            pass

    return metadata


# ── SKill.json 生成 ──────────────────────────

def generate_skill_json(metadata: dict, source_dir: str) -> dict:
    """生成完整的 skill.json 内容

    结合元数据和 SAST 检测结果，生成权限声明。

    Args:
        metadata: 元数据字典
        source_dir: 源码目录（用于 SAST 检测）

    Returns:
        skill.json 内容字典
    """
    # 运行 SAST 检测
    used = detect_used_permissions(source_dir)
    declared = metadata.get("permissions", [])
    combined = sorted(set(declared) | used)

    skill_json = {
        "name": metadata.get("name", "unknown"),
        "display_name": metadata.get("display_name", ""),
        "version": metadata.get("version", "1.0.0"),
        "description": metadata.get("description", ""),
        "category": metadata.get("category", "tool"),
        "entry": metadata.get("entry", {
            "class": _to_pascal(metadata.get("name", "skill")) + "Skill",
            "file": "main.py",
        }),
        "permissions": combined,
        "resources": metadata.get("resources", {
            "cpu": "low",
            "memory_mb": 128,
            "api_calls_per_minute": 20,
        }),
        "dependencies": metadata.get("dependencies", {
            "qingtian": ">=2.0.0",
            "skills": {},
        }),
        "lifecycle": "resident",
        "tags": [],
        "author": {},
        "compliance": {"data_handling": "local"},
        "license_info": {
            "type": "free",
            "retail_price_yuan": 0,
            "wholesale_ratio": 0.30,
            "trial_days": 7,
            "refund_days": 7,
        },
        "certificate": "",
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }

    return skill_json


def _to_pascal(snake: str) -> str:
    """snake_case → PascalCase"""
    return "".join(word.capitalize() for word in snake.split("_"))


# ── 迁移执行 ──────────────────────────

def convert_skill(
    source_dir: str,
    output_dir: str,
    skill_name: str | None = None,
) -> dict:
    """将旧格式 Skill 转换为新包格式

    Args:
        source_dir: 旧格式源码目录
        output_dir: 输出目录
        skill_name: Skill 名称（自动检测时为 None）

    Returns:
        迁移结果摘要
    """
    src = Path(source_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    # 自动检测 skill_name
    if skill_name is None:
        skill_name = src.name

    # 提取元数据
    logger.info("Extracting metadata for '%s' from %s", skill_name, source_dir)
    metadata = extract_metadata(skill_name, source_dir)
    logger.info("  display_name=%s, version=%s", metadata.get("display_name"), metadata.get("version"))

    # 生成 skill.json
    skill_json = generate_skill_json(metadata, source_dir)
    logger.info("  permissions detected: %s", skill_json["permissions"])

    # 创建输出目录
    out = Path(output_dir) / skill_name
    out.mkdir(parents=True, exist_ok=True)
    out_src = out / "src"
    out_src.mkdir(exist_ok=True)

    # 复制源码文件（排除 __pycache__、测试文件、__init__.py）
    copied = 0
    num_copied = 0
    for item in src.rglob("*"):
        if item.name == "__pycache__" or "__pycache__" in item.parts:
            continue
        if num_copied >= MAX_SCAN_FILES:
            logger.warning("Copy truncated at %d files (MAX_SCAN_FILES limit)", MAX_SCAN_FILES)
            break
        if item.is_file() and item.suffix == ".py":
            rel_path = item.relative_to(src)
            # B4: 跳过 __init__.py、测试文件、tests/ 目录
            if item.name == "__init__.py":
                continue
            if any(part.startswith("test") for part in rel_path.parts):
                continue
            dest = out_src / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            copied += 1
            num_copied += 1

    # 写 skill.json
    skill_json_path = out / "skill.json"
    with open(skill_json_path, "w", encoding="utf-8") as f:
        json.dump(skill_json, f, ensure_ascii=False, indent=2)

    logger.info(
        "Converted '%s': %d source files, %d permissions -> %s",
        skill_name, copied, len(metadata.get("permissions", [])), output_dir,
    )

    return {
        "skill_name": skill_name,
        "version": skill_json["version"],
        "source_files": copied,
        "declared_permissions": metadata.get("permissions", []),
        "detected_permissions": list(skill_json["permissions"]),
        "output_path": str(out),
        "skill_json": str(skill_json_path),
    }


def batch_convert(implementations_dir: str, output_root: str) -> list[dict]:
    """批量转换所有旧格式 Skill

    Args:
        implementations_dir: implementations 目录路径
        output_root: 输出根目录

    Returns:
        迁移结果列表
    """
    impl = Path(implementations_dir)
    if not impl.is_dir():
        raise FileNotFoundError(f"Implementations directory not found: {implementations_dir}")

    results = []
    for entry in sorted(impl.iterdir()):
        if entry.is_dir() and entry.name != "__pycache__":
            # 检查是否包含 Python 源码
            py_files = list(entry.rglob("*.py"))
            if not py_files:
                logger.info("Skipping '%s': no Python files", entry.name)
                continue
            try:
                result = convert_skill(str(entry), output_root, skill_name=entry.name)
                results.append(result)
            except Exception as e:
                logger.error("Failed to convert '%s': %s", entry.name, e)
                results.append({
                    "skill_name": entry.name,
                    "error": str(e),
                })

    logger.info(
        "Batch conversion complete: %d/%d succeeded",
        sum(1 for r in results if "error" not in r), len(results),
    )
    return results


def check_database_schema() -> list:
    """检查数据库 schema 是否兼容当前代码

    检查所有需要的表和列是否存在。

    Returns:
        不兼容项列表（空表示全部兼容）
    """
    issues = []

    required_tables = {
        f"{SCHEMA}.skill_definitions": [
            "id", "name", "display_name", "description", "category",
            "status", "version", "permissions", "sast_result",
        ],
        f"{SCHEMA}.skill_packages": [
            "id", "skill_id", "version", "filename", "sha256",
            "storage_path", "status", "installed_at",
        ],
        f"{SCHEMA}.rollback_snapshots": [
            "id", "skill_id", "version_from", "version_to",
            "snapshot_path", "snapshot_sha256",
        ],
        f"{SCHEMA}.agent_skills": [
            "id", "agent_id", "skill_id", "is_active", "pinned_version",
            "license_cert_id",
        ],
    }

    try:
        from common.db import get_pool

        async def _check():
            pool = await get_pool()
            async with pool.acquire() as conn:
                for table, columns in required_tables.items():
                    # 检查表是否存在
                    row = await conn.fetchrow(
                        "SELECT EXISTS (SELECT FROM information_schema.tables "
                        "WHERE table_schema = $1 AND table_name = $2)",
                        table.split(".")[0], table.split(".")[1],
                    )
                    if not row or not row[0]:
                        issues.append(f"Table '{table}' does not exist")
                        continue

                    # 检查列
                    for col in columns:
                        col_row = await conn.fetchrow(
                            "SELECT EXISTS (SELECT FROM information_schema.columns "
                            "WHERE table_schema = $1 AND table_name = $2 "
                            "AND column_name = $3)",
                            table.split(".")[0], table.split(".")[1], col,
                        )
                        if not col_row or not col_row[0]:
                            issues.append(f"Column '{table}.{col}' does not exist")

            return issues

        # 始终尝试 asyncio.run()，有 running loop 时 raise RuntimeError
        # NOTE(B14 P3): 此处保留 asyncio.run 不改为直接 await。
        #   check_database_schema 为同步函数，被同步 CLI main()（check-schema 分支）直接调用；
        #   改成 await 需把整个 CLI 调用链转 async，且已有异步变体 check_database_schema_async。
        #   改为 await 风险大，故保留现状，仅在循环内调用时走 check_database_schema_async。
        issues = asyncio.run(_check())

    except RuntimeError:
        return [{"info": "Async check skipped (event loop running). "
                         "Call check_database_schema_async() directly"}]
    except ImportError:
        issues.append("common.db not available (DB not configured)")
    except Exception as e:
        issues.append(f"Schema check failed: {e}")

    return issues


async def check_database_schema_async() -> list[dict]:
    """异步版本：检查数据库 schema 兼容性"""
    issues = []
    required_tables = [
        "skills.skill_definitions",
        "skills.skill_packages",
        "skills.rollback_snapshots",
        "skills.agent_skills",
        "skills.skill_versions",
        "skills.skill_reviews",
        "skills.skill_usage_stats",
    ]

    try:
        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            for table in required_tables:
                row = await conn.fetchrow(
                    "SELECT EXISTS (SELECT FROM information_schema.tables "
                    "WHERE table_schema = $1 AND table_name = $2)",
                    table.split(".")[0], table.split(".")[1],
                )
                if not row or not row[0]:
                    issues.append({"table": table, "exists": False})
    except Exception as e:
        issues.append({"error": str(e)})

    return issues


# ── CLI ──────────────────────────


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Qingtian Skill 迁移工具",
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # analyze — 分析单个 Skill
    analyze = sub.add_parser("analyze", help="分析 Skill 源码（只检测，不改动）")
    analyze.add_argument("skill_name", help="Skill 名称")
    analyze.add_argument(
        "source_dir", nargs="?",
        default=None,
        help="源码目录（默认: implementations/<name>）",
    )

    # convert — 转换单个 Skill
    conv = sub.add_parser("convert", help="转换旧格式 Skill 为新包格式")
    conv.add_argument("skill_name", help="Skill 名称")
    conv.add_argument(
        "source_dir", nargs="?",
        default=None,
        help="源码目录（默认: implementations/<name>）",
    )
    conv.add_argument(
        "output_dir", nargs="?",
        default=None,
        help="输出目录（默认: ./migrated/）",
    )

    # batch-convert — 批量转换
    batch = sub.add_parser("batch-convert", help="批量转换所有 implementations")
    batch.add_argument(
        "implementations_dir", nargs="?",
        default=None,
        help="implementations 目录（默认: osskill/implementations）",
    )
    batch.add_argument(
        "output_root", nargs="?",
        default=None,
        help="输出根目录（默认: ./migrated/）",
    )

    # check-schema — 检查数据库 schema
    sub.add_parser("check-schema", help="检查数据库 schema 兼容性")

    return parser


def main():
    parser = _make_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    if args.command == "analyze":
        skill_name = args.skill_name
        source_dir = args.source_dir or _default_source_dir(skill_name)
        if not source_dir or not os.path.isdir(source_dir):
            print(f"[ERR] Source directory not found: {source_dir}")
            sys.exit(1)

        print(f"\n=== 分析 '{skill_name}' ===")
        metadata = extract_metadata(skill_name, source_dir)
        print(f"  名称: {metadata.get('display_name', '?')}")
        print(f"  版本: {metadata.get('version', '?')}")
        print(f"  分类: {metadata.get('category', '?')}")

        used = detect_used_permissions(source_dir)
        declared = metadata.get("permissions", [])
        print(f"\n  已声明权限: {', '.join(sorted(declared)) or '(无)'}")
        print(f"  代码检测到: {', '.join(sorted(used)) or '(无)'}")

        missing = used - set(declared)
        if missing:
            print("\n  [MISSING] 缺失权限（建议补充）:")
            for p in sorted(missing):
                print(f"    + {p}")
        else:
            print("\n  [OK] 权限声明完整")

        print()

    elif args.command == "convert":
        skill_name = args.skill_name
        source_dir = args.source_dir or _default_source_dir(skill_name)
        output_dir = args.output_dir or "./migrated"

        if not source_dir or not os.path.isdir(source_dir):
            print("[ERR] Source directory not found: %s" % source_dir)
            sys.exit(1)

        try:
            result = convert_skill(source_dir, output_dir, skill_name)
            print("\n[OK] 转换成功: %s v%s" % (skill_name, result['version']))
            print("  源码: %d 文件 -> %s" % (result['source_files'], result['output_path']))
            print("  声明权限: %s" % result['declared_permissions'])
            print("  检测权限: %s" % result['detected_permissions'])
            print("  skill.json: %s" % result['skill_json'])
            print()
        except Exception as e:
            print("[ERR] 转换失败: %s" % e)
            sys.exit(1)

    elif args.command == "batch-convert":
        impl_dir = args.implementations_dir or _default_impl_dir()
        output_root = args.output_root or "./migrated"

        if not impl_dir or not os.path.isdir(impl_dir):
            print("[ERR] Implementations directory not found: %s" % impl_dir)
            sys.exit(1)

        print("\n=== 批量转换: %s -> %s ===\n" % (impl_dir, output_root))
        results = batch_convert(impl_dir, output_root)

        success = [r for r in results if "error" not in r]
        failed = [r for r in results if "error" in r]
        print("\n完成: %d 成功, %d 失败" % (len(success), len(failed)))
        for r in failed:
            print("  [ERR] %s: %s" % (r['skill_name'], r['error']))
        print()

    elif args.command == "check-schema":
        print("\n=== 数据库 Schema 检查 ===\n")
        issues = check_database_schema()
        if not issues:
            print("[OK] 所有表/列兼容\n")
        else:
            for issue in issues:
                if isinstance(issue, dict):
                    ok = issue.get('exists', True)
                    print("  %s %s" % ("[OK]" if ok else "[ERR]", issue))
                else:
                    print("  [ERR] %s" % issue)
            print()

    else:
        parser.print_help()


def _default_source_dir(skill_name: str) -> str | None:
    """默认源码目录"""
    candidates = [
        os.path.join(os.path.dirname(__file__), "implementations", skill_name),
        os.path.join(os.getcwd(), "implementations", skill_name),
        os.path.join(os.getcwd(), skill_name),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def _default_impl_dir() -> str | None:
    """默认 implementations 目录"""
    candidates = [
        os.path.join(os.path.dirname(__file__), "implementations"),
        os.path.join(os.getcwd(), "implementations"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


if __name__ == "__main__":
    main()
