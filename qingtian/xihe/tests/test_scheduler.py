"""羲和 — scheduler 纯函数测试 + agent_runtime 纯函数测试"""

import pytest

from xihe.scheduler import (
    PRIORITY_WEIGHTS,
    CPU_QUOTA_MAP,
    check_memory_pressure,
    read_cpu_usage,
    read_memory_usage,
    check_system_overload,
    CpuMonitor,
    cpu_monitor,
)
from xihe.agent_runtime import (
    _hex_to_ip,
    _apply_runtime_decision,
    EGRESS_WHITELIST,
    _make_key,
)


class TestSchedulerConstants:
    """调度器常量验证"""

    def test_priority_weights(self):
        assert PRIORITY_WEIGHTS["high"] == 200
        assert PRIORITY_WEIGHTS["normal"] == 100
        assert PRIORITY_WEIGHTS["low"] == 50

    def test_cpu_quota_map(self):
        assert CPU_QUOTA_MAP["high"] == (100000, 100000)
        assert CPU_QUOTA_MAP["normal"] == (50000, 100000)
        assert CPU_QUOTA_MAP["low"] == (25000, 100000)

    def test_unknown_priority_returns_false_on_ci(self):
        """未知优先级兜底 normal 权重，但不影响 set_cpu_weight 返回值"""
        from xihe.scheduler import set_cpu_weight
        # 返回值取决于 cgroups 是否可用（Windows/macOS/Linux），我们不对此做断言
        # 只验证不抛异常即可
        set_cpu_weight(999999, "unknown_priority")


class TestCheckMemoryPressure:
    """内存压力检查"""

    def test_no_usage_returns_false(self):
        # Windows 上 read_memory_usage 返回 None
        assert check_memory_pressure(999999, 1024) is False

    def test_no_limit_returns_false(self):
        assert check_memory_pressure(999999, None) is False

    def test_zero_limit_returns_false(self):
        assert check_memory_pressure(999999, 0) is False

    def test_negative_limit_returns_false(self):
        assert check_memory_pressure(999999, -1) is False


class TestReadCpuUsage:
    """CPU 使用率读取（非 Linux 返回 None）"""

    def test_none_on_windows(self):
        assert read_cpu_usage(999999) is None


class TestReadMemoryUsage:
    """内存使用率读取（非 Linux 返回 None）"""

    def test_none_on_windows(self):
        assert read_memory_usage(999999) is None


class TestCpuMonitor:
    """CPU 监控器"""

    def test_sample_none_without_prior(self):
        """首次采样无差值，返回 None"""
        mon = CpuMonitor()
        assert mon.sample(999999) is None

    def test_check_threshold_false_without_data(self):
        mon = CpuMonitor()
        assert mon.check_threshold(999999) is False

    def test_global_monitor_is_singleton(self):
        assert isinstance(cpu_monitor, CpuMonitor)


class TestCheckSystemOverload:
    """系统过载检测"""

    def test_no_proc_returns_none(self):
        # Windows 无 /proc/stat
        result = check_system_overload()
        # 返回 False 而非抛异常
        assert result is False


class TestHexToIp:
    """IP 十六进制转点分十进制"""

    def test_ipv4_localhost(self):
        # 127.0.0.1 = 0x0100007F in LE /proc/net
        assert _hex_to_ip("0100007F") == "127.0.0.1"

    def test_ipv4_standard(self):
        # /proc/net/tcp 使用 host byte order (LE on x86)，192.168.1.1 = "0101A8C0"
        assert _hex_to_ip("0101A8C0") == "192.168.1.1"

    def test_ipv4_google_dns(self):
        # 8.8.8.8 = "08080808"
        assert _hex_to_ip("08080808") == "8.8.8.8"

    def test_ipv4_private_10(self):
        # 10.0.0.1 = "0100000A"
        assert _hex_to_ip("0100000A") == "10.0.0.1"

    def test_ipv4_short_input(self):
        assert _hex_to_ip("7F") == "127.0.0.0"

    def test_empty_input(self):
        assert _hex_to_ip("") == "0.0.0.0"

    def test_invalid_hex(self):
        assert _hex_to_ip("ZZZZ") == "0.0.0.0"

    def test_ipv6_loopback(self):
        # ::1 = 00000000000000000000000000000001 in /proc/net（不压缩零）
        result = _hex_to_ip("00000000000000000000000000000001")
        assert ":" in result
        # 当前实现不压缩零，全展开格式
        assert result == "0000:0000:0000:0000:0000:0000:0000:0001"


class TestApplyRuntimeDecision:
    """风险决策矩阵"""

    def test_score_ge80_revokes(self):
        assert _apply_runtime_decision(80, "low") == "revoke"
        assert _apply_runtime_decision(90, "") == "revoke"
        assert _apply_runtime_decision(100, "critical") == "revoke"

    def test_score_50_80_with_high_pauses(self):
        assert _apply_runtime_decision(50, "high") == "pause"
        assert _apply_runtime_decision(60, "critical") == "pause"

    def test_score_50_80_with_medium_downgrades(self):
        assert _apply_runtime_decision(50, "medium") == "downgrade"
        assert _apply_runtime_decision(79, "medium") == "downgrade"

    def test_score_50_80_with_low_alerts(self):
        assert _apply_runtime_decision(50, "low") == "alert"
        assert _apply_runtime_decision(65, "") == "alert"

    def test_score_20_50_with_high_downgrades(self):
        assert _apply_runtime_decision(20, "high") == "downgrade"
        assert _apply_runtime_decision(35, "critical") == "downgrade"

    def test_score_20_50_with_medium_alerts(self):
        assert _apply_runtime_decision(20, "medium") == "alert"
        assert _apply_runtime_decision(49, "medium") == "alert"

    def test_score_20_50_with_low_logs(self):
        assert _apply_runtime_decision(20, "low") == "log"
        assert _apply_runtime_decision(30, "") == "log"

    def test_score_lt20_always_logs(self):
        assert _apply_runtime_decision(0, "critical") == "log"
        assert _apply_runtime_decision(10, "high") == "log"
        assert _apply_runtime_decision(19, "medium") == "log"

    def test_unknown_severity_treated_as_empty(self):
        assert _apply_runtime_decision(5, "unknown") == "log"


class TestEgressWhitelist:
    """出站白名单"""

    def test_contains_loopback(self):
        assert "127.0.0.1" in EGRESS_WHITELIST

    def test_contains_ipv6_loopback(self):
        assert "::1" in EGRESS_WHITELIST

    def test_contains_private_ranges(self):
        assert "10." in EGRESS_WHITELIST
        assert "172.16." in EGRESS_WHITELIST
        assert "192.168." in EGRESS_WHITELIST


class TestMakeKey:
    """内部索引键生成"""

    def test_with_agent(self):
        assert _make_key("bidding", "agent_01") == "agent_01:bidding"

    def test_without_agent(self):
        assert _make_key("bidding", "") == "bidding"

    def test_empty_skill_name(self):
        assert _make_key("", "agent_01") == "agent_01:"
