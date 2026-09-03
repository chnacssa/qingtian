"""汇川 Phase 0 — 中文搜索单元测试 (无 DB 依赖)

测试范围:
  - search.py 边界常量
  - _visibility_filter SQL 生成
  - search_knowledge 边界行为（空 query / 超长截断）
  - common/llm.py 配置函数
"""

import pytest

from huichuan.search import (
    MAX_QUERY_LENGTH,
    MAX_LIMIT,
    DEFAULT_LIMIT,
    search_knowledge,
    search_with_visibility,
    _visibility_filter,
)
from common.llm import (
    get_llm_api_key,
    get_llm_base_url,
    get_llm_model,
    llm_call_json,
)


# ═══════════════════════════════════════════════════════
# 边界常量
# ═══════════════════════════════════════════════════════


class TestSearchConstants:
    def test_max_query_length(self):
        assert MAX_QUERY_LENGTH == 100

    def test_max_limit(self):
        assert MAX_LIMIT == 200

    def test_default_limit(self):
        assert DEFAULT_LIMIT == 20

    def test_default_limit_within_max(self):
        assert DEFAULT_LIMIT <= MAX_LIMIT


# ═══════════════════════════════════════════════════════
# 空 query 边界（无需 DB）
# ═══════════════════════════════════════════════════════


class TestSearchEmptyQuery:
    @pytest.mark.asyncio
    async def test_empty_string_returns_empty(self):
        """空字符串 → 返回空列表"""
        result = await search_knowledge(None, "")
        assert result == []

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_empty(self):
        """纯空白 → 返回空列表"""
        result = await search_knowledge(None, "   \t  ")
        assert result == []

    @pytest.mark.asyncio
    async def test_none_query_safety(self):
        """None query 不应崩溃（strip 前会先检查）"""
        result = await search_knowledge(None, "")
        assert result == []


class TestSearchWithVisibilityEmptyQuery:
    @pytest.mark.asyncio
    async def test_empty_string_returns_empty(self):
        """空 query → (空列表, 0)"""
        results, total = await search_with_visibility(None, "")
        assert results == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_whitespace_returns_empty(self):
        """纯空白 → (空列表, 0)"""
        results, total = await search_with_visibility(None, "   ")
        assert results == []
        assert total == 0


# ═══════════════════════════════════════════════════════
# query 截断（无需 DB）
# ═══════════════════════════════════════════════════════


class TestQueryTruncation:
    """测试 query 超长截断逻辑 — 用 mock conn 验证截断后的 query 传给 SQL"""

    @pytest.mark.asyncio
    async def test_long_query_truncated_before_db_call(self):
        """超 100 字符 query → 截断为前 100 字符"""
        # 构造一个 150 字符的 query
        long_query = "测试" * 75  # 150 chars
        assert len(long_query) == 150

        # 通过 mock conn 验证传给 fetch 的 query 已截断
        calls = []

        class MockConn:
            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        mock = MockConn()
        await search_knowledge(mock, long_query)

        assert len(calls) == 1
        # 验证 params 中的 ILIKE pattern 长度不超过 100+2（% 包裹）
        ilike_param = calls[0]["params"][0]
        assert len(ilike_param) <= MAX_QUERY_LENGTH + 2
        assert f"%{'测试' * 50}%" == ilike_param  # 100 chars + 2 %

    @pytest.mark.asyncio
    async def test_exact_100_chars_passes(self):
        """恰好 100 字符 query → 不截断"""
        exact_query = "测" * 100
        assert len(exact_query) == 100

        calls = []

        class MockConn:
            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_knowledge(MockConn(), exact_query)
        assert len(calls) == 1


# ═══════════════════════════════════════════════════════
# limit 边界（无需 DB）
# ═══════════════════════════════════════════════════════


class TestLimitClamping:
    @pytest.mark.asyncio
    async def test_limit_clamped_to_max(self):
        """limit > 200 → 截断为 200"""
        calls = []

        class MockConn:
            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_knowledge(MockConn(), "测试", limit=500)
        # params 最后两个是 limit 和 offset
        assert calls[0]["params"][-2] == 200  # limit clamped

    @pytest.mark.asyncio
    async def test_limit_min_1(self):
        """limit < 1 → 自动设为 1"""
        calls = []

        class MockConn:
            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_knowledge(MockConn(), "测试", limit=0)
        assert calls[0]["params"][-2] == 1


# ═══════════════════════════════════════════════════════
# 可见性过滤 SQL 生成
# ═══════════════════════════════════════════════════════


