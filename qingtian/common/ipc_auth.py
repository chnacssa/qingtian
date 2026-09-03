"""内部 IPC 免认证通道校验 — A1 (R11) 修复

原 loopback(127.0.0.1/::1) 无条件免认证已废弃：本机任意进程（含 untrusted Skill
子进程、其他容器）可 curl 127.0.0.1 冒充内部通道，绕过全部 token 认证。

改为显式令牌通道（fail-closed）：
  - 请求必须来自 loopback（127.0.0.1 / ::1）
  - 且携带 X-Internal-Token 头，与环境变量 QINGTIAN_INTERNAL_IPC_TOKEN 一致
未配置环境变量时，内部 IPC 免认证通道完全关闭，请求走正常 Bearer 认证。

用法:
    from common.ipc_auth import is_internal_ipc
    if is_internal_ipc(request):
        return "internal-ipc"
"""

import hmac
import os

from fastapi import Request


def is_internal_ipc(request: Request) -> bool:
    """校验是否为可信任的内部 IPC 请求（loopback + 内部令牌）。"""
    if not (request.client and request.client.host in ("127.0.0.1", "::1")):
        return False
    expected = os.environ.get("QINGTIAN_INTERNAL_IPC_TOKEN", "")
    if not expected:
        # 未配置内部令牌 → 免认证通道关闭
        return False
    provided = request.headers.get("X-Internal-Token", "")
    if not provided:
        return False
    # 纯 ASCII 走 compare_digest 防时序；非 ASCII 回退普通比较（避免异常）
    if provided.isascii() and expected.isascii():
        return hmac.compare_digest(provided, expected)
    return provided == expected
