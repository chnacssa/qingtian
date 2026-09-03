"""A6 (R11): IPC 代理路径白名单 — 单元测试

验证：trusted Skill 经代理仅能访问业务数据前缀；管理/凭据/授权类
端点一律被 _proxy_path_allowed 拒绝（黑名单优先，未命中白名单拒绝）。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from xihe.agent_runtime import _proxy_path_allowed


class TestProxyPathAllowed:
    """纯逻辑：路径放行判定"""

    def test_empty_or_non_slash_rejected(self):
        assert not _proxy_path_allowed("")
        assert not _proxy_path_allowed("v1/foo")
        assert not _proxy_path_allowed(None)

    def test_business_paths_allowed(self):
        """实际业务前缀放行"""
        assert _proxy_path_allowed("/v1/huanyu/messages")
        assert _proxy_path_allowed("/v1/huanyu/admin-messages")
        assert _proxy_path_allowed("/v1/huanyu/reminders/pending")
        assert _proxy_path_allowed("/v1/yongheng/memories/search")
        assert _proxy_path_allowed("/v1/yongheng/profile")
        assert _proxy_path_allowed("/v1/yongheng/session/recover")  # 找回记忆（recover 只读本 agent 记忆）
        assert _proxy_path_allowed("/v1/huichuan/search")
        assert _proxy_path_allowed("/v1/zhice/tasks")
        assert _proxy_path_allowed("/v1/zhenyue/audit/logs")

    def test_admin_paths_rejected(self):
        """凭据/Agent 管理/审计管理端点一律拒绝"""
        assert not _proxy_path_allowed("/v1/auth/token")
        assert not _proxy_path_allowed("/v1/zhenyue/tokens")
        assert not _proxy_path_allowed("/v1/zhenyue/break-glass")
        assert not _proxy_path_allowed("/v1/xihe/agents")
        assert not _proxy_path_allowed("/v1/skills/admin")
        assert not _proxy_path_allowed("/v1/license/check")
        assert not _proxy_path_allowed("/peers/enterprise")

    def test_yongheng_token_creation_rejected(self):
        """yongheng 发令牌端点虽在业务域内，但属凭据操作必须拒绝"""
        assert not _proxy_path_allowed("/v1/yongheng/token/create")

    # ── P1 (2026-08-27 review #6): dot-segment 穿越防护 ──────────────

    def test_dot_segment_traversal_to_deny_list_rejected(self):
        """../ 穿越（httpx 发送前 RFC3986 归一化）不得绕过黑名单。

        实测 httpx.Request 对 "/v1/yongheng/profile/../../../v1/zhenyue/tokens"
        实发路径为 "/v1/zhenyue/tokens" —— 判定必须按归一化后语义执行。
        """
        assert not _proxy_path_allowed("/v1/yongheng/profile/../../../v1/zhenyue/tokens")
        assert not _proxy_path_allowed("/v1/huanyu/messages/../../v1/auth/token")
        assert not _proxy_path_allowed("/v1/huichuan/../../../v1/license/check")

    def test_dot_segment_traversal_cannot_smuggle_allow(self):
        """穿越形态（归一化后离开白名单前缀）一律拒绝，不得借白名单前缀走私"""
        assert not _proxy_path_allowed("/v1/yongheng/profile/../../v1/huanyu/messages")
        assert not _proxy_path_allowed("/v1/huanyu/messages/../../../etc/passwd")

    def test_encoded_dot_segment_fail_closed(self):
        """%2e%2e 编码形态 httpx 原样发送（不归一化）——不在任何白名单前缀，拒绝"""
        assert not _proxy_path_allowed("/v1/yongheng/profile/%2e%2e/%2e%2e/v1/zhenyue/tokens")

    def test_trailing_slash_and_normal_prefix_unchanged(self):
        """正常路径与尾斜杠归一化后语义不变，不误伤合法请求"""
        assert _proxy_path_allowed("/v1/huanyu/messages")
        assert _proxy_path_allowed("/v1/huanyu/messages/")
        assert _proxy_path_allowed("/v1/huichuan/files/list")
        assert not _proxy_path_allowed("/v1/yongheng/token/revoke")

    # ── 2026-08-27: zhice steps 覆盖面补齐（work_secretary.zhice_bridge 步骤提交被拦实锤）──

    def test_zhice_steps_lifecycle_allowed(self):
        """SOP 步骤生命周期端点放行（start/heartbeat/submit/review…业务数据操作）"""
        assert _proxy_path_allowed("/v1/zhice/steps/step-01/submit")
        assert _proxy_path_allowed("/v1/zhice/steps/step-01/start")
        assert _proxy_path_allowed("/v1/zhice/steps/step-01/heartbeat")
        assert _proxy_path_allowed("/v1/zhice/steps/step-01/review")

    def test_zhice_admin_endpoints_denied(self):
        """zhice admin 门端点（policies/workflows cleanup）显式拉黑——代理转发带
        admin Bearer，handler 内 role 检查之外 deny 优先双保险"""
        assert not _proxy_path_allowed("/v1/zhice/policies")
        assert not _proxy_path_allowed("/v1/zhice/policies/p-1")
        assert not _proxy_path_allowed("/v1/zhice/workflows/cleanup")
        # workflows 管理面未放行（默认拒绝）
        assert not _proxy_path_allowed("/v1/zhice/workflows")
        assert not _proxy_path_allowed("/v1/zhice/workflows/w-1/refine")

    def test_zhice_tasks_prefix_unchanged(self):
        """tasks 前缀行为不变（含子路径生命周期操作）"""
        assert _proxy_path_allowed("/v1/zhice/tasks")
        assert _proxy_path_allowed("/v1/zhice/tasks/t-1/cancel")
        assert not _proxy_path_allowed("/v1/zhice/unknown")

    def test_agent_management_rejected(self):
        """huanyu Agent 管理端点拒绝"""
        assert not _proxy_path_allowed("/v1/huanyu/agents")
        assert not _proxy_path_allowed("/v1/huanyu/agents/agent-1/runtime")
        assert not _proxy_path_allowed("/v1/huanyu/runtime/agents")

    def test_unknown_path_rejected_fail_closed(self):
        """未命中白名单一律拒绝（fail-closed）"""
        assert not _proxy_path_allowed("/v1/unknown/x")
        assert not _proxy_path_allowed("/v1/zhenyue/config")
        assert not _proxy_path_allowed("/v1/huanyu")
        assert not _proxy_path_allowed("/v1/huanyu/")


class TestProxyWhitelistIntegration:
    """代理分支：白名单拦截返回错误响应，不触达底座"""

    @pytest.mark.asyncio
    async def test_denied_path_sends_error_response(self):
        """管理端点请求 → 返回 -32601 拦截错误，不发起 httpx 调用"""
        from common.ipc import Request, Response
        from xihe.agent_runtime import _ParentIPCServer

        sent = []

        class _FakeTransport:
            def __init__(self):
                self._calls = 0

            async def receive(self):
                self._calls += 1
                if self._calls == 1:
                    return Request(
                        id="req-1",
                        method="api.post",
                        params={"path": "/v1/zhenyue/tokens", "body": {}},
                    )
                raise EOFError()

            async def send(self, msg):
                sent.append(msg)

        srv = _ParentIPCServer(agent_id="a1", trust_level="trusted")
        srv._transport = _FakeTransport()
        await srv._receive_loop()

        assert sent, "应产生拦截响应"
        assert isinstance(sent[0], Response)
        assert sent[0].id == "req-1"
        assert sent[0].error is not None
        assert sent[0].error["code"] == -32601

    @pytest.mark.asyncio
    async def test_allowed_path_does_not_hit_whitelist_deny(self):
        """业务路径未被白名单拦截（httpx 会尝试调用，用 mock 验证放行）"""
        import httpx
        from common.ipc import Request
        from xihe.agent_runtime import _ParentIPCServer

        sent = []

        class _FakeTransport:
            def __init__(self):
                self._calls = 0

            async def receive(self):
                self._calls += 1
                if self._calls == 1:
                    return Request(
                        id="req-2",
                        method="api.get",
                        params={"path": "/v1/yongheng/profile", "params": {}},
                    )
                raise EOFError()

            async def send(self, msg):
                sent.append(msg)

        srv = _ParentIPCServer(agent_id="a1", trust_level="trusted")
        srv._transport = _FakeTransport()

        with patch("xihe.agent_runtime._ParentIPCServer._get_admin_token",
                   new=AsyncMock(return_value="tok")) as gt:
            with patch("httpx.AsyncClient") as MockClient:
                mock_client = AsyncMock()
                mock_client.get.return_value = MagicMock(
                    status_code=200,
                    text="{}",
                    json=lambda: {},
                )
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                MockClient.return_value = mock_client
                await srv._receive_loop()

        # 白名单放行 → 走了 httpx 调用
        assert mock_client.get.await_count == 1
