"""
分类器单元测试
"""

import pytest
from xixing.classifier import classify_fast, _score_category, _check_structure_patterns


class TestScoreCategory:
    def test_plugin_keywords(self):
        scores = _score_category(
            "This is an openclaw skill with a plugin manifest for search",
            "My Skill Plugin",
            "https://github.com/skills/my-skill",
        )
        assert scores["plugin"] > scores["price"]
        assert scores["plugin"] > scores["standard"]

    def test_price_keywords(self):
        scores = _score_category(
            "混凝土 C30 价格信息 420 元/立方米 市场报价 工程材料",
            "工程材料价格表",
            "",
        )
        assert scores["price"] > scores["plugin"]
        assert scores["price"] > scores["knowledge"]

    def test_standard_keywords(self):
        scores = _score_category(
            "GB/T 12345 技术规范 行业标准 新能源变压器",
            "变压器技术标准",
            "",
        )
        assert scores["standard"] > scores["experience"]

    def test_experience_keywords(self):
        scores = _score_category(
            "踩坑记录：修复 Gateway 启动失败的经验教训，报错 error debug 解决方案",
            "Gateway 踩坑修复",
            "",
        )
        assert scores["experience"] > scores["price"]


class TestStructurePatterns:
    def test_plugin_manifest_detected(self):
        content = '{"manifest": {"name": "test"}, "prompt": "you are a helpful assistant"}'
        hits = _check_structure_patterns(content)
        assert hits["plugin"] is True

    def test_price_table_detected(self):
        content = """
        | 材料名称 | 规格 | 单价 |
        |---------|------|------|
        | 混凝土  | C30  | 420  |
        """
        hits = _check_structure_patterns(content)
        assert hits["price"] is True

    def test_standard_detected(self):
        content = "GB/T 12345-2026 执行标准 技术要求 技术参数详见附件"
        hits = _check_structure_patterns(content)
        assert hits["standard"] is True

    def test_no_patterns(self):
        content = "This is a simple blog post about random topics without any special structure."
        hits = _check_structure_patterns(content)
        for v in hits.values():
            assert v is False


class TestClassifyFast:
    def test_plugin_content(self):
        cat, conf = classify_fast(
            "openclaw skill with plugin manifest and prompt template for AI search",
            title="My Search Skill",
            url="https://clawhub.ai/skills/my-skill",
        )
        assert cat == "plugin"
        assert conf > 0.3

    def test_price_content(self):
        cat, conf = classify_fast(
            "工程材料价格 | 混凝土 C30 | 单价 420 元/立方米 | 合肥信息价",
            title="合肥工程材料价格",
            url="",
        )
        assert cat == "price"

    def test_low_confidence_defaults_general(self):
        cat, conf = classify_fast(
            "lorem ipsum dolor sit amet",
            title="unknown",
            url="",
        )
        assert cat == "general"
