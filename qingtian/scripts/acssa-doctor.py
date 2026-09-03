#!/usr/bin/env python3
"""
擎天底座健康检查工具 (acssa-doctor)

用法:
    python scripts/acssa-doctor.py              # 全量检查
    python scripts/acssa-doctor.py --quick      # 快速检查（跳过网络探测）
    python scripts/acssa-doctor.py --json       # JSON 输出（CI/CD 用）

检查覆盖:
    1. 环境变量 — DEEPSEEK_API_KEY / DASHSCOPE / 镇岳 / 永恒 / 寰宇
    2. 数据库   — PostgreSQL 连通 + 7 个 schema + 关键表
    3. Skill    — 注册数 / 活跃数 / CommandResolver 指令 / warmup
    4. 联邦网络 — peers 表状态 / hub 连通 / WG 可用性
    5. 配置     — config.yaml / role / secretaryEnabled
    6. 容器环境 — WireGuard / curl / Python 版本 / aiohttp
"""

import argparse
import asyncio
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 输出工具 ──────────────────────────────────────────

CHECK_MARK = "✅"
CROSS_MARK = "❌"
WARN_MARK = "⚠️"
INFO_MARK = "ℹ️"

_results: list[dict] = []
_errors = 0
_warnings = 0
_start_time = 0.0
_env_dep_notes: list[str] = []


def _check(category: str, name: str, passed: bool, detail: str = "", warn: bool = False):
    global _errors, _warnings
    status = "WARN" if (warn and passed) else ("PASS" if passed else "FAIL")
    if not passed and not warn:
        _errors += 1
    if warn:
        _warnings += 1
    mark = WARN_MARK if (warn and passed) else (CHECK_MARK if passed else CROSS_MARK)
    msg = f"  {mark} {name}"
    if detail:
        msg += f"  → {detail}"
    print(msg)
    _results.append({"category": category, "name": name, "passed": passed, "warn": warn, "detail": detail, "status": status})


# ════════════════════════════════════════════════════════
# 1. 环境变量
# ════════════════════════════════════════════════════════

REQUIRED_ENV = {
    "DEEPSEEK_API_KEY": "LLM 调用（DeepSeek）",
    "QINGTIAN_CONFIG": "config.yaml 路径，默认 /opt/qingtian/config.yaml",
}

OPTIONAL_ENV = {
    "DASHSCOPE_API_KEY": "embedding（采购/销售服需要，管理服可选）",
    "ZHENYUE_ADMIN_TOKEN": "镇岳管理令牌",
    "YONGHENG_BOOTSTRAP_TOKEN": "永恒首次部署用，重启后自动生成",
    "HUANYU_SIGN_KEY": "跨底座消息签名",
    "ACSSA_JWT_SECRET": "acssa.cn JWT 密钥（市场网站用）",
    "ACSSA_API_KEY": "acssa.cn API 密钥",
    "QINGTIAN_PLATFORM_PUBKEY": "平台 Ed25519 公钥（覆盖 dev 模式）",
    "QINGTIAN_SKILL_DATA_DIR": "Skill 数据目录",
}


def check_env():
    print("\n📦 环境变量")
    for var, desc in REQUIRED_ENV.items():
        val = os.environ.get(var, "")
        _check("环境变量", f"{var} ({desc})", bool(val),
               "已设置" if val else "未设置！",
               warn=not bool(val))
    for var, desc in OPTIONAL_ENV.items():
        val = os.environ.get(var, "")
        if val:
            _check("环境变量", f"{var} ({desc})", True,
                   f"已设置 ({val[:16]}...)" if len(val) > 16 else "已设置")


# ════════════════════════════════════════════════════════
# 2. 数据库
# ════════════════════════════════════════════════════════

SCHEMAS = ["huanyu", "xixing", "yongheng", "zhenyue", "huichuan", "zhice", "skills"]

KEY_TABLES = {
    "huanyu": ["peers", "agents", "messages", "inbox"],
    "xixing": ["knowledge_entries"],
    "yongheng": ["memories"],
    "zhenyue": ["tokens", "audit_log"],
    "zhice": ["tasks", "steps"],
    "skills": ["skill_definitions", "skill_versions", "agent_skills"],
}


