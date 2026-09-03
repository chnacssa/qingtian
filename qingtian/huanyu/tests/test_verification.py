"""
verification.py 单元测试
C0-C3 认证引擎 — VP 路由、升级、降级、webhook
"""

from unittest.mock import AsyncMock, patch

import pytest

from huanyu.verification import (
    C_LEVEL_WEIGHT,
    C_LEVEL_ORDER,
    VP_REGISTRY,
    _lookup_provider,
    _self_declaration_verify,
    upgrade_c_level,
    auto_downgrade,
    handle_risk_event,
    DOWNGRADE_EVENT_TYPES,
)


class TestVPLookup:
    def test_cn_routes_to_qcc(self):
        provider = _lookup_provider("CN")
        assert provider["name"] == "企查查"

    def test_cn_lowercase(self):
        provider = _lookup_provider("cn")
        assert provider["name"] == "企查查"

    def test_unregistered_returns_default(self):
        provider = _lookup_provider("XX")
        assert provider["name"] == "self_declaration"

    def test_default_supports_empty(self):
        provider = _lookup_provider("US")
        assert provider["supports"] == []

    def test_qcc_supports_c1_c2_c3_watch(self):
        p = VP_REGISTRY["CN"]
        assert "C1" in p["supports"]
        assert "C2" in p["supports"]
        assert "C3" in p["supports"]
        assert "watch" in p["supports"]


class TestCLevelOrder:
    def test_order_ascending(self):
        for i in range(len(C_LEVEL_ORDER) - 1):
            assert C_LEVEL_ORDER[i] < C_LEVEL_ORDER[i + 1]

    def test_weights_correlate_with_order(self):
        for i in range(len(C_LEVEL_ORDER) - 1):
            assert C_LEVEL_WEIGHT[C_LEVEL_ORDER[i]] < C_LEVEL_WEIGHT[C_LEVEL_ORDER[i + 1]]


class TestSelfDeclaration:
    @pytest.mark.asyncio
    async def test_returns_manual_review(self):
        result = await _self_declaration_verify("uscc123", "test_corp", "C1")
        assert result["pass"] is False
        assert "manual_review" in result["reason"]


class TestUpgradeCLevel:
    @pytest.mark.asyncio
    async def test_invalid_target_level(self):
        result = await upgrade_c_level("a1", "C5")
        assert result["success"] is False
        assert "invalid target_level" in result["error"]

    @pytest.mark.asyncio
    async def test_agent_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.verification.get_pool", return_value=mock_pool):
            result = await upgrade_c_level("a1", "C1")
            assert result["success"] is False
            assert result["error"] == "agent not found"

    @pytest.mark.asyncio
    async def test_skip_level_upgrade_allowed(self, mock_conn, mock_pool):
        """C0 → C2 跳过 C1 应被允许（允许越级升级）"""
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1",
            "c_level": "C0",
            "uscc": "91110108MA01XXXXX",
            "company_name": "测试公司",
        }

        # P1 (R11): 桩模式不再默认通过，升级路径需真实 VP 通过 —— mock _vp_verify。
        with patch("huanyu.verification._vp_verify", new=AsyncMock(return_value={
            "pass": True, "industry": "C", "scale": "medium", "risk_flags": [],
        })), patch("huanyu.verification.get_pool", return_value=mock_pool):
            result = await upgrade_c_level("a1", "C2")
            assert result["success"] is True
            assert result["c_level"] == "C2"

    @pytest.mark.asyncio
    async def test_qcc_stub_mode_fails_closed(self, mock_conn, mock_pool):
        """P1 (R11): 企查查 API Key 未配置（桩模式）不得授予 C 认证"""
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1",
            "c_level": "C0",
            "uscc": "91110108MA01XXXXX",
            "company_name": "测试公司",
        }

        # 强制桩模式：即使环境里有 QCC_API_KEY 也模拟 _stub 返回
        with patch("huanyu.verification._vp_http_call",
                   new=AsyncMock(return_value={"_stub": True})), \
             patch("huanyu.verification.get_pool", return_value=mock_pool):
            result = await upgrade_c_level("a1", "C3")  # country_code 默认 CN → QCC
            assert result["success"] is False
            assert result["error"] == "verification_failed"
            assert result["detail"]["reason"] == "qcc_api_key_not_configured"

    @pytest.mark.asyncio
    async def test_valid_upgrade_path_self_declaration_fails(self, mock_conn, mock_pool):
        """自声明模式（无 VP API Key）应返回 verification_failed"""
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1",
            "c_level": "C0",
            "uscc": "",
            "company_name": "",
        }

        # 使用未注册国家 → 路由到 self_declaration → pass=False
        with patch("huanyu.verification.get_pool", return_value=mock_pool):
            result = await upgrade_c_level("a1", "C1", country_code="XX")
            assert result["success"] is False
            assert result["error"] == "verification_failed"


