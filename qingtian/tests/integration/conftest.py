"""集成测试共享 fixtures — DB 连接检查 + 底座健康检查"""
import pytest
import httpx
import time
import os
import subprocess

# 自动加载 .env 文件（如果有）
_env_loaded = False
_env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
if os.path.isfile(_env_path):
    import re
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if not os.environ.get(k):
                    os.environ[k] = v
                    _env_loaded = True

BASE_URL = "http://127.0.0.1:1996"
OPENCLAW_URL = "http://127.0.0.1:18789"

# 测试 Agent 身份（category 必须符合 huanyu 合法枚举）
BUYER_CATEGORY = "biz:buyer"
SELLER_CATEGORY = "biz:seller"
VERIFIER_CATEGORY = "biz:seller"

# 注册后动态赋值
BUYER_ID = ""
SELLER_ID = ""
VERIFIER_ID = ""
ADMIN_TOKEN = None  # 从环境变量或 config 读取


def is_server_running() -> bool:
    """检查ACSSA 底座是否在运行。"""
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def base_url():
    if not is_server_running():
        pytest.skip("ACSSA 底座未启动 (http://127.0.0.1:1996/health)")
    return BASE_URL


@pytest.fixture(scope="session")
def admin_token(base_url) -> str:
    """获取 admin token（环境变量 → bootstrap → 跳过）。"""

    token = os.getenv("QINGTIAN_ADMIN_TOKEN", "")
    if token:
        return token

    # 尝试 ZHENYUE_ADMIN_TOKEN（镇岳 bootstrap 种子令牌）
    bootstrap = os.getenv("ZHENYUE_ADMIN_TOKEN", "")
    if bootstrap:
        try:
            resp = api("POST", "/v1/zhenyue/token/create", base_url, token=bootstrap,
                       json={"agent_id": "admin", "role": "admin"})
            if resp.status_code == 200:
                token = resp.json().get("token", "")
        except Exception:
            pass

    if token:
        return token
    pytest.skip("无法获取 admin token（设置 QINGTIAN_ADMIN_TOKEN 或 ZHENYUE_ADMIN_TOKEN 环境变量）")
    return ""


@pytest.fixture(scope="session")
def agents(base_url, admin_token):
    """注册全部测试 Agent，返回 {buyer_id, seller_id, verifier_id}。"""
    from tests.integration import conftest as _conftest_local

    def _register(name: str, category: str) -> str:
        resp = api("POST", "/v1/huanyu/agents/register", base_url, json={
            "name": name, "category": category,
            "server_host": "127.0.0.1",
        })
        if resp.status_code in (200, 409):
            data = resp.json() if resp.status_code == 200 else {}
            agent_id = data.get("agent_id", "")
            if not agent_id:
                # 409 冲突 — 尝试搜索已存在的
                sr = api("GET", f"/v1/huanyu/agents/search?q={name}", base_url)
                if sr.status_code == 200:
                    items = sr.json().get("agents", [])
                    if items:
                        agent_id = items[0].get("agent_id", "")
            if not agent_id:
                raise RuntimeError(f"Failed to register agent {name}: {resp.text[:200]}")
            return agent_id
        raise RuntimeError(f"Agent registration failed: {resp.status_code} {resp.text[:200]}")

    BUYER_ID = _register("采购Agent01", BUYER_CATEGORY)
    SELLER_ID = _register("销售Agent01", SELLER_CATEGORY)
    VERIFIER_ID = _register("验证Agent02", VERIFIER_CATEGORY)

    # 写入模块级变量，供 bid()/sid()/vid() 读取
    _conftest_local.BUYER_ID = BUYER_ID
    _conftest_local.SELLER_ID = SELLER_ID
    _conftest_local.VERIFIER_ID = VERIFIER_ID

    print(f"\n  Agents registered: buyer={BUYER_ID}, seller={SELLER_ID}, verifier={VERIFIER_ID}")
    return {
        "buyer": BUYER_ID,
        "seller": SELLER_ID,
        "verifier": VERIFIER_ID,
    }


@pytest.fixture(scope="session")
def buyer_token(base_url, admin_token, agents) -> str:
    """为采购 Agent 创建 token。"""
    from tests.integration import conftest as _conftest_local
    try:
        resp = api("POST", "/v1/zhenyue/token/create", base_url, admin_token,
                   json={"agent_id":_conftest_local.BUYER_ID, "role": "agent"})
        if resp.status_code == 200:
            return resp.json().get("token", "")
    except Exception:
        pass
    pytest.skip("无法创建 buyer token")
    return ""


@pytest.fixture(scope="session")
def seller_token(base_url, admin_token, agents) -> str:
    """为销售 Agent 创建 token。"""
    from tests.integration import conftest as _conftest_local
    try:
        resp = api("POST", "/v1/zhenyue/token/create", base_url, admin_token,
                   json={"agent_id":_conftest_local.SELLER_ID, "role": "agent"})
        if resp.status_code == 200:
            return resp.json().get("token", "")
    except Exception:
        pass
    pytest.skip("无法创建 seller token")
    return ""


@pytest.fixture(scope="session")
def yongheng_admin_token(base_url) -> str:
    """永恒系统 admin token — 使用环境变量或 bootstrap。"""

    token = os.environ.get("YONGHENG_BOOTSTRAP_TOKEN", "")
    if token:
        return token
    return ""


# ── 便捷访问函数（动态取 Agent ID）─────────────────────

def bid() -> str:
    return BUYER_ID


def sid() -> str:
    return SELLER_ID


def vid() -> str:
    return VERIFIER_ID


def api(method: str, path: str, base_url: str = BASE_URL, token: str = "", **kwargs):
    """统一 API 调用：自动拼接 base_url + 注入 Authorization header。"""
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "timeout" not in kwargs:
        kwargs["timeout"] = 60.0
    return httpx.request(method, f"{base_url}{path}", headers=headers, **kwargs)
