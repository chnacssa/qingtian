"""R11 P1 zhenyue API 层安全修复回归测试

覆盖：
  1. create_audit_entry 伪造 — 非平台调用方强制绑定认证身份
  2. key_service 私钥路由 — 仅本人 / 平台可读，他人 403
  3. init_break_glass — 断网应急令牌初始化（此前函数不存在，功能整体缺失）
  4. tool_rules — admin 角色门（auth_dependency 携带 role）
"""
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zhenyue.models import AuditEntryRequest


def _pool(conn=None):
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn or AsyncMock())
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire.return_value = ctx
    return pool


class TestCreateAuditEntryForgery:
    """P1 (R11): 审计归属可伪造 —— 非平台调用方不能用请求体 agent_id"""

    @pytest.mark.asyncio
    async def test_regular_agent_identity_forced(self):
        """普通 agent token → agent_id 强制为认证身份，忽略请求体伪造值"""
        req = AuditEntryRequest(agent_id="victim-agent", action="delete_file", severity="high")

        with (
            patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool())),
            patch("zhenyue.api.write_audit", new_callable=AsyncMock) as mock_write,
        ):
            mock_write.return_value = {
                "audit_uid": "uid-1", "created_at": "2026-08-16T00:00:00+00:00",
                "agent_id": "real-agent", "action": "delete_file", "severity": "high",
                "hash": "hash1",
            }
            from zhenyue.api import create_audit_entry
            result = await create_audit_entry(req, {
                "agent_id": "real-agent", "authenticated": True, "role": "agent", "capabilities": [],
            })

        # 落库的数据必须是认证身份，而非请求体伪造的 victim-agent
        written = mock_write.call_args.args[1]
        assert written["agent_id"] == "real-agent"
        assert written["agent_id"] != "victim-agent"
        assert result.agent_id == "real-agent"

    @pytest.mark.asyncio
    async def test_platform_caller_can_write_service_context(self):
        """平台（admin / internal-ipc）→ 保留请求体 agent_id（服务身份写审计）"""
        req = AuditEntryRequest(agent_id="service-x", action="cleanup", severity="low")

        with (
            patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool())),
            patch("zhenyue.api.write_audit", new_callable=AsyncMock) as mock_write,
        ):
            mock_write.return_value = {
                "audit_uid": "uid-2", "created_at": "2026-08-16T00:00:00+00:00",
                "agent_id": "service-x", "action": "cleanup", "severity": "low",
                "hash": "hash2",
            }
            from zhenyue.api import create_audit_entry
            result = await create_audit_entry(req, {
                "agent_id": "internal-ipc", "authenticated": True, "role": "admin", "capabilities": ["admin"],
            })

        assert mock_write.call_args.args[1]["agent_id"] == "service-x"
        assert result.agent_id == "service-x"


class TestGetPrivateKeyRoute:
    """P1 (R11): key_service.get_private_key 无路由 → Agent 无法取私钥；私钥须鉴权"""

    @pytest.mark.asyncio
    async def test_self_access_allowed(self):
        """Agent 本人 → 返回私钥"""
        with (
            patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool())),
            patch("zhenyue.api.key_service.get_private_key", new_callable=AsyncMock,
                  return_value="ab" * 32),
        ):
            from zhenyue.api import get_agent_private_key
            result = await get_agent_private_key("agent-1", {
                "agent_id": "agent-1", "authenticated": True, "role": "agent", "capabilities": [],
            })
        assert result["private_key"] == "ab" * 32

    @pytest.mark.asyncio
    async def test_other_agent_forbidden(self):
        """冒名访问他人私钥 → 403"""
        from fastapi import HTTPException
        with patch("zhenyue.api.get_pool", AsyncMock()):
            from zhenyue.api import get_agent_private_key
            with pytest.raises(HTTPException) as exc:
                await get_agent_private_key("victim", {
                    "agent_id": "attacker", "authenticated": True, "role": "agent", "capabilities": [],
                })
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_access_allowed(self):
        """平台管理员 → 可读取（运维/签出场景）"""
        with (
            patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool())),
            patch("zhenyue.api.key_service.get_private_key", new_callable=AsyncMock,
                  return_value="cd" * 32),
        ):
            from zhenyue.api import get_agent_private_key
            result = await get_agent_private_key("agent-1", {
                "agent_id": "admin", "authenticated": True, "role": "admin", "capabilities": ["admin"],
            })
        assert result["private_key"] == "cd" * 32


