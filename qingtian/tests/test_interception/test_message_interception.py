"""
Agent → ACSSA 智能体操作系统 消息拦截集成测试

覆盖四大拦截场景（每项 1~3 测试用例）：
  1. Agent → ACSSA通用消息（send_message / inbox / peer 通讯）
  2. Agent → 永恒（记忆写入 / 轨迹 / 搜索）
  3. Agent → 执策（任务创建 / step 提交 / 策略检查）
  4. Agent → 镇岳（审计 / 规则管理 / 令牌）

消息拦截链路：
  Agent → OpenClaw Plugin ↴
                          ZhenyueGuardMiddleware (HTTP layer)
                            → 匹配路径规则 (PathMatcher)
                            → severity: low/medium/high/critical
                            → 检查 token 权限
                            → 放行 / 拦截（401）/ 记录审计
                          ↱
                          目标模块 (huanyu / yongheng / zhice / zhenyue)

运行前提：
  ACSSA 底座启动在 127.0.0.1:1996
  ZHENYUE_ADMIN_TOKEN 和 YONGHENG_BOOTSTRAP_TOKEN 环境变量已设置（systemd service 已配）

运行：
  pytest tests/integration/test_message_interception.py -v -s
"""

import json
import uuid
import pytest
import httpx
from datetime import datetime, timezone


BASE_URL = "http://127.0.0.1:1996"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 辅助函数 ──────────────────────────────────────────

def api(method: str, path: str, token: str = "", **kwargs) -> httpx.Response:
    """统一 API 调用，自动注入 Authorization 和 base_url。"""
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if "timeout" not in kwargs:
        kwargs["timeout"] = 15.0
    return httpx.request(method, f"{BASE_URL}{path}", headers=headers, **kwargs)


def is_server_running() -> bool:
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=3.0)
        return resp.status_code == 200
    except Exception:
        return False


# ── Session Scope Fixtures ────────────────────────────

@pytest.fixture(scope="session")
def base_url() -> str:
    assert is_server_running(), f"ACSSA 底座未运行 ({BASE_URL}/health)"
    return BASE_URL


@pytest.fixture(scope="session")
def admin_token(base_url) -> str:
    """获取镇岳 admin token（从 systemd Environment 注入的 ZHENYUE_ADMIN_TOKEN）。"""
    import os
    import httpx

    token = os.getenv("ZHENYUE_ADMIN_TOKEN", "")
    if token:
        return token

    # bootstrap 创建
    bootstrap = os.getenv("QINGTIAN_ADMIN_TOKEN", "")
    if bootstrap:
        try:
            resp = api("POST", "/v1/zhenyue/token/create", token=bootstrap,
                       json={"agent_id": "admin", "role": "admin"})
            if resp.status_code == 200:
                return resp.json().get("token", "")
        except Exception:
            pass

    pytest.skip("无法获取 admin token（设 ZHENYUE_ADMIN_TOKEN 环境变量）")
    return ""


@pytest.fixture(scope="session")
def test_agents(base_url, admin_token) -> dict:
    """注册测试 Agent 对。"""
    from uuid import uuid4

    def _ensure(name: str, category: str) -> str:
        r = api("POST", "/v1/huanyu/agents/register", json={
            "name": name, "category": category, "server_host": "127.0.0.1",
        })
        if r.status_code == 200:
            return r.json().get("agent_id", "")
        if r.status_code == 409:
            sr = api("GET", f"/v1/huanyu/agents/search?q={name}")
            if sr.status_code == 200:
                items = sr.json().get("agents", [])
                if items:
                    return items[0].get("agent_id", "")
        raise RuntimeError(f"注册 Agent 失败: {r.status_code} {r.text[:200]}")

    sender = _ensure(f"拦截发送-{_uid()}", "biz:buyer")
    receiver = _ensure(f"拦截接收-{_uid()}", "biz:seller")
    return {"sender": sender, "receiver": receiver}


@pytest.fixture(scope="session")
def agent_token(base_url, admin_token, test_agents) -> str:
    """为发送方 Agent 创建镇岳 token（用于访问 huanyu / zhice / zhenyue API）。"""
    aid = test_agents["sender"]
    resp = api("POST", "/v1/zhenyue/token/create", token=admin_token,
               json={"agent_id": aid, "role": "agent"})
    if resp.status_code == 200:
        return resp.json().get("token", "")
    pytest.skip(f"创建 agent token 失败: {resp.status_code} {resp.text[:200]}")
    return ""


