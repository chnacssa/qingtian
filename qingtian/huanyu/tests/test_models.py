"""
models.py 单元测试
Pydantic v2 数据模型验证
"""

import pytest
from pydantic import ValidationError

from huanyu.models import (
    AGENT_CATEGORIES,
    AGENT_STATUSES,
    AGREEMENT_STATUSES,
    MESSAGE_PRIORITIES,
    MESSAGE_STATUSES,
    MESSAGE_TYPES,
    NEGOTIATION_STATUSES,
    AgentResponse,
    CreateAgreementRequest,
    NegotiationResponse,
    RatingResponse,
    RegisterAgentRequest,
    SendMessageRequest,
    StartNegotiationRequest,
    SubmitRatingRequest,
    StatsResponse,
    TopicPublishRequest,
    TopicSubscribeRequest,
)


# ── RegisterAgentRequest ────────────────────────────

class TestRegisterAgentRequest:
    def test_valid_minimal(self):
        req = RegisterAgentRequest(name="test", category="biz:buyer")
        assert req.name == "test"
        assert req.category == "biz:buyer"
        assert req.subcategory == ""
        assert req.capabilities == []

    def test_valid_full(self):
        req = RegisterAgentRequest(
            name="采购Agent-1",
            category="biz:seller",
            subcategory="steel",
            capabilities=["inquiry", "negotiate"],
            contact_info="tel:13800138000",
            server_host="procurement",
            metadata={"region": "east"},
        )
        assert req.server_host == "procurement"
        assert req.metadata["region"] == "east"

    def test_name_too_short(self):
        with pytest.raises(ValidationError):
            RegisterAgentRequest(name="", category="biz:buyer")

    def test_name_too_long(self):
        with pytest.raises(ValidationError):
            RegisterAgentRequest(name="x" * 129, category="biz:buyer")

    def test_invalid_category(self):
        with pytest.raises(ValidationError):
            RegisterAgentRequest(name="test", category="hacker")

    def test_all_valid_categories(self):
        for cat in AGENT_CATEGORIES:
            req = RegisterAgentRequest(name="test", category=cat)
            assert req.category == cat


# ── SendMessageRequest ──────────────────────────────

class TestSendMessageRequest:
    def test_valid_minimal(self):
        req = SendMessageRequest(from_agent="a1", to_agent="a2")
        assert req.message_type == "info"
        assert req.priority == "normal"

    def test_valid_full(self):
        req = SendMessageRequest(
            from_agent="a1", to_agent="a2",
            message_type="inquiry",
            payload={"product": "螺纹钢"},
            priority="high",
            reply_to="msg-uuid",
            negotiation_id="nego-uuid",
            topic="钢材.螺纹钢",
        )
        assert req.message_type == "inquiry"
        assert req.priority == "high"
        assert req.topic == "钢材.螺纹钢"

    def test_missing_from_agent(self):
        with pytest.raises(ValidationError):
            SendMessageRequest(from_agent="", to_agent="a2")

    def test_missing_to_agent(self):
        with pytest.raises(ValidationError):
            SendMessageRequest(from_agent="a1", to_agent="")

    def test_invalid_message_type(self):
        with pytest.raises(ValidationError):
            SendMessageRequest(from_agent="a1", to_agent="a2", message_type="bad")

    def test_invalid_priority(self):
        with pytest.raises(ValidationError):
            SendMessageRequest(from_agent="a1", to_agent="a2", priority="critical")

    def test_all_message_types_valid(self):
        for mt in MESSAGE_TYPES:
            req = SendMessageRequest(from_agent="a1", to_agent="a2", message_type=mt)
            assert req.message_type == mt


# ── StartNegotiationRequest ─────────────────────────

class TestStartNegotiationRequest:
    def test_valid(self):
        req = StartNegotiationRequest(
            buyer_id="b1", supplier_id="s1",
            product_category="steel",
            max_counters=3,
        )
        assert req.max_counters == 3

    def test_default_max_counters(self):
        req = StartNegotiationRequest(buyer_id="b1", supplier_id="s1")
        assert req.max_counters == 5

    def test_max_counters_too_low(self):
        with pytest.raises(ValidationError):
            StartNegotiationRequest(buyer_id="b1", supplier_id="s1", max_counters=0)

    def test_max_counters_too_high(self):
        with pytest.raises(ValidationError):
            StartNegotiationRequest(buyer_id="b1", supplier_id="s1", max_counters=21)

    def test_missing_buyer(self):
        with pytest.raises(ValidationError):
            StartNegotiationRequest(buyer_id="", supplier_id="s1")


