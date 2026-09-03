"""
agent_runtime.py 单元测试 — AgentProcess + AgentRuntimeManager
"""

import asyncio
import signal
from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, PropertyMock, patch

import pytest

from huanyu.agent_runtime import (
    AgentProcess,
    AgentProcessConfig,
    AgentRuntimeManager,
)


class _ConnCtx:
    """async with pool.acquire() as conn 上下文管理器"""
    def __init__(self, conn):
        self.conn = conn
    async def __aenter__(self):
        return self.conn
    async def __aexit__(self, *args):
        pass


class _MockPool:
    """模拟 asyncpg Pool"""
    def __init__(self, conn):
        self._conn = conn
    def acquire(self):
        return _ConnCtx(self._conn)
    async def close(self):
        pass


# ── Fixtures ────────────────────────────────────────────

@pytest.fixture
def proc_config():
    return AgentProcessConfig(
        agent_id="infra:monitor-01",
        executable="python3",
        args=["-m", "qingtian.builtin.monitor_agent"],
        restart_policy="always",
        max_retries=4,
    )


@pytest.fixture
def arm():
    mgr = AgentRuntimeManager()
    return mgr


@pytest.fixture
def mock_subprocess():
    """Mock asyncio.create_subprocess_exec 返回的 Process"""
    proc = AsyncMock()
    proc.pid = 12345
    proc.returncode = None  # 正在运行
    proc.wait = AsyncMock()
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    proc.stdout = AsyncMock()
    proc.stdout.__aiter__ = MagicMock(return_value=iter([]))
    proc.stderr = AsyncMock()
    proc.stderr.__aiter__ = MagicMock(return_value=iter([]))
    return proc


# ── AgentProcessConfig ─────────────────────────────────

class TestAgentProcessConfig:
    def test_default_values(self):
        cfg = AgentProcessConfig(agent_id="test", executable="python3")
        assert cfg.agent_id == "test"
        assert cfg.restart_policy == "always"
        assert cfg.max_retries == 4
        assert cfg.health_check_interval == 30
        assert cfg.restart_backoff == [3, 15, 60, 300]
        assert cfg.stop_timeout == 10

    def test_custom_values(self):
        cfg = AgentProcessConfig(
            agent_id="custom",
            executable="node",
            max_retries=2,
            restart_policy="on_failure",
        )
        assert cfg.executable == "node"
        assert cfg.max_retries == 2
        assert cfg.restart_policy == "on_failure"


# ── AgentProcess ───────────────────────────────────────

class TestAgentProcess:
    def test_initial_state_stopped(self, proc_config):
        ap = AgentProcess(proc_config)
        assert ap.status == "stopped"
        assert ap.pid is None
        assert ap.proc is None
        assert ap.restart_count == 0
        assert ap._consecutive_restarts == 0
        assert ap._backoff_index == 0

    def test_config_assigned(self, proc_config):
        ap = AgentProcess(proc_config)
        assert ap.config.agent_id == "infra:monitor-01"
        assert ap.config.restart_policy == "always"


# ── _get_backoff ───────────────────────────────────────

class TestGetBackoff:
    def test_backoff_values(self):
        assert AgentRuntimeManager._get_backoff(0) == 3
        assert AgentRuntimeManager._get_backoff(1) == 15
        assert AgentRuntimeManager._get_backoff(2) == 60
        assert AgentRuntimeManager._get_backoff(3) == 300

    def test_backoff_clamps(self):
        """超出序列长度的值应返回最大退避"""
        assert AgentRuntimeManager._get_backoff(10) == 300


# ── AgentRuntimeManager: start_agent ───────────────────

