"""权限规则 — SAST + 迁移工具共享的权限检测规则

两个消费方：
  - osskill/sast.py — SAST 静态分析（导入 _PERMISSION_RULES）
  - osskill/migration.py — 迁移工具（导入 CALL_TO_PERM）
"""

# ── 权限规则（权威定义） ─────────────────────────────

_PERMISSION_RULES: dict[str, dict] = {
    "network": {
        "level": "L2",
        "description": "底座 API 内网调用 + HTTP 出站",
        "imports": {"requests", "httpx", "aiohttp", "urllib", "socket", "urlib3"},
        "calls": {
            "requests.get", "requests.post", "requests.put", "requests.delete",
            "requests.patch", "requests.head", "requests.request",
            "httpx.get", "httpx.post", "httpx.put", "httpx.delete",
            "httpx.patch", "httpx.head", "httpx.request",
            "httpx.Client", "httpx.AsyncClient",
            "aiohttp.ClientSession", "aiohttp.ClientSession.get",
            "aiohttp.ClientSession.post", "aiohttp.ClientSession.put",
            "aiohttp.ClientSession.delete",
            "socket.socket", "socket.create_connection",
            "urllib.request.urlopen", "urllib.request.Request",
            "websockets.connect",
        },
        "patterns": [r"aiohttp\.ClientSession", r"httpx\.(Async)?Client"],
    },
    "filesystem": {
        "level": "L2",
        "description": "Skill data 目录读写",
        "imports": {"os", "shutil", "pathlib"},
        "calls": {
            "open",  # 内置 open，需结合参数判断是否跨目录
            "os.remove", "os.unlink", "os.rename", "os.replace",
            "os.makedirs", "os.listdir", "os.walk", "os.scandir",
            "os.path.exists", "os.path.isfile", "os.path.isdir",
            "shutil.copy", "shutil.copy2", "shutil.move", "shutil.rmtree",
            "shutil.copytree", "shutil.make_archive",
            "pathlib.Path.write_text", "pathlib.Path.write_bytes",
            "pathlib.Path.mkdir", "pathlib.Path.unlink",
            "pathlib.Path.rmdir",
        },
        "patterns": [],
    },
    "system": {
        "level": "L2",
        "description": "系统命令执行",
        "imports": {"subprocess", "shlex", "pexpect", "fabric"},
        "calls": {
            "subprocess.run", "subprocess.Popen", "subprocess.call",
            "subprocess.check_call", "subprocess.check_output",
            "os.system", "os.popen", "os.execl", "os.execle",
            "os.execlp", "os.execv", "os.execve", "os.execvp",
            "shlex.split",
            "pickle.loads", "pickle.load",
            "marshal.loads", "marshal.load",
            "yaml.load",
            "eval", "exec", "compile",
        },
        "patterns": [r"subprocess\."],
    },
    "llm": {
        "level": "L2",
        "description": "LLM 代理调用",
        "imports": set(),
        "calls": {
            "ctx.llm", "ctx.llm.chat",
        },
        "patterns": [r"ctx\.llm"],
    },
    "skills": {
        "level": "L2",
        "description": "跨 Skill 调用",
        "imports": set(),
        "calls": {
            "ctx.call_skill",
        },
        "patterns": [r"ctx\.call_skill"],
    },
    "identity": {
        "level": "L3",
        "description": "Agent 身份凭证（需人工审核）",
        "imports": set(),
        "calls": {
            "ctx.identity", "ctx.identity.sign",
            "sign_message", "verify_signature",
        },
        "patterns": [r"ctx\.identity"],
    },
    "lifecycle": {
        "level": "L3",
        "description": "管理其他 Skill（需人工审核）",
        "imports": set(),
        "calls": {
            "install_skill", "uninstall_skill", "restart_skill",
            "ctx.lifecycle",
        },
        "patterns": [r"ctx\.lifecycle"],
    },
    "network:outbound": {
        "level": "L3",
        "description": "任意外网出站连接（需人工审核）",
        "imports": set(),
        "calls": {
            "socket.socket",
        },
        "patterns": [r"socket\.socket"],
    },
}


def get_permission_rules() -> dict[str, dict]:
    """获取完整的权限规则表（深拷贝兜底）"""
    import copy
    return copy.deepcopy(_PERMISSION_RULES)


# ── 调用 → 权限映射（供 migration.py 使用） ─────────────

def _build_call_to_perm() -> dict[str, str]:
    """将 _PERMISSION_RULES 展平为 {调用名: 权限名} 映射

    级别优先级: L3 > L2。L3 规则先注册，L2 不得覆盖，
    否则 socket.socket 等调用会先被 L2 network 占用，
    导致 L3 network:outbound 的人工审核门禁失效。
    """
    mapping: dict[str, str] = {}
    # L3 优先注册（key 为 level 字符串，reverse 使 L3 排前）
    ordered = sorted(
        _PERMISSION_RULES.items(),
        key=lambda kv: kv[1].get("level", "L2"),
        reverse=True,
    )
    for perm, rules in ordered:
        for call in rules.get("calls", set()):
            if call not in mapping:
                mapping[call] = perm
        for imp in rules.get("imports", set()):
            mapping[imp] = perm  # import 名直接映射
    return mapping


CALL_TO_PERM: dict[str, str] = _build_call_to_perm()
"""API 调用 → 所需权限映射。

用法:
    CALL_TO_PERM["requests.get"] → "network"
    CALL_TO_PERM["subprocess.run"] → "system"

由 _PERMISSION_RULES 自动生成，与 SAST 规则保持一致。
"""

# ── 文件系统操作白名单 ──────────────────────────

SKIP_FILE_READ_IMPORTS = {"json", "yaml", "csv", "toml", "configparser"}

# ── 网络调用白名单 ─────────────────────────────

NETWORK_WHITELIST = {
    "requests.compat",
    "requests.structures",
}
