"""IPC 层自定义异常

JSON-RPC 2.0 标准错误码：
  -32700   Parse error
  -32600   Invalid Request
  -32601   Method not found
  -32602   Invalid params
  -32603   Internal error
  -32000..-32099  Server error (reserved)
"""


class IPCError(Exception):
    """IPC 通用异常基类"""
    code: int = -32000
    message: str = "IPC error"

    def __init__(self, message: str | None = None, data: object = None):
        self.message = message or self.message
        self.data = data
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "data": self.data,
        }


class ParseError(IPCError):
    """无效 JSON"""
    code = -32700
    message = "Parse error"


class InvalidRequestError(IPCError):
    """请求结构无效（缺少 jsonrpc/method/id 等必需字段）"""
    code = -32600
    message = "Invalid Request"


class MethodNotFoundError(IPCError):
    """请求的方法不存在"""
    code = -32601
    message = "Method not found"

    def __init__(self, method: str):
        super().__init__(f"Method not found: {method}")
        self.data = {"method": method}


class InvalidParamsError(IPCError):
    """请求参数无效"""
    code = -32602
    message = "Invalid params"


class InternalError(IPCError):
    """服务端内部错误"""
    code = -32603
    message = "Internal error"


class TimeoutError(IPCError):
    """请求超时"""
    code = -32000
    message = "Request timeout"


class ConnectionClosedError(IPCError):
    """连接已关闭"""
    code = -32001
    message = "Connection closed"


class MethodCallError(IPCError):
    """方法调用抛出的异常"""
    code = -32002
    message = "Method call error"

    def __init__(self, method: str, original: Exception):
        super().__init__(f"Error calling '{method}': {original}")
        self.data = {"method": method, "original": str(original)}
