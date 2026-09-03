"""
Adapter 异常层次
"""


class AdapterError(Exception):
    """所有 Adapter 异常的基类"""


class AdapterAuthFailed(AdapterError):
    """认证失败 — 凭据存在但无效。

    与"无凭据"不同。抛出此异常会阻止认证链继续尝试后续适配器。
    适用于：token 过期、HMAC 签名不匹配、凭据格式错误。
    """


class AdapterConfigError(AdapterError):
    """适配器配置无效（启动时检查，非运行时）"""


class AdapterConnectionError(AdapterError):
    """无法连接到 Agent 框架"""
