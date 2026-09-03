"""Skill 管理 CLI — 离线吊销、恢复操作

用法:
    python -m osskill.cli revocation import <path>    # 导入黑板名单文件
    python -m osskill.cli blacklist list               # 列出黑板名单
    python -m osskill.cli blacklist check <name>       # 检查 Skill 是否在黑名单中
    python -m osskill.cli restore <name>               # 从快照恢复 Skill
"""

import argparse
import asyncio
import json
import logging
import os
import sys

from .database import SCHEMA

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("osskill.cli")


def _get_data_dir() -> str:
    """获取数据目录"""
    return os.environ.get("QINGTIAN_SKILL_DATA_DIR", "/opt/qingtian/skills/data")


def _get_acssa_base_url() -> str:
    return os.environ.get("QINGTIAN_MGMT_URL", "https://ain.acssa.cn")


def _get_api_key() -> str:
    return os.environ.get("QINGTIAN_SKILL_API_KEY", "")


async def _cmd_revocation_import(path: str):
    """导入黑板名单文件"""
    from osskill.market_integration import RevocationManager

    mgr = RevocationManager()
    count = mgr.import_file(path)
    if count > 0:
        print(f"✓ 成功导入 {count} 条黑板名单")
    else:
        print("! 未导入任何条目（文件为空或格式错误）")


async def _cmd_blacklist_list():
    """列出所有黑板名单"""
    from osskill.market_integration import RevocationManager

    mgr = RevocationManager()
    entries = mgr.get_blacklisted()
    if not entries:
        print("黑板名单为空")
        return

    print(f"黑板名单 ({len(entries)} 条):")
    print("-" * 60)
    for name, entry in entries.items():
        reason = entry.get("reason", "unknown")
        revoked_at = entry.get("revoked_at", 0)
        print(f"  {name}: {reason} (revoked_at={revoked_at})")


async def _cmd_blacklist_check(skill_name: str):
    """检查 Skill 是否在黑名单中"""
    from osskill.market_integration import RevocationManager

    mgr = RevocationManager()
    if mgr.is_blacklisted(skill_name):
        entry = mgr.get_blacklist_entry(skill_name)
        print(f"✗ {skill_name} 在黑名单中")
        print(f"  原因: {entry.get('reason', 'unknown')}")
        print(f"  时间: {entry.get('revoked_at', 0)}")
    else:
        print(f"✓ {skill_name} 不在黑名单中")


async def _cmd_restore(skill_name: str):
    """从备份恢复 Skill"""
    from .backup import list_backups, restore_backup

    # 查找最新的包含此 Skill 的备份
    backups = list_backups()
    target_backup = None
    for b in reversed(backups):
        skills = b.get("contents", {}).get("skills", [])
        if skill_name in skills:
            target_backup = b
            break

    if target_backup is None:
        print(f"✗ 未找到包含 '{skill_name}' 的备份")
        sys.exit(1)

    backup_name = target_backup.get("backup_name", "")
    from .backup import BACKUP_DIR
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    if not os.path.isfile(backup_path):
        print(f"✗ 备份文件不存在: {backup_path}")
        sys.exit(1)

    # 从备份恢复
    from common.config import get as cfg_get
    data_dir = cfg_get("skill.data_dir", "/opt/qingtian/skills/data")
    summary = restore_backup(backup_path, skill_data_dir=data_dir)

    print(f"✓ Skill '{skill_name}' 已从备份恢复")
    print(f"  备份: {backup_name}")
    print(f"  恢复的 Skills: {summary.get('restored_skills', [])}")
    print(f"  恢复的 Licenses: {summary.get('restored_licenses', [])}")
    print(f"  使用: python -m osskill.cli install {skill_name} <package_path> 重新部署")


async def _cmd_install(skill_name: str, package_path: str):
    """从包目录安装 Skill"""
    from .runtime_service import RuntimeService
    from .loader import SkillRegistry

    registry = SkillRegistry()
    service = RuntimeService(skill_registry=registry)

    try:
        result = await service.install(
            skill_name=skill_name,
            package_path=package_path,
            agent_id=os.environ.get("QINGTIAN_AGENT_ID", ""),
        )
        print(f"✓ Skill '{skill_name}' 安装成功")
        print(f"  版本: {result.get('version', '?')}")
        print(f"  状态: {result.get('status', '?')}")
    except Exception as e:
        print(f"✗ 安装失败: {e}")
        sys.exit(1)


