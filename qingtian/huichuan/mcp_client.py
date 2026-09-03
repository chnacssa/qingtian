"""G4 MCP 客户端 — 消费外部 MCP server 工具集（设计文档 §11.7）

手写 JSON-RPC 2.0 over HTTP（环境无 mcp 第三方库）。
与 huichuan/mcp.py 的 FastMCP server 互补：server 对外暴露，本类消费外部。

用法:
    async with McpClient("http://localhost:9100/mcp") as mcp:
        tools = await mcp.list_tools()      # [{name, description, inputSchema}]
        out = await mcp.call_tool("search", {"q": "定额"})

所有协议/传输失败统一收敛为 McpError（不泄漏原始 httpx/解析异常）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("huichuan.mcp_client")


class McpError(Exception):
    """MCP 协议/传输失败（统一异常类型，供调用方单一捕获）。"""


class McpClient:
    """MCP client（JSON-RPC 2.0 over HTTP）。

    Args:
        base_url: 外部 MCP server 端点（如 http://host:port/mcp）
        transport: 传输方式标记（当前仅 http；保留供未来 sse/streamable 扩展）
        timeout: HTTP 超时秒数
        client: 可选 httpx.AsyncClient（单测注入 MockTransport 用）
    """

    def __init__(self, base_url: str, transport: str = "http", timeout: int = 15,
                 client: httpx.AsyncClient | None = None):
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._request_id = 0

    async def close(self):
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        """JSON-RPC 2.0 单次请求。返回 result；协议/传输失败抛 McpError。"""
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id,
                "method": method, "params": params or {}}
        try:
            resp = await self._client.post(self._base_url, json=body)
        except httpx.HTTPError as e:
            raise McpError(f"MCP 传输失败（{method}）: {e}") from e
        if not 200 <= resp.status_code < 300:
            raise McpError(f"MCP HTTP {resp.status_code}（{method}）: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise McpError(f"MCP 响应非 JSON（{method}）: {resp.text[:200]}") from e
        if not isinstance(data, dict):
            raise McpError(f"MCP 响应结构非法（{method}）")
        if data.get("error") is not None:
            err = data["error"]
            code = err.get("code") if isinstance(err, dict) else None
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            raise McpError(f"MCP 错误 {code}（{method}）: {msg}")
        return data.get("result")

    async def initialize(self) -> dict:
        """MCP 握手（可选调用，确认协议版本与能力）。"""
        result = await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "qingtian-huichuan", "version": "0.1"},
        })
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> list[dict]:
        """tools/list → [{name, description, inputSchema}]。"""
        result = await self._rpc("tools/list")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise McpError("MCP tools/list 返回缺少 tools 列表")
        return tools

    async def call_tool(self, name: str, arguments: dict) -> Any:
        """tools/call → 归一化结果。

        content 数组取 text / json 项拼成字符串（LLM 友好）；
        无 content 时回退返回整个 result。
        """
        result = await self._rpc("tools/call",
                                 {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            return result
        content = result.get("content", [])
        texts: list[str] = []
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                texts.append(str(item["text"]))
            elif item.get("type") == "json" and "json" in item:
                texts.append(json.dumps(item["json"], ensure_ascii=False))
        if texts:
            return "\n".join(texts)
        return result.get("result") if "result" in result else result