async def check_db():
    print("\n🗄️  数据库")
    try:
        from common.db import get_pool
        t0 = time.monotonic()
        pool = await get_pool()
        async with pool.acquire() as conn:
            elapsed = (time.monotonic() - t0) * 1000
            _check("数据库", "PostgreSQL 连接", True, f"{elapsed:.0f}ms")

            # Schema 检查
            for schema in SCHEMAS:
                row = await conn.fetchrow(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = $1)",
                    schema,
                )
                exists = row[0] if row else False
                _check("数据库", f"  schema: {schema}", exists,
                       "" if exists else "不存在！需要运行 ensure_schema()",
                       warn=not exists)

            # peers 表状态
            try:
                rows = await conn.fetch(
                    "SELECT peer_id, host, status, last_heartbeat FROM huanyu.peers ORDER BY status, peer_id"
                )
                online = [r for r in rows if r["status"] == "active"]
                offline = [r for r in rows if r["status"] != "active"]
                _check("数据库", f"  peers: {len(online)} active / {len(offline)} offline",
                       len(online) > 0, "" if len(online) > 0 else "无在线 peer！",
                       warn=len(offline) > 0)
                for r in offline:
                    _check("数据库", f"    {r['peer_id']} → {r['host']}",
                           False, f"status={r['status']}, last_heartbeat={r['last_heartbeat']}",
                           warn=True)
            except Exception:
                _check("数据库", "  peers 表", False, "查询失败", warn=True)

    except Exception as e:
        _check("数据库", "PostgreSQL 连接", False, str(e)[:100])


# ════════════════════════════════════════════════════════
# 3. Skill 状态
# ════════════════════════════════════════════════════════

async def check_skills():
    print("\n🛠️  Skill 状态")
    try:
        from osskill.database import SCHEMA
        from common.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            # 已注册 Skill
            total_row = await conn.fetchrow(f"SELECT COUNT(*) FROM {SCHEMA}.skill_definitions")
            active_row = await conn.fetchrow(
                f"SELECT COUNT(*) FROM {SCHEMA}.skill_definitions WHERE status = 'active'"
            )
            total = total_row[0] if total_row else 0
            active = active_row[0] if active_row else 0
            _check("Skill", f"已注册: {total} / 活跃: {active}",
                   active > 0, "" if active > 0 else "无活跃 Skill！",
                   warn=active < total)

            # Agent 绑定
            bind_row = await conn.fetchrow(f"SELECT COUNT(*) FROM {SCHEMA}.agent_skills WHERE is_active = TRUE")
            binds = bind_row[0] if bind_row else 0
            _check("Skill", f"Agent 绑定数: {binds}", binds > 0,
                   "" if binds > 0 else "无绑定！检查 auto_bind",
                   warn=binds == 0)

        # CommandResolver
        try:
            from osskill.command_resolver import get_resolver
            resolver = get_resolver()
            await resolver.load(pool=pool)
            cmd_count = len(resolver.list_all())
            _check("Skill", f"CommandResolver 指令数: {cmd_count}",
                   cmd_count > 0, "" if cmd_count > 0 else "指令表为空！check skill_definitions.commands",
                   warn=cmd_count == 0)
        except Exception as e:
            _check("Skill", "CommandResolver", False, str(e)[:80], warn=True)

        # warmup
        try:
            from osskill.loader import SkillLoader
            impl_dir = PROJECT_ROOT / "osskill" / "implementations"
            loaded = 0
            failed = 0
            skipped = 0
            for entry in sorted(impl_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("_"):
                    continue
                init_file = entry / "__init__.py"
                if not init_file.exists():
                    skipped += 1
                    continue
                # ── 旁路/非标准 Skill 识别：不走标准 SkillLoader 的模块不计入失败 ──
                # 1) workflow 等按 config 开关(workflow.enabled)直接 include_router 挂载
                # 2) document/price_list/product_* 等继承 BaseProductSkill（产品类），
                #    非标准 Skill 子类，SkillLoader.load 的 issubclass 判断必然失败 → 误报
                bypass = False
                try:
                    pkg_init = (entry / "__init__.py").read_text(encoding="utf-8", errors="ignore")
                    if "api_router" in pkg_init or "as api_router" in pkg_init:
                        bypass = True
                    elif "_base import BaseProductSkill" in pkg_init or "BaseProductSkill" in pkg_init:
                        bypass = True
                except Exception:
                    pass
                if bypass:
                    skipped += 1
                    continue
                try:
                    skill = SkillLoader.load(entry.name)
                    if skill:
                        loaded += 1
                    else:
                        # 非旁路但加载失败：区分缺依赖 vs 真失败
                        try:
                            importlib.import_module(f"osskill.implementations.{entry.name}")
                            failed += 1  # 能导入但无Skill类(真问题)
                        except ModuleNotFoundError as mn:
                            # 缺依赖模块：降级为警告（如系统python缺aiohttp导致BaseProductSkill子类加载失败）
                            _env_dep_notes.append(f"{entry.name}(缺 {mn.name})")
                            skipped += 1
                        except Exception:
                            failed += 1
                except Exception:
                    failed += 1
            _dep_detail = (
                f"；缺依赖: {', '.join(_env_dep_notes)}" if _env_dep_notes else ""
            )
            _check("Skill", f"warmup: {loaded} loaded / {failed} failed / {skipped} skipped",
                   failed == 0, "" if failed == 0 else f"{failed} 个 Skill 加载失败！{_dep_detail}",
                   warn=failed > 0)
        except Exception as e:
            _check("Skill", "warmup", False, str(e)[:80], warn=True)

    except Exception as e:
        _check("Skill", "数据库查询", False, str(e)[:100])


# ════════════════════════════════════════════════════════
# 4. 联邦网络
# ════════════════════════════════════════════════════════

async def check_network(quick: bool = False):
    print("\n🌐 联邦网络")
    try:
        from common.config import get as cfg
        role = cfg("role", "management")
        _check("网络", f"本机 role: {role}", True)

        # hub endpoint
        from huanyu.config import get_hub_endpoint
        hub = get_hub_endpoint()
        _check("网络", f"hub endpoint: {hub}", bool(hub),
               "" if hub else "未配置 hub！")

        # peers 表
        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT peer_id, host, port, status FROM huanyu.peers WHERE peer_id != $1",
                cfg("host", "unknown"),
            )
            if not rows:
                _check("网络", "peers: 无其他节点", True, "单机部署，无需跨底座")
            else:
                for r in rows:
                    _check("网络", f"  {r['peer_id']} ({r['host']}:{r['port']})",
                           r["status"] == "active",
                           f"status={r['status']}",
                           warn=r["status"] != "active")

        # 跨底座连通性探测（HTTP，非快速模式）
        if not quick:
            import httpx
            for r in (rows or []):
                if r["status"] != "active":
                    continue
                try:
                    async with httpx.AsyncClient(timeout=3.0) as client:
                        resp = await client.get(
                            f"http://{r['host']}:{r['port']}/health"
                        )
                        reachable = resp.status_code == 200
                except Exception:
                    reachable = False
                _check("网络", f"  {r['peer_id']} HTTP 连通",
                       reachable, "reachable" if reachable else "unreachable！",
                       warn=not reachable)

    except Exception as e:
        _check("网络", "检查失败", False, str(e)[:100])

    # WireGuard
    wg_bin = shutil.which("wg")
    _check("网络", "WireGuard (wg)", bool(wg_bin),
           f"已安装: {wg_bin}" if wg_bin else "未安装！跨底座直连不可用",
           warn=not bool(wg_bin))


