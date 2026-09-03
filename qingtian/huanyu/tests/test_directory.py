"""
directory.py 单元测试
Agent 目录服务 — 使用 mock asyncpg
"""

from unittest.mock import patch

import pytest

from huanyu.directory import (
    check_stale_agents,
    check_suspended_agents,
    discover_agents,
    get_agent,
    get_categories,
    get_stats,
    get_topic_subscribers,
    heartbeat,
    register_agent,
    register_agent_silent,
    search_agents,
    soft_delete_agent,
    subscribe_topics,
    unsubscribe_topics,
)


class TestRegisterAgent:
    @pytest.mark.asyncio
    async def test_register_basic(self, mock_conn, mock_pool):
        new_row = {
            "agent_id": "new-uuid",
            "ain": "ain:1:acssa:cn-hf-localhost:biz:buyer:001",
            "public_key": "pk_pem",
            "cert_fingerprint": "fp_abc",
            "name": "test-agent",
            "category": "biz:buyer",
            "status": "active",
            "server_host": "localhost",
            "created_at": None,
        }
        # 第一次 fetchrow: 预检（None = 新注册），第二次: INSERT RETURNING
        mock_conn.fetchrow.side_effect = [None, new_row]
        mock_conn.fetchval.return_value = 0

        with (
            patch("huanyu.directory.get_pool", return_value=mock_pool),
            patch("huanyu.ain.get_pool", return_value=mock_pool),
            patch("common.db.get_pool", return_value=mock_pool),
        ):
            result = await register_agent("test-agent", "biz:buyer")
            assert result["name"] == "test-agent"
            assert result["category"] == "biz:buyer"

    @pytest.mark.asyncio
    async def test_register_with_capabilities(self, mock_conn, mock_pool):
        new_row = {
            "agent_id": "new-uuid",
            "ain": "ain:1:acssa:cn-hf-procurement:biz:seller:001",
            "public_key": "pk_pem",
            "cert_fingerprint": "fp_abc",
            "name": "test-agent",
            "category": "biz:seller",
            "status": "active",
            "server_host": "procurement",
            "created_at": None,
        }
        mock_conn.fetchrow.side_effect = [None, new_row]
        mock_conn.fetchval.return_value = 0

        with (
            patch("huanyu.directory.get_pool", return_value=mock_pool),
            patch("huanyu.ain.get_pool", return_value=mock_pool),
            patch("common.db.get_pool", return_value=mock_pool),
        ):
            result = await register_agent(
                "test-agent", "biz:seller",
                subcategory="steel",
                capabilities=["inquiry", "negotiate"],
                server_host="procurement",
            )
            assert result["server_host"] == "procurement"