class TestInitBreakGlass:
    """P1 (R11): init_break_glass 此前不存在 → ImportError 被吞，应急破窗缺失"""

    def test_creates_token_file_when_missing(self, tmp_path):
        token_path = tmp_path / "break_glass.token"
        with (
            patch("zhenyue.api.zcfg.get_break_glass_enabled", return_value=True),
            patch("zhenyue.api.zcfg.get_break_glass_token_path", return_value=str(token_path)),
        ):
            from zhenyue.api import init_break_glass
            init_break_glass()

        assert token_path.exists()
        token = token_path.read_text()
        assert len(token) >= 32

    def test_skips_when_disabled(self, tmp_path):
        token_path = tmp_path / "break_glass.token"
        with (
            patch("zhenyue.api.zcfg.get_break_glass_enabled", return_value=False),
            patch("zhenyue.api.zcfg.get_break_glass_token_path", return_value=str(token_path)),
        ):
            from zhenyue.api import init_break_glass
            init_break_glass()
        assert not token_path.exists()

    def test_preserves_existing_token(self, tmp_path):
        token_path = tmp_path / "break_glass.token"
        token_path.write_text("existing-token")
        with (
            patch("zhenyue.api.zcfg.get_break_glass_enabled", return_value=True),
            patch("zhenyue.api.zcfg.get_break_glass_token_path", return_value=str(token_path)),
        ):
            from zhenyue.api import init_break_glass
            init_break_glass()
        assert token_path.read_text() == "existing-token"


class TestToolRulesRoleGate:
    """P1 (R11): 工具规则管理 auth 无 role → 恒 403；auth_dependency 携带 role 后按角色放行"""

    @pytest.mark.asyncio
    async def test_regular_agent_forbidden(self):
        from fastapi.responses import JSONResponse
        from zhenyue.tool_rules_api import create_rule
        from zhenyue.models import ToolRuleRequest
        req = ToolRuleRequest(tool="ls", match="rm -rf", severity="block")
        resp = await create_rule(req, {"agent_id": "agent-1", "role": "agent", "capabilities": []})
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_allowed(self):
        from zhenyue import tool_rules_api
        from zhenyue.models import ToolRuleRequest
        req = ToolRuleRequest(tool="ls", match="rm -rf", severity="block")
        before = set(tool_rules_api._rules_cache.keys())
        try:
            with patch("zhenyue.tool_rules_api._save_rules"):
                resp = await tool_rules_api.create_rule(
                    req, {"agent_id": "admin", "role": "admin", "capabilities": ["admin"]},
                )
            assert isinstance(resp, dict)
            assert resp["tool"] == "ls"
        finally:
            # 清理缓存副作用
            for rid in set(tool_rules_api._rules_cache.keys()) - before:
                del tool_rules_api._rules_cache[rid]


# ── P2 (R11): scheduler start/stop 未 await ─────────────────

class TestSchedulerControlAwait:
    """P2 (R11): start/stop 为 async，调用处此前未 await → 启停无操作"""

    @pytest.mark.asyncio
    async def test_start_is_awaited(self):
        from zhenyue.api import scheduler_start
        with patch("zhenyue.api.start_scheduler",
                   new=AsyncMock(return_value=None)) as start_mock:
            resp = await scheduler_start(_admin="admin:console")
        start_mock.assert_awaited_once()
        assert resp["status"] == "ok"

    @pytest.mark.asyncio
    async def test_stop_is_awaited(self):
        from zhenyue.api import scheduler_stop
        with patch("zhenyue.api.stop_scheduler",
                   new=AsyncMock(return_value=None)) as stop_mock:
            resp = await scheduler_stop(_admin="admin:console")
        stop_mock.assert_awaited_once()
        assert resp["status"] == "ok"


# ── P2 (R11): /tokens/verify 防枚举 oracle ─────────────────

def _verify_request(headers: dict | None = None,
                    client: tuple = ("1.2.3.4", 9999)):
    from starlette.requests import Request
    hdrs = [(k.encode(), str(v).encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "method": "POST", "path": "/v1/zhenyue/tokens/verify",
        "headers": hdrs, "query_string": b"", "scheme": "http",
        "server": ("test", 80), "client": client,
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class TestTokenVerifyOracleGuard:
    """P2 (R11): /tokens/verify 无鉴权 → IP 限流 + 已认证仅可验证自己的 token"""

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_enumeration(self):
        """超过 IP 限流 → 429，拒绝继续枚举"""
        from fastapi import HTTPException
        from zhenyue.api import verify_token
        from zhenyue.models import ValidateTokenRequest
        with patch("zhenyue.api.rate_limiter.check",
                   new=AsyncMock(return_value=False)):
            req = _verify_request()
            with pytest.raises(HTTPException) as exc:
                await verify_token(ValidateTokenRequest(token="guess"), req)
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_authenticated_agent_cannot_verify_other_token(self, mock_conn):
        """已认证普通 agent 只能验证自己的 token → 验证他人 403"""
        from fastapi import HTTPException
        from zhenyue.api import verify_token
        from zhenyue.models import ValidateTokenRequest
        mock_conn.fetchrow.return_value = {
            "agent_id": "victim", "role": "agent", "expires_at": None, "revoked": False,
        }
        with patch("zhenyue.api.rate_limiter.check", new=AsyncMock(return_value=True)), \
             patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool(mock_conn))), \
             patch("zhenyue.api.auth_dependency", new=AsyncMock(
                 return_value={"agent_id": "attacker", "role": "agent", "capabilities": []})):
            req = _verify_request(headers={"Authorization": "Bearer attacker-token"})
            with pytest.raises(HTTPException) as exc:
                await verify_token(ValidateTokenRequest(token="victim-token"), req)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_authenticated_agent_can_verify_own_token(self, mock_conn):
        """已认证普通 agent 验证自己的 token → 放行"""
        from zhenyue.api import verify_token
        from zhenyue.models import ValidateTokenRequest
        mock_conn.fetchrow.return_value = {
            "agent_id": "me", "role": "agent", "expires_at": None, "revoked": False,
        }
        with patch("zhenyue.api.rate_limiter.check", new=AsyncMock(return_value=True)), \
             patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool(mock_conn))), \
             patch("zhenyue.api.auth_dependency", new=AsyncMock(
                 return_value={"agent_id": "me", "role": "agent", "capabilities": []})):
            req = _verify_request(headers={"Authorization": "Bearer my-token"})
            resp = await verify_token(ValidateTokenRequest(token="my-token"), req)
        assert resp.valid is True
        assert resp.agent_id == "me"

    @pytest.mark.asyncio
    async def test_unauthenticated_caller_still_allowed_for_portal(self, mock_conn):
        """未认证（门户登录校验用户 token）仍可用，仅受 IP 限流约束"""
        from fastapi import HTTPException
        from zhenyue.api import verify_token
        from zhenyue.models import ValidateTokenRequest
        mock_conn.fetchrow.return_value = {
            "agent_id": "me", "role": "agent", "expires_at": None, "revoked": False,
        }
        with patch("zhenyue.api.rate_limiter.check", new=AsyncMock(return_value=True)), \
             patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool(mock_conn))), \
             patch("zhenyue.api.auth_dependency",
                   new=AsyncMock(side_effect=HTTPException(401, detail="no auth"))):
            resp = await verify_token(ValidateTokenRequest(token="my-token"), _verify_request())
        assert resp.valid is True
        assert resp.agent_id == "me"