class TestAutoDowngrade:
    @pytest.mark.asyncio
    async def test_agent_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.verification.get_pool", return_value=mock_pool):
            result = await auto_downgrade("a1", "test reason")
            assert result["success"] is False
            assert result["error"] == "agent not found"

    @pytest.mark.asyncio
    async def test_already_c0_noop(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"agent_id": "a1", "c_level": "C0"}

        with patch("huanyu.verification.get_pool", return_value=mock_pool):
            result = await auto_downgrade("a1", "test reason")
            assert result["success"] is True
            assert "already C0" in result["message"]

    @pytest.mark.asyncio
    async def test_downgrades_from_c2(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"agent_id": "a1", "c_level": "C2"}

        with patch("huanyu.verification.get_pool", return_value=mock_pool):
            result = await auto_downgrade("a1", "business_abnormal: 经营异常")
            assert result["success"] is True
            assert result["previous_level"] == "C2"
            assert result["c_level"] == "C0"
            assert "business_abnormal" in result["reason"]



def _signed_risk_event(payload: dict) -> dict:
    """review(2026-08-24 P0-3): 风险 webhook 补 HMAC 验签后，测试构造合法签名调用。"""
    import hashlib
    import hmac
    import json as _json
    import time as _time
    from huanyu.signing import _get_key
    ts = str(int(_time.time()))
    canon = _json.dumps(payload, ensure_ascii=False, sort_keys=True)
    sig = hmac.new(_get_key(), f"risk_event:{ts}:{canon}".encode(), hashlib.sha256).hexdigest()
    return payload, sig, ts


class TestHandleRiskEvent:
    @pytest.mark.asyncio
    async def test_ignored_event_type(self):
        _evt = {
            "company_name": "test",
            "event_type": "normal_update",
            "severity": "high",
        }
        result = await handle_risk_event(*_signed_risk_event(_evt))
        assert result["action"] == "ignored"

    @pytest.mark.asyncio
    async def test_logged_only_low_severity(self):
        _evt = {
            "company_name": "test",
            "registration_number": "91110108MA01XXXXX",
            "event_type": "business_abnormal",
            "severity": "low",
        }
        result = await handle_risk_event(*_signed_risk_event(_evt))
        assert result["action"] == "logged_only"

    @pytest.mark.asyncio
    async def test_high_severity_trigger(self, mock_conn, mock_pool):
        """high severity + downgrade event → triggers downgrade"""
        mock_conn.fetchrow.side_effect = [
            {"agent_id": "a1"},
            {"agent_id": "a1", "c_level": "C1"},
        ]

        with patch("huanyu.verification.get_pool", return_value=mock_pool):
            _evt = {
                "company_name": "测试公司",
                "registration_number": "91110108MA01XXXXX",
                "event_type": "business_abnormal",
                "severity": "high",
                "detail": "工商异常名录",
            }
            result = await handle_risk_event(*_signed_risk_event(_evt))
            assert result["action"] == "downgraded"
            assert result["agent_id"] == "a1"
            assert result["event_type"] == "business_abnormal"

    @pytest.mark.asyncio
    async def test_unmatched_agent(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.verification.get_pool", return_value=mock_pool):
            _evt = {
                "company_name": "unknown_corp",
                "registration_number": "000000000000000000",
                "event_type": "dishonesty",
                "severity": "critical",
            }
            result = await handle_risk_event(*_signed_risk_event(_evt))
            assert result["action"] == "unmatched"


class TestDowngradeEventTypes:
    def test_all_known_types(self):
        expected = {
            "business_abnormal", "dishonesty", "blacklist",
            "license_revoked", "deregistered",
            "tax_irregularity", "judicial_freeze",
        }
        assert DOWNGRADE_EVENT_TYPES == expected