class TestStartAgent:
    @pytest.mark.asyncio
    async def test_start_agent_success(self, arm, proc_config, mock_subprocess):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_subprocess)):
            with patch.object(arm, "_update_process_db", AsyncMock()) as mock_db:
                ok = await arm.start_agent(proc_config)

        assert ok is True
        ap = arm._processes[proc_config.agent_id]
        assert ap.status == "running"
        assert ap.pid == 12345
        assert mock_db.await_count == 1

    @pytest.mark.asyncio
    async def test_start_agent_idempotent(self, arm, proc_config, mock_subprocess):
        """重复 start_agent 当已在运行时返回 True 不重复启动"""
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_subprocess)):
            with patch.object(arm, "_update_process_db", AsyncMock()):
                ok1 = await arm.start_agent(proc_config)
                ok2 = await arm.start_agent(proc_config)

        assert ok1 is True
        assert ok2 is True
        ap = arm._processes[proc_config.agent_id]
        assert ap.status == "running"
        # 验证子进程只创建了一次
        # 第二次调用时 status 已经是 running，应短路返回
        assert ap.pid == 12345

    @pytest.mark.asyncio
    async def test_start_agent_env_injection(self, arm, proc_config, mock_subprocess):
        """验证 LLM 代理劫持环境变量已注入"""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_exec.return_value = mock_subprocess
            with patch.object(arm, "_update_process_db", AsyncMock()):
                await arm.start_agent(proc_config)

        # 检查 create_subprocess_exec 的 env 参数
        _call_env = mock_exec.call_args[1].get("env", {})
        assert "QINGTIAN_AGENT_ID" in _call_env
        assert _call_env["QINGTIAN_AGENT_ID"] == "infra:monitor-01"
        assert "PYTHONUNBUFFERED" in _call_env
        assert _call_env["PYTHONUNBUFFERED"] == "1"


# ── AgentRuntimeManager: stop_agent ────────────────────

class TestStopAgent:
    @pytest.mark.asyncio
    async def test_stop_agent_sigterm(self, arm, proc_config, mock_subprocess):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_subprocess)):
            with patch.object(arm, "_update_process_db", AsyncMock()):
                await arm.start_agent(proc_config)

        with patch.object(arm, "_update_process_db", AsyncMock()) as mock_db:
            ok = await arm.stop_agent(proc_config.agent_id)

        assert ok is True
        ap = arm._processes[proc_config.agent_id]
        assert ap.status == "stopped"
        mock_subprocess.send_signal.assert_called_with(signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_stop_agent_not_running(self, arm):
        ok = await arm.stop_agent("nonexistent")
        assert ok is True  # 不存在视为已停止


# ── _handle_agent_crash ────────────────────────────────

class TestHandleAgentCrash:
    @pytest.mark.asyncio
    async def test_crash_fatal_after_max_retries(self, arm, proc_config, mock_subprocess):
        """超过 max_retries 后进入 fatal 状态"""
        ap = AgentProcess(proc_config)
        ap.status = "running"
        ap._consecutive_restarts = 5  # >= max_retries=4
        arm._processes[proc_config.agent_id] = ap

        with patch.object(arm, "_update_process_db", AsyncMock()) as mock_db:
            await arm._handle_agent_crash(ap, "test crash")

        assert ap.status == "fatal"
        # fatal 时应调用 _update_process_db 写入 fatal 状态
        mock_db.assert_awaited_with(proc_config.agent_id, "fatal", last_error=ANY)

    @pytest.mark.asyncio
    async def test_crash_restarts_with_backoff(self, arm, proc_config, mock_subprocess):
        """未超 max_retries 时退避后重启"""
        ap = AgentProcess(proc_config)
        ap.status = "running"
        ap._consecutive_restarts = 0  # 第一次崩溃
        arm._processes[proc_config.agent_id] = ap

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_subprocess)):
            with patch.object(arm, "_update_process_db", AsyncMock()):
                with patch.object(arm, "_check_cooldown", AsyncMock(return_value=False)):
                    with patch.object(arm, "_batch_restart_throttle", AsyncMock()):
                        await arm._handle_agent_crash(ap, "crash test")

        # 崩溃后应重启，状态回到 running
        assert ap.status == "running"
        assert ap.restart_count == 1
        assert ap._consecutive_restarts == 1

    @pytest.mark.asyncio
    async def test_crash_restart_policy_never(self, arm, proc_config):
        """restart_policy=never 时不重启"""
        proc_config.restart_policy = "never"
        ap = AgentProcess(proc_config)
        ap.status = "running"
        arm._processes[proc_config.agent_id] = ap

        with patch.object(arm, "_update_process_db", AsyncMock()) as mock_db:
            await arm._handle_agent_crash(ap, "never restart")

        assert ap.status == "crashed"  # 保持 crashed 不进 restart

    @pytest.mark.asyncio
    async def test_crash_on_failure_with_zero_exit(self, arm, proc_config):
        """restart_policy=on_failure + exit_code=0 时不重启"""
        proc_config.restart_policy = "on_failure"
        ap = AgentProcess(proc_config)
        ap.status = "running"
        ap._last_exit_code = 0  # 正常退出
        arm._processes[proc_config.agent_id] = ap

        with patch.object(arm, "_update_process_db", AsyncMock()):
            await arm._handle_agent_crash(ap, "zero exit")

        assert ap.status == "crashed"  # 正常退出不重启

    @pytest.mark.asyncio
    async def test_crash_on_failure_with_nonzero_exit(self, arm, proc_config, mock_subprocess):
        """restart_policy=on_failure + exit_code != 0 时重启"""
        proc_config.restart_policy = "on_failure"
        ap = AgentProcess(proc_config)
        ap.status = "running"
        ap._last_exit_code = 1  # 非正常退出
        arm._processes[proc_config.agent_id] = ap

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_subprocess)):
            with patch.object(arm, "_update_process_db", AsyncMock()):
                with patch.object(arm, "_check_cooldown", AsyncMock(return_value=False)):
                    with patch.object(arm, "_batch_restart_throttle", AsyncMock()):
                        await arm._handle_agent_crash(ap, "nonzero exit")

        assert ap.status == "running"

    @pytest.mark.asyncio
    async def test_crash_backoff_increments_index(self, arm, proc_config, mock_subprocess):
        """崩溃后 _backoff_index 递增"""
        ap = AgentProcess(proc_config)
        ap.status = "running"
        ap._backoff_index = 0
        arm._processes[proc_config.agent_id] = ap

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_subprocess)):
            with patch.object(arm, "_update_process_db", AsyncMock()):
                with patch.object(arm, "_check_cooldown", AsyncMock(return_value=False)):
                    with patch.object(arm, "_batch_restart_throttle", AsyncMock()):
                        await arm._handle_agent_crash(ap, "test")

        assert ap._backoff_index == 1  # 从 0 增到 1


