"""
capabilities_service.py 单元测试
Agent 能力查询
"""

import pytest
from unittest.mock import patch

from zhenyue.capabilities_service import (
    get_allowed_tools,
    get_trust_weight,
    get_max_message_rpm,
    has_capability,
    get_trust_upgrade_requirements,
    check_action_allowed,
)


class TestGetAllowedTools:
    def test_returns_list(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {"allowed_tools": ["read", "write", "execute"]}
            tools = get_allowed_tools("verified")
            assert "read" in tools

    def test_empty_for_unknown_level(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            tools = get_allowed_tools("unknown")
            assert "read" in tools  # falls back to basic defaults


class TestGetTrustWeight:
    def test_default_weight(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            w = get_trust_weight("basic")
            assert w == 0.3

    def test_custom_weight(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {"trust_weight": 0.8}
            w = get_trust_weight("trusted")
            assert w == 0.8


class TestGetMaxMessageRPM:
    def test_default_rpm(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            rpm = get_max_message_rpm("basic")
            assert rpm == 60  # builtin default for basic

    def test_custom_rpm(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {"max_message_rpm": 500}
            rpm = get_max_message_rpm("enterprise")
            assert rpm == 500


class TestHasCapability:
    def test_has_capability(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {"allowed_tools": ["deploy", "monitor"]}
            assert has_capability("admin", "deploy") is True

    def test_no_capability(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {"allowed_tools": ["read"]}
            assert has_capability("basic", "deploy") is False


class TestGetTrustUpgradeRequirements:
    def test_basic_to_verified(self):
        req = get_trust_upgrade_requirements("basic")
        assert req["next"] == "verified"
        assert req["min_transactions"] == 5
        assert req["min_rating"] == 3.5

    def test_verified_to_trusted(self):
        req = get_trust_upgrade_requirements("verified")
        assert req["next"] == "trusted"
        assert req["min_transactions"] == 20
        assert req["min_rating"] == 4.0

    def test_trusted_no_next(self):
        req = get_trust_upgrade_requirements("trusted")
        assert req["next"] is None

    def test_admin_no_next(self):
        req = get_trust_upgrade_requirements("admin")
        assert req["next"] is None

    def test_unknown_returns_empty(self):
        req = get_trust_upgrade_requirements("unknown")
        assert req == {}


class TestCheckActionAllowed:
    def test_admin_wildcard_passes_all(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            allowed, missing = check_action_allowed("admin", ["delete_agent", "system_config", "any_action"])
            assert allowed is True
            assert missing == []

    def test_basic_missing_admin_caps(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            allowed, missing = check_action_allowed("basic", ["delete_agent", "admin"])
            assert allowed is False
            assert "delete_agent" in missing
            assert "admin" in missing

    def test_verified_has_inquire(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            allowed, missing = check_action_allowed("verified", ["read", "inquire"])
            assert allowed is True

    def test_verified_missing_negotiate(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            allowed, missing = check_action_allowed("verified", ["read", "negotiate"])
            assert allowed is False
            assert "negotiate" in missing

    def test_empty_required_caps_passes(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            allowed, missing = check_action_allowed("basic", [])
            assert allowed is True
            assert missing == []

    def test_unknown_level_falls_to_basic(self):
        with patch("zhenyue.capabilities_service.cfg.get_capabilities") as mock_caps:
            mock_caps.return_value = {}
            allowed, missing = check_action_allowed("unknown", ["read"])
            assert allowed is True