# ── SubmitRatingRequest ─────────────────────────────

class TestSubmitRatingRequest:
    def test_valid(self):
        req = SubmitRatingRequest(
            from_agent="a1", to_agent="a2",
            score=5,
            comment="服务很好",
        )
        assert req.score == 5

    def test_score_too_low(self):
        with pytest.raises(ValidationError):
            SubmitRatingRequest(from_agent="a1", to_agent="a2", score=0)

    def test_score_too_high(self):
        with pytest.raises(ValidationError):
            SubmitRatingRequest(from_agent="a1", to_agent="a2", score=6)

    def test_comment_too_long(self):
        with pytest.raises(ValidationError):
            SubmitRatingRequest(from_agent="a1", to_agent="a2", score=3, comment="x" * 501)

    def test_dimensions(self):
        req = SubmitRatingRequest(
            from_agent="a1", to_agent="a2",
            score=4,
            dimensions={"speed": 5, "quality": 4, "comm": 3},
        )
        assert req.dimensions["speed"] == 5


# ── CreateAgreementRequest ──────────────────────────

class TestCreateAgreementRequest:
    def test_valid(self):
        req = CreateAgreementRequest(
            negotiation_id="n1", buyer_id="b1", supplier_id="s1",
            product="螺纹钢", quantity="200吨",
            unit_price="3500元/吨", total_price="700000元",
        )
        assert req.product == "螺纹钢"

    def test_empty_product(self):
        with pytest.raises(ValidationError):
            CreateAgreementRequest(
                negotiation_id="n1", buyer_id="b1", supplier_id="s1",
                product="", quantity="200吨",
                unit_price="3500", total_price="700000",
            )


# ── Topic requests ──────────────────────────────────

class TestTopicRequests:
    def test_subscribe_valid(self):
        req = TopicSubscribeRequest(
            agent_id="a1",
            topics=["钢材.螺纹钢", "钢材.线材"],
        )
        assert len(req.topics) == 2

    def test_subscribe_empty_topics(self):
        with pytest.raises(ValidationError):
            TopicSubscribeRequest(agent_id="a1", topics=[])

    def test_publish_valid(self):
        req = TopicPublishRequest(
            topic="钢材.螺纹钢",
            message_type="inquiry",
            payload={"product": "螺纹钢"},
            from_agent="a1",
        )
        assert req.topic == "钢材.螺纹钢"

    def test_publish_empty_topic(self):
        with pytest.raises(ValidationError):
            TopicPublishRequest(topic="", from_agent="a1")


# ── Response models ─────────────────────────────────

class TestResponseModels:
    def test_agent_response(self):
        resp = AgentResponse(
            agent_id="uuid",
            name="test",
            category="biz:buyer",
            subcategory="",
            capabilities=[],
            server_host="localhost",
            status="active",
            trust_level="basic",
        )
        assert resp.agent_id == "uuid"

    def test_stats_response_defaults(self):
        resp = StatsResponse()
        assert resp.total_agents == 0
        assert resp.active_agents == 0


# ── 枚举完整性 ──────────────────────────────────────

class TestEnums:
    def test_agent_categories(self):
        assert "biz:buyer" in AGENT_CATEGORIES
        assert "biz:seller" in AGENT_CATEGORIES
        assert "sys:admin" in AGENT_CATEGORIES

    def test_agent_statuses(self):
        assert "active" in AGENT_STATUSES
        assert "suspended" in AGENT_STATUSES
        assert "deleted" in AGENT_STATUSES

    def test_message_types(self):
        essential = {"inquiry", "quote", "counter", "accept", "reject", "info"}
        assert essential.issubset(set(MESSAGE_TYPES))

    def test_negotiation_statuses(self):
        assert "expired" in NEGOTIATION_STATUSES
        assert "accepted" in NEGOTIATION_STATUSES

    def test_agreement_statuses(self):
        assert "disputed" in AGREEMENT_STATUSES
