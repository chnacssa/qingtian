"""
guard.py 单元测试
守卫规则引擎 — rule matching, priority ordering
"""

from unittest.mock import AsyncMock, patch

import pytest

from zhenyue.guard import GuardEngine, get_engine


class TestGuardEngine:
    @pytest.mark.asyncio
    async def test_allow_rule(self, mock_conn, mock_pool):
        """allow 规则 → allowed=True"""
        mock_conn.fetch.return_value = [
            {"rule_id": "r1", "name": "allow-read", "description": "", "rule_type": "allow",
             "match_pattern": "read:*", "priority": 10, "enabled": True, "created_at": None},
        ]

        engine = GuardEngine()
        engine._rules = []

        with patch("zhenyue.guard.get_pool", return_value=mock_pool):
            result = await engine.check(agent_id="agent-1", action="read", target="/data/file.txt")
            assert result["allowed"] is True
            assert result["rule"] == "allow-read"

    @pytest.mark.asyncio
    async def test_deny_rule(self, mock_conn, mock_pool):
        """deny 规则 → allowed=False"""
        mock_conn.fetch.return_value = [
            {"rule_id": "r2", "name": "deny-delete", "description": "", "rule_type": "deny",
             "match_pattern": "delete:*", "priority": 10, "enabled": True, "created_at": None},
        ]

        engine = GuardEngine()
        engine._rules = []

        with patch("zhenyue.guard.get_pool", return_value=mock_pool):
            result = await engine.check(agent_id="agent-1", action="delete", target="/data/secret.txt")
            assert result["allowed"] is False
            assert result["rule"] == "deny-delete"

    @pytest.mark.asyncio
    async def test_audit_rule(self, mock_conn, mock_pool):
        """audit 规则 → 写审计日志，allowed=True"""
        mock_conn.fetch.return_value = [
            {"rule_id": "r3", "name": "audit-write", "description": "", "rule_type": "audit",
             "match_pattern": "write:*", "priority": 5, "enabled": True, "created_at": None},
        ]

        engine = GuardEngine()
        engine._rules = []

        with (
            patch("zhenyue.guard.get_pool", return_value=mock_pool),
            patch("zhenyue.guard.write_audit", new_callable=AsyncMock) as mock_audit,
        ):
            result = await engine.check(agent_id="agent-1", action="write", target="/data/file.txt")
            assert result["allowed"] is True
            assert result["rule"] == "audit-write"
            mock_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_priority_ordering(self, mock_conn, mock_pool):
        """高优先级规则先匹配"""
        mock_conn.fetch.return_value = [
            {"rule_id": "r_high", "name": "high-priority", "description": "", "rule_type": "deny",
             "match_pattern": "*:*", "priority": 100, "enabled": True, "created_at": None},
            {"rule_id": "r_low", "name": "low-priority", "description": "", "rule_type": "allow",
             "match_pattern": "read:*", "priority": 10, "enabled": True, "created_at": None},
        ]

        engine = GuardEngine()
        engine._rules = []

        with patch("zhenyue.guard.get_pool", return_value=mock_pool):
            # 即使 action=read 匹配 low-priority，但 high-priority 是通配 deny 且优先级更高
            result = await engine.check(agent_id="agent-1", action="read", target="/data/file.txt")
            assert result["allowed"] is False
            assert result["rule"] == "high-priority"

    @pytest.mark.asyncio
    async def test_no_match_default_allow(self, mock_conn, mock_pool):
        """无匹配规则 → 默认 allowed=True"""
        mock_conn.fetch.return_value = []

        engine = GuardEngine()
        engine._rules = []

        with patch("zhenyue.guard.get_pool", return_value=mock_pool):
            result = await engine.check(agent_id="agent-1", action="unknown", target="/something")
            assert result["allowed"] is True
            assert result["rule"] is None

    def test_get_engine_singleton(self):
        """get_engine() 返回同一实例"""
        e1 = get_engine()
        e2 = get_engine()
        assert e1 is e2

    @pytest.mark.asyncio
    async def test_add_rule(self, mock_conn, mock_pool):
        """add_rule 插入规则并重新加载"""
        mock_conn.fetchval.return_value = "new-rule-uuid"
        mock_conn.fetch.return_value = []

        engine = GuardEngine()
        engine._rules = []

        with patch("zhenyue.guard.get_pool", return_value=mock_pool):
            result = await engine.add_rule(
                name="test-rule",
                rule_type="deny",
                match_pattern="delete:secret/*",
                priority=50,
                description="Test deny rule",
            )
            assert result["name"] == "test-rule"
            assert result["rule_type"] == "deny"

    @pytest.mark.asyncio
    async def test_add_rule_invalid_type(self):
        """add_rule 无效 rule_type → ValueError"""
        engine = GuardEngine()
        with pytest.raises(ValueError, match="Invalid rule_type"):
            await engine.add_rule(name="bad", rule_type="invalid", match_pattern="*")

    @pytest.mark.asyncio
    async def test_delete_rule(self, mock_conn, mock_pool):
        """delete_rule 删除规则"""
        mock_conn.execute.return_value = "DELETE 1"
        mock_conn.fetch.return_value = []

        engine = GuardEngine()
        engine._rules = []

        with patch("zhenyue.guard.get_pool", return_value=mock_pool):
            result = await engine.delete_rule("some-rule-id")
            assert result is True
