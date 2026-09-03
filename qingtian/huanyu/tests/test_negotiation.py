"""
negotiation.py 单元测试
谈判状态机 + 协议 + 评分 — 使用 mock asyncpg
"""

from unittest.mock import patch

import pytest

from huanyu.negotiation import (
    C_LEVEL_WEIGHT,
    BASE_SCORE_WEIGHTS,
    compute_base_score,
    compute_final_rank,
    rank_suppliers,
    create_agreement,
    expire_stale_negotiations,
    get_agreement,
    get_agent_ratings,
    get_negotiation,
    list_agreements,
    list_negotiations,
    notify_expiring_soon,
    notify_silent_negotiations,
    record_counter,
    start_negotiation,
    submit_rating,
    transition_negotiation,
)


class TestStartNegotiation:
    @pytest.mark.asyncio
    async def test_create(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "negotiation_id": "nego-1",
            "buyer_id": "buyer-1",
            "supplier_id": "supplier-1",
            "status": "active",
            "counter_count": 0,
            "expires_at": None,
            "created_at": None,
        }

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await start_negotiation("buyer-1", "supplier-1")
            assert result["status"] == "active"
            assert result["negotiation_id"] == "nego-1"

    @pytest.mark.asyncio
    async def test_with_custom_max_counters(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "negotiation_id": "nego-1",
            "buyer_id": "buyer-1",
            "supplier_id": "supplier-1",
            "status": "active",
            "counter_count": 0,
            "expires_at": None,
            "created_at": None,
        }

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await start_negotiation("buyer-1", "supplier-1", max_counters=8)
            assert result["status"] == "active"


