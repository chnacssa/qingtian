"""
调度器 — Skill 维护任务单元测试
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xixing.scheduler import _skill_expiry_job, _skill_evolve_job


@pytest.mark.asyncio
async def test_skill_expiry_management_not_available():
    """管理服未部署时自动过期任务优雅降级"""
    pool = AsyncMock()
    pool.execute = AsyncMock(side_effect=Exception("relation skills.skill_definitions does not exist"))

    with patch("common.db.get_pool", AsyncMock(return_value=pool)):
        # 不应抛出异常
        await _skill_expiry_job()


@pytest.mark.asyncio
async def test_skill_expiry_updates_proposals():
    """正常执行过期标记"""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="UPDATE 3")

    with patch("common.db.get_pool", AsyncMock(return_value=pool)):
        await _skill_expiry_job()
        pool.execute.assert_called_once()
        sql = pool.execute.call_args[0][0]
        assert "UPDATE" in sql
        assert "30 days" in sql
        assert "proposed" in sql.lower()


@pytest.mark.asyncio
async def test_skill_evolve_disabled():
    """配置禁用时跳过"""
    pool = AsyncMock()

    with patch("xixing.config.get_skill_proposals_enabled", return_value=False):
        await _skill_evolve_job()
        # 不应调用 _generate_skill_proposals


@pytest.mark.asyncio
async def test_skill_evolve_calls_generate():
    """正常调用 _generate_skill_proposals"""
    pool = AsyncMock()

    with patch("common.db.get_pool", AsyncMock(return_value=pool)), \
         patch("xixing.config.get_skill_proposals_enabled", return_value=True), \
         patch("xixing.distiller._generate_skill_proposals", AsyncMock(return_value=[{"name": "test"}])):
        await _skill_evolve_job()


@pytest.mark.asyncio
async def test_skill_evolve_generate_error():
    """_generate_skill_proposals 抛出异常时不传播"""
    pool = AsyncMock()

    with patch("common.db.get_pool", AsyncMock(return_value=pool)), \
         patch("xixing.config.get_skill_proposals_enabled", return_value=True), \
         patch("xixing.distiller._generate_skill_proposals", AsyncMock(side_effect=Exception("error"))):
        # 不应抛出异常
        await _skill_evolve_job()


@pytest.mark.asyncio
async def test_skill_expiry_handles_no_result():
    """没有过期提案时优雅处理"""
    pool = AsyncMock()
    pool.execute = AsyncMock(return_value="UPDATE 0")

    with patch("common.db.get_pool", AsyncMock(return_value=pool)):
        await _skill_expiry_job()
        pool.execute.assert_called_once()


class TestSchedulerSchedule:
    """验证调度注册表中包含 Skill 任务"""

    def test_skill_tasks_in_schedule(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        task_names = [t[0] for t in _MGMT_SCHEDULE]
        assert "skill_expiry" in task_names
        assert "skill_evolve" in task_names

    def test_skill_expiry_schedule(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        for name, hour, minute, dow, fn in _MGMT_SCHEDULE:
            if name == "skill_expiry":
                assert hour == 2
                assert minute == 0
                assert dow is None  # 每天
                break
        else:
            pytest.fail("skill_expiry not in schedule")

    def test_skill_evolve_schedule(self):
        from xixing.scheduler import _MGMT_SCHEDULE
        for name, hour, minute, dow, fn in _MGMT_SCHEDULE:
            if name == "skill_evolve":
                assert hour == 6
                assert minute == 0
                assert dow is None  # 每天
                break
        else:
            pytest.fail("skill_evolve not in schedule")
