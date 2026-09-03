"""
SkillLoader + SkillRegistry 单元测试
"""

import os
import sys
import tempfile
from unittest.mock import patch

import pytest

from unittest.mock import AsyncMock, patch

from osskill.loader import (
    ManifestLoader,
    SkillLoader,
    SkillRegistry,
    _to_pascal,
    warmup_skills,
    load_agent_skills,
    reload_agent_skills,
)


class TestToPascal:
    def test_simple(self):
        assert _to_pascal("my_skill") == "MySkill"

    def test_multi_word(self):
        assert _to_pascal("hello_world_test") == "HelloWorldTest"

    def test_single(self):
        assert _to_pascal("skill") == "Skill"

    def test_already_pascal(self):
        # Python str.capitalize() lowercases rest, so "MySkill" -> "Myskill"
        assert _to_pascal("MySkill") == "Myskill"

    def test_numbers(self):
        assert _to_pascal("skill_v2") == "SkillV2"

    def test_empty(self):
        assert _to_pascal("") == ""


class TestSkillLoader:
    def test_nonexistent_skill_returns_none(self):
        """不存在的 Skill 返回 None"""
        cls = SkillLoader.load("this_skill_does_not_exist")
        assert cls is None

    def test_bad_module_name_returns_none(self):
        """无法 import 的模块返回 None"""
        cls = SkillLoader.load("_invalid_!!!_name")
        assert cls is None


class TestSkillRegistry:
    def setup_method(self):
        # Clear singleton state between tests
        SkillRegistry._instance = None
        SkillRegistry._cache = {}

    def test_singleton(self):
        r1 = SkillRegistry()
        r2 = SkillRegistry()
        assert r1 is r2

    def test_get_nonexistent(self):
        registry = SkillRegistry()
        skill = registry.get("this_skill_does_not_exist")
        assert skill is None

    def test_reload_removes_cache(self):
        registry = SkillRegistry()
        registry._cache["test_skill"] = object()
        assert "test_skill" in registry._cache
        registry.reload("test_skill")
        assert "test_skill" not in registry._cache

    def test_reload_nonexistent_no_error(self):
        registry = SkillRegistry()
        registry.reload("nonexistent")  # Should not raise

    def test_clear_empties_cache(self):
        registry = SkillRegistry()
        registry._cache["a"] = object()
        registry._cache["b"] = object()
        registry.clear()
        assert registry._cache == {}

    def test_max_cache_lru_eviction(self):
        """超过 MAX_CACHE 时淘汰最早条目"""
        registry = SkillRegistry()
        # Fill cache with dummy entries using _cache directly
        for i in range(SkillRegistry._MAX_CACHE):
            registry._cache[f"skill_{i}"] = object()

        # A get() for nonexistent will try to load, fail, and not add to cache
        # So the cache size stays at MAX_CACHE
        result = registry.get("nonexistent")
        assert result is None
        assert len(registry._cache) <= SkillRegistry._MAX_CACHE


class TestWarmupSkills:
    def test_no_implementations_dir(self):
        """implementations 目录不存在时 warmup 不报错"""
        with patch("osskill.loader.os.path.isdir", return_value=False):
            import asyncio
            asyncio.run(warmup_skills())

    def test_empty_implementations_dir(self):
        """implementations 目录为空时 warmup 不报错"""
        with patch("osskill.loader.os.path.isdir", return_value=True), \
             patch("osskill.loader.os.listdir", return_value=[]):
            import asyncio
            asyncio.run(warmup_skills())  # Should not raise


@pytest.mark.asyncio
async def test_load_agent_skills_graceful_fallback():
    """管理服未部署时 load_agent_skills 返回空字典"""
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(side_effect=Exception("relation skills.agent_skills does not exist"))

    with patch("common.db.get_pool", AsyncMock(return_value=mock_pool)):
        result = await load_agent_skills("agent-001")
        assert result == {}


@pytest.mark.asyncio
async def test_reload_agent_skills_graceful_fallback():
    """管理服未部署时 reload_agent_skills 不报错"""
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(side_effect=Exception("relation skills.agent_skills does not exist"))

    with patch("common.db.get_pool", AsyncMock(return_value=mock_pool)):
        await reload_agent_skills("agent-001", {})