class TestTransitionNegotiation:
    @pytest.mark.asyncio
    async def test_accept(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "negotiation_id": "nego-1",
            "status": "accepted",
            "updated_at": None,
        }

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await transition_negotiation("nego-1", "accepted")
            assert result["status"] == "accepted"

    @pytest.mark.asyncio
    async def test_reject(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "negotiation_id": "nego-1",
            "status": "rejected",
            "updated_at": None,
        }

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await transition_negotiation("nego-1", "rejected")
            assert result["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_invalid_transition(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None  # 状态不允许流转

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await transition_negotiation("nego-1", "accepted")
            assert result["status"] == "error"


class TestRecordCounter:
    @pytest.mark.asyncio
    async def test_record(self, mock_conn, mock_pool):
        mock_conn.fetchrow.side_effect = [
            {"counter_count": 0, "max_counters": 5, "status": "active"},  # 第一次查询
            {"negotiation_id": "nego-1", "counter_count": 1, "max_counters": 5, "status": "active"},  # UPDATE 返回
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await record_counter("nego-1", {"price": "3500"})
            assert result["counter_count"] == 1

    @pytest.mark.asyncio
    async def test_exceeded_max_counters(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"counter_count": 5, "max_counters": 5, "status": "active"}

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await record_counter("nego-1", {"price": "3500"})
            assert result["status"] == "error"
            assert "上限" in result["error"]

    @pytest.mark.asyncio
    async def test_not_active(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"counter_count": 0, "max_counters": 5, "status": "accepted"}

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await record_counter("nego-1", {"price": "3500"})
            assert result["status"] == "error"


class TestExpireStale:
    @pytest.mark.asyncio
    async def test_expire(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"negotiation_id": "n1"},
            {"negotiation_id": "n2"},
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            count = await expire_stale_negotiations()
            assert count == 2


class TestNotifications:
    @pytest.mark.asyncio
    async def test_silent_notifications(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"negotiation_id": "n1", "buyer_id": "b1", "supplier_id": "s1", "last_activity_at": None},
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            silent = await notify_silent_negotiations()
            assert len(silent) == 1

    @pytest.mark.asyncio
    async def test_expiring_soon(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"negotiation_id": "n1", "buyer_id": "b1", "supplier_id": "s1", "expires_at": None},
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            expiring = await notify_expiring_soon()
            assert len(expiring) == 1


class TestQueryNegotiations:
    @pytest.mark.asyncio
    async def test_get_negotiation(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"negotiation_id": "n1", "status": "active"}

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            nego = await get_negotiation("n1")
            assert nego["status"] == "active"

    @pytest.mark.asyncio
    async def test_get_not_found(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = None

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            nego = await get_negotiation("n99")
            assert nego is None

    @pytest.mark.asyncio
    async def test_list_negotiations(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"negotiation_id": "n1", "status": "active"},
            {"negotiation_id": "n2", "status": "accepted"},
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            negos = await list_negotiations()
            assert len(negos) == 2

    @pytest.mark.asyncio
    async def test_list_by_agent(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [{"negotiation_id": "n1", "status": "active"}]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            negos = await list_negotiations(agent_id="a1")
            assert len(negos) == 1


class TestCreateAgreement:
    @pytest.mark.asyncio
    async def test_success(self, mock_conn, mock_pool):
        mock_conn.fetchrow.side_effect = [
            {"status": "accepted"},  # 谈判状态检查
            {"agreement_id": "agr-1", "negotiation_id": "n1", "product": "螺纹钢",
             "quantity": "200吨", "total_price": "700000", "status": "active", "created_at": None},
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await create_agreement(
                "n1", "b1", "s1",
                product="螺纹钢", quantity="200吨",
                unit_price="3500", total_price="700000",
            )
            assert result["agreement_id"] == "agr-1"

    @pytest.mark.asyncio
    async def test_not_accepted(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"status": "active"}

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await create_agreement(
                "n1", "b1", "s1",
                product="螺纹钢", quantity="200吨",
                unit_price="3500", total_price="700000",
            )
            assert result["status"] == "error"
            assert "接受" in result["error"]


class TestAgreements:
    @pytest.mark.asyncio
    async def test_get_agreement(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {"agreement_id": "a1", "status": "active"}

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            agr = await get_agreement("a1")
            assert agr["status"] == "active"

    @pytest.mark.asyncio
    async def test_list_agreements(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"agreement_id": "a1", "product": "螺纹钢"},
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            agrs = await list_agreements(agent_id="b1")
            assert len(agrs) == 1


class TestRatings:
    @pytest.mark.asyncio
    async def test_submit_rating(self, mock_conn, mock_pool):
        mock_conn.fetchrow.return_value = {
            "rating_id": "r1",
            "from_agent": "a1",
            "to_agent": "a2",
            "score": 5,
            "created_at": None,
        }

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await submit_rating("a1", "a2", 5, comment="很好")
            assert result["score"] == 5

    @pytest.mark.asyncio
    async def test_get_agent_ratings(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = [
            {"rating_id": "r1", "score": 5},
            {"rating_id": "r2", "score": 4},
        ]
        mock_conn.fetchrow.return_value = {
            "avg_score": 4.5,
            "total_ratings": 2,
            "unique_raters": 2,
        }

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await get_agent_ratings("a1")
            assert result["avg_score"] == 4.5
            assert result["total_ratings"] == 2
            assert len(result["ratings"]) == 2


# ── 供应商排序 (QACP v0.6 §6.2) ────────────────────────


class TestComputeBaseScore:
    def test_perfect_match(self):
        bs = compute_base_score(industry_match=1.0, scale_match=1.0, price_score=1.0, quality_score=1.0)
        assert bs == 1.0

    def test_zero_all(self):
        bs = compute_base_score(industry_match=0.0, scale_match=0.0, price_score=0.0, quality_score=0.0)
        assert bs == 0.0

    def test_default_halves(self):
        bs = compute_base_score()
        # 0.30*0 + 0.30*0 + 0.20*0.5 + 0.20*0.5 = 0.20
        assert bs == 0.20

    def test_typical_mixed(self):
        bs = compute_base_score(industry_match=1.0, scale_match=0.8, price_score=0.7, quality_score=0.6)
        # 0.30*1.0 + 0.30*0.8 + 0.20*0.7 + 0.20*0.6 = 0.30+0.24+0.14+0.12 = 0.80
        assert round(bs, 4) == 0.80

    def test_clamp_max_one(self):
        bs = compute_base_score(industry_match=5.0, scale_match=5.0, price_score=5.0, quality_score=5.0)
        assert bs == 1.0


class TestComputeFinalRank:
    def test_c0_min(self):
        fr = compute_final_rank(0.2, "C0", 3.0)
        assert round(fr, 4) == round(0.2 * 0.3 * 3.0, 4)  # 0.18

    def test_c3_max(self):
        fr = compute_final_rank(1.0, "C3", 5.0)
        assert fr == 7.5  # 1.0 * 1.5 * 5.0

    def test_unknown_level_falls_to_c0(self):
        fr = compute_final_rank(1.0, "X99", 3.0)
        assert fr == 0.9  # 1.0 * 0.3 * 3.0

    def test_default_reputation(self):
        fr = compute_final_rank(0.71, "C2", 3.0)
        assert round(fr, 4) == 2.13  # 0.71 * 1.0 * 3.0


class TestCLevelWeights:
    def test_ordering(self):
        """C0 < C1 < C2 < C3"""
        w = C_LEVEL_WEIGHT
        assert w["C0"] < w["C1"] < w["C2"] < w["C3"]

    def test_c1_is_double_c0(self):
        assert C_LEVEL_WEIGHT["C1"] == 2.0 * C_LEVEL_WEIGHT["C0"]

    def test_four_levels(self):
        assert len(C_LEVEL_WEIGHT) == 4


class TestBaseScoreWeights:
    def test_sums_to_one(self):
        total = sum(BASE_SCORE_WEIGHTS.values())
        assert round(total, 4) == 1.0

    def test_all_factors_present(self):
        for key in ("industry_match", "scale_match", "price_score", "quality_score"):
            assert key in BASE_SCORE_WEIGHTS


class TestRankSuppliers:
    @pytest.mark.asyncio
    async def test_empty_candidates(self, mock_conn, mock_pool):
        mock_conn.fetch.return_value = []

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await rank_suppliers(buyer_industry="st")
            assert result == []

    @pytest.mark.asyncio
    async def test_ranks_and_sorts(self, mock_conn, mock_pool):
        mock_conn.fetch.side_effect = [
            # 1st call: candidates
            [
                {"agent_id": "a1", "name": "Supplier A", "industry": "st", "c_level": "C3", "scale": "medium"},
                {"agent_id": "a2", "name": "Supplier B", "industry": "st", "c_level": "C0", "scale": "medium"},
                {"agent_id": "a3", "name": "Supplier C", "industry": "mt", "c_level": "C2", "scale": "small"},
            ],
            # 2nd call: reputation
            [
                {"agent_id": "a1", "avg_score": 4.5, "total_ratings": 10},
                {"agent_id": "a2", "avg_score": 3.0, "total_ratings": 3},
                {"agent_id": "a3", "avg_score": 3.8, "total_ratings": 5},
            ],
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await rank_suppliers(buyer_industry="st", buyer_scale="medium", required_c_level="C0")

        assert len(result) == 3
        # C3 with 4.5 rep should rank highest
        assert result[0]["agent_id"] == "a1"
        assert result[0]["c_level"] == "C3"
        assert result[0]["c_level_weight"] == 1.5
        # C0 should rank lowest
        assert result[-1]["agent_id"] == "a2"
        assert result[-1]["c_level"] == "C0"
        assert result[-1]["c_level_weight"] == 0.3
        # Verify descending order
        for i in range(len(result) - 1):
            assert result[i]["final_rank"] >= result[i + 1]["final_rank"]

    @pytest.mark.asyncio
    async def test_required_c_level_filter(self, mock_conn, mock_pool):
        mock_conn.fetch.side_effect = [
            [{"agent_id": "a1", "name": "S1", "industry": "st", "c_level": "C2", "scale": "medium"}],
            [],
        ]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await rank_suppliers(buyer_industry="st", required_c_level="C2")

        assert len(result) == 1
        assert result[0]["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_limit(self, mock_conn, mock_pool):
        candidates = [
            {"agent_id": f"a{i}", "name": f"S{i}", "industry": "st", "c_level": "C2", "scale": "medium"}
            for i in range(10)
        ]
        ratings = [
            {"agent_id": f"a{i}", "avg_score": 3.5, "total_ratings": i}
            for i in range(10)
        ]
        mock_conn.fetch.side_effect = [candidates, ratings]

        with patch("huanyu.negotiation.get_pool", return_value=mock_pool):
            result = await rank_suppliers(buyer_industry="st", limit=5)

        assert len(result) == 5