class TestRegisterAgentSilent:
    """register_agent_silent — 无感注册（总线自动注册用）"""

    @pytest.mark.asyncio
    async def test_register_silent_basic(self, mock_conn, mock_pool):
        new_row = {
            "agent_id": "auto-uuid",
            "ain": "ain:1:acssa:cn-hf-localhost:biz:buyer:001",
            "name": "buyer-01",
            "category": "biz:buyer",
            "status": "active",
        }
        mock_conn.fetchval.return_value = 0
        mock_conn.fetchrow.return_value = new_row

        with (
            patch("huanyu.directory.get_pool", return_value=mock_pool),
            patch("huanyu.ain.get_pool", return_value=mock_pool),
            patch("common.db.get_pool", return_value=mock_pool),
        ):
            result = await register_agent_silent(
                agent_id="biz:buyer-01",
                name="buyer-01",
                category="biz:buyer",
                server_host="localhost",
                metadata={"auto_registered": True},
            )
        assert result["agent_id"] == "auto-uuid"
        assert result["name"] == "buyer-01"
        assert result["category"] == "biz:buyer"
        assert result["status"] == "active"
        # 无感注册不返回 private_key
        assert "private_key" not in result

    @pytest.mark.asyncio
    async def test_register_silent_uses_passed_agent_id(self, mock_conn, mock_pool):
        """P2 (R11): 传入的 agent_id 必须生效——新建时显式写入 agent_id 列。

        回归：此前 agent_id 参数仅出现在日志，行数据 agent_id 全由 DB 默认生成。
        """
        new_row = {
            "agent_id": "biz:buyer-01",
            "ain": "ain:1:acssa:cn-hf-localhost:biz:buyer:001",
            "name": "buyer-01",
            "category": "biz:buyer",
            "status": "active",
        }
        # 第一次 fetchrow = 按 agent_id 查重（None = 不存在），第二次 = INSERT RETURNING
        mock_conn.fetchrow.side_effect = [None, new_row]
        mock_conn.fetchval.return_value = 0

        with (
            patch("huanyu.directory.get_pool", return_value=mock_pool),
            patch("huanyu.ain.get_pool", return_value=mock_pool),
            patch("common.db.get_pool", return_value=mock_pool),
        ):
            result = await register_agent_silent(
                agent_id="biz:buyer-01",
                name="buyer-01",
                category="biz:buyer",
                server_host="localhost",
                metadata={"auto_registered": True},
            )
        assert result["agent_id"] == "biz:buyer-01"
        # INSERT（第 2 次 fetchrow）必须把传入 agent_id 显式写入首列（$1）
        args = mock_conn.fetchrow.call_args_list[1].args
        assert "agent_id" in args[0]
        assert args[1] == "biz:buyer-01"

    @pytest.mark.asyncio
    async def test_register_silent_existing_agent_id_updates(self, mock_conn, mock_pool):
        """P2 (R11): agent_id 已存在 → 按 agent_id 更新（复用原 AIN，不重算实例）。"""
        existing_row = {
            "agent_id": "biz:buyer-01",
            "ain": "ain:1:acssa:cn-hf-localhost:biz:buyer:001",
            "name": "buyer-01",
            "category": "biz:buyer",
            "status": "active",
        }
        mock_conn.fetchrow.return_value = existing_row

        with (
            patch("huanyu.directory.get_pool", return_value=mock_pool),
            patch("huanyu.ain.get_pool", return_value=mock_pool),
            patch("common.db.get_pool", return_value=mock_pool),
        ):
            result = await register_agent_silent(
                agent_id="biz:buyer-01",
                name="buyer-01",
                category="biz:buyer",
            )
        assert result["agent_id"] == "biz:buyer-01"
        assert result["status"] == "active"
        # 走 UPDATE 分支：不调用 next_instance（fetchval 未被用于计数）
        update_args = mock_conn.fetchrow.call_args_list[1].args
        assert "UPDATE" in update_args[0]
        assert "WHERE agent_id = $1" in update_args[0]

    @pytest.mark.asyncio
    async def test_register_silent_upsert(self, mock_conn, mock_pool):
        """验证 ON CONFLICT 更新路径"""
        existing_row = {
            "agent_id": "existing-uuid",
            "ain": "ain:1:acssa:cn-hf-localhost:biz:buyer:001",
            "name": "buyer-01",
            "category": "biz:buyer",
            "status": "active",
        }
        mock_conn.fetchval.return_value = 0
        mock_conn.fetchrow.return_value = existing_row

        with (
            patch("huanyu.directory.get_pool", return_value=mock_pool),
            patch("huanyu.ain.get_pool", return_value=mock_pool),
            patch("common.db.get_pool", return_value=mock_pool),
        ):
            result = await register_agent_silent(
                agent_id="biz:buyer-01",
                name="buyer-01",
                category="biz:buyer",
            )
        assert result["agent_id"] == "existing-uuid"
        assert result["status"] == "active"


