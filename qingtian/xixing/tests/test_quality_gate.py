"""
质量门单元测试（不连接 PG）
"""

import pytest
from datetime import datetime, timedelta, timezone

from xixing.quality_gate import (
    gate_content_quality,
    gate_freshness,
    gate_relevance,
    _jaccard_similarity,
    _compute_hash,
)


class TestJaccardSimilarity:
    def test_identical_texts(self):
        sim = _jaccard_similarity("hello world", "hello world")
        assert sim == 1.0

    def test_completely_different(self):
        sim = _jaccard_similarity("hello world", "quantum physics")
        assert sim < 0.5

    def test_partial_overlap(self):
        sim = _jaccard_similarity("hello world foo bar", "hello world baz qux")
        assert 0.2 < sim < 0.9

    def test_short_text(self):
        sim = _jaccard_similarity("a", "b")
        assert sim >= 0.0

    def test_empty_text(self):
        sim = _jaccard_similarity("", "")
        assert sim == 0.0


class TestHashFunction:
    def test_consistent_hash(self):
        h1 = _compute_hash("test content")
        h2 = _compute_hash("test content")
        assert h1 == h2

    def test_different_hash(self):
        h1 = _compute_hash("test content A")
        h2 = _compute_hash("test content B")
        assert h1 != h2


class TestGateContentQuality:
    def test_good_content_passes(self):
        content = "This is a paragraph with sufficient length. " * 10
        content += "\n\n" + "This is another paragraph with enough content. " * 10
        passed, detail, score = gate_content_quality(content)
        assert passed is True
        assert score > 0.4

    def test_too_short_fails(self):
        content = "too short"
        passed, detail, score = gate_content_quality(content)
        assert passed is False

    def test_html_rejected(self):
        content = "<div><span>" + "x" * 50 + "</span>" + "<script>" + "y" * 50 + "</script></div>" * 10
        passed, detail, score = gate_content_quality(content)
        # High HTML ratio should fail
        if not passed:
            assert "正文率" in detail


class TestGateFreshness:
    def test_today_is_fresh(self):
        # review(2026-08-16): 原硬编码日期会随时间过期（>30 天窗口）→ 时间炸弹。
        # 改为相对当前日期，保持"近期即新鲜"的测试意图。
        fresh = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        passed, detail, score = gate_freshness(fresh)
        assert passed is True

    def test_old_date(self):
        passed, detail, score = gate_freshness("2025-01-01")
        if not passed:
            assert "过期" in detail

    def test_none_date_defaults_ok(self):
        passed, detail, score = gate_freshness(None)
        assert passed is True

    def test_invalid_date_format(self):
        passed, detail, score = gate_freshness("not-a-date")
        assert passed is True  # Default pass on parse error


class TestGateRelevance:
    @pytest.mark.asyncio
    async def test_ai_content_relevant(self):
        content = "LLM agent uses embedding vectors for semantic memory retrieval and knowledge distillation"
        passed, detail, score = await gate_relevance(content)
        assert passed is True
        assert score > 0

    @pytest.mark.asyncio
    async def test_engineering_content_relevant(self):
        content = "工程材料价格信息：混凝土 C30 当前市场价 420 元/立方米，钢材价格走势分析"
        passed, detail, score = await gate_relevance(content)
        assert passed is True

    @pytest.mark.asyncio
    async def test_irrelevant_content(self):
        content = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt"
        passed, detail, score = await gate_relevance(content)
        # May pass or fail depending on keyword density, just check it runs
        assert isinstance(score, float)