# ── P2 (R11): get_audit_entry detail_enc 受控解密 ─────────

_FAKE_UID = "12345678-1234-1234-1234-123456789012"


class TestGetAuditEntryDecrypt:
    """P2 (R11): 审计详情原样返回 detail_enc 密文无解密出口 → 受控解密"""

    @pytest.mark.asyncio
    async def test_admin_gets_plaintext_detail(self, mock_conn):
        from zhenyue.api import get_audit_entry
        mock_conn.fetchrow.return_value = {
            "audit_uid": _FAKE_UID, "agent_id": "a1", "action": "delete_file",
            "detail_enc": "encrypted-blob", "hash": "h1",
        }
        with patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool(mock_conn))), \
             patch("zhenyue.api.encryptor.decrypt",
                   return_value={"reason": "违规操作"}) as dec:
            entry = await get_audit_entry(
                _FAKE_UID, None,
                {"agent_id": "admin:console", "role": "admin", "capabilities": ["admin"]},
            )
        assert entry["detail"] == {"reason": "违规操作"}
        assert "detail_enc" not in entry
        dec.assert_called_once_with("encrypted-blob")

    @pytest.mark.asyncio
    async def test_owner_agent_can_read(self, mock_conn):
        """本 agent 本人可读自己的审计详情（含明文 detail）"""
        from zhenyue.api import get_audit_entry
        mock_conn.fetchrow.return_value = {
            "audit_uid": _FAKE_UID, "agent_id": "me", "action": "chat",
            "detail_enc": "enc-blob", "hash": "h2",
        }
        with patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool(mock_conn))), \
             patch("zhenyue.api.encryptor.decrypt", return_value={"k": "v"}):
            entry = await get_audit_entry(
                _FAKE_UID, None,
                {"agent_id": "me", "role": "agent", "capabilities": []},
            )
        assert entry["detail"] == {"k": "v"}
        assert "detail_enc" not in entry

    @pytest.mark.asyncio
    async def test_non_owner_agent_forbidden(self, mock_conn):
        """他人 agent → 403，不可读对方审计详情"""
        from fastapi import HTTPException
        from zhenyue.api import get_audit_entry
        mock_conn.fetchrow.return_value = {
            "audit_uid": _FAKE_UID, "agent_id": "victim", "action": "delete_file",
            "detail_enc": "enc-blob", "hash": "h3",
        }
        with patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool(mock_conn))):
            with pytest.raises(HTTPException) as exc:
                await get_audit_entry(
                    _FAKE_UID, None,
                    {"agent_id": "attacker", "role": "agent", "capabilities": []},
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_decrypt_failure_returns_sanitized_view(self, mock_conn):
        """解密失败（密钥轮换等）不 500，回退脱敏视图 detail=None"""
        from zhenyue.api import get_audit_entry
        mock_conn.fetchrow.return_value = {
            "audit_uid": _FAKE_UID, "agent_id": "me", "action": "chat",
            "detail_enc": "old-enc-blob", "hash": "h4",
        }
        with patch("zhenyue.api.get_pool", AsyncMock(return_value=_pool(mock_conn))), \
             patch("zhenyue.api.encryptor.decrypt",
                   side_effect=ValueError("Cannot decrypt")):
            entry = await get_audit_entry(
                _FAKE_UID, None,
                {"agent_id": "me", "role": "agent", "capabilities": []},
            )
        assert entry["detail"] is None
        assert "detail_enc" not in entry