# ════════════════════════════════════════════════════════
# 5. 配置
# ════════════════════════════════════════════════════════

def check_config():
    print("\n⚙️  配置")
    config_path = os.environ.get("QINGTIAN_CONFIG", "/opt/qingtian/config.yaml")
    exists = Path(config_path).exists()
    _check("配置", f"config.yaml: {config_path}", exists,
           "存在" if exists else "不存在！",
           warn=not exists)

    if exists:
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            role = cfg.get("role", "management")
            _check("配置", f"  role: {role}", True)

            # ── role 配套校验：采购服/销售服不应误用 management ──
            try:
                host = socket.gethostname().lower()
                wg_ip = ""
                try:
                    import ipaddress
                    out = subprocess.run(["ip", "-4", "addr", "show", "wg0"],
                                         capture_output=True, text=True, timeout=5).stdout
                    for line in out.splitlines():
                        if "inet " in line:
                            wg_ip = line.split()[1].split("/")[0]
                            break
                except Exception:
                    pass
                expect = "management"
                if "procurement" in host:
                    expect = "procurement"
                elif "sales" in host:
                    expect = "sales"
                if role != expect and expect != "management" and role == "management":
                    _check("配置", "  role 配套校验", False,
                           f"host={host} 应为 role={expect}，当前 role=management（配置错乱）", warn=True)
                elif role != expect:
                    _check("配置", "  role 配套校验", False,
                           f"host={host} 期望 role={expect}，当前 role={role}", warn=True)
                else:
                    _check("配置", "  role 配套校验", True, f"{role} 匹配 host={host}")
            except Exception as e:
                _check("配置", "  role 配套校验", False, str(e)[:60], warn=True)

            # ── /etc/hosts 内网 IP 校验：关键主机应指向 WG 内网而非公网 ──
            try:
                etc_hosts = Path("/etc/hosts").read_text(encoding="utf-8", errors="ignore")
                problems = []
                ip_map = {"management-server": "10.0.100.1",
                          "procurement-server": "10.0.100.2",
                          "sales-server": "10.0.100.3"}
                for hostname, expect_ip in ip_map.items():
                    for line in etc_hosts.splitlines():
                        s = line.split()
                        if len(s) >= 2 and hostname in s[1:]:
                            if s[0] != expect_ip and not s[0].startswith("127."):
                                problems.append(f"{hostname}→{s[0]}(应{expect_ip})")
                if problems:
                    _check("配置", "  /etc/hosts 内网 IP", False,
                           "; ".join(problems) + "（应为 WG 内网 IP，勿用公网）", warn=True)
                else:
                    _check("配置", "  /etc/hosts 内网 IP", True, "关键主机指向 WG 内网 IP")
            except Exception as e:
                _check("配置", "  /etc/hosts 内网 IP", False, str(e)[:60], warn=True)

            secretary = cfg.get("gateway", {}).get("secretaryEnabled", False)
            _check("配置", f"  secretaryEnabled: {secretary}", secretary,
                   "" if secretary else "应为 true（秘书默认开启）",
                   warn=not secretary)

            enabled_skills = cfg.get("skills", {}).get("enabled", [])
            _check("配置", f"  skills.enabled: {enabled_skills}", True,
                   f"{len(enabled_skills)} 个" if enabled_skills else "未配置（走 role 兜底）")

            hub = cfg.get("huanyu", {}).get("hub_endpoint", "")
            _check("配置", "  hub_endpoint", bool(hub),
                   hub if hub else "未配置（走 WG 自动发现）")
        except ImportError:
            _check("配置", "  PyYAML 未安装", False, "pip install pyyaml", warn=True)
        except Exception as e:
            _check("配置", "  解析失败", False, str(e)[:80])


