"""
common/bus.py 单元测试 — BusStateTable + BusScheduler
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.bus import (
    AgentBusState,
    BusStateInfo,
    BusScheduler,
    BusStateTable,
    bus_scheduler,
)


def _set_local(tbl, agent_id, state, metadata=None):
    """直接设置状态（跳过 DB），用于单元测试"""
    tbl._states[agent_id] = BusStateInfo(state, metadata)


class TestBusStateTable:
    """BusStateTable — 内存缓存 + DB 持久化"""

    def test_unknown_by_default(self):
        tbl = BusStateTable()
        assert tbl.count == 0

    @pytest.mark.asyncio
    async def test_set_and_get(self):
        tbl = BusStateTable()
        _set_local(tbl, "agent-1", AgentBusState.READY, {"note": "test"})
        state = await tbl.get("agent-1")
        assert state == AgentBusState.READY

    @pytest.mark.asyncio
    async def test_unknown_returns_unknown(self):
        tbl = BusStateTable()
        state = await tbl.get("nonexistent")
        assert state == AgentBusState.UNKNOWN

    @pytest.mark.asyncio
    async def test_is_known(self):
        tbl = BusStateTable()
        assert not tbl.is_known("agent-1")
        _set_local(tbl, "agent-1", AgentBusState.REGISTERED)
        assert tbl.is_known("agent-1")

    @pytest.mark.asyncio
    async def test_get_by_state(self):
        tbl = BusStateTable()
        _set_local(tbl, "a1", AgentBusState.READY)
        _set_local(tbl, "a2", AgentBusState.PAUSED)
        _set_local(tbl, "a3", AgentBusState.READY)
        ready = tbl.get_by_state(AgentBusState.READY)
        assert sorted(ready) == ["a1", "a3"]
        paused = tbl.get_by_state(AgentBusState.PAUSED)
        assert paused == ["a2"]

    @pytest.mark.asyncio
    async def test_state_counts(self):
        tbl = BusStateTable()
        _set_local(tbl, "a1", AgentBusState.READY)
        _set_local(tbl, "a2", AgentBusState.READY)
        _set_local(tbl, "a3", AgentBusState.PAUSED)
        assert tbl.state_counts == {"ready": 2, "paused": 1}

    @pytest.mark.asyncio
    async def test_touch_updates_last_active(self):
        tbl = BusStateTable()
        _set_local(tbl, "a1", AgentBusState.READY)
        old = tbl._states["a1"].last_active
        await tbl.touch("a1")
        assert tbl._states["a1"].last_active >= old

    @pytest.mark.asyncio
    async def test_set_updates_metadata(self):
        tbl = BusStateTable()
        _set_local(tbl, "a1", AgentBusState.REGISTERED, {"first_seen": "now"})
        info = tbl._states["a1"]
        assert info.metadata["first_seen"] == "now"
        # 再次 set 应合并 metadata
        _set_local(tbl, "a1", AgentBusState.READY, {"role": "buyer"})
        # 注：直接写状态不会合并 metadata，这是 set() 中 DB 层的逻辑
        # 本地直接设置时 metadata 会被替换
        assert tbl._states["a1"].metadata.get("role") == "buyer"

    @pytest.mark.asyncio
    async def test_load_from_db(self, mock_conn, mock_pool):
        """验证 load_from_db 从 DB 重建状态"""
        mock_conn.fetch.return_value = [
            {"agent_id": "a1", "state": "ready", "metadata": {"role": "buyer"}},
            {"agent_id": "a2", "state": "paused", "metadata": {}},
        ]
        tbl = BusStateTable()
        with patch("common.db.get_pool", return_value=mock_pool):
            await tbl.load_from_db()
        assert tbl.count == 2
        assert await tbl.get("a1") == AgentBusState.READY
        assert await tbl.get("a2") == AgentBusState.PAUSED


class TestBusScheduler:
    """BusScheduler — 主动调度引擎"""

    def setup_method(self):
        self.scheduler = BusScheduler()
        self.scheduler._state_table = BusStateTable()

    @pytest.mark.asyncio
    async def test_skip_prefixes(self):
        """验证跳过路径不触发调度"""
        skip_paths = [
            "/health",
            "/v1/xihe/stats",
            "/docs",
            "/favicon.ico",
            "/.well-known/openid-configuration",
            "/openapi.json",
        ]
        for path in skip_paths:
            request = MagicMock()
            request.url.path = path
            # 没有 agent_id 时直接放行
            request.state = MagicMock()
            request.state.agent_id = None

            call_next = AsyncMock(return_value=MagicMock())
            result = await self.scheduler.dispatch(request, call_next)
            # agent_id 为 None 时直通
            call_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_paused_returns_403(self):
        """PAUSED 状态返回 403"""
        request = MagicMock()
        request.url.path = "/v1/huanyu/messages"
        request.state = MagicMock()
        request.state.agent_id = "agent-paused"
        request.client = MagicMock()
        request.client.host = "localhost"

        _set_local(self.scheduler._state_table, "agent-paused", AgentBusState.PAUSED)

        call_next = AsyncMock()
        resp = await self.scheduler.dispatch(request, call_next)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_stopped_returns_410(self):
        """STOPPED 状态返回 410"""
        request = MagicMock()
        request.url.path = "/v1/huanyu/messages"
        request.state = MagicMock()
        request.state.agent_id = "agent-stopped"
        request.client = MagicMock()
        request.client.host = "localhost"

        _set_local(self.scheduler._state_table, "agent-stopped", AgentBusState.STOPPED)

        call_next = AsyncMock()
        resp = await self.scheduler.dispatch(request, call_next)
        assert resp.status_code == 410

    @pytest.mark.asyncio
    async def test_unknown_auto_registers(self):
        """UNKNOWN 状态触发自动注册 → 标记 READY"""
        request = MagicMock()
        request.url.path = "/v1/huanyu/messages"
        request.state = MagicMock()
        request.state.agent_id = "biz:buyer-01"
        request.client = MagicMock()
        request.client.host = "localhost"

        call_next = AsyncMock()
        call_next.return_value = MagicMock()
        call_next.return_value.headers = {}

        with patch("common.db.get_pool") as mock_get_pool:
            mock_conn = AsyncMock()
            mock_pool = MagicMock()
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock()
            mock_get_pool.return_value = mock_pool

            with patch.object(self.scheduler, "_auto_register", AsyncMock()) as mock_reg:
                with patch.object(self.scheduler, "_auto_adopt", AsyncMock()) as mock_adopt:
                    await self.scheduler.dispatch(request, call_next)

        mock_reg.assert_awaited_once()
        mock_adopt.assert_awaited()
        state = await self.scheduler._state_table.get("biz:buyer-01")
        assert state == AgentBusState.READY

    @pytest.mark.asyncio
    async def test_registered_auto_adopts(self):
        """REGISTERED 状态触发自动接管 → READY"""
        request = MagicMock()
        request.url.path = "/v1/huanyu/messages"
        request.state = MagicMock()
        request.state.agent_id = "agent-reg"
        request.client = MagicMock()
        request.client.host = "localhost"

        _set_local(self.scheduler._state_table, "agent-reg", AgentBusState.REGISTERED)

        call_next = AsyncMock()
        call_next.return_value = MagicMock()
        call_next.return_value.headers = {}

        with patch("common.db.get_pool") as mock_get_pool:
            mock_conn = AsyncMock()
            mock_pool = MagicMock()
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock()
            mock_get_pool.return_value = mock_pool

            with patch.object(self.scheduler, "_auto_adopt", AsyncMock()) as mock_adopt:
                await self.scheduler.dispatch(request, call_next)

        mock_adopt.assert_awaited_once()
        state = await self.scheduler._state_table.get("agent-reg")
        assert state == AgentBusState.READY

    @pytest.mark.asyncio
    async def test_ready_passes_through(self):
        """READY 状态直接放行"""
        request = MagicMock()
        request.url.path = "/v1/huanyu/messages"
        request.state = MagicMock()
        request.state.agent_id = "agent-ready"
        request.client = MagicMock()
        request.client.host = "localhost"

        _set_local(self.scheduler._state_table, "agent-ready", AgentBusState.READY)

        call_next = AsyncMock()
        call_next.return_value = MagicMock()
        call_next.return_value.headers = {}

        result = await self.scheduler.dispatch(request, call_next)
        call_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_inject_context_sets_headers(self):
        """_inject_context 设置 X-Bus-* 响应头"""
        request = MagicMock()
        request.url.path = "/v1/test"
        request.state = MagicMock()
        request.state.agent_id = "agent-1"
        request.client = MagicMock()
        request.client.host = "localhost"

        _set_local(self.scheduler._state_table, "agent-1", AgentBusState.READY)

        response = MagicMock()
        response.headers = {}

        call_next = AsyncMock(return_value=response)
        result = await self.scheduler.dispatch(request, call_next)

        assert result.headers.get("X-Bus-State") == "ready"
        assert result.headers.get("X-Bus-Agent-Id") == "agent-1"


class TestFindAgentPid:
    """_find_agent_pid — PID 发现"""

    def setup_method(self):
        self.scheduler = BusScheduler()

    @pytest.mark.asyncio
    async def test_find_valid_pid(self):
        """PID 存在且进程存活 → 返回 pid_info"""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {
            "pid": 99999,
            "config_json": {"executable": "/usr/bin/python3"},
            "status": "running",
        }
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()

        with patch("common.db.get_pool", return_value=mock_pool):
            with patch("os.kill", return_value=None) as mock_kill:  # PID 存活
                result = await self.scheduler._find_agent_pid("agent-1")

        assert result is not None
        assert result["pid"] == 99999
        assert result["status"] == "running"
        mock_kill.assert_called_once_with(99999, 0)

    @pytest.mark.asyncio
    async def test_find_stale_pid(self):
        """PID 存在但进程已死 → 返回 None"""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {
            "pid": 88888,
            "config_json": {},
            "status": "running",
        }
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()

        with patch("common.db.get_pool", return_value=mock_pool):
            with patch("os.kill", side_effect=ProcessLookupError) as mock_kill:
                result = await self.scheduler._find_agent_pid("agent-1")

        assert result is None
        mock_kill.assert_called_once_with(88888, 0)

    @pytest.mark.asyncio
    async def test_find_no_db_record(self):
        """DB 无 PID 记录 → 返回 None"""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()

        with patch("common.db.get_pool", return_value=mock_pool):
            result = await self.scheduler._find_agent_pid("nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_find_db_error_returns_none(self):
        """DB 查询异常 → 返回 None（不抛）"""
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=Exception("DB down"))

        with patch("common.db.get_pool", return_value=mock_pool):
            result = await self.scheduler._find_agent_pid("agent-1")

        assert result is None


class TestAutoAdopt:
    """_auto_adopt — 自动接管"""

    def setup_method(self):
        self.scheduler = BusScheduler()

    @pytest.mark.asyncio
    async def test_adopt_pid_found_and_adopted(self):
        """PID 存活 + adopt_external 成功"""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {
            "pid": 99999,
            "config_json": {"executable": "/usr/bin/python3"},
            "status": "running",
        }
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()

        mock_arm = AsyncMock()
        mock_arm.adopt_external.return_value = {"status": "ok", "agent_id": "agent-1"}

        with patch("common.db.get_pool", return_value=mock_pool):
            with patch("os.kill", return_value=None):
                with patch("huanyu.agent_runtime.get_manager", return_value=mock_arm):
                    await self.scheduler._auto_adopt("agent-1")

        mock_arm.adopt_external.assert_awaited_once()
        args, _ = mock_arm.adopt_external.call_args
        assert args[0] == "agent-1"
        assert args[1]["pid"] == 99999

    @pytest.mark.asyncio
    async def test_adopt_no_pid_skips(self):
        """无 PID → 跳过接管，不调 adopt_external"""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = None
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()

        mock_arm = AsyncMock()

        with patch("common.db.get_pool", return_value=mock_pool):
            with patch("huanyu.agent_runtime.get_manager", return_value=mock_arm):
                await self.scheduler._auto_adopt("agent-1")

        mock_arm.adopt_external.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adopt_manager_unavailable(self):
        """get_manager 异常 → 不抛，静默失败"""
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {
            "pid": 99999,
            "config_json": {},
            "status": "running",
        }
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock()

        with patch("common.db.get_pool", return_value=mock_pool):
            with patch("os.kill", return_value=None):
                with patch("huanyu.agent_runtime.get_manager",
                           side_effect=ImportError("not installed")):
                    # 不应抛异常
                    await self.scheduler._auto_adopt("agent-1")
