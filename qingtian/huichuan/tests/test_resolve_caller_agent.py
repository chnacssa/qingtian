"""汇川 _resolve_caller_agent 身份解析测试（P1-1，无 DB 依赖）。

P1-1 背景：IPC 代理（trusted Skill）以 admin Bearer token 认证、用 X-Agent-ID 头
透传真实用户身份。若 admin 短路在 X-Agent-ID 之前，private 经验检索会按 admin
身份过滤（工作秘书沉淀到汇川后"随时可查"断裂）。修复：admin 凭据 + X-Agent-ID
并存时优先 X-Agent-ID；agent 自身 Bearer 仍优先不受影响；纯 admin 仍返回 admin。
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from huichuan.api import _resolve_caller_agent


def _make_request(headers: dict, query_string: str = "") -> Request:
    hdrs = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/huichuan/search",
        "raw_path": b"/v1/huichuan/search",
        "query_string": query_string.encode(),
        "root_path": "",
        "headers": hdrs,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 1996),
    }
    return Request(scope)


def _patched_auth(fake_auth) -> ExitStack:
    """组合 patch：get_pool 返回假连接池 + authenticate 替换，绕开真实 DB。"""
    pool = MagicMock()
    acquire_cm = AsyncMock()  # pool.acquire() 返回的异步上下文管理器
    pool.acquire.return_value = acquire_cm
    stack = ExitStack()
    stack.enter_context(patch("huichuan.api.get_pool", new=AsyncMock(return_value=pool)))
    stack.enter_context(patch("huichuan.api.authenticate", new=AsyncMock(side_effect=fake_auth)))
    return stack


async def _fake_auth_admin(conn, token):
    return {"agent_id": "admin", "role": "admin", "capabilities": []}


async def _fake_auth_agent(conn, token):
    return {"agent_id": "agent-real", "role": "agent", "capabilities": []}


@pytest.mark.asyncio
async def test_admin_bearer_plus_x_agent_id_prefers_x_agent_id():
    """IPC 代理：admin Bearer + X-Agent-ID → 解析为 X-Agent-ID（P1-1 核心）。"""
    req = _make_request({
        "Authorization": "Bearer admin-token",
        "X-Agent-ID": "agent-ws-user",
    })
    with _patched_auth(_fake_auth_admin):
        assert await _resolve_caller_agent(req) == "agent-ws-user"


@pytest.mark.asyncio
async def test_admin_bearer_without_x_agent_id_returns_admin():
    """纯 admin 调用（门户管理端）无 X-Agent-ID → 仍返回 admin。"""
    req = _make_request({"Authorization": "Bearer admin-token"})
    with _patched_auth(_fake_auth_admin):
        assert await _resolve_caller_agent(req) == "admin"


@pytest.mark.asyncio
async def test_agent_bearer_keeps_priority():
    """agent 自身 Bearer → 保持优先，不受 X-Agent-ID 影响。"""
    req = _make_request({
        "Authorization": "Bearer agent-token",
        "X-Agent-ID": "other-agent",
    })
    with _patched_auth(_fake_auth_agent):
        assert await _resolve_caller_agent(req) == "agent-real"


@pytest.mark.asyncio
async def test_no_bearer_uses_x_agent_id():
    """无 Bearer → X-Agent-ID header。"""
    req = _make_request({"X-Agent-ID": "agent-ws-user"})
    with _patched_auth(None):
        assert await _resolve_caller_agent(req) == "agent-ws-user"


@pytest.mark.asyncio
async def test_no_credentials_falls_back_to_query_param():
    """无 Bearer 无 header → query param 兼容路径。"""
    req = _make_request({}, query_string="agent_id=agent-query")
    with _patched_auth(None):
        assert await _resolve_caller_agent(req) == "agent-query"
