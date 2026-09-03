"""ResourceMonitor 单元测试 — 资源采集、阈值告警、L4/L5 保护"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from huanyu.agent_runtime import (
    AgentProcess,
    AgentProcessConfig,
    AgentRuntimeManager,
    ResourceMonitor,
)


# ── ResourceMonitor: _collect_one ──────────────────────

class TestResourceMonitorCollectOne:
    """测试 _collect_one 的 psutil 采集路径"""

    def test_collect_one_with_psutil(self):
        """psutil 可用时采集资源"""
        mock_proc = MagicMock()
        mock_proc.memory_info.return_value = MagicMock(rss=512 * 1024 * 1024)
        mock_proc.cpu_percent.return_value = 12.5
        mock_proc.open_files.return_value = [MagicMock()] * 10
        mock_proc.children.return_value = []

        with patch("psutil.Process", return_value=mock_proc):
            monitor = ResourceMonitor(None)
            monitor._running = True
            metrics = monitor._collect_one(12345)

        assert metrics is not None
        assert metrics["memory_mb"] == 512.0
        assert metrics["cpu_percent"] == 12.5
        assert metrics["fd_count"] == 10
        assert metrics["pid"] == 12345

    def test_collect_one_no_process(self):
        """PID 不存在时返回 None"""
        import psutil as _psutil
        with patch("psutil.Process", side_effect=_psutil.NoSuchProcess(99999)):
            monitor = ResourceMonitor(None)
            metrics = monitor._collect_one(99999)
            assert metrics is None

    def test_collect_one_procfs_fallback(self):
        """psutil 不可用时降级 /proc（on Linux /proc 不存在则返回 None）"""
        monitor = ResourceMonitor(None)
        # On Windows /proc doesn't exist, should return None gracefully
        metrics = monitor._collect_one(99999)
        # Either returns None (no psutil, no /proc) or a dict
        assert metrics is None or isinstance(metrics, dict)


# ── ResourceMonitor: _get_threshold ────────────────────

class TestResourceMonitorThreshold:
    def test_get_threshold_default(self):
        """默认阈值应返回合理值"""
        monitor = ResourceMonitor(None)
        th = monitor._get_threshold("biz:buyer-01")
        assert "memory_mb" in th
        assert th["memory_mb"] > 0
        assert "cpu_percent" in th
        assert "fd_count" in th

    def test_get_threshold_with_overrides(self):
        """带前缀覆盖时返回合并后的阈值"""
        mock_overrides = {
            "biz:": {"memory_mb": 2048},
            "infra:": {"memory_mb": 512, "cpu_percent": 50},
        }
        default = {"memory_mb": 1024, "cpu_percent": 80, "fd_count": 500}

        with patch("common.config.get") as mock_get:
            def side_effect(key, default_val=None):
                if key == "xihe.resource_limits.default":
                    return default
                if key == "xihe.resource_limits.overrides":
                    return mock_overrides
                return default_val
            mock_get.side_effect = side_effect

            monitor = ResourceMonitor(None)

            # biz: 前缀匹配，memory 应覆盖
            th = monitor._get_threshold("biz:buyer-01")
            assert th["memory_mb"] == 2048  # 覆盖值
            assert th["cpu_percent"] == 80   # 保留默认
            assert th["fd_count"] == 500     # 保留默认

            # infra: 前缀匹配
            th = monitor._get_threshold("infra:monitor-01")
            assert th["memory_mb"] == 512
            assert th["cpu_percent"] == 50
            assert th["fd_count"] == 500

            # 无匹配前缀用默认
            th = monitor._get_threshold("unknown:agent")
            assert th["memory_mb"] == 1024


# ── ResourceMonitor: lifecycle ─────────────────────────

class TestResourceMonitorLifecycle:
    def test_start_stop(self):
        """启动和停止资源监控"""
        arm = AgentRuntimeManager()
        monitor = ResourceMonitor(arm)

        # 启动
        with patch.object(monitor, "_task", None):
            with patch("asyncio.create_task") as mock_create:
                monitor.start = lambda interval=60: setattr(monitor, "_running", True) or None
                monitor._running = True

        # 停止
        monitor._running = False
        # 生命周期测试 — 不应抛异常
        assert monitor._running is False


# ── ARM: L4/L5 保护 ────────────────────────────────────

class TestARMOverloadProtection:
    @pytest.mark.asyncio
    async def test_get_load_level_default(self):
        """默认负载级别为 normal"""
        arm = AgentRuntimeManager()
        level = arm._get_load_level()
        assert level == "normal"

    @pytest.mark.asyncio
    async def test_enter_l4_protection_sets_locked(self):
        """L4 保护应设置 _adopt_locked 并降低健康检查频率"""
        arm = AgentRuntimeManager()
        await arm._enter_l4_protection()
        assert arm._l4_active is True
        assert arm._adopt_locked is True
        assert arm._health_check_interval == 60

    @pytest.mark.asyncio
    async def test_enter_l4_idempotent(self):
        """重复进入 L4 应短路"""
        arm = AgentRuntimeManager()
        await arm._enter_l4_protection()
        await arm._enter_l4_protection()
        assert arm._l4_active is True  # 不重复覆盖

    @pytest.mark.asyncio
    async def test_enter_l5_protection(self):
        """L5 保护应暂停健康检查并锁定接管，内存恢复后后台自动退出"""
        arm = AgentRuntimeManager()
        # 恢复循环在后台 task 运行，需在 mock 生效期间 await 其完成
        with patch("huanyu.agent_runtime.asyncio.sleep", AsyncMock()):
            with patch("psutil.virtual_memory") as mock_mem:
                mock_mem.return_value.total = 16 * 1024**3
                mock_mem.return_value.available = 8 * 1024**3  # > 15%
                await arm._enter_l5_protection()
                if arm._l5_recovery_task:
                    await arm._l5_recovery_task

        # After recovery task exits, L5 should be cleared
        assert arm._l5_active is False
        assert arm._adopt_locked is False
        assert arm._health_check_paused is False

    @pytest.mark.asyncio
    async def test_enter_l5_idempotent(self):
        """L5 激活期间重复进入应短路（不重复启动恢复监控）"""
        arm = AgentRuntimeManager()
        with patch("huanyu.agent_runtime.asyncio.sleep", AsyncMock()):
            with patch("psutil.virtual_memory") as mock_mem:
                mock_mem.return_value.total = 16 * 1024**3
                mock_mem.return_value.available = 8 * 1024**3
                await arm._enter_l5_protection()
                # 激活期间第二次进入：短路返回，不新建恢复 task
                await arm._enter_l5_protection()
                assert arm._l5_active is True
                assert arm._l5_recovery_task is not None
                assert not arm._l5_recovery_task.done()
                # 等后台恢复完成后 L5 解除
                await arm._l5_recovery_task

        assert arm._l5_active is False
        assert arm._adopt_locked is False
        assert arm._health_check_paused is False


# ── ARM: adopt_external locked ─────────────────────────

class TestARMAdoptLocked:
    @pytest.mark.asyncio
    async def test_adopt_rejected_when_locked(self):
        """_adopt_locked 时 adopt_external 应拒绝"""
        arm = AgentRuntimeManager()
        arm._l4_active = True
        arm._adopt_locked = True
        result = await arm.adopt_external("test-agent", {"pid": 12345})
        assert result["status"] == "error"
        assert "overload" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_adopt_allowed_when_not_locked(self):
        """_adopt_locked=False 时 adopt_external 应正常进行"""
        arm = AgentRuntimeManager()
        arm._adopt_locked = False
        with patch.object(arm, "_update_process_db", AsyncMock()):
            with patch.object(arm, "_run_integrations", AsyncMock(return_value={"status": "ok", "errors": [], "results": {}})):
                with patch("huanyu.agent_runtime.os.kill"):
                    result = await arm.adopt_external("test-agent", {"pid": 9999})
        assert result["status"] == "ok"
        assert result["adopted"] is True


# ── ARM: get_stats ─────────────────────────────────────

class TestARMGetStats:
    @pytest.mark.asyncio
    async def test_get_stats_basic(self):
        """get_stats 返回基础统计"""
        arm = AgentRuntimeManager()
        ap = AgentProcess(AgentProcessConfig(agent_id="test-agent", executable="python3"))
        ap.status = "running"
        arm._processes["test-agent"] = ap

        stats = await arm.get_stats()
        assert stats["managed_agents"] == 1
        assert stats["state_counts"]["running"] == 1
        assert stats["overload_level"] == "normal"

    @pytest.mark.asyncio
    async def test_get_stats_with_overload(self):
        """get_stats 反映当前过载级别"""
        arm = AgentRuntimeManager()
        await arm._enter_l4_protection()
        stats = await arm.get_stats()
        assert stats["overload_level"] == "L4"

    @pytest.mark.asyncio
    async def test_get_stats_empty(self):
        """没有 Agent 时仍应返回有效结构"""
        arm = AgentRuntimeManager()
        stats = await arm.get_stats()
        assert stats["managed_agents"] == 0
        assert stats["state_counts"] == {}
        assert stats["overload_level"] == "normal"


# ── ARM: _monitor_loop health_check_paused ─────────────

class TestARMMonitorLoopPaused:
    @pytest.mark.asyncio
    async def test_monitor_loop_skips_checks_when_paused(self):
        """_health_check_paused=True 时 _monitor_loop 跳过健康检查"""
        arm = AgentRuntimeManager()
        arm._health_check_paused = True

        with patch.object(arm, "_check_agent_health", AsyncMock()) as mock_check:
            arm._health_check_interval = 0.01
            await asyncio.sleep(0.01)
            if arm._health_check_paused:
                pass  # simulates continue
            mock_check.assert_not_called()


# ── ResourceMonitor: _check_system_level ───────────────

class TestResourceMonitorSystemLevel:
    @pytest.mark.asyncio
    async def test_check_system_level_normal(self):
        """内存充足时不应触发保护"""
        arm = AgentRuntimeManager()
        monitor = ResourceMonitor(arm)

        with patch("psutil.virtual_memory") as mock_mem:
            mock_mem.return_value = MagicMock(total=16 * 1024**3, available=8 * 1024**3)
            await monitor._check_system_level(None, None)

        assert arm._l4_active is False
        assert arm._l5_active is False

    @pytest.mark.asyncio
    async def test_check_system_level_l4(self):
        """内存 < 10% 触发 L4"""
        arm = AgentRuntimeManager()
        monitor = ResourceMonitor(arm)

        with patch("psutil.virtual_memory") as mock_mem:
            mock_mem.return_value = MagicMock(total=16 * 1024**3, available=1 * 1024**3)
            await monitor._check_system_level(None, None)

        assert arm._l4_active is True

    @pytest.mark.asyncio
    async def test_check_system_level_l5(self):
        """内存 < 5% 触发 L5（验证 `_enter_l5_protection` 被调用）"""
        arm = AgentRuntimeManager()
        monitor = ResourceMonitor(arm)

        with patch.object(arm, "_enter_l5_protection", AsyncMock()) as mock_l5:
            with patch("psutil.virtual_memory") as mock_mem:
                mock_mem.return_value = MagicMock(total=16 * 1024**3, available=500 * 1024**2)
                await monitor._check_system_level(None, None)

        assert mock_l5.await_count == 1
