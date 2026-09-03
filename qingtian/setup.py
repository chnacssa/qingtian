#!/usr/bin/env python3
"""
ACSSA 智能体操作系统 — 一键初始化脚本

运行: python3 setup.py

逐项交互式填写配置，自动完成：
  1. 生成随机密钥（master.key / admin token / break_glass token）
  2. 生成 config.yaml
  3. 创建所需目录
  4. 检测 PostgreSQL 连接并创建数据库/扩展
  5. 检测 Redis 连接
  6. 打印 systemd unit 模板
  7. 首次 Schema 初始化 + 测试套件运行
"""

import os
import sys
import secrets
import subprocess
import re
import base64

from pathlib import Path

from common.config import (
    default_llm_model,
    default_llm_base_url,
    default_llm_provider,
    default_llm_key_var,
    default_llm_backup_profile,
)

# 部署目标目录 — 由用户输入确定，默认为脚本所在目录
DEPLOY_DIR = Path(__file__).resolve().parent

# ── 工具函数 ──────────────────────────────────────────

def ask(prompt: str, default: str = "", validate=None, secret: bool = False) -> str:
    """交互式提问，带默认值和校验。"""
    hint = f" [{default}]" if default else ""
    while True:
        try:
            if secret:
                import getpass
                value = getpass.getpass(f"  {prompt}{hint}: ").strip()
            else:
                value = input(f"  {prompt}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消。")
            sys.exit(0)

        if not value and default:
            value = default
        if not value:
            print("  ⚠️  此项必填")
            continue
        if validate:
            ok, msg = validate(value)
            if not ok:
                print(f"  ⚠️  {msg}")
                continue
        return value


def ask_yn(prompt: str, default: bool = True) -> bool:
    yn = "Y/n" if default else "y/N"
    v = input(f"  {prompt} [{yn}]: ").strip().lower()
    if not v:
        return default
    return v in ("y", "yes")