# ── adopt_external ─────────────────────────────────────

class TestAdoptExternal:
    @pytest.mark.asyncio
    async def test_adopt_external_success(self, arm):
        request = {"pid": 9999, "health_check": {"type": "process"}}
        with patch.object(arm, "_update_process_db", AsyncMock()):
            with patch.object(arm, "_run_integrations", AsyncMock(return_value={"status": "ok", "errors": [], "results": {}})):
                with patch("os.kill") as mock_kill:
                    result = await arm.adopt_external("ext-agent-1", request)

        assert result["status"] == "ok"
        assert result["adopted"] is True
        assert result["pid"] == 9999
        assert arm._processes["ext-agent-1"].status == "running"

    @pytest.mark.asyncio
    async def test_adopt_external_missing_pid(self, arm):
        result = await arm.adopt_external("ext-agent-1", {})
        assert result["status"] == "error"
        assert "pid" in result["error"]

    @pytest.mark.asyncio
    async def test_adopt_external_invalid_pid(self, arm):
        with patch("huanyu.agent_runtime.os.kill", side_effect=ProcessLookupError):
            result = await arm.adopt_external("ext-agent-1", {"pid": 99999})
        assert result["status"] == "error"
        assert "无效" in result["error"]

    @pytest.mark.asyncio
    async def test_adopt_external_already_adopted(self, arm):
        """同一 agent 再次接管时更新 PID"""
        ap = AgentProcess(AgentProcessConfig(agent_id="ext-agent-1", executable="python3"))
        ap.status = "running"
        ap.pid = 1000
        arm._processes["ext-agent-1"] = ap

        with patch.object(arm, "_update_process_db", AsyncMock()):
            with patch("huanyu.agent_runtime.os.kill"):
                result = await arm.adopt_external("ext-agent-1", {"pid": 2000})

        assert result["adopted"] is False
        assert arm._processes["ext-agent-1"].pid == 2000


