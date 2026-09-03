"""
resolver.py 单元测试
递归解析引擎 — 本地解析 / scope 检查 / 理事会 / CRL / RAC 验证
"""

from unittest.mock import patch

import pytest

from huanyu.resolver import (
    CouncilManager,
    CRLManager,
    RACData,
    ResolutionResult,
    _country_from_ain,
    _is_in_scope,
    _resolve_local,
    verify_rac,
    get_council_manager,
    get_crl_manager,
)


class TestCountryFromAin:
    def test_cn(self):
        assert _country_from_ain("ain:1:acssa:cn-hf-management:biz:buyer:001") == "CN"

    def test_us(self):
        assert _country_from_ain("ain:1:acssa:us-ny-sales:biz:seller:001") == "US"

    def test_jp(self):
        assert _country_from_ain("ain:1:acssa:jp-tk-trade:biz:broker:001") == "JP"

    def test_invalid_returns_none(self):
        assert _country_from_ain("not-an-ain") is None

    def test_empty_returns_none(self):
        assert _country_from_ain("") is None


class TestIsInScope:
    def test_exact_match(self):
        assert _is_in_scope("CN", "CN") is True

    def test_no_match(self):
        assert _is_in_scope("US", "CN") is False

    def test_multi_scope_included(self):
        assert _is_in_scope("CN", "CN,JP,KR") is True

    def test_multi_scope_not_included(self):
        assert _is_in_scope("US", "CN,JP,KR") is False

    def test_wildcard(self):
        assert _is_in_scope("US", "*") is True

    def test_wildcard_any_country(self):
        assert _is_in_scope("CN", "*") is True

    def test_scope_case_insensitive(self):
        """scope entries are upper()'d; input should already be uppercase (from _country_from_ain)"""
        assert _is_in_scope("CN", "cn, jp") is True
        assert _is_in_scope("JP", "cn,jp,kr") is True
        assert _is_in_scope("US", "cn,jp") is False

    def test_whitespace_handling(self):
        assert _is_in_scope("CN", " CN , JP ") is True


class TestResolutionResult:
    def test_defaults(self):
        r = ResolutionResult(ain="test_ain", found=False)
        assert r.found is False
        assert r.resolved_by == "local"
        assert r.resolution_chain == []

    def test_found_agent(self):
        r = ResolutionResult(
            ain="ain:1:acssa:cn-hf-management:biz:buyer:001",
            found=True,
            agent_id="a1",
            name="test",
            category="biz:buyer",
            server_host="host1",
            c_level="C2",
        )
        assert r.found is True
        assert r.c_level == "C2"
        assert r.resolved_by == "local"


class TestRACData:
    def test_valid_rac(self):
        rac = RACData(
            rac_fingerprint="fp_001",
            resolver_ain="ain:1:acssa:cn-hf-root:infra:resolver:001",
            resolver_org="acssa",
            scope=["CN"],
            endpoint="https://ain.acssa.cn",
            public_key="pk_test",
            valid_until="2028-12-31T23:59:59Z",
            council_signatures=[
                {"seat_id": "cn-1", "signature_b64": "s1", "signed_at": "2026-01-01T00:00:00Z"},
                {"seat_id": "cn-2", "signature_b64": "s2", "signed_at": "2026-01-01T00:00:00Z"},
                {"seat_id": "cn-3", "signature_b64": "s3", "signed_at": "2026-01-01T00:00:00Z"},
                {"seat_id": "cn-4", "signature_b64": "s4", "signed_at": "2026-01-01T00:00:00Z"},
            ],
        )
        assert len(rac.scope) == 1
        assert "CN" in rac.scope
        assert len(rac.council_signatures) == 4

    def test_multi_country_scope(self):
        rac = RACData(
            rac_fingerprint="fp_002",
            resolver_ain="ain:1:acssa:cn-hf-root:infra:resolver:002",
            resolver_org="acssa",
            scope=["CN", "JP", "KR"],
            endpoint="https://asia.acssa.cn",
            public_key="pk_asia",
            valid_until="2028-06-30T23:59:59Z",
            council_signatures=[],
        )
        assert len(rac.scope) == 3


class TestCouncilManager:
    def test_quorum_is_four(self):
        cm = CouncilManager()
        assert cm.quorum == 4

    def test_seats_empty_initially(self):
        cm = CouncilManager()
        assert cm.seats == []

    def test_no_bootstrap_keys(self):
        cm = CouncilManager()
        assert cm.has_bootstrap() is False

    def test_get_nonexistent_key(self):
        cm = CouncilManager()
        assert cm.get_public_key("nonexistent") is None

    def test_verify_empty_signatures(self):
        cm = CouncilManager()
        rac = RACData(
            rac_fingerprint="fp", resolver_ain="ain:.:.::.::",
            resolver_org="acssa", scope=["CN"], endpoint="https://x.cn",
            public_key="pk", valid_until="2030-01-01Z",
            council_signatures=[],
        )
        valid, reason = cm.verify_signatures(rac)
        assert valid is False
        assert "无理事会签名" in reason

    def test_singleton(self):
        cm1 = get_council_manager()
        cm2 = get_council_manager()
        assert cm1 is cm2


class TestCRLManager:
    def test_not_revoked_by_default(self):
        crl = CRLManager()
        assert crl.is_revoked("any_fingerprint") is False

    def test_singleton(self):
        crl1 = get_crl_manager()
        crl2 = get_crl_manager()
        assert crl1 is crl2


class TestVerifyRAC:
    @pytest.mark.asyncio
    async def test_empty_signatures_fails(self):
        rac = RACData(
            rac_fingerprint="fp", resolver_ain="ain:1:acssa:cn-hf-root:infra:resolver:001",
            resolver_org="acssa", scope=["CN"], endpoint="https://x.cn",
            public_key="pk", valid_until="2030-12-31T23:59:59Z",
            council_signatures=[],
        )
        # Without bootstrap keys, council verification will fail on empty sigs
        valid, reason = await verify_rac(rac)
        assert valid is False


class TestResolveLocal:
    @pytest.mark.asyncio
    async def test_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.resolver.get_pool", return_value=mock_pool):
            result = await _resolve_local("ain:1:acssa:cn-hf-management:biz:buyer:001")
            assert result is None

    @pytest.mark.asyncio
    async def test_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "agent_id": "a1",
            "ain": "ain:1:acssa:cn-hf-management:biz:buyer:001",
            "name": "test_agent",
            "category": "biz:buyer",
            "server_host": "host1",
            "status": "active",
            "public_key": "pk",
            "cert_fingerprint": "fp",
            "c_level": "C1",
            "industry": "st",
            "scale": "medium",
        }

        with patch("huanyu.resolver.get_pool", return_value=mock_pool):
            result = await _resolve_local("ain:1:acssa:cn-hf-management:biz:buyer:001")
            assert result is not None
            assert result["agent_id"] == "a1"
            assert result["c_level"] == "C1"
            assert result["industry"] == "st"
