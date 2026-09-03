"""G4 MCP 客户端单测（实施文档 §九 test_mcp_client）

httpx.MockTransport 注入模拟 server：list_tools / call_tool 归一化 /
协议错误与 HTTP 错误 → McpError。
"""

import json

import httpx
import pytest

from huichuan.mcp_client import McpClient, McpError


def _make_client(handler):
    """构造注入 MockTransport 的 McpClient。"""
    transport = httpx.MockTransport(handler)
    return McpClient("http://mcp.example.com/mcp",
                     client=httpx.AsyncClient(transport=transport))


# ── G4-1: list_tools ──


@pytest.mark.asyncio
async def test_list_tools():
    async def handler(request):
        payload = json.loads(request.content)
        assert payload["method"] == "tools/list"
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": payload["id"],
            "result": {"tools": [
                {"name": "search", "description": "定额搜索",
                 "inputSchema": {"type": "object"}},
            ]}})

    client = _make_client(handler)
    try:
        tools = await client.list_tools()
    finally:
        await client.close()
    assert tools[0]["name"] == "search"
    assert tools[0]["inputSchema"]["type"] == "object"


# ── G4-2: call_tool 归一化 ──


@pytest.mark.asyncio
async def test_call_tool_text_content_normalized():
    async def handler(request):
        payload = json.loads(request.content)
        assert payload["method"] == "tools/call"
        assert payload["params"]["name"] == "search"
        assert payload["params"]["arguments"] == {"q": "定额"}
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": payload["id"],
            "result": {"content": [{"type": "text", "text": "找到 2 条"},
                                   {"type": "text", "text": "——完毕"}]}})

    client = _make_client(handler)
    try:
        out = await client.call_tool("search", {"q": "定额"})
    finally:
        await client.close()
    assert out == "找到 2 条\n——完毕"


@pytest.mark.asyncio
async def test_call_tool_json_content_serialized():
    async def handler(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": payload["id"],
            "result": {"content": [{"type": "json", "json": {"price": 100}}]}})

    client = _make_client(handler)
    try:
        out = await client.call_tool("x", {})
    finally:
        await client.close()
    assert json.loads(out) == {"price": 100}


# ── G4-3: 错误收敛为 McpError ──


@pytest.mark.asyncio
async def test_rpc_error_raises_mcp_error():
    async def handler(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": payload["id"],
            "error": {"code": -32601, "message": "method not found"}})

    client = _make_client(handler)
    try:
        with pytest.raises(McpError):
            await client.list_tools()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_http_error_raises_mcp_error():
    async def handler(request):
        return httpx.Response(500, text="internal error")

    client = _make_client(handler)
    try:
        with pytest.raises(McpError):
            await client.list_tools()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_initialize_handshake():
    async def handler(request):
        payload = json.loads(request.content)
        assert payload["method"] == "initialize"
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": payload["id"],
            "result": {"protocolVersion": "2024-11-05"}})

    client = _make_client(handler)
    try:
        r = await client.initialize()
    finally:
        await client.close()
    assert r["protocolVersion"] == "2024-11-05"
