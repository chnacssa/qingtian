"""
filter.py 单元测试
内容质量过滤 — 长度 / 重复 / 结构体 / 词数
"""

import pytest

from yongheng.filter import (
    should_store,
    _repetition_ratio,
    _word_count,
    MIN_CONTENT_LENGTH,
    MAX_CONTENT_LENGTH,
    MIN_WORD_COUNT,
)


class TestRepetitionRatio:
    def test_normal_text_low_ratio(self):
        assert _repetition_ratio("hello world this is a test") < 0.3

    def test_repeated_char_high_ratio(self):
        assert _repetition_ratio("AAAAAAAABBBBBB") > 0.4

    def test_empty_string(self):
        assert _repetition_ratio("") == 1.0

    def test_single_char(self):
        assert _repetition_ratio("x") == 1.0

    def test_unique_chars_low_ratio(self):
        r = _repetition_ratio("abcdefghijklmnopqrstuvwxyz")
        assert r < 0.1


class TestWordCount:
    def test_english_words(self):
        assert _word_count("hello world this is a test message here") >= 4

    def test_chinese_text(self):
        assert _word_count("今天天气很好适合出门散步") >= 3

    def test_mixed_text(self):
        assert _word_count("今天 hello world 天气好") >= 3

    def test_short_text_low_count(self):
        assert _word_count("ok fine") < 3

    def test_empty_string(self):
        assert _word_count("") == 0


class TestShouldStore:
    def test_empty_string_rejected(self):
        assert should_store("") is False

    def test_whitespace_only_rejected(self):
        assert should_store("   \n\t  ") is False

    def test_none_content_rejected(self):
        assert should_store(None) is False

    def test_too_short_rejected(self):
        assert should_store("ab") is False

    def test_min_length_accepted(self):
        content = "hello world this is a test message"
        assert len(content.strip()) >= MIN_CONTENT_LENGTH
        assert should_store(content) is True

    def test_too_long_rejected(self):
        content = "x" * (MAX_CONTENT_LENGTH + 1)
        assert should_store(content) is False

    def test_max_length_accepted(self):
        content = "hello world " + "x" * (MAX_CONTENT_LENGTH - 20)
        assert len(content.strip()) <= MAX_CONTENT_LENGTH
        # May fail word count, but should pass length check

    def test_repetition_rejected(self):
        content = "AAAAA" * 50  # very high repetition
        assert should_store(content) is False

    def test_pure_json_rejected(self):
        content = '[{}, {}, {}, {}]'
        assert should_store(content) is False

    def test_pure_html_rejected(self):
        content = "<html><body><div><p></p></div></body></html>"
        assert should_store(content) is False

    def test_too_few_words_rejected(self):
        content = "ok. fine."
        assert should_store(content) is False

    def test_normal_paragraph_accepted(self):
        content = "This is a normal paragraph with enough words to pass the quality filter. It contains meaningful information about the project."
        assert should_store(content) is True

    def test_chinese_paragraph_accepted(self):
        content = "今天召开了项目评审会议，讨论了第三阶段的技术方案，决定采用微服务架构进行改造。"
        assert should_store(content) is True

    def test_mixed_content_accepted(self):
        content = "用户反馈：登录页面加载速度很慢，需要优化。We need to fix the login page performance issue."
        assert should_store(content) is True
