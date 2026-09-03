"""
Skill 提案配置单元测试
"""

from unittest.mock import patch

import pytest

from xixing.config import (
    get_skill_proposals_enabled,
    get_skill_proposals_min_frequency,
    get_skill_proposals_max_per_round,
)


class TestSkillProposalsEnabled:
    def test_default_enabled(self):
        with patch("xixing.config.get", return_value=True) as mock_get:
            assert get_skill_proposals_enabled() is True
            mock_get.assert_called_once_with("skill_proposals.enabled", True)

    def test_explicit_disabled(self):
        with patch("xixing.config.get", return_value=False):
            assert get_skill_proposals_enabled() is False

    def test_explicit_enabled(self):
        with patch("xixing.config.get", return_value=True):
            assert get_skill_proposals_enabled() is True


class TestSkillProposalsMinFrequency:
    def test_default_value(self):
        with patch("xixing.config.get", return_value=10) as mock_get:
            assert get_skill_proposals_min_frequency() == 10
            mock_get.assert_called_once_with("skill_proposals.min_frequency", 10)

    def test_custom_value(self):
        with patch("xixing.config.get", return_value=25):
            assert get_skill_proposals_min_frequency() == 25

    def test_zero_fallback_to_default(self):
        """0 不符合 >=1，应返回兜底值 10"""
        with patch("xixing.config.get", return_value=0):
            assert get_skill_proposals_min_frequency() == 10

    def test_negative_fallback_to_default(self):
        with patch("xixing.config.get", return_value=-5):
            assert get_skill_proposals_min_frequency() == 10

    def test_string_fallback_to_default(self):
        with patch("xixing.config.get", return_value="not-a-number"):
            assert get_skill_proposals_min_frequency() == 10

    def test_none_fallback_to_default(self):
        with patch("xixing.config.get", return_value=None):
            assert get_skill_proposals_min_frequency() == 10


class TestSkillProposalsMaxPerRound:
    def test_default_value(self):
        with patch("xixing.config.get", return_value=5) as mock_get:
            assert get_skill_proposals_max_per_round() == 5
            mock_get.assert_called_once_with("skill_proposals.max_proposals_per_round", 5)

    def test_custom_value(self):
        with patch("xixing.config.get", return_value=3):
            assert get_skill_proposals_max_per_round() == 3

    def test_zero_fallback_to_default(self):
        with patch("xixing.config.get", return_value=0):
            assert get_skill_proposals_max_per_round() == 5

    def test_negative_fallback_to_default(self):
        with patch("xixing.config.get", return_value=-1):
            assert get_skill_proposals_max_per_round() == 5
