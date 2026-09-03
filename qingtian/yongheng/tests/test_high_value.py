"""
high_value.py 单元测试
关键词扫描 + LLM 队列
"""

import pytest

from yongheng.high_value import (
    keyword_scan,
    enqueue_llm_check,
    _HIGH_VALUE_KEYWORDS,
)


class TestKeywordScan:
    def test_decision_keyword(self):
        assert keyword_scan("我们决定了采用方案A") is True

    def test_price_keyword(self):
        assert keyword_scan("报价单已发送给客户") is True

    def test_risk_keyword(self):
        assert keyword_scan("产能不足可能导致延迟交付") is True

    def test_deploy_keyword(self):
        assert keyword_scan("今晚进行生产环境部署") is True

    def test_rollback_keyword(self):
        assert keyword_scan("需要紧急回滚到上一个版本") is True

    def test_no_keyword(self):
        assert keyword_scan("今天天气很好，适合出门散步") is False

    def test_empty_string(self):
        assert keyword_scan("") is False

    def test_multiple_keywords(self):
        content = "紧急：配置变更导致生产故障，需要立即回滚"
        assert keyword_scan(content) is True

    def test_all_keywords_present(self):
        for kw in _HIGH_VALUE_KEYWORDS:
            assert keyword_scan(f"这是包含 {kw} 的测试内容") is True


class TestKeywordList:
    def test_keywords_not_empty(self):
        assert len(_HIGH_VALUE_KEYWORDS) > 0

    def test_all_decision_keywords_present(self):
        decision_kws = ["决定", "决策", "选定", "确认采用", "批准", "否决", "暂缓"]
        for kw in decision_kws:
            assert kw in _HIGH_VALUE_KEYWORDS, f"Missing decision keyword: {kw}"

    def test_all_risk_keywords_present(self):
        risk_kws = ["风险", "产能不足", "缺货", "延迟", "质量问题"]
        for kw in risk_kws:
            assert kw in _HIGH_VALUE_KEYWORDS, f"Missing risk keyword: {kw}"
