"""生产环境验证测试 — 共享 fixtures。

与 integration/conftest.py 复用原则：
  - 基础连接检查（base_url、server liveness）→ 复用
  - 测试 fixture（agents、tokens）→ 独立，避免与集成测试互相污染
  - 全部 production 测试运行在同一底座实例上，不启动新进程
"""
import os
import pytest
import httpx

BASE_URL = os.getenv("QINGTIAN_BASE_URL", "http://127.0.0.1:1996")


def is_server_running() -> bool:
    """检查ACSSA 底座是否健康。"""
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=5.0)
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def base_url():
    if not is_server_running():
        pytest.skip(f"ACSSA 底座未启动 ({BASE_URL}/health)")
    return BASE_URL


@pytest.fixture(scope="session")
def admin_token(base_url) -> str:
    """获取 admin token — 环境变量 → bootstrap → 跳过。"""
    token = os.getenv("QINGTIAN_ADMIN_TOKEN", "")
    if token:
        return token

    bootstrap = os.getenv("ZHENYUE_ADMIN_TOKEN", "")
    if bootstrap:
        try:
            resp = api("POST", "/v1/zhenyue/token/create", base_url, token=bootstrap,
                       json={"agent_id": "admin", "role": "admin"})
            if resp.status_code == 200:
                return resp.json().get("token", "")
        except Exception:
            pass

    pytest.skip("无法获取 admin token（设置 QINGTIAN_ADMIN_TOKEN 环境变量）")
    return ""


@pytest.fixture(scope="session")
def agents(base_url, admin_token):
    """注册一组测试 Agent，返回 {buyer, seller, monitor} id 字典。"""
    from tests.production import conftest as _local

    def _register(name: str, category: str) -> str:
        resp = api("POST", "/v1/huanyu/agents/register", base_url, json={
            "name": name, "category": category,
            "server_host": "127.0.0.1",
        })
        if resp.status_code in (200, 201):
            return resp.json().get("agent_id", "")
        if resp.status_code == 409:
            sr = api("GET", f"/v1/huanyu/agents/search?q={name}", base_url)
            if sr.status_code == 200:
                items = sr.json().get("agents", [])
                if items:
                    return items[0].get("agent_id", "")
        raise RuntimeError(f"注册 Agent {name} 失败: {resp.status_code} {resp.text[:200]}")

    buyer = _register("生产验证采购", "biz:buyer")
    seller = _register("生产验证销售", "biz:seller")
    monitor = _register("生产验证监控", "infra:monitor")

    _local.BUYER_ID = buyer
    _local.SELLER_ID = seller
    _local.MONITOR_ID = monitor
    return {"buyer": buyer, "seller": seller, "monitor": monitor}


# 模块级变量，供 api() 的内部引用
BUYER_ID = ""
SELLER_ID = ""
MONITOR_ID = ""


def bid() -> str:
    """当前测试的采购 Agent ID。"""
    return BUYER_ID


def sid() -> str:
    """当前测试的销售 Agent ID。"""
    return SELLER_ID


def mid() -> str:
    """当前测试的监控 Agent ID。"""
    return MONITOR_ID


def api(method: str, path: str, base_url: str = BASE_URL, token: str = "", **kwargs):
    """统一 API 调用：自动拼接 base_url + 注入 Authorization header。"""
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "timeout" not in kwargs:
        kwargs["timeout"] = 30.0
    return httpx.request(method, f"{base_url}{path}", headers=headers, **kwargs)