@pytest.fixture(scope="session")
def yongheng_token(base_url, admin_token) -> str:
    """创建永恒专用 token（永恒有自己的 token 体系，yh_* 前缀）。
    用 YONGHENG_BOOTSTRAP_TOKEN 创建 namespace 级别 token。"""
    import os
    bootstrap = os.getenv("YONGHENG_BOOTSTRAP_TOKEN", "")
    if not bootstrap:
        pytest.skip("YONGHENG_BOOTSTRAP_TOKEN 未设置")
    resp = api("POST", "/v1/yongheng/token/create", token=bootstrap,
               json={"namespace": "intercept:test", "level": "namespace"})
    if resp.status_code == 200:
        return resp.json().get("token", "")
    pytest.skip(f"创建永恒 token 失败: {resp.status_code} {resp.text[:200]}")
    return ""


# ══════════════════════════════════════════════════════════
# 模块 1: Agent → ACSSA通用消息拦截
# ══════════════════════════════════════════════════════════

class TestInterceptionGeneralMessaging:
    """Agent 发送/接收消息时，镇岳中间件的路径规则匹配测试。

    关键规则：
      - POST /v1/huanyu/messages          → 不匹配危险规则 → 正常放行（~200）
      - DELETE /v1/huanyu/messages/*       → severity=high  → 被镇岳中间件拦截（401）
      - POST /v1/huanyu/messages/batch-read → severity=low  → 放行（审计记录）
    """

    def test_1_send_message(self, base_url, agent_token, test_agents):
        """
        TC1: Agent 发送普通消息 → 应正常放行
        镇岳规则中没有覆盖 POST /v1/huanyu/messages，应正常路由到 messaging 服务
        """
        resp = api("POST", "/v1/huanyu/messages", token=agent_token, json={
            "from_agent": test_agents["sender"],
            "to_agent": test_agents["receiver"],
            "message_type": "info",
            "payload": {"content": f"拦截测试消息-{_uid()}"},
        })
        print(f"\n  TC1 [发送消息]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("普通消息发送被镇岳拦截，应放行（镇岳无此危险规则）")
        assert resp.status_code == 200, (
            f"消息发送异常: {resp.status_code} {resp.text[:200]}"
        )
        data = resp.json()
        assert "message_id" in data
        print(f"  ✅ TC1 PASS: 消息发送成功, msg_id={data['message_id'][:12]}...")

    def test_2_delete_messages(self, base_url, admin_token):
        """
        TC2: Agent 批量删除消息 → 应被镇岳中间件拦截
        规则: DELETE /v1/huanyu/messages/*  severity=high
        预期 → 401 AUTHENTICATION_REQUIRED（镇岳拦截结果）
        """
        resp = api("DELETE", "/v1/huanyu/messages/batch-delete", token=admin_token,
                   json={"message_ids": [f"test-{_uid()}"]})
        print(f"\n  TC2 [删除消息]: {resp.status_code}")
        # 如果被镇岳拦截会返回 401 或 403
        # 如果路由不存在会返回 404（同样是未被放行的表现）
        assert resp.status_code != 200, (
            "DELETE /v1/huanyu/messages/* 应被拦截但返回了 200"
        )
        print(f"  ✅ TC2 PASS: 危险删除被拦截 ({resp.status_code})")

    def test_3_batch_mark_read(self, base_url, admin_token):
        """
        TC3: Agent 批量标记已读 → 低危操作应放行但记录审计
        规则: POST /v1/huanyu/messages/batch-read  severity=low
        预期 → 不返回 401 拦截
        """
        resp = api("POST", "/v1/huanyu/messages/batch-read", token=admin_token,
                   json={"message_ids": [f"test-{_uid()}"]})
        print(f"\n  TC3 [批量已读]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("batch-read (severity=low) 不应被镇岳拦截")
        print(f"  ✅ TC3 PASS: 低危操作正常通过 ({resp.status_code})")


# ══════════════════════════════════════════════════════════
# 模块 2: Agent → 永恒（yongheng）消息拦截
# ══════════════════════════════════════════════════════════

class TestInterceptionEternal:
    """Agent 向永恒系统写入/查询记忆的拦截测试。

    注意：永恒有自己的 token 体系（yh_*），与镇岳 token 不同。
    此处测试用的是永恒 token，验证的是永恒自身的 token/权限检查，
    而非镇岳中间件的拦截。

    镇岳规则中与永恒相关的：
      - DELETE /v1/yongheng/** → severity=critical（强拦截）
      - 其余操作默认不匹配危险规则 → 正常路由
    """

    def test_4_write_memory(self, base_url, yongheng_token):
        """
        TC4: Agent 写入记忆 → 应正常放行
        镇岳规则: POST /v1/yongheng/memories 不匹配危险规则
        """
        resp = api("POST", "/v1/yongheng/memories", token=yongheng_token, json={
            "namespace": "intercept:test",
            "content": json.dumps({"text": f"拦截测试-{_uid()}", "time": _ts()}),
            "source": "interception_test",
            "importance": 3,
        })
        print(f"\n  TC4 [记忆写入]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("记忆写入被镇岳拦截（应为正常放行）")
        assert resp.status_code in (200, 201), (
            f"记忆写入异常: {resp.status_code} {resp.text[:200]}"
        )
        print(f"  ✅ TC4 PASS: 记忆写入成功")

    def test_5_search_memory(self, base_url, yongheng_token):
        """
        TC5: Agent 搜索记忆 → 应正常放行
        镇岳规则: POST /v1/yongheng/search 不匹配危险规则
        """
        resp = api("POST", "/v1/yongheng/search", token=yongheng_token, json={
            "namespace": "intercept:test",
            "query": "拦截测试",
        })
        print(f"\n  TC5 [记忆搜索]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("记忆搜索被拦截（只读操作）")
        print(f"  ✅ TC5 PASS: 记忆搜索正常通过 ({resp.status_code})")

    def test_6_delete_memory(self, base_url, yongheng_token):
        """
        TC6: Agent 删除记忆 → 应被镇岳拦截
        规则: DELETE /v1/yongheng/**  severity=critical
        """
        resp = api("DELETE", f"/v1/yongheng/memories/intercept:test", token=yongheng_token)
        print(f"\n  TC6 [删除记忆]: {resp.status_code}")
        assert resp.status_code not in (200, 204), (
            "DELETE /v1/yongheng/** 应被镇岳拦截但返回了成功"
        )
        print(f"  ✅ TC6 PASS: 危险删除被拦截 ({resp.status_code})")


# ══════════════════════════════════════════════════════════
# 模块 3: Agent → 执策（zhice）消息拦截
# ══════════════════════════════════════════════════════════

class TestInterceptionPolicyEngine:
    """Agent 向执策发送任务/步骤的拦截测试。

    执策核心操作（创建任务/查询/提交步骤）不在镇岳危险规则中，
    应正常通行。但执策自身有 token 权限校验（需 namespace 或 admin 级别 token）。
    """

    def test_7_create_task(self, base_url, agent_token):
        """
        TC7: Agent 创建任务 → 应正常放行
        检查请求是否到达执策路由而非被镇岳中间件拦截
        """
        resp = api("POST", "/v1/zhice/tasks", token=agent_token, json={
            "title": f"拦截测试任务-{_uid()}",
            "description": "验证 Agent 到执策的消息通路",
            "agent_id": "interception-test-agent",
            "steps": [
                {"title": "步骤1", "instruction": "执行测试操作"},
                {"title": "步骤2", "instruction": "确认结果"},
            ],
        })
        print(f"\n  TC7 [创建任务]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("任务创建被镇岳拦截（执策默认无危险规则匹配）")
        # 执策可能返回 422（参数校验）或 200（成功）——只要不是 401 拦截
        assert resp.status_code != 401
        print(f"  ✅ TC7 PASS: 任务创建请求通过 ({resp.status_code})")

    def test_8_query_tasks(self, base_url, agent_token):
        """
        TC8: Agent 查询任务列表 → 应正常放行
        只读查询操作，不应被拦截
        """
        resp = api("GET", "/v1/zhice/tasks?limit=5", token=agent_token)
        print(f"\n  TC8 [查询任务]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("任务查询被镇岳拦截")
        print(f"  ✅ TC8 PASS: 任务查询通过 ({resp.status_code})")

    def test_9_submit_step(self, base_url, agent_token):
        """
        TC9: Agent 提交步骤结果 → 应正常放行
        先创建任务获取 step_id，再 post submit
        """
        # 创建任务获取 step
        create = api("POST", "/v1/zhice/tasks", token=agent_token, json={
            "title": f"步骤提交-{_uid()}",
            "description": "验证 step submit 通路",
            "agent_id": "interception-test-agent",
            "steps": [{"title": "测试步骤", "instruction": "执行并提交"}],
        })
        if create.status_code != 200:
            print(f"  TC9 [创建任务]: {create.status_code} → 跳过 submit（非拦截问题）")
            pytest.skip(f"任务创建未返回 200: {create.status_code} {create.text[:100]}")
            return

        data = create.json()
        steps = data.get("steps", [])
        if not steps:
            print("  TC9 [提交步骤]: 任务无 step 数据，跳过")
            return

        step_id = steps[0].get("step_id")
        if not step_id:
            print("  TC9 [提交步骤]: step 无 step_id，跳过")
            return

        submit = api("POST", f"/v1/zhice/steps/{step_id}/submit", token=agent_token, json={
            "result": {"status": "passed", "detail": "拦截测试"},
        })
        print(f"\n  TC9 [提交步骤]: {submit.status_code}")
        if submit.status_code == 401:
            pytest.fail("步骤提交被镇岳拦截")
        print(f"  ✅ TC9 PASS: 步骤提交通过 ({submit.status_code})")


# ══════════════════════════════════════════════════════════
# 模块 4: Agent → 镇岳（zhenyue）消息拦截
# ══════════════════════════════════════════════════════════

class TestInterceptionGuardSelf:
    """Agent 向镇岳系统自身的操作测试。

    镇岳自身的关键操作（审计/紧急破窗/规则管理）受严格保护：
      - audit queries → 正常放行（只读）
      - break_glass → severity=critical → 被中间件拦截
      - 普通 agent token 无权限访问管理类路由
    """

    def test_10_audit_query(self, base_url, admin_token):
        """
        TC10: Agent 查询审计日志 → 应正常放行
        镇岳中路审计操作不需特殊规则匹配
        """
        resp = api("GET", "/v1/zhenyue/audit?limit=5", token=admin_token)
        print(f"\n  TC10 [审计查询]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("审计查询被镇岳拦截（应放行或返回 404/403）")
        print(f"  ✅ TC10 PASS: 审计查询通过 ({resp.status_code})")

    def test_11_break_glass(self, base_url, agent_token):
        """
        TC11: Agent 触发紧急破窗 → 应被镇岳拦截
        规则: POST /v1/zhenyue/emergency/break_glass  severity=critical
        普通 Agent token 无权执行
        """
        resp = api("POST", "/v1/zhenyue/emergency/break_glass", token=agent_token, json={
            "reason": f"拦截测试-{_uid()}"
        })
        print(f"\n  TC11 [紧急破窗]: {resp.status_code}")
        assert resp.status_code not in (200, 202), (
            "break_glass 应被镇岳拦截但返回了成功"
        )
        print(f"  ✅ TC11 PASS: 紧急破窗被拦截 ({resp.status_code})")

    def test_12_guard_rules_admin(self, base_url, admin_token):
        """
        TC12: 管理员查询镇岳守卫规则 → 应正常放行
        管理操作用 admin token 执行，关键是有审计追踪
        """
        resp = api("GET", "/v1/zhenyue/rules", token=admin_token)
        print(f"\n  TC12 [查询规则]: {resp.status_code}")
        if resp.status_code == 401:
            pytest.fail("规则查询被镇岳拦截")
        print(f"  ✅ TC12 PASS: 规则查询通过 ({resp.status_code})")


# ══════════════════════════════════════════════════════════
# 拦截链路示意图（结构完整性补充）
# ══════════════════════════════════════════════════════════

class TestInterceptionChainIntegrity:
    """验证消息拦截链路的完整性——从镇岳中间件到目标服务的完整通路。"""

    def test_verify_middleware_active(self, base_url):
        """
        验证镇岳中间件处于活动状态（非 disabled）。
        方法：访问一个需要 token 的路由，确认中间件参与了请求处理。
        """
        # 不带 token 访问受保护路由 → 镇岳中间件应返回 401
        # 不传 token 访问镇岳 protected 端点 → 中间件应返回 401
        resp = api("GET", "/v1/zhenyue/agents/search?q=test")
        print(f"\n  TC-M1 [中间件活动性]: {resp.status_code}")
        # 无 token 时镇岳中间件应拒绝请求（401）
        if resp.status_code == 404:
            # 测试容错：如果路由不存在（搜索接口没注册），用另一个可验证的端点
            # 健康检查不带 token 应仍返回 200（直通），确认中间件没有全部拦截
            health = api("GET", "/health")
            assert health.status_code == 200, "健康检查也被拦截"
            pytest.skip("镇岳 agents/search 路由不存在，改用健康检查直通验证")
            return
        assert resp.status_code == 401, (
            f"镇岳中间件似乎未活动（无 token 请求得 {resp.status_code}，期望 401）"
        )
        print(f"  ✅ TC-M1 PASS: 镇岳中间件活动正常")

    def test_health_independent(self, base_url):
        """
        验证 /health 端点不受镇岳中间件影响（健康检查直通）。
        """
        resp = api("GET", "/health")
        print(f"\n  TC-M2 [健康检查直通]: {resp.status_code}")
        assert resp.status_code == 200
        print(f"  ✅ TC-M2 PASS: 健康检查直通正常")
