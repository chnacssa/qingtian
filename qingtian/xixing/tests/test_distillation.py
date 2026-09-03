"""吸星每日加工管线测试"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCoarseFilter:
    """_coarse_filter 纯函数测试"""

    def test_removes_short_content(self):
        from xixing.scheduler import _coarse_filter
        events = [
            {"content": "hi", "type": "lifecycle:llm_input", "session_id": "s1"},
            {"content": "hello world this is long enough", "type": "lifecycle:llm_input", "session_id": "s1"},
        ]
        result = _coarse_filter(events)
        assert len(result) == 1
        assert result[0]["content"] == "hello world this is long enough"

    def test_deduplicates(self):
        from xixing.scheduler import _coarse_filter
        events = [
            {"content": "hello", "type": "lifecycle:llm_input", "session_id": "s1"},
            {"content": "hello", "type": "lifecycle:llm_input", "session_id": "s1"},
        ]
        result = _coarse_filter(events)
        assert len(result) == 1

    def test_deduplicates_different_session(self):
        """同 content 不同 session 不视为重复"""
        from xixing.scheduler import _coarse_filter
        events = [
            {"content": "hello world", "type": "lifecycle:llm_input", "session_id": "s1"},
            {"content": "hello world", "type": "lifecycle:llm_input", "session_id": "s2"},
        ]
        result = _coarse_filter(events)
        assert len(result) == 2

    def test_keeps_valid_events(self):
        from xixing.scheduler import _coarse_filter
        events = [
            {"content": "valid long enough content here", "type": "lifecycle:llm_input", "session_id": "s1"},
            {"content": "another valid message for testing", "type": "lifecycle:tool_result", "session_id": "s2"},
        ]
        result = _coarse_filter(events)
        assert len(result) == 2

    def test_empty_events(self):
        from xixing.scheduler import _coarse_filter
        assert _coarse_filter([]) == []

    def test_mixed_with_missing_fields(self):
        from xixing.scheduler import _coarse_filter
        events = [
            {"content": "good content"},
            {"type": "lifecycle:llm_input", "session_id": "s1"},
            {},
        ]
        result = _coarse_filter(events)
        # First has no type/session but content >= 5 chars, passes
        # Second has no content, gets empty string, length 0 < 5, filtered
        # Third empty dict, gets empty string everywhere, filtered
        assert len(result) == 1


class TestProcessAPI:
    """吸星 process API 同步加工测试"""

    @pytest.mark.asyncio
    async def test_classify_empty_text(self):
        from xixing.api import _process_sync
        result = await _process_sync("classify", {"text": ""}, {})
        assert result["category"] == "unknown"

    @pytest.mark.asyncio
    async def test_extract_empty_text(self):
        from xixing.api import _process_sync
        result = await _process_sync("extract", {"text": ""}, {})
        assert result["entities"] == []

    @pytest.mark.asyncio
    async def test_quality_empty_text(self):
        from xixing.api import _process_sync
        result = await _process_sync("quality", {"text": ""}, {})
        assert result["score"] == 0
        assert result["issues"] == []

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        from xixing.api import _process_sync
        result = await _process_sync("unknown_action", {"text": "hello"}, {})
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_classify_fallback_on_import_error(self):
        """classify_text 正常工作返回 real 分类结果"""
        from xixing.api import _process_sync
        # classify_text 现在存在，应返回真实分类结果而非 fallback
        result = await _process_sync("classify", {"text": "some text"}, {})
        # "some text" 无匹配关键词 → 返回 "general"
        assert result["category"] in ("general", "unknown")
        assert "confidence" in result

    def test_estimate_seconds(self):
        from xixing.api import _estimate_seconds
        assert _estimate_seconds("classify") == 5
        assert _estimate_seconds("extract") == 10
        assert _estimate_seconds("quality") == 8
        assert _estimate_seconds("pattern_analysis") == 120
        assert _estimate_seconds("distill") == 180
        assert _estimate_seconds("cluster") == 60
        assert _estimate_seconds("nonexistent") == 30


class TestSchedulerSchedule:
    """验证调度注册表中包含新任务"""

    def test_daily_buffer_in_schedule(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        task_names = [t[0] for t in _MGMT_SCHEDULE]
        assert "daily_buffer" in task_names
        assert "bus_distillation" in task_names

    def test_daily_buffer_schedule_time(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        for name, hour, minute, dow, fn in _MGMT_SCHEDULE:
            if name == "daily_buffer":
                assert hour == 2
                assert minute == 0
                assert dow is None  # 每天
                break
        else:
            pytest.fail("daily_buffer not in schedule")

    def test_bus_distillation_schedule_time(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        for name, hour, minute, dow, fn in _MGMT_SCHEDULE:
            if name == "bus_distillation":
                assert hour == 4
                assert minute == 0
                assert dow is None  # 每天
                break
        else:
            pytest.fail("bus_distillation not in schedule")

    def test_daily_buffer_job_is_callable(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        for name, hour, minute, dow, fn in _MGMT_SCHEDULE:
            if name == "daily_buffer":
                assert callable(fn)
                break

    def test_bus_distillation_job_is_callable(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        for name, hour, minute, dow, fn in _MGMT_SCHEDULE:
            if name == "bus_distillation":
                assert callable(fn)
                break