async def _cmd_rollback(skill_name: str):
    """回滚 Skill 到上一个快照版本"""
    from .runtime_service import RuntimeService
    from .loader import SkillRegistry

    # 查找最新快照
    snapshots = await get_rollback_snapshots_by_name(skill_name)
    if not snapshots:
        print(f"✗ 未找到 '{skill_name}' 的回滚快照")
        sys.exit(1)

    latest = snapshots[0]
    snapshot_path = latest.get("snapshot_path", "")
    version_to = latest.get("version_to", "?")

    if not os.path.isfile(snapshot_path):
        print(f"✗ 快照文件不存在: {snapshot_path}")
        sys.exit(1)

    # 从快照恢复包文件
    import tempfile
    import tarfile
    import shutil

    from common.config import get as cfg_get
    pkg_dir = cfg_get("skill.package_dir", "/opt/qingtian/skills/packages")

    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(snapshot_path, "r:gz") as tar:
            tar.extractall(tmpdir, filter="data")

        # 复制到包目录
        skill_pkg_dir = os.path.join(pkg_dir, skill_name)
        if os.path.exists(skill_pkg_dir):
            shutil.rmtree(skill_pkg_dir)
        shutil.copytree(os.path.join(tmpdir, "skill"), skill_pkg_dir)

    # 重新安装
    registry = SkillRegistry()
    service = RuntimeService(skill_registry=registry)

    try:
        await service.uninstall(skill_name=skill_name, purge_data=False)
        result = await service.install(
            skill_name=skill_name,
            package_path=skill_pkg_dir,
            agent_id=os.environ.get("QINGTIAN_AGENT_ID", ""),
            version=version_to,
        )
        print(f"✓ Skill '{skill_name}' 已回滚到 v{version_to}")
        print(f"  状态: {result.get('status', '?')}")
    except Exception as e:
        print(f"✗ 回滚失败: {e}")
        sys.exit(1)


