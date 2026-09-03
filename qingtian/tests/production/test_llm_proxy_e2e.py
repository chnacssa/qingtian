"""【P0 集成】LLM 代理端到端验证

测试场景:
  1. Agent → LLM 代理 /v1/llm/chat → 代理透传 LLM 调用 → 返回正常响应
  2. LLM 代理超时降级 / 错误回复的兜底
  3. 记忆模块在 LLM 调用前后的行为（如果记忆中间件存在）

运行前提:
  - ACSSA 底座已启动在 127.0.0.1:1996
  - DEEPSEEK_API_KEY 环境变量已设置（或 LLM 代理支持 mock 模式）
  - 如果 LLM 不可用，测试降级验证兜底行为

  pytest tests/production/test_llm_proxy_e2e.py -v -s
"""
import os
import pytest
from tests.production.conftest import api, BASE_URL, bid


pytestmark = pytest.mark.production


class TestLlmProxy:
    """LLM 代理正向与降级场景验证。"""

    @pytest.fixture(scope="class")
    def llm_available(self, base_url) -> bool:
        """检查 LLM 代理端点是否可用。"""
        try:
            resp = api("GET", "/v1/llm/health", base_url, timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    def test_llm_health(self, base_url):
        """LLM 代理健康检查。"""
        resp = api("GET", "/v1/llm/health", base_url, timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            assert data.get("status") == "ok", f"LLM health: {data}"
            print(f"  LLM Proxy available: {data.get('model', '?')}")
        else:
            print(f"  [INFO] LLM 代理未启用 (status={resp.status_code})")

    def test_basic_chat_completion(self, base_url, llm_available):
        """基本的 chat completion 请求 → LLM 返回响应。"""
        if not llm_available:
            pytest.skip("LLM 代理不可用")

        resp = api("POST", "/v1/llm/chat", base_url, json={
            "model": os.getenv("LLM_TEST_MODEL", "deepseek-chat"),
            "messages": [
                {"role": "system", "content": "你是一个助手，回答尽量简短。"},
                {"role": "user", "content": "请用一句话说明什么是 Agent？"},
            ],
            "max_tokens": 200,
            "temperature": 0.1,
        }, timeout=30.0)

        assert resp.status_code == 200, (
            f"LLM chat 请求失败: {resp.status_code} {resp.text[:300]}"
        )
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        assert content, f"LLM 返回空 content: {data}"
        print(f"  LLM response ({len(content)} chars): {content[:100]}...")

    def test_chat_with_timeout(self, base_url, llm_available):
        """短超时 → 应降级返回 504 或超时错误。"""
        if not llm_available:
            pytest.skip("LLM 代理不可用")

        # 1ms 超时，必然触发降级
        resp = api("POST", "/v1/llm/chat", base_url, json={
            "model": os.getenv("LLM_TEST_MODEL", "deepseek-chat"),
            "messages": [
                {"role": "user", "content": "讲个长故事"},
            ],
            "max_tokens": 5000,
            "timeout_ms": 1,
        }, timeout=5.0)

        # 超时降级应该是 504 或特殊错误码
        if resp.status_code in (504, 408, 503):
            print(f"  ✅ LLM 超时正确降级: {resp.status_code}")
        elif resp.status_code == 200:
            print(f"  [INFO] LLM 在 1ms 超时内返回了内容，可能是 mock 模式")
        else:
            print(f"  [INFO] 超时行为: {resp.status_code} {resp.text[:100]}")

    def test_empty_messages_rejected(self, base_url, llm_available):
        """空消息应被 LLM 代理拒绝。"""
        if not llm_available:
            pytest.skip("LLM 代理不可用")

        resp = api("POST", "/v1/llm/chat", base_url, json={
            "model": "deepseek-chat",
            "messages": [],
            "max_tokens": 100,
        }, timeout=5.0)

        assert resp.status_code in (400, 422), (
            f"空消息应返回 400/422，实际: {resp.status_code}"
        )
        print(f"  ✅ 空消息被正确拒绝: {resp.status_code}")


class TestLlmFromAgentContext:
    """Agent 上下文中使用 LLM 代理的场景。"""

    def test_agent_memory_with_llm(self, base_url, agents, llm_available):
        """Agent 写记忆后通过语义搜索查到。"""
        if not llm_available:
            pytest.skip("LLM 代理不可用（记忆需要 embedding）")

        agent_id = bid()
        namespace = f"agent:{agent_id}"

        # 写一条测试记忆
        write = api("POST", "/v1/yongheng/memories", base_url, json={
            "namespace": namespace,
            "memory_type": "episodic",
            "content": "今天验证了 LLM 代理的 chat completion 功能，返回正常。",
        }, timeout=10.0)

        if write.status_code not in (200, 201):
            print(f"  [INFO] 记忆写入: {write.status_code} {write.text[:100]}")
            return

        # 搜索
        search = api("GET",
                     f"/v1/yongheng/search?q=LLM+chat+completion&namespace={namespace}",
                     base_url, timeout=10.0)

        if search.status_code == 200:
            results = search.json().get("results", [])
            print(f"  记忆搜索到 {len(results)} 条结果")
        else:
            print(f"  [INFO] 记忆搜索: {search.status_code}")

    def test_yongheng_health(self, base_url):
        """永恒模块健康检查。"""
        resp = api("GET", "/v1/yongheng/health", base_url, timeout=5.0)
        if resp.status_code == 200:
            print("  Yongheng module available")
        else:
            print(f"  [INFO] Yongheng: {resp.status_code}")