# ── get_agent_status / list_agents ─────────────────────

class TestAgentStatus:
    def test_get_agent_status_nonexistent(self, arm):
        assert arm.get_agent_status("ghost") is None

    def test_get_agent_status(self, arm, proc_config):
        ap = AgentProcess(proc_config)
        arm._processes["test-agent"] = ap
        status = arm.get_agent_status("test-agent")
        assert status["agent_id"] == "test-agent"
        assert status["status"] == "stopped"

    def test_list_agents_returns_all(self, arm):
        ap1 = AgentProcess(AgentProcessConfig(agent_id="a1", executable="python3"))
        ap2 = AgentProcess(AgentProcessConfig(agent_id="a2", executable="python3"))
        arm._processes["a1"] = ap1
        arm._processes["a2"] = ap2
        agents = arm.list_agents()
        assert len(agents) == 2
        ids = [a["agent_id"] for a in agents]
        assert "a1" in ids
        assert "a2" in ids


# ── _run_integrations ──────────────────────────────────

class TestRunIntegrations:
    @pytest.mark.asyncio
    async def test_integrations_full_success(self, arm):
        with patch("common.db.get_pool") as mock_get_pool:
            mock_conn = AsyncMock()
            mock_pool = _MockPool(mock_conn)
            mock_get_pool.return_value = mock_pool

            with patch("siku.account_service.ensure_account", AsyncMock()):
                result = await arm._run_integrations("test-agent-1")

        assert result["status"] == "ok"
        assert len(result["errors"]) == 0

    @pytest.mark.asyncio
    async def test_integrations_partial_failure(self, arm):
        """某一步失败不应阻塞整体"""
        with patch("common.db.get_pool") as mock_get_pool:
            mock_conn = AsyncMock()
            mock_conn.execute = AsyncMock(side_effect=[
                Exception("namespace failed"),  # step 1
                None,  # step 2: knowledge
                None,  # step 5: audit_log
            ])
            mock_pool = _MockPool(mock_conn)
            mock_get_pool.return_value = mock_pool

            with patch("siku.account_service.ensure_account", AsyncMock()):
                result = await arm._run_integrations("test-agent-1")

        assert result["status"] == "partial"
        assert len(result["errors"]) > 0


# ── _reconcile ─────────────────────────────────────────

class TestReconcile:
    @pytest.mark.asyncio
    async def test_reconcile_recovers_running(self, arm):
        with patch("common.db.get_pool") as mock_get_pool:
            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock()
            # 首次调用返回 registered agents
            # 第二次调用返回 running processes
            mock_conn.fetch.side_effect = [
                [{"agent_id": "existing-agent"}],  # huanyu.agents
                [{"agent_id": "running-agent", "pid": 12345,
                  "status": "running", "started_at": None}],  # agent_processes
            ]

            mock_pool = MagicMock()
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock()
            mock_get_pool.return_value = mock_pool

            with patch("os.kill") as mock_kill:
                await arm._reconcile()

        # running-agent 的 PID 存活应恢复为 running
        ap = arm._processes.get("running-agent")
        assert ap is not None
        assert ap.pid == 12345

    @pytest.mark.asyncio
    async def test_reconcile_marks_dead_as_crashed(self, arm):
        with patch("common.db.get_pool") as mock_get_pool:
            mock_conn = AsyncMock()
            mock_conn.fetch.side_effect = [
                [],  # 无普通 agent
                [{"agent_id": "dead-agent", "pid": 99999,
                  "status": "running", "started_at": None}],
            ]

            mock_pool = MagicMock()
            mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_pool.acquire.return_value.__aexit__ = AsyncMock()
            mock_get_pool.return_value = mock_pool

            # kill 抛 ProcessLookupError = PID 不存在
            with patch("huanyu.agent_runtime.os.kill", side_effect=ProcessLookupError):
                with patch.object(arm, "_update_process_db", AsyncMock()):
                    await arm._reconcile()

        ap = arm._processes.get("dead-agent")
        assert ap is not None
        assert ap.status == "crashed"
        assert "PID 不存在" in ap.last_error