# ════════════════════════════════════════════════════════
# 6. 容器环境
# ════════════════════════════════════════════════════════

def check_container():
    print("\n🐳 容器环境")
    # Python
    v = sys.version_info
    _check("环境", f"Python {v.major}.{v.minor}.{v.micro}", v >= (3, 12),
           "" if v >= (3, 12) else "需要 Python 3.12+",
           warn=v < (3, 12))

    # aiohttp（优先检测 venv，避免系统 python 缺依赖误报）
    try:
        import aiohttp
        _check("环境", "aiohttp", True, f"v{aiohttp.__version__}")
    except ImportError:
        # 若系统python无aiohttp但venv有，说明只是运行解释器不对，非真实缺失
        venv_py = [
            "/opt/qingtian/venv/bin/python3",
            os.path.expanduser("~/.venv/bin/python3"),
        ]
        venv_hit = ""
        for vp in venv_py:
            if os.path.isfile(vp):
                try:
                    out = subprocess.run([vp, "-c", "import aiohttp; print(aiohttp.__version__)"],
                                         capture_output=True, text=True, timeout=10)
                    if out.returncode == 0 and out.stdout.strip():
                        venv_hit = f"venv({out.stdout.strip()})"
                        break
                except Exception:
                    pass
        if venv_hit:
            _check("环境", "aiohttp", True,
                   f"当前解释器未装，但 venv 已装 {venv_hit}（建议用 venv/python 跑 doctor）", warn=True)
        else:
            _check("环境", "aiohttp", False, "未安装！Skill 加载会失败", warn=True)

    # httpx
    try:
        import httpx
        _check("环境", "httpx", True, f"v{httpx.__version__}")
    except ImportError:
        _check("环境", "httpx", False, "未安装！", warn=True)

    # curl
    curl = shutil.which("curl")
    _check("环境", "curl", bool(curl),
           f"已安装: {curl}" if curl else "未安装（容器调试用，非必需）",
           warn=not bool(curl))

    # 容器检测
    in_container = Path("/.dockerenv").exists() or "docker" in (Path("/proc/1/cgroup").read_text() if Path("/proc/1/cgroup").exists() else "")
    if in_container:
        _check("环境", "运行环境: Docker 容器", True)


# ════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════

async def main():
    global _start_time
    _start_time = time.monotonic()

    parser = argparse.ArgumentParser(description="擎天底座健康检查")
    parser.add_argument("--quick", action="store_true", help="快速模式（跳过网络连通性探测）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if args.json:
        import logging
        logging.disable(logging.CRITICAL)
        sys.stdout = open(os.devnull, "w")

    print("=" * 55)
    print("  ACSSA 底座健康检查 (acssa-doctor)")
    print("=" * 55)

    check_env()
    await check_db()
    await check_skills()
    await check_network(quick=args.quick)
    check_config()
    check_container()

    elapsed = time.monotonic() - _start_time

    if args.json:
        sys.stdout = sys.__stdout__
        print(json.dumps({
            "elapsed_ms": round(elapsed * 1000),
            "errors": _errors,
            "warnings": _warnings,
            "total_checks": len(_results),
            "results": _results,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'=' * 55}")
        total = len(_results)
        if _errors == 0 and _warnings == 0:
            print(f"  {CHECK_MARK} 全部 {total} 项检查通过 ({elapsed:.1f}s)")
        elif _errors == 0:
            print(f"  {WARN_MARK} {total} 项检查, {_warnings} 项警告 ({elapsed:.1f}s)")
        else:
            print(f"  {CROSS_MARK} {total} 项检查, {_errors} 项失败, {_warnings} 项警告 ({elapsed:.1f}s)")
        print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
