"""huichuan_client.py 单元测试"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zhice.huichuan_client import search_knowledge, search_same_category_experience


def _mock_async_context_manager(return_value):
    """创建模拟的 async context manager。

    pool.acquire() 返回一个 async context manager（非 coroutine），
    其 __aenter__ 被 await 后返回 conn 对象。
    """
    mgr = MagicMock()
    mgr.__aenter__ = AsyncMock(return_value=return_value)
    mgr.__aexit__ = AsyncMock(return_value=None)
    return mgr


@pytest.fixture
def huichuan_mock():
    """为测试注入 huichuan.service 假模块（lazy import 需要 sys.modules）"""
    mock_svc = MagicMock()
    mock_svc.search = AsyncMock()
    sys.modules["huichuan"] = MagicMock()
    sys.modules["huichuan.service"] = mock_svc
    yield mock_svc
    sys.modules.pop("huichuan", None)
    sys.modules.pop("huichuan.service", None)


class TestSearchKnowledge:
    @pytest.mark.asyncio
    async def test_search_empty_query(self):
        result = await search_knowledge("")
        assert result == []

    @pytest.mark.asyncio
    async def test_search_success(self, huichuan_mock):
        huichuan_mock.search.return_value = [
            {"title": "Test", "score": 0.8, "summary": "test"},
            {"title": "Low", "score": 0.3, "summary": "low"},
        ]
        results = await search_knowledge("test query", max_results=5, min_score=0.5)
        assert len(results) == 1
        assert results[0]["title"] == "Test"

    @pytest.mark.asyncio
    async def test_search_service_unavailable(self):
        """不注入 huichuan 模块 → ImportError → 返回空列表"""
        sys.modules.pop("huichuan", None)
        sys.modules.pop("huichuan.service", None)
        results = await search_knowledge("test")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_score_filtering(self, huichuan_mock):
        huichuan_mock.search.return_value = [
            {"title": "A", "score": 0.9, "summary": "a"},
            {"title": "B", "score": 0.6, "summary": "b"},
            {"title": "C", "score": 0.4, "summary": "c"},
            {"title": "D", "score": 0.7, "summary": "d"},
        ]
        results = await search_knowledge("test", min_score=0.5, max_results=2)
        assert len(results) == 2
        assert results[0]["title"] == "A"
        assert results[1]["title"] == "B"

    @pytest.mark.asyncio
    async def test_search_general_exception(self, huichuan_mock):
        huichuan_mock.search.side_effect = RuntimeError("connection refused")
        results = await search_knowledge("test")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_agent_category(self, huichuan_mock):
        """agent_category 非空时传给 huichuan_search"""
        huichuan_mock.search.return_value = [
            {"title": "Cat Result", "score": 0.9, "summary": "cat"},
        ]
        results = await search_knowledge("test", agent_category="biz:buyer")
        huichuan_mock.search.assert_called_once()
        kwargs = huichuan_mock.search.call_args.kwargs
        assert kwargs.get("agent_category") == "biz:buyer"
        assert len(results) == 1


class TestSearchSameCategoryExperience:
    @pytest.mark.asyncio
    async def test_no_category_found(self):
        """agent 无 category 时返回空列表"""
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = _mock_async_context_manager(mock_conn)

        with patch("zhice.huichuan_client.get_pool", AsyncMock(return_value=mock_pool)):
            results = await search_same_category_experience("unknown-agent", "test query")
        assert results == []

    @pytest.mark.asyncio
    async def test_with_category(self, huichuan_mock):
        """agent 有 category 时执行 search_knowledge"""
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value={"category": "biz:buyer"})

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = _mock_async_context_manager(mock_conn)

        huichuan_mock.search.return_value = [
            {"title": "同岗经验", "score": 0.8, "summary": "同 category 经验"},
        ]
        with patch("zhice.huichuan_client.get_pool", AsyncMock(return_value=mock_pool)):
            results = await search_same_category_experience("agent-1", "test query")
        assert len(results) == 1
        assert results[0]["title"] == "同岗经验"

    @pytest.mark.asyncio
    async def test_empty_category_field(self):
        """category 字段为空字符串时返回空列表"""
        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value={"category": ""})

        mock_pool = MagicMock()
        mock_pool.acquire.return_value = _mock_async_context_manager(mock_conn)

        with patch("zhice.huichuan_client.get_pool", AsyncMock(return_value=mock_pool)):
            results = await search_same_category_experience("agent-1", "test")
        assert results == []

    @pytest.mark.asyncio
    async def test_db_exception(self):
        """DB 异常时 gracefully 返回空列表"""
        mock_pool = MagicMock()
        mock_pool.acquire.side_effect = RuntimeError("DB gone")

        with patch("zhice.huichuan_client.get_pool", AsyncMock(return_value=mock_pool)):
            results = await search_same_category_experience("agent-1", "test")
        assert results == []
