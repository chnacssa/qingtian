"""IPC JSON-RPC 2.0 协议编解码

子进程与父进程通过 STDIN/STDOUT 管道通信，每条消息为一行 JSON。
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .errors import IPCError, InvalidRequestError


def make_id() -> str:
    """生成唯一请求 ID"""
    return uuid.uuid4().hex[:16]


@dataclass
class Request:
    """JSON-RPC 2.0 请求 — id 为空时表示通知（无需响应）"""
    method: str
    params: dict = field(default_factory=dict)
    id: str = ""              # 空字符串 = notification
    jsonrpc: str = "2.0"

    def is_notification(self) -> bool:
        return not self.id


@dataclass
class Response:
    """JSON-RPC 2.0 响应"""
    id: str
    result: Any = None
    error: dict | None = None
    jsonrpc: str = "2.0"

    def is_success(self) -> bool:
        """响应是否成功（无 error）"""
        return self.error is None

    def raise_for_error(self) -> None:
        """如果响应包含 error 则抛出 IPCError"""
        if self.error is not None:
            raise IPCError(
                self.error.get("message", "IPC error"),
                data=self.error.get("data"),
            )


def encode(msg) -> str:
    """将消息编码为 JSON 字符串 — 通知不包含 id 字段"""
    if isinstance(msg, Request):
        obj = {"jsonrpc": msg.jsonrpc, "method": msg.method}
        if msg.id:
            obj["id"] = msg.id
        if msg.params:
            obj["params"] = msg.params
    elif isinstance(msg, Response):
        obj = {"jsonrpc": msg.jsonrpc, "id": msg.id}
        if msg.error:
            obj["error"] = msg.error
        else:
            obj["result"] = msg.result
    elif hasattr(msg, "to_dict"):
        obj = msg.to_dict()
    else:
        obj = msg
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def decode(text: str):
    """将 JSON 字符串解码为 Request / Response

    对 JSON-RPC 2.0 消息做严格校验：
      - 无效 JSON → InvalidRequestError
      - 缺少 jsonrpc 或版本不为 "2.0" → InvalidRequestError
      - method 为空字符串 → InvalidRequestError
      - Response 缺少 id → InvalidRequestError
    """
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidRequestError(f"Invalid JSON: {e}") from e

    if not isinstance(obj, dict):
        return obj

    jsonrpc = obj.get("jsonrpc")
    if jsonrpc is None:
        raise InvalidRequestError("Missing 'jsonrpc' field")
    if jsonrpc != "2.0":
        raise InvalidRequestError(f"Invalid jsonrpc version: {jsonrpc}")

    if "method" in obj:
        method = obj.get("method", "")
        if not method:
            raise InvalidRequestError("Method must not be empty")
        return Request(
            id=obj.get("id", ""), method=method,
            params=obj.get("params", {}), jsonrpc=jsonrpc,
        )
    elif "result" in obj or "error" in obj:
        if "id" not in obj:
            raise InvalidRequestError("Response missing 'id' field")
        return Response(
            id=obj["id"], result=obj.get("result"),
            error=obj.get("error"), jsonrpc=jsonrpc,
        )
    return obj
