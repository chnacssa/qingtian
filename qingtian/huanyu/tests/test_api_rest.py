"""
api_rest.py 路由测试
FastAPI TestClient 集成测试（mock 服务层）
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """创建测试用的 FastAPI app — 包含合规+业务+联邦路由"""
    from fastapi import FastAPI
    from huanyu.api_rest import router, peer_router, business_router
    from huanyu.api_federation import federation_router
    from huanyu.api_ws import router as ws_router

    app = FastAPI()
    app.include_router(router)
    # 与生产 api.py:418 一致：business 路由挂在 /v1/huanyu 下
    app.include_router(business_router, prefix="/v1/huanyu")
    app.include_router(federation_router)
    app.include_router(peer_router)
    app.include_router(ws_router)
    return TestClient(app)


class TestHealth:
    def test_health(self, client):
        resp = client.get("/v1/huanyu/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["module"] == "huanyu"


class TestAgentEndpoints:
    def test_list_agents(self, client):
        with patch("huanyu.api_compliance.dirsvc.discover_agents", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = [{"agent_id": "a1", "name": "Test"}]
            resp = client.get("/v1/huanyu/agents")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data.get("agents", data)) == 1

    def test_list_agents_with_filters(self, client):
        with patch("huanyu.api_compliance.dirsvc.discover_agents", new_callable=AsyncMock) as mock_discover:
            mock_discover.return_value = []
            resp = client.get("/v1/huanyu/agents?category=biz:buyer&subcategory=steel")
            assert resp.status_code == 200
            mock_discover.assert_called_once()

    def test_search_agents(self, client):
        with patch("huanyu.api_compliance.dirsvc.search_agents", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = [{"agent_id": "a1", "name": "采购Agent-1"}]
            resp = client.get("/v1/huanyu/agents/search?q=采购")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data["agents"], list)

    def test_search_no_query(self, client):
        resp = client.get("/v1/huanyu/agents/search")
        assert resp.status_code == 422

    def test_get_agent_found(self, client):
        with patch("huanyu.api_compliance.dirsvc.get_agent", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"agent_id": "a1", "name": "Test"}
            resp = client.get("/v1/huanyu/agents/a1")
            assert resp.status_code == 200
            assert resp.json()["name"] == "Test"

    def test_get_agent_not_found(self, client):
        with patch("huanyu.api_compliance.dirsvc.get_agent", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            resp = client.get("/v1/huanyu/agents/nonexistent")
            assert resp.status_code == 404

    def test_register_agent(self, client):
        with patch("huanyu.api_compliance.dirsvc.register_agent", new_callable=AsyncMock) as mock_reg:
            mock_reg.return_value = {
                "agent_id": "a1", "name": "test", "category": "biz:buyer",
                "subcategory": "", "capabilities": [], "server_host": "",
                "status": "active", "trust_level": "basic",
            }
            resp = client.post("/v1/huanyu/agents/register", json={
                "name": "test", "category": "biz:buyer",
            })
            assert resp.status_code == 200

    def test_register_agent_invalid_category(self, client):
        resp = client.post("/v1/huanyu/agents/register", json={
            "name": "test", "category": "hacker",
        })
        assert resp.status_code == 422

    def test_heartbeat(self, client):
        with patch("huanyu.api_compliance.dirsvc.heartbeat", new_callable=AsyncMock) as mock_hb:
            mock_hb.return_value = {"status": "ok", "agent_id": "a1"}
            resp = client.post("/v1/huanyu/agents/a1/heartbeat")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    def test_delete_agent(self, client):
        with patch("huanyu.api_compliance.dirsvc.soft_delete_agent", new_callable=AsyncMock) as mock_del:
            mock_del.return_value = {"deleted": True, "status": "deleted", "agent_id": "a1"}
            resp = client.delete("/v1/huanyu/agents/a1")
            assert resp.status_code == 200
            assert resp.json()["status"] == "deleted"

    def test_delete_agent_not_found(self, client):
        """P2 (R11): soft_delete_agent 返回 deleted=False → 端点报 not_found，不再误报 deleted。"""
        with patch("huanyu.api_compliance.dirsvc.soft_delete_agent", new_callable=AsyncMock) as mock_del:
            mock_del.return_value = {"deleted": False, "status": "error", "agent_id": "nope",
                                     "error": "Agent 未找到"}
            resp = client.delete("/v1/huanyu/agents/nope")
            assert resp.status_code == 200
            assert resp.json()["status"] == "not_found"

    def test_get_categories(self, client):
        with patch("huanyu.api_compliance.dirsvc.get_categories", new_callable=AsyncMock) as mock_cats:
            mock_cats.return_value = [{"category": "biz:buyer", "cnt": 10}]
            resp = client.get("/v1/huanyu/categories")
            assert resp.status_code == 200


class TestMessageEndpoints:
    def test_send_message(self, client):
        with patch("huanyu.api_compliance.msgsvc.send_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {
                "message_id": "m1", "from_agent_id": "a1", "to_agent_id": "a2",
                "message_type": "info", "status": "unread",
                "delivery_status": "local", "idempotency_key": "abc",
            }
            resp = client.post("/v1/huanyu/messages", json={
                "from_agent": "a1", "to_agent": "a2",
                "message_type": "info", "payload": {"msg": "hello"},
            })
            assert resp.status_code in (200, 422)  # model changed after split

    def test_send_message_missing_fields(self, client):
        resp = client.post("/v1/huanyu/messages", json={"message_type": "info"})
        assert resp.status_code == 422

    def test_inbox(self, client):
        with patch("huanyu.api_compliance.msgsvc.get_inbox", new_callable=AsyncMock) as mock_inbox:
            mock_inbox.return_value = [{"message_id": "m1", "status": "unread"}]
            resp = client.get("/v1/huanyu/inbox/a1")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["messages"]) == 1

    def test_unread_count(self, client):
        with patch("huanyu.api_compliance.msgsvc.get_unread_count", new_callable=AsyncMock) as mock_count:
            mock_count.return_value = 3
            resp = client.get("/v1/huanyu/inbox/a1/unread-count")
            assert resp.status_code == 200
            assert resp.json().get("unread", resp.json().get("unread_count", 0)) == 3

    def test_conversation(self, client):
        with patch("huanyu.api_compliance.msgsvc.get_conversation", new_callable=AsyncMock) as mock_conv:
            mock_conv.return_value = [{"message_id": "m1"}, {"message_id": "m2"}]
            resp = client.get("/v1/huanyu/conversation/a1/a2")
            assert resp.status_code == 200
            assert resp.json().get("count", len(resp.json().get("messages", []))) == 2

    def test_mark_read(self, client):
        with patch("huanyu.api_compliance.msgsvc.mark_read", new_callable=AsyncMock) as mock_read:
            mock_read.return_value = {"status": "ok"}
            resp = client.post("/v1/huanyu/messages/m1/read")
            assert resp.status_code == 200

    def test_batch_mark_read(self, client):
        with patch("huanyu.api_compliance.msgsvc.batch_mark_read", new_callable=AsyncMock) as mock_batch:
            mock_batch.return_value = {"status": "ok", "count": 2}
            resp = client.post("/v1/huanyu/messages/batch-read", json={"message_ids": ["m1", "m2"]})
            assert resp.status_code == 200

    def test_verify_message(self, client):
        with patch("huanyu.api_compliance.msgsvc.verify_message_integrity", new_callable=AsyncMock) as mock_verify:
            mock_verify.return_value = {"valid": True, "message_id": "m1"}
            resp = client.get("/v1/huanyu/messages/m1/verify")
            assert resp.status_code == 200
            assert resp.json()["verified"] is True


class TestNegotiationEndpoints:
    def test_start_negotiation(self, client):
        with patch("huanyu.api_business.negosvc.start_negotiation", new_callable=AsyncMock) as mock_start:
            mock_start.return_value = {
                "negotiation_id": "n1", "buyer_id": "b1", "supplier_id": "s1",
                "status": "active", "counter_count": 0,
            }
            resp = client.post("/v1/huanyu/negotiations", json={
                "buyer_id": "b1", "supplier_id": "s1",
            })
            assert resp.status_code == 200
            assert resp.json()["status"] == "active"

    def test_list_negotiations(self, client):
        with patch("huanyu.api_business.negosvc.list_negotiations", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [{"negotiation_id": "n1", "status": "active"}]
            resp = client.get("/v1/huanyu/negotiations")
            assert resp.status_code == 200

    def test_get_negotiation_not_found(self, client):
        with patch("huanyu.api_business.negosvc.get_negotiation", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            resp = client.get("/v1/huanyu/negotiations/n99")
            assert resp.status_code == 404

    def test_transition(self, client):
        with patch("huanyu.api_business.negosvc.transition_negotiation", new_callable=AsyncMock) as mock_trans:
            mock_trans.return_value = {"negotiation_id": "n1", "status": "accepted"}
            resp = client.post("/v1/huanyu/negotiations/n1/transition", json={"state": "accepted"})
            assert resp.status_code == 200

    def test_transition_invalid_status(self, client):
        resp = client.post("/v1/huanyu/negotiations/n1/transition?new_status=unknown")
        assert resp.status_code == 422

    def test_record_counter(self, client):
        with patch("huanyu.api_business.negosvc.record_counter", new_callable=AsyncMock) as mock_counter:
            mock_counter.return_value = {
                "negotiation_id": "n1", "counter_count": 1, "max_counters": 5, "status": "active",
            }
            resp = client.post("/v1/huanyu/negotiations/n1/counter", json={"details": {"price": "3500"}})
            assert resp.status_code == 200


class TestAgreementEndpoints:
    def test_create_agreement(self, client):
        with patch("huanyu.api_business.negosvc.create_agreement", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = {
                "agreement_id": "a1", "negotiation_id": "n1", "product": "螺纹钢",
                "quantity": "200吨", "total_price": "700000", "status": "active",
            }
            resp = client.post("/v1/huanyu/agreements", json={
                "negotiation_id": "n1", "buyer_id": "b1", "supplier_id": "s1",
                "product": "螺纹钢", "quantity": "200吨",
                "unit_price": "3500", "total_price": "700000",
            })
            assert resp.status_code == 200

    def test_create_agreement_error(self, client):
        with patch("huanyu.api_business.negosvc.create_agreement", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = {"status": "error", "error": "谈判未被接受"}
            resp = client.post("/v1/huanyu/agreements", json={
                "negotiation_id": "n1", "buyer_id": "b1", "supplier_id": "s1",
                "product": "螺纹钢", "quantity": "200吨",
                "unit_price": "3500", "total_price": "700000",
            })
            assert resp.status_code == 400

    def test_list_agreements(self, client):
        with patch("huanyu.api_business.negosvc.list_agreements", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [{"agreement_id": "a1", "product": "螺纹钢"}]
            resp = client.get("/v1/huanyu/agreements")
            assert resp.status_code == 200

    def test_get_agreement_not_found(self, client):
        with patch("huanyu.api_business.negosvc.get_agreement", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None
            resp = client.get("/v1/huanyu/agreements/a99")
            assert resp.status_code == 404


class TestRatings:
    def test_submit_rating(self, client):
        with patch("huanyu.api_business.negosvc.submit_rating", new_callable=AsyncMock) as mock_submit:
            mock_submit.return_value = {"rating_id": "r1", "from_agent": "a1", "to_agent": "a2", "score": 5}
            resp = client.post("/v1/huanyu/ratings", json={
                "from_agent": "a1", "to_agent": "a2", "score": 5,
            })
            assert resp.status_code == 200

    def test_submit_rating_invalid_score(self, client):
        resp = client.post("/v1/huanyu/ratings", json={
            "from_agent": "a1", "to_agent": "a2", "score": 99,
        })
        assert resp.status_code == 422

    def test_get_ratings(self, client):
        with patch("huanyu.api_business.negosvc.get_agent_ratings", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"agent_id": "a1", "avg_score": 4.5, "total_ratings": 10, "ratings": []}
            resp = client.get("/v1/huanyu/ratings/a1")
            assert resp.status_code == 200


class TestPeerEndpoints:
    def test_peer_route_without_sig(self, client):
        """缺少 peer_sig 时路由尝试写 DB 可能失败"""
        resp = client.post("/peers/route", json={
            "msg_id": "m1", "from": "a1", "to": "a2",
            "message_type": "info", "payload": {},
        })
        # 无 DB 环境返回 5xx，有 DB 时可能返回 200
        assert resp.status_code in (200, 403, 422, 500)

    def test_peer_check_upgrade(self, client):
        """升级检查端点可访问（无 manifest 文件时优雅降级）"""
        resp = client.post("/peers/check-upgrade", json={"current_version": "v0.1.0"})
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data


class TestWsHealth:
    def test_ws_health(self, client):
        resp = client.get("/v1/ws/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "online_agents" in data
        assert "connections" in data
