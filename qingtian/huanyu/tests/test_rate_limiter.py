"""
rate_limiter.py 单元测试
per-AIN 限流 — tier 配额 + AIN 数量限制
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from huanyu.rate_limiter import (
    TIER_LIMITS,
    check_ain_limit,
    tier_from_trust_level,
)


class TestTierLimits:
    def test_free_limits(self):
        free = TIER_LIMITS["free"]
        assert free["msg_per_sec"] == 20
        assert free["max_ains"] == 3

    def test_pro_limits(self):
        pro = TIER_LIMITS["pro"]
        assert pro["msg_per_sec"] == 100
        assert pro["max_ains"] == 50

    def test_enterprise_unlimited(self):
        ent = TIER_LIMITS["enterprise"]
        assert ent["msg_per_sec"] == 0
        assert ent["max_ains"] == 0

    def test_alliance_unlimited(self):
        al = TIER_LIMITS["alliance"]
        assert al["msg_per_sec"] == 0
        assert al["max_ains"] == 0

    def test_only_four_tiers(self):
        assert len(TIER_LIMITS) == 4
        assert "free" in TIER_LIMITS
        assert "pro" in TIER_LIMITS
        assert "enterprise" in TIER_LIMITS
        assert "alliance" in TIER_LIMITS


class TestTierFromTrustLevel:
    def test_basic_to_free(self):
        assert tier_from_trust_level("basic") == "free"

    def test_verified_to_pro(self):
        assert tier_from_trust_level("verified") == "pro"

    def test_trusted_to_enterprise(self):
        assert tier_from_trust_level("trusted") == "enterprise"

    def test_admin_to_alliance(self):
        assert tier_from_trust_level("admin") == "alliance"

    def test_unknown_falls_to_free(self):
        assert tier_from_trust_level("unknown") == "free"

    def test_empty_falls_to_free(self):
        assert tier_from_trust_level("") == "free"


class TestCheckAinLimit:
    @pytest.mark.asyncio
    async def test_under_limit(self, mock_conn, mock_pool):
        mock_conn.fetchval.return_value = 1  # 1 AIN, limit is 3

        with patch("huanyu.rate_limiter.get_pool", return_value=mock_pool):
            result = await check_ain_limit("server1", "free")
            assert result is True

    @pytest.mark.asyncio
    async def test_at_limit(self, mock_conn, mock_pool):
        mock_conn.fetchval.return_value = 3  # exactly at free limit

        with patch("huanyu.rate_limiter.get_pool", return_value=mock_pool):
            result = await check_ain_limit("server1", "free")
            assert result is False

    @pytest.mark.asyncio
    async def test_over_limit(self, mock_conn, mock_pool):
        mock_conn.fetchval.return_value = 5

        with patch("huanyu.rate_limiter.get_pool", return_value=mock_pool):
            result = await check_ain_limit("server1", "free")
            assert result is False

    @pytest.mark.asyncio
    async def test_enterprise_unlimited(self, mock_conn, mock_pool):
        """enterprise tier bypasses AIN limit check entirely"""
        with patch("huanyu.rate_limiter.get_pool", return_value=mock_pool):
            result = await check_ain_limit("server1", "enterprise")
            assert result is True

    @pytest.mark.asyncio
    async def test_pro_limit_fifty(self, mock_conn, mock_pool):
        mock_conn.fetchval.return_value = 49

        with patch("huanyu.rate_limiter.get_pool", return_value=mock_pool):
            result = await check_ain_limit("server1", "pro")
            assert result is True
