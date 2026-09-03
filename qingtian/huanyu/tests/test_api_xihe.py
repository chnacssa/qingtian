"""
api_xihe.py 单元测试 — 羲和 API 端点
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from huanyu.api_xihe import (
    AdoptSelfRequest,
    BriefingResponse,
    ReportStatusRequest,
    _get_default_modules,
    router,
)
from huanyu.agent_runtime import (
    AgentProcess,
    AgentProcessConfig,
    AgentRuntimeManager,
)


# ── _get_default_modules ───────────────────────────────

class TestDefaultModules:
    def test_default_modules_contains_all_services(self):
        modules = _get_default_modules("biz:buyer-01")
        assert "memory" in modules
        assert "knowledge" in modules
        assert "tasks" in modules
        assert "billing" in modules
        assert "inbox" in modules
        assert modules["memory"]["namespace"] == "agent:biz:buyer-01"


# ── adopt_self ─────────────────────────────────────────

class TestAdoptSelf:
    @pytest.mark.asyncio
    async def test_adopt_self_success(self):
        from huanyu.api_xihe import adopt_self

        req = AdoptSelfRequest(pid=12345)

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr.adopt_external = AsyncMock(return_value={
                "status": "ok", "agent_id": "test-agent", "adopted": True, "pid": 12345,
            })
            mock_get_mgr.return_value = mgr

            result = await adopt_self("test-agent", req)

        assert result["status"] == "ok"
        assert result["adopted"] is True

    @pytest.mark.asyncio
    async def test_adopt_self_error_raises_400(self):
        from huanyu.api_xihe import adopt_self

        req = AdoptSelfRequest(pid=0)

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr.adopt_external = AsyncMock(return_value={
                "status": "error", "error": "pid 无效",
            })
            mock_get_mgr.return_value = mgr

        with pytest.raises(HTTPException) as exc:
            await adopt_self("test-agent", req)
        assert exc.value.status_code == 400


# ── agent_status / list_agents ─────────────────────────

class TestAgentStatus:
    @pytest.mark.asyncio
    async def test_get_status_found(self):
        from huanyu.api_xihe import agent_status

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr.get_agent_status.return_value = {
                "agent_id": "test-agent", "status": "running", "pid": 12345,
            }
            mock_get_mgr.return_value = mgr

            result = await agent_status("test-agent")
        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_status_not_found(self):
        from huanyu.api_xihe import agent_status

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr.get_agent_status.return_value = None
            mock_get_mgr.return_value = mgr

        with pytest.raises(HTTPException) as exc:
            await agent_status("ghost")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_list_agents(self):
        from huanyu.api_xihe import list_agents

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr.list_agents.return_value = [
                {"agent_id": "a1", "status": "running"},
                {"agent_id": "a2", "status": "running"},
            ]
            mock_get_mgr.return_value = mgr

            result = await list_agents(status=None)
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_list_agents_filtered(self):
        from huanyu.api_xihe import list_agents

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr.list_agents.return_value = [
                {"agent_id": "a1", "status": "running"},
                {"agent_id": "a2", "status": "paused"},
                {"agent_id": "a3", "status": "running"},
            ]
            mock_get_mgr.return_value = mgr

            result = await list_agents(status="running")
        assert result["count"] == 2


# ── pause / resume / stop ─────────────────────────────

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_pause_agent(self):
        from huanyu.api_xihe import pause_agent

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3"))
        ap.status = "running"

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mgr._update_process_db = AsyncMock()
            mock_get_mgr.return_value = mgr

            result = await pause_agent("agent-1", None)
        assert result["status"] == "ok"
        assert ap.status == "paused"

    @pytest.mark.asyncio
    async def test_pause_already_paused(self):
        from huanyu.api_xihe import pause_agent

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3"))
        ap.status = "paused"

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mgr._update_process_db = AsyncMock()
            mock_get_mgr.return_value = mgr

            result = await pause_agent("agent-1", None)
        assert "已处于暂停状态" in result["message"]

    @pytest.mark.asyncio
    async def test_resume_agent(self):
        from huanyu.api_xihe import resume_agent

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3"))
        ap.status = "paused"
        ap.pid = 12345

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mgr._update_process_db = AsyncMock()
            mock_get_mgr.return_value = mgr

            with patch("os.kill") as mock_kill:
                result = await resume_agent("agent-1", None)
        assert result["new_status"] == "running"
        assert result["pid_alive"] is True

    @pytest.mark.asyncio
    async def test_stop_agent(self):
        from huanyu.api_xihe import stop_agent

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3"))
        ap.status = "running"
        ap.pid = 12345

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mgr._update_process_db = AsyncMock()
            mock_get_mgr.return_value = mgr

            with patch("os.kill") as mock_kill:
                result = await stop_agent("agent-1", None)
        assert result["status"] == "ok"


# ── resume-from-fatal ─────────────────────────────────

class TestResumeFromFatal:
    @pytest.mark.asyncio
    async def test_resume_from_fatal_resets_state(self):
        from huanyu.api_xihe import resume_from_fatal

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3", auto_start=False))
        ap.status = "fatal"
        ap._consecutive_restarts = 5
        ap.restart_count = 5
        ap.last_error = "too many crashes"

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mgr._update_process_db = AsyncMock()
            mock_get_mgr.return_value = mgr

            result = await resume_from_fatal("agent-1", None)
        assert result["new_status"] == "stopped"
        assert ap._consecutive_restarts == 0
        assert ap.restart_count == 0
        assert ap.last_error == ""

    @pytest.mark.asyncio
    async def test_resume_from_fatal_not_found(self):
        from huanyu.api_xihe import resume_from_fatal

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {}
            mock_get_mgr.return_value = mgr

        with pytest.raises(HTTPException) as exc:
            await resume_from_fatal("ghost", None)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_resume_from_non_fatal_is_noop(self):
        from huanyu.api_xihe import resume_from_fatal

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3"))
        ap.status = "running"

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mock_get_mgr.return_value = mgr

            result = await resume_from_fatal("agent-1", None)
        assert "非 fatal 状态" in result["notice"]


# ── restart with force ────────────────────────────────

class TestRestart:
    @pytest.mark.asyncio
    async def test_restart_force_resets_fatal(self):
        from huanyu.api_xihe import restart_agent

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3", auto_start=False))
        ap.status = "fatal"

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mgr._update_process_db = AsyncMock()
            mgr.restart_agent = AsyncMock(return_value=True)
            mock_get_mgr.return_value = mgr

            result = await restart_agent("agent-1", None, force=True)
        assert result["status"] == "ok"
        assert result["force"] is True
        assert ap.status == "stopped"
        assert ap._consecutive_restarts == 0


# ── briefing ───────────────────────────────────────────

class TestBriefing:
    @pytest.mark.asyncio
    async def test_briefing_returns_modules(self):
        from huanyu.api_xihe import agent_briefing

        ap = AgentProcess(AgentProcessConfig(agent_id="biz:buyer-01", executable="python3"))
        ap.status = "running"

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"biz:buyer-01": ap}
            mock_get_mgr.return_value = mgr

            result = await agent_briefing("biz:buyer-01")
        assert result["agent_id"] == "biz:buyer-01"
        assert result["state"] == "running"
        assert "modules" in result
        assert "memory" in result["modules"]


# ── report-status ─────────────────────────────────────

class TestReportStatus:
    @pytest.mark.asyncio
    async def test_report_status_started_updates_pid(self):
        from huanyu.api_xihe import report_status

        ap = AgentProcess(AgentProcessConfig(agent_id="agent-1", executable="python3"))
        ap.status = "stopped"
        ap.pid = None

        req = ReportStatusRequest(
            status="started",
            version="2.1.0",
            health={"pid": 88888, "memory_mb": 256, "uptime_seconds": 30},
        )

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {"agent-1": ap}
            mgr._update_process_db = AsyncMock()
            mock_get_mgr.return_value = mgr

            result = await report_status("agent-1", req)
        assert result["status"] == "ok"
        assert result["ack"] is True
        assert ap.pid == 88888
        assert ap.status == "running"

    @pytest.mark.asyncio
    async def test_report_status_unknown_agent(self):
        """未托管的 Agent 报状态不报错"""
        from huanyu.api_xihe import report_status

        req = ReportStatusRequest(status="started")

        with patch("huanyu.api_xihe._get_mgr") as mock_get_mgr:
            mgr = MagicMock()
            mgr._processes = {}
            mock_get_mgr.return_value = mgr

            result = await report_status("ghost", req)
        assert result["status"] == "ok"