class TestDiscoverAgents:
    @pytest.mark.asyncio
    async def test_discover_all_active(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"agent_id": "a1", "name": "Agent-1", "category": "biz:buyer"},
            {"agent_id": "a2", "name": "Agent-2", "category": "biz:seller"},
        ]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            agents = await discover_agents()
            assert len(agents) == 2

    @pytest.mark.asyncio
    async def test_discover_by_category(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [{"agent_id": "a1", "name": "Agent-1", "category": "biz:buyer"}]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            agents = await discover_agents(category="biz:buyer")
            assert len(agents) == 1

    @pytest.mark.asyncio
    async def test_discover_by_capability(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [{"agent_id": "a1"}]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            agents = await discover_agents(capability="negotiate")
            assert len(agents) == 1


class TestGetAgent:
    @pytest.mark.asyncio
    async def test_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"agent_id": "a1", "name": "test"}

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            agent = await get_agent("a1")
            assert agent["name"] == "test"

    @pytest.mark.asyncio
    async def test_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            agent = await get_agent("nonexistent")
            assert agent is None


class TestSearchAgents:
    @pytest.mark.asyncio
    async def test_search(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [{"agent_id": "a1", "name": "采购Agent-1"}]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            agents = await search_agents("采购")
            assert len(agents) == 1
            assert agents[0]["name"] == "采购Agent-1"


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_success(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"agent_id": "a1", "status": "active"}

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            result = await heartbeat("a1")
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_heartbeat_unknown_agent(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            result = await heartbeat("nonexistent")
            assert result["status"] == "error"
            assert "未注册" in result["error"]


class TestSoftDelete:
    @pytest.mark.asyncio
    async def test_soft_delete(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"agent_id": "a1"}

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            result = await soft_delete_agent("a1")
            assert result["status"] == "deleted"
            # P2 (R11): 显式 bool 标记存在（供 api_compliance 按真实结果判定，不再靠 dict 真值）
            assert result["deleted"] is True

    @pytest.mark.asyncio
    async def test_soft_delete_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            result = await soft_delete_agent("nonexistent")
            assert result["status"] == "error"
            # P2 (R11): 不存在 → deleted=False，明确失败而非恒真值
            assert result["deleted"] is False


class TestTopics:
    @pytest.mark.asyncio
    async def test_subscribe(self, mock_conn, mock_pool):
        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            result = await subscribe_topics("a1", ["钢材.螺纹钢"])
            assert result["status"] == "ok"
            assert result["topics"] == ["钢材.螺纹钢"]

    @pytest.mark.asyncio
    async def test_unsubscribe(self, mock_conn, mock_pool):
        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            result = await unsubscribe_topics("a1", ["钢材.螺纹钢"])
            assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_subscribers(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"agent_id": "a1", "name": "Agent-1", "server_host": "procurement"},
        ]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            subs = await get_topic_subscribers("钢材.螺纹钢")
            assert len(subs) == 1


class TestStaleAgentChecks:
    @pytest.mark.asyncio
    async def test_check_stale(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [{"agent_id": "a1"}, {"agent_id": "a2"}]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            with patch("huanyu.directory.root_get", return_value="test-host"):
                stale = await check_stale_agents()
        assert len(stale) == 2
        # 仅判定本底座 agent：SQL 必须带 server_host = $1，且参数是本底座 host
        sql, args = mock_conn.fetch.call_args[0]
        assert "server_host = $1" in sql
        assert args == "test-host"

    @pytest.mark.asyncio
    async def test_check_stale_filters_cross_base_agents(self, mock_conn, mock_pool):
        """跨底座 agent（server_host ≠ 本底座）不参与判定——管理服/hub 上同步来的
        agent 本地无交流记录，若无过滤会被每 5 分钟误杀成 inactive。"""
        mock_conn.fetch.return_value = []

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            with patch("huanyu.directory.root_get", return_value="this-host"):
                stale = await check_stale_agents()
        sql, args = mock_conn.fetch.call_args[0]
        assert "server_host = $1" in sql
        assert args == "this-host"
        assert len(stale) == 0

    @pytest.mark.asyncio
    async def test_check_suspended(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [{"agent_id": "a1"}]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            suspended = await check_suspended_agents()
            assert len(suspended) == 1


class TestStats:
    @pytest.mark.asyncio
    async def test_get_stats(self, mock_conn, mock_pool):
        mock_conn.fetchval.side_effect = [100, 80, 5000, 20, 300, 150]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            stats = await get_stats()
            assert stats["total_agents"] == 100
            assert stats["active_agents"] == 80
            assert stats["total_messages"] == 5000
            assert stats["active_negotiations"] == 20


class TestCategories:
    @pytest.mark.asyncio
    async def test_get_categories(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"category": "biz:buyer", "cnt": 10},
            {"category": "biz:seller", "cnt": 8},
        ]

        with patch("huanyu.directory.get_pool", return_value=mock_pool):
            cats = await get_categories()
            assert len(cats) == 2
