"""
蒸馏模块 — Skill 提案生成单元测试
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xixing.distiller import _generate_skill_proposals, _llm_analyze_skills


class MockRow:
    """模拟 asyncpg 行对象"""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __getitem__(self, key):
        return getattr(self, key, None)

    def get(self, key, default=None):
        return getattr(self, key, default)


@pytest.mark.asyncio
async def test_generate_no_data_returns_empty():
    """无对话/纠正数据时返回空列表"""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])  # conversations 和 corrections 均为空

    with patch("xixing.config.get_skill_proposals_enabled", return_value=True):
        result = await _generate_skill_proposals(pool)
        assert result == []


@pytest.mark.asyncio
async def test_generate_disabled_returns_empty():
    """配置禁用时返回空列表"""
    pool = AsyncMock()
    with patch("xixing.config.get_skill_proposals_enabled", return_value=False):
        result = await _generate_skill_proposals(pool)
        assert result == []


@pytest.mark.asyncio
async def test_llm_analyze_returns_proposals():
    """_llm_analyze_skills 正常返回提案"""
    conversations = [
        MockRow(content="如何查询混凝土价格", skill_used="price_query", confidence=0.9, feedback=None)
    ]
    corrections = []
    existing_skills = [{"name": "price_query", "description": "价格查询"}]

    with patch("common.llm.llm_call_json", AsyncMock(return_value=[
        {
            "name": "steel_price_query",
            "display_name": "钢材价格查询",
            "description": "查询钢材市场价格",
            "category": "cost",
            "frequency": 25,
            "sample_queries": ["螺纹钢价格", "钢材信息价"],
            "knowledge_categories": ["钢材"],
            "existing_skill_overlap": False,
        }
    ])):
        proposals, rejected = await _llm_analyze_skills(
            conversations=conversations,
            corrections=corrections,
            existing_skills=existing_skills,
        )
        assert len(proposals) == 1
        assert proposals[0]["name"] == "steel_price_query"
        assert proposals[0]["frequency"] == 25
        assert rejected == []


@pytest.mark.asyncio
async def test_llm_analyze_handles_wrapped_response():
    """_llm_analyze_skills 处理 {proposals: [...]} 包装格式"""
    conversations = [MockRow(content="test", skill_used="test", confidence=0.5, feedback=None)]
    corrections = []
    existing_skills = []

    with patch("common.llm.llm_call_json", AsyncMock(return_value={
        "proposals": [
            {
                "name": "new_skill",
                "display_name": "新技能",
                "description": "desc",
                "category": "general",
                "frequency": 12,
                "sample_queries": [],
                "knowledge_categories": [],
                "existing_skill_overlap": False,
            }
        ]
    })):
        proposals, rejected = await _llm_analyze_skills(
            conversations=conversations,
            corrections=corrections,
            existing_skills=existing_skills,
        )
        assert len(proposals) == 1
        assert proposals[0]["name"] == "new_skill"


@pytest.mark.asyncio
async def test_llm_analyze_skips_bad_response():
    """LLM 返回无效格式时返回空列表"""
    conversations = [MockRow(content="test", skill_used="test", confidence=0.5, feedback=None)]
    corrections = []
    existing_skills = []

    with patch("common.llm.llm_call_json", AsyncMock(return_value="not a list or dict")):
        proposals, rejected = await _llm_analyze_skills(
            conversations=conversations,
            corrections=corrections,
            existing_skills=existing_skills,
        )
        assert proposals == []


@pytest.mark.asyncio
async def test_llm_analyze_none_response():
    """LLM 返回 None 时返回空列表"""
    conversations = [MockRow(content="test", skill_used="test", confidence=0.5, feedback=None)]
    corrections = []
    existing_skills = []

    with patch("common.llm.llm_call_json", AsyncMock(return_value=None)):
        proposals, rejected = await _llm_analyze_skills(
            conversations=conversations,
            corrections=corrections,
            existing_skills=existing_skills,
        )
        assert proposals == []


@pytest.mark.asyncio
async def test_generate_dedup_existing():
    """与已有 Skill 同名的提案被过滤"""
    pool = AsyncMock()

    # 模拟有对话数据
    pool.fetch = AsyncMock(return_value=[
        MockRow(content="How to query price", skill_used="price_query", confidence=0.9, feedback=None),
    ])
    # 第二次 fetch 返回模拟的 corrections 表（空）
    # 第三次 fetch 返回已有 skill
    pool.fetch.side_effect = None  # reset

    # 使用 side_effect 模拟多次 fetch 调用
    async def fetch_side_effect(sql, *args):
        if "baishitong.conversations" in sql:
            return [MockRow(content="How to query price", skill_used="price_query", confidence=0.9, feedback=None)]
        if "baishitong.corrections" in sql:
            return []
        if "skill_definitions" in sql:
            return [MockRow(name="price_query", description="价格查询")]
        return []
    pool.fetch = AsyncMock(side_effect=fetch_side_effect)

    with patch("xixing.config.get_skill_proposals_enabled", return_value=True), \
         patch("xixing.distiller._llm_analyze_skills", AsyncMock(return_value=(
             [{
                 "name": "price_query",
                 "display_name": "价格查询",
                 "description": "test",
                 "category": "cost",
                 "frequency": 30,
                 "sample_queries": ["q1"],
                 "knowledge_categories": [],
                 "existing_skill_overlap": True,
             }],
             [],
         ))):
        result = await _generate_skill_proposals(pool)
        # 与已有 skill 重名，被过滤
        assert result == []


@pytest.mark.asyncio
async def test_generate_frequency_filter():
    """频次低于阈值的提案被过滤"""
    pool = AsyncMock()

    async def fetch_side_effect(sql, *args):
        if "baishitong.conversations" in sql:
            return [MockRow(content="q", skill_used="s", confidence=0.5, feedback=None)]
        if "baishitong.corrections" in sql:
            return []
        if "skill_definitions" in sql:
            return []
        return []
    pool.fetch = AsyncMock(side_effect=fetch_side_effect)

    with patch("xixing.config.get_skill_proposals_enabled", return_value=True), \
         patch("xixing.config.get_skill_proposals_min_frequency", return_value=20), \
         patch("xixing.distiller._llm_analyze_skills", AsyncMock(return_value=(
             [{
                 "name": "low_freq_skill",
                 "display_name": "低频",
                 "description": "test",
                 "category": "general",
                 "frequency": 5,
                 "sample_queries": [],
                 "knowledge_categories": [],
                 "existing_skill_overlap": False,
             }],
             [],
         ))):
        result = await _generate_skill_proposals(pool)
        assert result == []


@pytest.mark.asyncio
async def test_generate_full_flow():
    """完整流程：数据 → LLM → 去重 → 频次 → 返回提案"""
    pool = AsyncMock()

    async def fetch_side_effect(sql, *args):
        if "baishitong.conversations" in sql:
            return [MockRow(content="q", skill_used="s", confidence=0.5, feedback=None)]
        if "baishitong.corrections" in sql:
            return []
        if "skill_definitions" in sql:
            return []
        return []
    pool.fetch = AsyncMock(side_effect=fetch_side_effect)
    pool.execute = AsyncMock(return_value="UPDATE 0")

    with patch("xixing.config.get_skill_proposals_enabled", return_value=True), \
         patch("xixing.config.get_skill_proposals_min_frequency", return_value=3), \
         patch("xixing.config.get_skill_proposals_max_per_round", return_value=5), \
         patch("xixing.distiller._llm_analyze_skills", AsyncMock(return_value=(
             [{
                 "name": "steel_query",
                 "display_name": "钢材查询",
                 "description": "查询钢材价格",
                 "category": "cost",
                 "frequency": 15,
                 "sample_queries": ["螺纹钢今天多少钱"],
                 "knowledge_categories": ["钢材"],
                 "existing_skill_overlap": False,
             }],
             [],
         ))), \
         patch("osskill.database.insert_proposal", AsyncMock(return_value={
             "id": 1, "name": "steel_query", "status": "proposed",
         })):
        result = await _generate_skill_proposals(pool)
        assert len(result) == 1
        assert result[0]["name"] == "steel_query"



@pytest.mark.asyncio
async def test_generate_management_server_not_accessible():
    """管理服表不可访问时优雅降级"""
    pool = AsyncMock()

    async def fetch_side_effect(sql, *args):
        if "baishitong.conversations" in sql:
            return [MockRow(content="q", skill_used="s", confidence=0.5, feedback=None)]
        if "baishitong.corrections" in sql:
            return []
        if "skill_definitions" in sql:
            raise Exception("relation skills.skill_definitions does not exist")
        return []
    pool.fetch = AsyncMock(side_effect=fetch_side_effect)

    with patch("xixing.config.get_skill_proposals_enabled", return_value=True), \
         patch("xixing.config.get_skill_proposals_min_frequency", return_value=3), \
         patch("xixing.config.get_skill_proposals_max_per_round", return_value=5), \
         patch("xixing.distiller._llm_analyze_skills", AsyncMock(return_value=(
             [{
                 "name": "new_skill",
                 "display_name": "新技能",
                 "description": "test",
                 "category": "general",
                 "frequency": 10,
                 "sample_queries": [],
                 "knowledge_categories": [],
                 "existing_skill_overlap": False,
             }],
             [],
         ))), \
         patch("osskill.database.insert_proposal", AsyncMock(return_value={
             "id": 1, "name": "new_skill", "status": "proposed",
         })):
        # 不应抛出异常
        result = await _generate_skill_proposals(pool)
        assert len(result) == 1