def check_cmd(cmd: str) -> bool:
    """检查命令是否存在。"""
    try:
        subprocess.run(["which", cmd], capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


def _check_port_owner(port: str) -> list[str]:
    """检测端口被哪些非 systemd-redis 进程占用。

    Returns 冲突进程描述列表；无冲突返回空列表。
    """
    owners = []
    try:
        out = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            if f":{port}" not in line:
                continue
            pid = ""
            for part in line.split():
                if "pid=" in part:
                    pid = part.split("pid=")[-1].split(",")[0]
                    break
            if not pid:
                continue
            try:
                p_out = subprocess.run(
                    ["ps", "-p", pid, "-o", "pid,comm,args", "--no-headers"],
                    capture_output=True, text=True, timeout=3,
                ).stdout.strip()
            except Exception:
                p_out = f"PID {pid}"
            # 仅当不是 systemd redis-server 时报告
            if "redis-server" in p_out and ("systemd" in p_out or "/usr/sbin/redis-server" in p_out):
                continue
            owners.append(p_out)
    except Exception:
        pass

    # Docker 容器检测
    try:
        docker_ps = subprocess.run(
            ["docker", "ps", "--filter", f"publish={port}", "--format", "{{.ID}} {{.Image}} {{.Names}}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if docker_ps:
            for line in docker_ps.splitlines():
                if line.strip():
                    owners.append(f"[Docker] {line.strip()}")
    except Exception:
        pass

    return owners


def print_step(n: int, title: str):
    print(f"\n{'─'*50}")
    print(f"  Step {n}: {title}")
    print(f"{'─'*50}")


def print_ok(msg: str):
    print(f"  ✅ {msg}")


def print_warn(msg: str):
    print(f"  ⚠️  {msg}")


# ── 校验函数 ──────────────────────────────────────────

def validate_host(v: str) -> tuple[bool, str]:
    if not re.match(r"^[a-zA-Z0-9\-_.]+$", v):
        return False, "hostname 只能包含字母、数字、-、_、."
    return True, ""


def validate_port(v: str) -> tuple[bool, str]:
    try:
        p = int(v)
        if 1 <= p <= 65535:
            return True, ""
        return False, "端口范围 1-65535"
    except ValueError:
        return False, "必须是数字"


def validate_role(v: str) -> tuple[bool, str]:
    if v in ("management", "worker"):
        return True, ""
    return False, "角色必须是 management（主控）或 worker（部门服务器）"


# ── 生成 YAML ─────────────────────────────────────────

def generate_config(values: dict) -> str:
    """根据填写值生成 config.yaml。"""
    role = values["role"]
    return f"""# ACSSA 智能体操作系统（底座 OS）全局配置
# 由 setup.py 自动生成 — {values["_date"]}
#
# role: company | management | procurement | sales
#   - company: 企业部署（开源默认角色），单底座，跨企业协作经官方 Hub 中转
#   - management: 全局调度 + 全量采集/扫描/蒸馏（内测）
#   - procurement: 仅 Agent 个人进化（内测）
#   - sales: 仅 Agent 个人进化（内测）

role: {role}
host: {values["host"]}
enterprise:
  name: {values.get("enterprise_name", "")}
  short: {values.get("enterprise_short", "")}
  plan: {values.get("plan", "basic")}
  license_key: "{values.get("license_key", "")}"
  agent_limit: {values.get("agent_limit", 0)}
  signup_date: "{values.get("signup_date", "")}"
  admin_email: "{values.get("admin_email", "")}"

# ── 部门 & Agent & LLM 配置 ─────────────────────────
departments:
{values.get("departments_yaml", "  {}")}

# 数据库
database:
  host: {values["db_host"]}
  port: {values["db_port"]}
  db: {values["db_name"]}
  user: {values["db_user"]}
  password: "{values["db_password"]}"

# 服务端口
service:
  port: {values["service_port"]}

# ── 吸星（知识进化）──────────────────────────────────
xixing:
  schema_name: xixing
  global_namespace: global
  base_dir: {values["openclaw_workspace"]}
  timezone: {values["timezone"]}

  collect:
    mode: daily-all
    fetch_timeout: 60
    max_content_size: 524288
    user_agent: "Qingtian-Xixing/3.0"

  quality_gate:
    min_content_length: 200
    min_relevance_score: 0.5
    min_quality_score: 0.4
    fuzzy_dedup_threshold: 0.8
    max_freshness_days: 30

  classifier:
    llm_fallback_threshold: 0.6
    model: "{default_llm_model()}"

  distiller:
    enabled: true
    model: "{default_llm_model()}"
    max_source_memories: 500
    min_cluster_size: 3

  scanner:
    enabled: true
    top_n: 10

  xizhenji:
    auto_capture: true
    min_severity_for_llm: "high"

  scheduler:
    enabled: true

# ── 寰宇（通信目录）──────────────────────────────────
huanyu:
  schema_name: huanyu
  sign_key: "{values['huanyu_sign_key']}"
  max_counters: 5
  peer_id: "{values['host']}"
  peer_name: "{values['host']}"
  peer_port: {values["service_port"]}
  redis_url: "{values["redis_url"]}"
  release_manifest: "{values['deploy_dir']}/releases/manifest.json"

# ── 永恒（记忆检索）──────────────────────────────────
yongheng:
  schema_name: yongheng
  embedding:
    provider: fastembed
    model_name: "BAAI/bge-small-zh-v1.5"
    cache_path: "{values["models_dir"]}"
    threads: {values["embed_threads"]}
    dimension: 512
  llm:
    provider: {default_llm_provider()}
    base_url: "{default_llm_base_url()}"
    api_key: "${{{default_llm_key_var()}}}"
    high_value_model: "{default_llm_model()}"
    digest_model: "{default_llm_model()}"
    agentic_model: "{default_llm_model()}"
  consolidate:
    token_budget: 20000
    max_records_per_run: 2000
    min_days_between: 7
  rate_limit:
    write: 60
    search: 120
    context: 60
    session_start: 60
    session_end: 30
  batch:
    max_size: 20
  search:
    rrf_k: 60
    default_top_k: 5
    context_default_top_k: 10
    time_decay:
      recent_days: 30
      medium_days: 90
      recent_weight: 1.0
      medium_weight: 0.5
    hit_exemption:
      min_hits: 5
      max_bonus: 0.1
      reset_after_days: 180
  learned:
    max_items_soft: 50
    min_confidence: 0.5
    duplicate_threshold: 5

# ── 镇岳（安全审计）──────────────────────────────────
zhenyue:
  schema_name: zhenyue
  encryption:
    key_dir: "{values["keys_dir"]}"
    master_key_file: "master.key"
  auth:
    bootstrap_admin_token: "${{ZHENYUE_ADMIN_TOKEN}}"
  audit:
    prev_hash_genesis: "0000000000000000000000000000000000000000000000000000000000000000"
    auto_verify_schedule: "0 3 * * *"
  approval:
    default_timeout_high: 3600
    default_timeout_critical: 1800
    escalation_after_high: 600
    escalation_after_critical: 300
    approver_chains: {{}}
  capabilities:
    basic:
      allowed_tools: ["search_agents", "get_agent", "get_inbox"]
      max_message_rpm: 60
      trust_weight: 0.1
    verified:
      allowed_tools: ["search_agents", "get_agent", "get_inbox", "send_message", "start_negotiation"]
      max_message_rpm: 120
      trust_weight: 0.3
    trusted:
      allowed_tools: ["search_agents", "get_agent", "get_inbox", "send_message", "start_negotiation", "create_agreement", "submit_rating"]
      max_message_rpm: 300
      trust_weight: 0.6
    admin:
      allowed_tools: ["*"]
      max_message_rpm: 1000
      trust_weight: 1.0
  rate_limit:
    per_agent_rpm: 60
    global_rpm: 500
  break_glass:
    enabled: true
    token_path: "{values["keys_dir"]}/break_glass.token"
    allowed_actions: ["stop_agent", "isolate_agent", "block_ip"]
    cooldown_minutes: 30
  message_signing:
    enabled: true
    algorithm: "hmac-sha256"
    time_window_seconds: 300
    key_rotation_hours: 24
    grace_period_seconds: 300
"""


def generate_systemd_unit(values: dict) -> str:
    deploy = values["deploy_dir"]
    port = values["service_port"]
    return f"""[Unit]
Description=ACSSA 智能体操作系统 底座 OS
After=network.target postgresql.service redis.service
Wants=postgresql.service redis.service

[Service]
Type=simple
User={values["systemd_user"]}
Group={values["systemd_user"]}
WorkingDirectory={deploy}
Environment="ZHIPU_API_KEY={values["zhipu_key"]}"
Environment="DEEPSEEK_API_KEY={values["deepseek_key"]}"
Environment="ZHENYUE_ADMIN_TOKEN={values["admin_token"]}"
Environment="YONGHENG_BOOTSTRAP_TOKEN={values["yongheng_bootstrap_token"]}"
Environment="HUANYU_SIGN_KEY={values["huanyu_sign_key"]}"
Environment="QINGTIAN_CONFIG={deploy}/config.yaml"
ExecStart={sys.executable} {deploy}/main.py
ExecStartPost=/bin/bash -c 'for i in 1 2 3 4 5; do sleep 2 && curl -sf http://localhost:{port}/health && exit 0; done; exit 1'
Restart=always
RestartSec=10
RuntimeMaxSec=86400
StandardOutput=append:{values["logs_dir"]}/qingtian.log
StandardError=append:{values["logs_dir"]}/qingtian.log

[Install]
WantedBy=multi-user.target
"""


# ── 主流程 ────────────────────────────────────────────

def main():
    print("""
  ╔══════════════════════════════════════════════╗
  ║      ACSSA 智能体操作系统 底座 OS — 一键初始化向导        ║
  ║      v0.2.0                                  ║
  ╚══════════════════════════════════════════════╝
  """)
    print("  按提示填写配置项，回车使用默认值。\n")
    print("  需要提前准备：")
    print("    - PostgreSQL 16+ 数据库（已安装 pgvector + pgcrypto）")
    print("    - Redis 5.0+")
    print("    - DeepSeek API Key")
    print("    - Python 3.12+ 虚拟环境（推荐）")
    print()

    values = {}
    values["_date"] = subprocess.run(["date", "+%Y-%m-%d %H:%M:%S"], capture_output=True, text=True).stdout.strip()

    # ── Step 1: 角色 ──────────────────────────────────
    print_step(1, "服务器角色")
    print("  management = 主控底座（cron/采集/扫描/蒸馏 + API + Agent 执行）")
    print("  worker     = 部门服务器（仅 API + Agent 执行，cron 由 management 负责）")
    values["role"] = ask("角色", "management", validate=validate_role)
    print_ok(f"角色: {values['role']}")

    values["host"] = ask("服务器 hostname", subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip(), validate=validate_host)
    print_ok(f"host: {values['host']}")

    # ── Step 1b: 企业 Agent 规划 ──────────────────────
    print_step("1b", "企业信息 & Agent 规划")
    values["enterprise_name"] = ask("企业全称", "XX有限公司")
    values["enterprise_short"] = ask("企业简称", values["enterprise_name"][:4])

    # 服务计划选择
    print("\n  服务计划:")
    print("    basic    = 基础版（免费）— 自行维护")
    print("    enterprise = 企业版（500元/Agent/月，前20家早鸟200元）")
    plan = ask("  计划", "basic")
    values["plan"] = plan
    values["signup_date"] = datetime.now().strftime("%Y-%m-%d")

    if plan == "enterprise":
        from common.license import generate_license_key
        values["agent_limit"] = int(ask("  Agent 数量上限", "10"))
        values["admin_email"] = ask("  管理员邮箱", f"admin@{values['enterprise_short']}.com")
        values["license_key"] = generate_license_key(
            values["enterprise_name"],
            values["signup_date"],
            values["agent_limit"],
        )
        print_ok(f"License Key 已生成: {values['license_key']}")
    else:
        values["agent_limit"] = 0
        values["admin_email"] = ""
        values["license_key"] = ""

    from datetime import datetime
    print_ok(f"企业: {values['enterprise_name']} ({plan})")

    departments = []
    agents = []
    dept_count = int(ask("部门数量", "3"))
    for i in range(dept_count):
        print(f"\n  ── 部门 {i+1}/{dept_count} ──")
        dept_name = ask("  部门名称", f"部门{i+1}")

        # 部门 LLM 密钥
        print(f"    {dept_name} LLM 配置（可选，不填则用全局 Key）:")
        dept_primary_key = ask(f"    主模型 API Key（${dept_name.upper()}_LLM_KEY）", "", secret=True)
        dept_backup_key = ""
        dept_backup_model = ""
        if dept_primary_key:
            dept_primary_model = ask(f"    主模型名", default_llm_model())
            if ask_yn(f"    配置备份模型？", False):
                dept_backup_key = ask("    备份模型 API Key", "", secret=True)
                dept_backup_model = ask(
                    "    备份模型名",
                    (default_llm_backup_profile() or {}).get("model", "")) if dept_backup_key else ""
        else:
            dept_primary_model = default_llm_model()

        agent_count = int(ask(f"  {dept_name} 的 Agent 数量", "1"))
        dept_agents = []
        for j in range(agent_count):
            agent_name = ask(f"    Agent {j+1} 名称", f"{dept_name}Agent")
            capabilities_raw = ask(f"    {agent_name} 能力标签（逗号分隔）", "")
            capabilities = [c.strip() for c in capabilities_raw.split(",") if c.strip()]
            agent = {
                "name": agent_name,
                "department": dept_name,
                "capabilities": capabilities,
            }
            dept_agents.append(agent)
            agents.append(agent)
        departments.append({
            "name": dept_name,
            "agents": dept_agents,
            "llm_primary_model": dept_primary_model,
            "llm_backup_model": dept_backup_model if dept_backup_key else "",
            "llm_primary_key_env": f"{dept_name.upper()}_LLM_KEY",
            "llm_backup_key_env": f"{dept_name.upper()}_LLM_BACKUP_KEY" if dept_backup_key else "",
        })

    values["departments"] = departments
    values["agents"] = agents
    # 生成 YAML departments 段（含 LLM 配置）
    dept_yaml_lines = []
    for d in departments:
        dept_yaml_lines.append(f"  {d['name']}:")
        dept_yaml_lines.append(f"    llm:")
        dept_yaml_lines.append(f"      primary:")
        dept_yaml_lines.append(f"        provider: {default_llm_provider()}")
        dept_yaml_lines.append(f"        base_url: {default_llm_base_url()}")
        dept_yaml_lines.append(f"        api_key: ${{{d['llm_primary_key_env']}}}")
        dept_yaml_lines.append(f"        model: {d['llm_primary_model']}")
        if d.get("llm_backup_key_env"):
            dept_yaml_lines.append(f"      backup:")
            dept_yaml_lines.append(f"        provider: qwen")
            dept_yaml_lines.append(f"        base_url: https://dashscope.aliyuncs.com/compatible-mode/v1")
            dept_yaml_lines.append(f"        api_key: ${{{d['llm_backup_key_env']}}}")
            dept_yaml_lines.append(f"        model: {d['llm_backup_model']}")
        # agents
        dept_yaml_lines.append(f"    agents:")
        for a in d["agents"]:
            dept_yaml_lines.append(f"      - name: {a['name']}")
            dept_yaml_lines.append(f"        capabilities: {a['capabilities']}")
    values["departments_yaml"] = "\n".join(dept_yaml_lines) if dept_yaml_lines else "  {}"
    print_ok(f"已规划 {len(departments)} 个部门, {len(agents)} 个 Agent")

    # ── 生成企业规划 Markdown ──
    md_path = DEPLOY_DIR / f"{values['enterprise_short']}_企业Agent规划.md"
    md_lines = [
        f"# {values['enterprise_name']} — ACSSA 智能体操作系统 Agent 规划",
        "",
        f"> 生成时间: {values['_date']}",
        f"> 服务器: {values['host']} (role={values['role']})",
        f"> 端口: {values['service_port']}",
        "",
        "## 企业概况",
        "",
        f"- **企业**: {values['enterprise_name']}",
        f"- **简称**: {values['enterprise_short']}",
        f"- **服务计划**: {values.get('plan', 'basic')} {'（早鸟200元/Agent/月）' if values.get('plan')=='enterprise' else '（免费）'}",
        f"- **License Key**: {values.get('license_key', 'N/A')}",
        f"- **Agent 上限**: {values.get('agent_limit', '无限制')}",
        f"- **部门数**: {len(departments)}",
        f"- **Agent 总数**: {len(agents)}",
        f"- **管理员邮箱**: {values.get('admin_email', '未配置')}",
        f"- **数据库**: {values['db_user']}@{values['db_host']}:{values['db_port']}/{values['db_name']}",
        "",
        "## 部门 & Agent 清单",
        "",
    ]
    for d in departments:
        md_lines.append(f"### {d['name']}")
        md_lines.append("")
        md_lines.append(f"- **主模型**: {d.get('llm_primary_model', 'glm-5.3-flash')}")
        if d.get("llm_backup_model"):
            md_lines.append(f"- **备份模型**: {d['llm_backup_model']}")
        md_lines.append("")
        md_lines.append("| # | Agent 名称 | 能力标签 |")
        md_lines.append("|---|-----------|---------|")
        for j, a in enumerate(d["agents"], 1):
            caps = ", ".join(a["capabilities"]) if a["capabilities"] else "待配置"
            md_lines.append(f"| {j} | {a['name']} | {caps} |")
        md_lines.append("")

    md_lines += [
        "## LLM 模型配置",
        "",
        "| 部门 | 主模型 | 备份模型 | 环境变量（主） | 环境变量（备） |",
        "|------|--------|---------|---------------|---------------|",
    ]
    for d in departments:
        primary_env = d.get("llm_primary_key_env", "")
        backup_env = d.get("llm_backup_key_env", "")
        md_lines.append(
            f"| {d['name']} | {d.get('llm_primary_model','glm-5.3-flash')} | "
            f"{d.get('llm_backup_model') or '—'} | "
            f"`{primary_env}` | "
            f"{'`'+backup_env+'`' if backup_env else '—'} |"
        )
    md_lines += [
        "",
        "## 知识库领域规划",
        "",
        "| 领域 | 说明 | 关联部门 |",
        "|------|------|---------|",
    ]
    for d in departments:
        md_lines.append(f"| {d['name']}域 | {d['name']}相关文档/知识 | {d['name']} |")
    md_lines += [
        "",
        "## 部署清单",
        "",
        "```bash",
        f"# 服务器: {values['host']}",
        f"# 角色: {values['role']}",
        f"# 配置: {DEPLOY_DIR}/config.yaml",
        "",
        f"source {DEPLOY_DIR}/.env",
        f"python3 {DEPLOY_DIR}/main.py",
        "```",
        "",
        "## 备注",
        "",
        "- 能力标签决定 Agent 在执策任务分解时被分配哪些步骤",
        "- 如有新部门，重新运行 `python3 setup.py` 更新此文件后调整 config.yaml",
    ]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    print_ok(f"企业规划文档已生成: {md_path}")

    # ── Step 2: 数据库 ────────────────────────────────
    print_step(2, "PostgreSQL 连接")
    values["db_host"] = ask("数据库主机", "localhost")
    values["db_port"] = ask("数据库端口", "5432", validate=validate_port)
    values["db_name"] = ask("数据库名", "qingtian")
    values["db_user"] = ask("数据库用户", "qingtian")
    values["db_password"] = ask("数据库密码", "", secret=True)

    # 尝试连接
    print("  正在检测数据库连接...")
    try:
        import asyncpg
        async def _test_db():
            conn = await asyncpg.connect(
                host=values["db_host"],
                port=int(values["db_port"]),
                database=values["db_name"],
                user=values["db_user"],
                password=values["db_password"],
                timeout=10,
            )
            ver = await conn.fetchval("SELECT version();")
            await conn.close()
            return ver
        import asyncio
        ver = asyncio.run(_test_db())
        print_ok(f"已连接: {ver.split(',')[0]}")
    except Exception as e:
        print_warn(f"无法连接数据库: {e}")
        if not ask_yn("数据库暂不可达，继续生成配置？", False):
            sys.exit(1)

    # 检查扩展
    print("  正在检测 PostgreSQL 扩展...")
    try:
        async def _check_ext():
            conn = await asyncpg.connect(
                host=values["db_host"], port=int(values["db_port"]),
                database=values["db_name"], user=values["db_user"],
                password=values["db_password"], timeout=10,
            )
            exts = await conn.fetch("SELECT extname FROM pg_extension;")
            await conn.close()
            return [r["extname"] for r in exts]
        exts = asyncio.run(_check_ext())
        for required in ("vector", "pgcrypto"):
            if required in exts:
                print_ok(f"扩展 {required} 已安装")
            else:
                print_warn(f"扩展 {required} 未安装，请执行: CREATE EXTENSION {required};")
    except Exception:
        pass

    # ── Step 3: Redis ─────────────────────────────────
    print_step(3, "Redis 连接")
    # 按角色建议 Redis URL：管理服务器连本地，采购/销售通过 wg0 连管理服务器
    role = values["role"]
    if role == "management":
        redis_default = "redis://localhost:6379"
        print("  管理服务器应连接本地 systemd Redis")
    else:
        mgmt_wg_ip = ask("管理服务器 wg0 IP", "10.0.100.1")
        redis_default = f"redis://{mgmt_wg_ip}:6379"
        print(f"  {role} 服务器应通过 wg0 隧道连接管理服务器 Redis: {redis_default}")
        print("  ⚠️  请确保管理服务器的 Redis 已配置为绑定 wg0 IP")
    values["redis_url"] = ask("Redis URL", redis_default)
    print("  正在检测 Redis 连接...")
    try:
        import redis
        r = redis.from_url(values["redis_url"])
        if r.ping():
            print_ok("Redis 连接正常")
        r.close()
    except Exception as e:
        print_warn(f"Redis 暂不可达 (预期内，采购/销售须等 wg0 通了才行): {e}")
        if not ask_yn("Redis 暂不可达，继续？", True):
            sys.exit(1)

    # Redis 端口归属检测
    if role == "management":
        print("  正在检测 Redis 端口归属...")
        redis_port = values["redis_url"].rsplit(":", 1)[-1]
        port_owners = _check_port_owner(redis_port)
        if port_owners:
            print_warn(f"端口 {redis_port} 被以下进程占用(非 systemd redis-server):")
            for owner in port_owners:
                print(f"    - {owner}")
            print("  部署前请处理：")
            print("    1) docker stop <container-id>         # 停 Docker Redis 容器")
            print("    2) kill <pid>                          # 停 socat 转发")
            print("    3) systemctl start redis-server        # 启 systemd Redis")
            if not ask_yn("端口冲突未解决，继续？", True):
                sys.exit(1)
        else:
            print_ok("Redis 端口归属正常")
    else:
        print("  ⚠️  采购/销售服务器不需要本地 Redis，请确认：")
        print("    1) docker ps | grep redis  # 有 Docker Redis 的话，停掉")
        print("    2) 确认 wg0 隧道已通: ping <管理服务器 wg0 IP>")
        if ask_yn("是否检查本地是否有多余的 Docker Redis？", True):
            try:
                docker_redis = subprocess.run(
                    ["docker", "ps", "--filter", "ancestor=redis", "--format",
                     "{{.ID}} {{.Image}} {{.Names}} {{.Ports}}"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                if docker_redis:
                    print_warn("发现以下 Docker Redis 容器，建议停掉：")
                    for line in docker_redis.splitlines():
                        print(f"    - {line}")
                    print("  停止命令: docker stop <container-id> && docker rm <container-id>")
            except Exception:
                pass

    # ── Step 4: API Key ───────────────────────────────
    print_step(4, "API 密钥")
    values["zhipu_key"] = ask(
        f"智谱 API Key（主模型 {default_llm_model()}）", "", secret=True)
    if values["zhipu_key"]:
        print_ok("智谱 Key 已设置")
    else:
        print_warn("未设置智谱 Key，LLM 主模型不可用")
    values["deepseek_key"] = ask("DeepSeek API Key（备用模型）", "", secret=True)
    if values["deepseek_key"]:
        print_ok("DeepSeek Key 已设置")
    else:
        print_warn("未设置 DeepSeek Key，LLM 备用模型不可用")

    # 生成 admin token（镇岳）
    values["admin_token"] = secrets.token_hex(32)
    print_ok(f"镇岳 Admin Token 已生成: {values['admin_token'][:12]}...")

    # 生成 bootstrap token（永恒首次部署用）
    values["yongheng_bootstrap_token"] = secrets.token_hex(32)
    print_ok(f"永恒 Bootstrap Token 已生成: {values['yongheng_bootstrap_token'][:12]}...")

    # 生成寰宇签名密钥（跨底座通信用，三台服务器必须相同）
    values["huanyu_sign_key"] = secrets.token_hex(32)
    print_ok(f"寰宇签名密钥已生成: {values['huanyu_sign_key'][:12]}...（⚠️ 三台服务器必须统一）")

    # ── Step 5: 目录路径 ────────────────────────────────
    print_step(5, "目录路径")
    deploy_default = str(DEPLOY_DIR)
    deploy_dir = ask("部署目标目录（config.yaml / keys / models / logs 均置于此）", deploy_default)
    values["deploy_dir"] = deploy_dir
    deploy_path = Path(deploy_dir)

    values["openclaw_workspace"] = ask("OpenClaw workspace 路径", "/root/.openclaw/workspace")
    values["models_dir"] = ask("模型缓存目录", str(deploy_path / "models"))
    values["keys_dir"] = ask("密钥目录", str(deploy_path / "keys"))
    values["logs_dir"] = ask("日志目录", str(deploy_path / "logs"))

    # ── Step 6: 高级选项 ──────────────────────────────
    print_step(6, "高级选项")
    values["service_port"] = ask("服务端口", "1996", validate=validate_port)
    values["timezone"] = ask("调度器时区", "Asia/Shanghai")
    values["embed_threads"] = ask("嵌入模型线程数", "2")
    values["systemd_user"] = ask("systemd 运行用户", "qingtian")
    print_ok(f"端口 {values['service_port']}, 时区 {values['timezone']}")

    # ── Step 7: 确认 ──────────────────────────────────
    print_step(7, "确认配置")
    print(f"""
  角色:       {values['role']}
  主机:       {values['host']}
  部署目录:   {values['deploy_dir']}
  数据库:     {values['db_user']}@{values['db_host']}:{values['db_port']}/{values['db_name']}
  Redis:      {values['redis_url']}
  端口:       {values['service_port']}
  时区:       {values['timezone']}
  智谱(主):   {'已设置' if values['zhipu_key'] else '未设置'}
  DeepSeek(备): {'已设置' if values['deepseek_key'] else '未设置'}
  模型目录:   {values['models_dir']}
  密钥目录:   {values['keys_dir']}
  日志目录:   {values['logs_dir']}
  systemd用户: {values['systemd_user']}
""")
    if not ask_yn("确认生成？", True):
        print("  已取消。")
        sys.exit(1)

    # ── Step 8: 生成文件 ──────────────────────────────
    print_step(8, "生成配置文件")

    keys_dir = Path(values["keys_dir"])
    models_dir = Path(values["models_dir"])
    logs_dir = Path(values["logs_dir"])
    deploy_dir = Path(values["deploy_dir"])
    config_path = deploy_dir / "config.yaml"
    env_path = deploy_dir / ".env"
    unit_path = deploy_dir / "qingtian.service"

    # 创建目录
    for d in [keys_dir, models_dir, logs_dir]:
        d.mkdir(parents=True, exist_ok=True)
    print_ok(f"目录已创建: keys/ models/ logs/ (位于 {deploy_dir})")

    # 生成主密钥（Fernet 标准：base64(urlsafe(32随机字节))，与 encryptor.py 的 Fernet 一致）
    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    (keys_dir / "master.key").write_bytes(master_key)
    (keys_dir / "master.key").chmod(0o600)
    print_ok(f"主密钥已生成 (Fernet): {keys_dir / 'master.key'}")

    # 生成断网应急令牌
    break_glass = secrets.token_hex(16)
    (keys_dir / "break_glass.token").write_text(break_glass)
    (keys_dir / "break_glass.token").chmod(0o600)
    print_ok(f"断网令牌已生成: {keys_dir / 'break_glass.token'}")

    # 备份旧 config
    if config_path.exists():
        backup = config_path.with_suffix(".yaml.bak")
        config_path.rename(backup)
        print_ok(f"旧配置已备份: {backup}")

    # 写入 config.yaml
    config_path.write_text(generate_config(values), encoding="utf-8")
    print_ok(f"配置已生成: {config_path}")

    # 写入 systemd unit
    unit_path.write_text(generate_systemd_unit(values), encoding="utf-8")
    print_ok(f"systemd unit 已生成: {unit_path}")

    # 写入 .env
    env_content = f"""# ACSSA 智能体操作系统环境变量 — 部署前 source 此文件
export ZHIPU_API_KEY="{values['zhipu_key']}"
export DEEPSEEK_API_KEY="{values['deepseek_key']}"
export ZHENYUE_ADMIN_TOKEN="{values['admin_token']}"
export YONGHENG_BOOTSTRAP_TOKEN="{values['yongheng_bootstrap_token']}"
export HUANYU_SIGN_KEY="{values['huanyu_sign_key']}"
export QINGTIAN_CONFIG="{config_path}"
"""
    env_path.write_text(env_content, encoding="utf-8")
    env_path.chmod(0o600)
    print_ok(f".env 已生成: {env_path}")

    # ── Step 9: 安装依赖 + 初始化 Schema ──────────────
    print_step(9, "初始化")

    if ask_yn("立即安装 Python 依赖？", False):
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(deploy_dir / "requirements.txt")], check=False)
        print_ok("依赖安装完成")

    if ask_yn("立即初始化数据库 Schema？", True):
        try:
            import asyncio, asyncpg
            os.environ["QINGTIAN_CONFIG"] = str(config_path)
            os.environ["ZHIPU_API_KEY"] = values["zhipu_key"]
            os.environ["DEEPSEEK_API_KEY"] = values["deepseek_key"]
            os.environ["ZHENYUE_ADMIN_TOKEN"] = values["admin_token"]

            async def _init_all():
                from common.db import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    for mod, name in [
                        ("xixing.database", "xixing"),
                        ("yongheng.database", "yongheng"),
                        ("huanyu.database", "huanyu"),
                        ("zhenyue.database", "zhenyue"),
                    ]:
                        m = __import__(mod, fromlist=["ensure_schema"])
                        await m.ensure_schema()
                        print(f"    {name} schema ready")
                await pool.close()
            asyncio.run(_init_all())
            print_ok("所有 Schema 初始化成功")
        except Exception as e:
            print_warn(f"Schema 初始化失败: {e}")
            print("  可稍后启动服务自动初始化，或手动运行: python3 -c 'from xixing.database import ensure_schema; import asyncio; asyncio.run(ensure_schema())'")

    # ── 完成 ──────────────────────────────────────────
    deploy = values["deploy_dir"]
    print(f"""
╔══════════════════════════════════════════════╗
║           ✅ 初始化完成！                     ║
╚══════════════════════════════════════════════╝

  部署目录: {deploy}

  接下来：

  1. cd {deploy} && source .env               # 加载环境变量
  2. python3 {deploy}/main.py                  # 前台启动测试
  3. curl http://localhost:{values["service_port"]}/health  # 健康检查
  4. sudo cp {deploy}/qingtian.service /etc/systemd/system/
     sudo systemctl daemon-reload
     sudo systemctl enable --now qingtian     # 安装为系统服务
""")

    if not values["zhipu_key"]:
        print("  ⚠️  别忘了设置 ZHIPU_API_KEY 环境变量后再启动服务（LLM 主模型）")
    if not values["deepseek_key"]:
        print("  ⚠️  未设置 DEEPSEEK_API_KEY，LLM 备用模型不可用")
    print(f"  📋 镇岳 Admin Token: {values['admin_token']}")
    print(f"  📋 永恒 Bootstrap Token: {values['yongheng_bootstrap_token']}")
    print("     首次部署用此 Token 创建正式的 admin token：")
    print(f"     curl -X POST http://localhost:{values['service_port']}/v1/yongheng/token/create \\")
    print(f'       -H "Authorization: Bearer {values["yongheng_bootstrap_token"]}" \\')
    print(f'       -H "Content-Type: application/json" \\')
    print(f'       -d \'{{"namespace":"admin","level":"admin"}}\'')


if __name__ == "__main__":
    main()