async def get_rollback_snapshots_by_name(skill_name: str) -> list[dict]:
    """按 Skill 名称查询回滚快照"""
    try:
        from common.db import get_pool

        pool = await get_pool()
        rows = await pool.fetch(
            f"""SELECT rs.id, rs.version_from, rs.version_to,
                      rs.snapshot_path, rs.snapshot_sha256,
                      rs.reason, rs.created_at
               FROM {SCHEMA}.rollback_snapshots rs
               JOIN {SCHEMA}.skill_definitions sd ON sd.id = rs.skill_id
               WHERE sd.name = $1
               ORDER BY rs.created_at DESC""",
            skill_name,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query rollback snapshots failed for %s: %s", skill_name, e)
        return []


async def _cmd_deps_check(skill_name: str):
    """检查 Skill 的依赖是否满足

    P2 (R11)：原实现只 add 单节点且无依赖 → 空图 validate 恒返回 [] →
    "依赖检查通过"恒成立，从未真正校验任何东西。
    修复：读取已安装包 manifest 的 dependencies.skills 声明，构建覆盖
    目标 + 全部（含传递）依赖节点的有向图，做 DAG 循环检测 + 依赖/版本校验；
    无依赖声明时明确提示"无依赖可校验"，不再假装通过。
    """
    from .deps import DependencyGraph
    from common.config import get as cfg_get

    # 1. 读取已安装包的 manifest 依赖声明（skill.json dependencies.skills）
    pkg_dir = cfg_get("skill.package_dir", "")
    manifest_path = os.path.join(pkg_dir, skill_name, "skill.json") if pkg_dir else ""
    declared_deps: dict[str, str] = {}
    if manifest_path and os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            declared_deps = dict(manifest.get("dependencies", {}).get("skills", {}) or {})
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("读取 %s 的 skill.json 失败: %s", skill_name, e)

    if not declared_deps:
        # 无依赖声明 → 明确提示，不再输出"依赖检查通过"
        if manifest_path and os.path.isfile(manifest_path):
            print(f"ℹ️ Skill '{skill_name}' 未声明 skills 依赖，无依赖可校验")
        else:
            print(f"ℹ️ Skill '{skill_name}'：未找到已安装包清单 "
                  f"{manifest_path or '(package_dir 未配置)'}，无法读取依赖声明")
        return

    # 2. 已安装版本表（best-effort，DB 不可用仅校验声明完整性）
    installed_versions: dict[str, str] = {}
    try:
        from common.db import get_pool
        pool = await get_pool()
        rows = await pool.fetch(
            f"SELECT name, version FROM {SCHEMA}.skill_definitions WHERE status = 'active'",
        )
        installed_versions = {r["name"]: r["version"] for r in rows}
    except Exception as e:
        logger.warning("查询已安装 Skill 版本失败（仅校验声明完整性）: %s", e)

    def _read_manifest_deps(name: str) -> dict[str, str]:
        """从已安装包 manifest 读取某个 Skill 的传递依赖。"""
        if not pkg_dir:
            return {}
        m_path = os.path.join(pkg_dir, name, "skill.json")
        if not os.path.isfile(m_path):
            return {}
        try:
            with open(m_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            return dict(m.get("dependencies", {}).get("skills", {}) or {})
        except (OSError, json.JSONDecodeError):
            return {}

    def _add_with_deps(name: str, version: str, deps: dict[str, str], depth: int = 0) -> None:
        """递归加入目标及其已安装依赖。

        has_node 判重天然防环，depth 兜底防深递归。
        未安装的依赖不加入节点 → validate() 会按"依赖缺失"报出。
        """
        if depth > 10:
            return
        if not graph.has_node(name):
            graph.add_node(name, version=version or "1.0.0", deps=deps)
        for dep_name in deps:
            if graph.has_node(dep_name) or dep_name not in installed_versions:
                continue  # 已加入 或 未安装（留给 validate 报 missing）
            _add_with_deps(dep_name, installed_versions[dep_name],
                           _read_manifest_deps(dep_name), depth + 1)

    # 3. 构建覆盖多节点的依赖图
    graph = DependencyGraph()
    _add_with_deps(skill_name, installed_versions.get(skill_name, ""), declared_deps)

    # 4. DAG 校验：先查环，再查缺失/版本
    cycle = graph.detect_cycle()
    if cycle:
        print(f"✗ Skill '{skill_name}' 依赖存在循环: {' → '.join(cycle)}")
        return

    errors = graph.validate()
    if errors:
        print(f"✗ Skill '{skill_name}' 依赖检查发现 {len(errors)} 个问题:")
        for err in errors:
            print(f"  - {err}")
    else:
        print(f"✓ Skill '{skill_name}' 依赖检查通过（{len(graph._nodes)} 个节点）")


async def _cmd_migrate(skill_name: str, source_dir: str):
    """迁移工具：分析 Skill 代码，自动建议补充 permissions 声明"""
    import ast
    from pathlib import Path

    src = Path(source_dir)
    if not src.is_dir():
        print(f"✗ 目录不存在: {source_dir}")
        sys.exit(1)

    # 读取 skill.json
    manifest_path = src / "skill.json"
    if not manifest_path.exists():
        print(f"✗ 未找到 skill.json: {manifest_path}")
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = set(manifest.get("permissions", []))

    # SAST 风格扫描：找出代码中使用的 API 调用
    call_to_perm = {
        "requests.get": "network", "requests.post": "network",
        "httpx.Client": "network", "httpx.AsyncClient": "network",
        "aiohttp.ClientSession": "network", "urllib.request": "network",
        "socket.socket": "network:outbound",
        "os.remove": "filesystem", "os.unlink": "filesystem",
        "os.mkdir": "filesystem", "os.makedirs": "filesystem",
        "shutil.rmtree": "filesystem", "shutil.move": "filesystem",
        "shutil.copy": "filesystem",
        "subprocess.run": "system", "subprocess.Popen": "system",
        "subprocess.call": "system", "os.system": "system", "os.popen": "system",
        "pickle.loads": "system", "marshal.loads": "system", "yaml.load": "system",
        "eval": "system", "exec": "system", "compile": "system",
        "open": "filesystem",
    }

    used_perms = set()
    for py_file in src.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                parts = []
                cur = node.func
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                    name = ".".join(reversed(parts))
                    if name in call_to_perm:
                        used_perms.add(call_to_perm[name])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in call_to_perm:
                    used_perms.add(call_to_perm[node.func.id])

    missing = used_perms - declared
    over = declared - used_perms - {"skills", "llm"}

    print(f"\n=== 迁移分析: {skill_name} ===\n")
    print(f"  已声明权限: {', '.join(sorted(declared)) or '(无)'}")
    print(f"  代码检测到: {', '.join(sorted(used_perms)) or '(无)'}")

    if missing:
        print("\n  [MISSING] 缺失权限（建议补充）:")
        for p in sorted(missing):
            print("    + %s" % p)
        print("\n  建议在 skill.json 的 permissions 中添加:")
        suggested = sorted(declared | missing)
        print("    %s" % json.dumps(suggested))
    else:
        print("\n  [OK] 权限声明完整，无需迁移")

    if over:
        print("\n  [EXTRA] 过度声明（可考虑移除）:")
        for p in sorted(over):
            print("    - %s" % p)

    print()


async def _cmd_deps_graph():
    """显示依赖图"""
    from .deps import DependencyGraph

    graph = DependencyGraph()
    cycle = graph.detect_cycle()
    if cycle:
        print(f"! 检测到循环依赖: {' → '.join(cycle)}")
    else:
        order = graph.topo_sort()
        if order:
            print(f"拓扑排序: {' → '.join(order)}")
        else:
            print("依赖图为空")


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(description="Qingtian Skill CLI")
    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # revocation import
    rev_parser = subparsers.add_parser("revocation", help="吊销管理")
    rev_sub = rev_parser.add_subparsers(dest="rev_action")
    rev_import = rev_sub.add_parser("import", help="导入黑板名单文件")
    rev_import.add_argument("path", help="黑板名单 JSON 文件路径")

    # blacklist list/check
    bl_parser = subparsers.add_parser("blacklist", help="黑板名单管理")
    bl_sub = bl_parser.add_subparsers(dest="bl_action")
    bl_sub.add_parser("list", help="列出黑板名单")
    bl_check = bl_sub.add_parser("check", help="检查 Skill 是否在黑名单中")
    bl_check.add_argument("name", help="Skill 名称")

    # restore
    restore_parser = subparsers.add_parser("restore", help="从快照恢复 Skill")
    restore_parser.add_argument("name", help="Skill 名称")

    # install
    install_parser = subparsers.add_parser("install", help="安装 Skill")
    install_parser.add_argument("name", help="Skill 名称")
    install_parser.add_argument("package_path", help="Skill 包目录路径")

    # rollback
    rollback_parser = subparsers.add_parser("rollback", help="回滚 Skill 到上一版本")
    rollback_parser.add_argument("name", help="Skill 名称")

    # migrate
    migrate_parser = subparsers.add_parser("migrate", help="迁移工具：分析代码并建议补充 permissions")
    migrate_parser.add_argument("name", help="Skill 名称")
    migrate_parser.add_argument("source_dir", help="Skill 源码目录路径")

    # deps
    deps_parser = subparsers.add_parser("deps", help="依赖管理")
    deps_sub = deps_parser.add_subparsers(dest="deps_action")
    deps_check = deps_sub.add_parser("check", help="检查依赖")
    deps_check.add_argument("name", help="Skill 名称")
    deps_sub.add_parser("graph", help="显示依赖图")

    args = parser.parse_args()

    if args.command == "revocation":
        if args.rev_action == "import":
            asyncio.run(_cmd_revocation_import(args.path))
        else:
            rev_parser.print_help()
    elif args.command == "blacklist":
        if args.bl_action == "list":
            asyncio.run(_cmd_blacklist_list())
        elif args.bl_action == "check":
            asyncio.run(_cmd_blacklist_check(args.name))
        else:
            bl_parser.print_help()
    elif args.command == "restore":
        asyncio.run(_cmd_restore(args.name))
    elif args.command == "install":
        asyncio.run(_cmd_install(args.name, args.package_path))
    elif args.command == "rollback":
        asyncio.run(_cmd_rollback(args.name))
    elif args.command == "migrate":
        asyncio.run(_cmd_migrate(args.name, args.source_dir))
    elif args.command == "deps":
        if args.deps_action == "check":
            asyncio.run(_cmd_deps_check(args.name))
        elif args.deps_action == "graph":
            asyncio.run(_cmd_deps_graph())
        else:
            deps_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