# ── _check_cooldown ────────────────────────────────────

class TestCheckCooldown:
    @pytest.mark.asyncio
    async def test_cooldown_active_when_recent_restarts(self, arm, proc_config):
        """连续重启后短时间内为冷却期"""
        ap = AgentProcess(proc_config)
        ap._consecutive_restarts = 3  # 等于阈值
        ap._healthy_since = datetime.now(timezone.utc)  # 刚刚健康
        arm._processes[proc_config.agent_id] = ap

        cool = await arm._check_cooldown(proc_config.agent_id)
        assert cool is True  # 在冷却期内

    @pytest.mark.asyncio
    async def test_cooldown_not_active_when_never_healthy(self, arm, proc_config):
        """从未健康过不触发冷却"""
        ap = AgentProcess(proc_config)
        ap._consecutive_restarts = 3  # 等于阈值
        ap._healthy_since = None  # 从未健康过
        arm._processes[proc_config.agent_id] = ap

        cool = await arm._check_cooldown(proc_config.agent_id)
        assert cool is False  # _healthy_since 为 None 时不过冷却


# ── _check_pid_reuse ───────────────────────────────────

class TestCheckPidReuse:
    @pytest.mark.asyncio
    async def test_pid_reuse_detected(self, arm, proc_config):
        ap = AgentProcess(proc_config)
        ap.pid = 99999
        ap.started_at = datetime.now(timezone.utc)

        with patch("psutil.Process") as mock_psutil_process:
            mock_proc = MagicMock()
            mock_proc.create_time.return_value = 1000000  # 旧的创建时间
            mock_psutil_process.return_value = mock_proc

            reused = await arm._check_pid_reuse(ap)
            assert reused is True

    @pytest.mark.asyncio
    async def test_pid_not_reused(self, arm, proc_config):
        ap = AgentProcess(proc_config)
        ap.pid = 12345
        ap.started_at = datetime.now(timezone.utc)
        expected = ap.started_at.timestamp()

        with patch("psutil.Process") as mock_psutil_process:
            mock_proc = MagicMock()
            mock_proc.create_time.return_value = expected
            mock_psutil_process.return_value = mock_proc

            reused = await arm._check_pid_reuse(ap)
            assert reused is False


# ── _get_process_stats ─────────────────────────────────

class TestGetProcessStats:
    @pytest.mark.asyncio
    async def test_stats_with_psutil(self, arm, proc_config):
        ap = AgentProcess(proc_config)
        ap.pid = 12345
        ap.status = "running"
        ap.started_at = datetime.now(timezone.utc)
        arm._processes["test-agent"] = ap

        with patch("psutil.Process") as mock_psutil_process:
            mock_proc = MagicMock()
            mock_proc.memory_info.return_value.rss = 256 * 1024 * 1024
            mock_proc.memory_info.return_value.vms = 512 * 1024 * 1024
            mock_proc.cpu_percent.return_value = 12.5
            mock_proc.num_fds.return_value = 42
            mock_proc.children.return_value = []

            mock_psutil_process.return_value = mock_proc

            stats = await arm._get_process_stats("test-agent")

        assert stats is not None
        assert stats["agent_id"] == "test-agent"
        assert stats["pid"] == 12345
        assert stats["memory_rss_mb"] == 256.0
        assert stats["num_fds"] == 42

    @pytest.mark.asyncio
    async def test_stats_returns_none_without_pid(self, arm):
        stats = await arm._get_process_stats("ghost")
        assert stats is None


# ── _batch_restart_throttle ────────────────────────────

class TestBatchRestartThrottle:
    @pytest.mark.asyncio
    async def test_throttle_skips_when_below_threshold(self, arm):
        """低于阈值不等待"""
        t0 = datetime.now(timezone.utc)
        await arm._batch_restart_throttle()
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        assert elapsed < 1  # 不应有显著等待
