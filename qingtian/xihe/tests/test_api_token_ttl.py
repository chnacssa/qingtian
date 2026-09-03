"""P2-12: _ParentIPCServer._get_admin_token — 1h TTL 过期刷新测试。"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xihe.agent_runtime import _ParentIPCServer


class _FakeResp:
    def __init__(self, status_code=200, token="tok-1"):
        self.status_code = status_code
        self._token = token

    def json(self):
        return {"token": self._token}


def _server() -> _ParentIPCServer:
    return _ParentIPCServer(agent_id="a1", trust_level="trusted")


def _client_with(resp) -> AsyncMock:
    """构造 AsyncMock 客户端：async with 返回自身，post 返回给定响应。"""
    mock_client = AsyncMock()
    mock_client.post.return_value = resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    return mock_client


@pytest.mark.asyncio
async def test_first_call_fetches_token():
    srv = _server()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _client_with(_FakeResp(200, "tok-abc"))
        MockClient.return_value = mock_client

        token = await srv._get_admin_token("http://x:1996")
        assert token == "tok-abc"
        assert mock_client.post.await_count == 1
        # 记录获取时间
        assert srv._api_token_at is not None


@pytest.mark.asyncio
async def test_cached_within_ttl_no_refetch():
    srv = _server()
    srv._api_token = "tok-cached"
    srv._api_token_at = time.time()

    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _client_with(_FakeResp(200, "unused"))
        MockClient.return_value = mock_client

        token = await srv._get_admin_token("http://x:1996")
        assert token == "tok-cached"
        # TTL 内不重新请求
        mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_refetch_after_ttl_expired():
    srv = _server()
    srv._api_token = "tok-old"
    srv._api_token_at = time.time() - 4000  # 已过期（>1h）

    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _client_with(_FakeResp(200, "tok-new"))
        MockClient.return_value = mock_client

        token = await srv._get_admin_token("http://x:1996")
        assert token == "tok-new"
        assert mock_client.post.await_count == 1


@pytest.mark.asyncio
async def test_failed_fetch_returns_empty_and_records_ts():
    """获取失败 → 返回空 token，但记录时间戳，下次仍会重试"""
    srv = _server()
    with patch("httpx.AsyncClient") as MockClient:
        mock_client = _client_with(_FakeResp(500, ""))
        MockClient.return_value = mock_client

        token = await srv._get_admin_token("http://x:1996")
        assert token == ""
        assert srv._api_token_at is not None