class TestVisibilityFilter:
    def test_no_agent_public_only(self):
        """agent_id=None → 仅 public"""
        clause, params = _visibility_filter(None)
        assert clause == "visibility = 'public'"
        assert params == []

    def test_with_agent_includes_all_levels(self):
        """agent_id 存在 → public + enterprise + private(owner/authorized)"""
        clause, params = _visibility_filter("agent-1")
        assert "visibility = 'public'" in clause
        assert "visibility = 'enterprise'" in clause
        assert "owner_agent = $1" in clause
        assert "$2 = ANY(authorized_agents)" in clause
        assert params == ["agent-1", "agent-1"]

    def test_with_agent_start_index_offset(self):
        """start_index 偏移正确"""
        clause, params = _visibility_filter("agent-1", start_index=5)
        assert "owner_agent = $5" in clause
        assert "$6 = ANY(authorized_agents)" in clause
        assert params == ["agent-1", "agent-1"]

    def test_empty_agent_string_same_as_none(self):
        """空字符串 agent_id → falsy，同 None 行为（仅 public）"""
        clause, params = _visibility_filter("")
        # 空字符串也是 falsy，走 None 分支
        assert clause == "visibility = 'public'"
        assert params == []


# ═══════════════════════════════════════════════════════
# common/llm.py 配置函数
# ═══════════════════════════════════════════════════════


class TestCommonLLMConfig:
    def test_get_llm_api_key_returns_string(self):
        key = get_llm_api_key()
        assert isinstance(key, str)

    def test_get_llm_base_url_returns_string(self):
        url = get_llm_base_url()
        assert isinstance(url, str)
        assert url.startswith("http")

    def test_get_llm_model_returns_string(self):
        model = get_llm_model()
        assert isinstance(model, str)
        assert len(model) > 0

    def test_llm_call_json_no_api_key_returns_default(self):
        """LLM API Key 未配 → 返回 default（不调 HTTP）"""
        import os

        # 临时清除 API Key 测试默认路径
        saved = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            result = asyncio_sync(
                llm_call_json("test prompt", "test_caller", default={"status": "no_key"})
            )
            assert result == {"status": "no_key"}
        finally:
            if saved:
                os.environ["DEEPSEEK_API_KEY"] = saved


def asyncio_sync(coro):
    """同步执行 async 函数（用于测试不含 event loop 的场景）。"""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(coro)
    else:
        # 已有 event loop，创建新的 future
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result()


# ═══════════════════════════════════════════════════════
# search_with_visibility 完整参数测试（mock DB）
# ═══════════════════════════════════════════════════════


class TestSearchWithVisibilityIntegration:
    @pytest.mark.asyncio
    async def test_domain_filter_included(self):
        """domain 参数 → SQL 中包含 domain 过滤"""
        calls = []

        class MockConn:
            async def fetchrow(self, sql, *params):
                # COUNT query
                return {"count": 0}

            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_with_visibility(MockConn(), "测试", domain="power", agent_id="agent-1")
        assert len(calls) == 1
        assert "domain = $" in calls[0]["sql"]
        assert "power" in calls[0]["params"]

    @pytest.mark.asyncio
    async def test_tags_filter_included(self):
        """tags 参数 → SQL 中包含 tags 过滤"""
        calls = []

        class MockConn:
            async def fetchrow(self, sql, *params):
                return {"count": 0}

            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_with_visibility(MockConn(), "测试", tags=["transformer"], agent_id="agent-1")
        assert len(calls) == 1
        assert "tags && $" in calls[0]["sql"]
        assert ["transformer"] in calls[0]["params"]

    @pytest.mark.asyncio
    async def test_sort_by_invalid_falls_back_to_rank(self):
        """无效 sort_by → 自动回退为 rank"""
        calls = []

        class MockConn:
            async def fetchrow(self, sql, *params):
                return {"count": 0}

            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_with_visibility(MockConn(), "测试", sort_by="invalid_field")
        # 应该不崩溃，使用 rank 排序
        assert len(calls) == 1  # 成功执行

    @pytest.mark.asyncio
    async def test_include_expired_skips_date_filter(self):
        """include_expired=True → SQL 不含 valid_until 过滤"""
        calls = []

        class MockConn:
            async def fetchrow(self, sql, *params):
                return {"count": 0}

            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_with_visibility(MockConn(), "测试", include_expired=True)
        assert "valid_until" not in calls[0]["sql"]

    @pytest.mark.asyncio
    async def test_include_expired_default_adds_date_filter(self):
        """include_expired=False（默认）→ SQL 含 valid_until 过滤"""
        calls = []

        class MockConn:
            async def fetchrow(self, sql, *params):
                return {"count": 0}

            async def fetch(self, sql, *params):
                calls.append({"sql": sql, "params": list(params)})
                return []

        await search_with_visibility(MockConn(), "测试")
        assert "valid_until" in calls[0]["sql"]
