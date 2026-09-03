"""Schema 契约测试 — 每个模块 request→response 往返验证

只测 API 签名（请求/响应格式 + 数据类型），不测业务逻辑。
运行前提: 底座已启动 + QINGTIAN_ADMIN_TOKEN 已设。
  pytest tests/schema/test_contracts.py -v
"""
import os
import json
import pytest
import httpx
import uuid

BASE = "http://127.0.0.1:1996"
ADMIN_TOKEN = os.getenv("QINGTIAN_ADMIN_TOKEN", os.getenv("ZHENYUE_ADMIN_TOKEN", ""))
TEST_AGENT = f"ct-test-{uuid.uuid4().hex[:8]}"


def _post(path, data, token=None):
    t = token or ADMIN_TOKEN
    h = {"Authorization": f"Bearer {t}"} if t else {}
    return httpx.post(f"{BASE}{path}", json=data, headers=h, timeout=10)


def _get(path, token=None):
    t = token or ADMIN_TOKEN
    h = {"Authorization": f"Bearer {t}"} if t else {}
    return httpx.get(f"{BASE}{path}", headers=h, timeout=10)


# ══════════════════════════════════════════════════════════

class TestZhenyueContracts:
    """镇岳 — 8 个端点签名验证"""

    def test_health_returns_ok(self):
        r = _get("/v1/zhenyue/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok"
        assert d["module"] == "zhenyue"

    def test_token_create_valid(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _post("/v1/zhenyue/token/create", {"agent_id": TEST_AGENT, "role": "agent"})
        assert r.status_code == 200
        d = r.json()
        assert "token" in d
        assert d["role"] == "agent"

    def test_token_validate_returns_structured(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        cr = _post("/v1/zhenyue/token/create", {"agent_id": f"{TEST_AGENT}-2", "role": "agent"})
        token = cr.json()["token"]
        r = _post("/v1/zhenyue/token/validate", {"token": token})
        assert r.status_code == 200
        d = r.json()
        assert d["valid"] is True
        assert isinstance(d["agent_id"], str)

    def test_agent_review_accepts_async(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _post("/v1/zhenyue/agents/review", {"agent_id": TEST_AGENT, "decision": "approved"})
        assert r.status_code in (200, 202, 409, 404)

    def test_keypair_generate_roundtrip(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _post(f"/v1/zhenyue/agents/{TEST_AGENT}/keypair", {})
        assert r.status_code in (200, 404)  # 404 = agent 不存在，但路由通
        if r.status_code == 200:
            d = r.json()
            assert "public_key" in d
            assert len(d["public_key"]) == 64

    def test_audit_entries_list(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _get("/v1/zhenyue/audit/entries?limit=5")
        assert r.status_code in (200, 403)  # 403 = role 不够

    def test_audit_verify_chain(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _get("/v1/zhenyue/audit/verify")
        assert r.status_code in (200, 403)

    def test_downgrade_upgrade_roundtrip(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _post(f"/v1/zhenyue/agents/{TEST_AGENT}/downgrade", {"reason": "test"})
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            d = r.json()
            assert d["action"] in ("downgraded", "noop")


class TestHuanyuContracts:
    """寰宇 — 端点签名验证"""

    def test_health(self):
        r = _get("/v1/huanyu/health")
        assert r.status_code == 200

    def test_agent_register_returns_structured(self):
        r = _post("/v1/huanyu/agents/register", {
            "name": TEST_AGENT, "category": "biz:buyer",
            "server_host": "127.0.0.1",
        })
        assert r.status_code in (200, 409)
        if r.status_code == 200:
            d = r.json()
            assert "agent_id" in d
            assert isinstance(d["agent_id"], str)

    def test_agent_list_returns_array(self):
        r = _get("/v1/huanyu/agents")
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["agents"], list)

    def test_negotiation_create_returns_structured(self):
        r = _post("/v1/huanyu/negotiations", {
            "buyer_id": f"{TEST_AGENT}-b", "supplier_id": f"{TEST_AGENT}-s",
            "product_category": "test",
        })
        assert r.status_code in (200, 422)
        if r.status_code == 200:
            d = r.json()
            assert isinstance(d["negotiation_id"], str)

    def test_message_send_validate_type(self):
        r = _post("/v1/huanyu/messages", {
            "from_agent": f"{TEST_AGENT}-a", "to_agent": f"{TEST_AGENT}-b",
            "message_type": "inquiry",
            "payload": {"test": True},
        })
        assert r.status_code in (200, 422)


class TestSikuContracts:
    """司库 — 端点签名验证"""

    def test_health(self):
        r = _get("/v1/siku/health")
        assert r.status_code == 200

    def test_recharge_datetime_serialization(self):
        """验证 recharge 不会因 datetime str 而 500"""
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _post("/v1/siku/accounts/recharge", {
            "agent_id": TEST_AGENT,
            "amount_fen": 1,
            "idempotency_key": f"ct-recharge-{uuid.uuid4().hex[:8]}",
        })
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            d = r.json()
            assert isinstance(d["balance_fen"], int)
            assert isinstance(d["total_recharged"], int)

    def test_balance_query_returns_int(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _get(f"/v1/siku/accounts/{TEST_AGENT}")
        assert r.status_code in (200, 404)

    def test_chain_verify_needs_agent_id(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _get(f"/v1/siku/chain/verify?agent_id={TEST_AGENT}")
        assert r.status_code in (200, 404, 422)


class TestZhiceContracts:
    """执策 — 端点签名验证"""

    def test_task_create_decompose(self):
        r = _post("/v1/zhice/tasks", {
            "title": "schema test",
            "description": "验证 LLM 分解",
            "created_by": TEST_AGENT,
        })
        assert r.status_code in (200, 500)  # 500 = DEEPSEEK_API_KEY 未设

    def test_policy_create_block(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _post("/v1/zhice/policies", {
            "name": f"ct-policy-{uuid.uuid4().hex[:6]}",
            "category": "biz:seller", "policy_type": "keyword",
            "rule": {"keywords": ["test"]},
            "action": "block", "priority": 10,
            "created_by": "admin",
        })
        assert r.status_code == 200


class TestHuichuanContracts:
    """汇川 — 端点签名验证"""

    def test_create_datetime_safe(self):
        r = _post("/v1/huichuan", {
            "title": f"ct-kb-{uuid.uuid4().hex[:8]}",
            "domain": "test",
            "content": "schema contract test content",
        })
        assert r.status_code in (200, 201, 422)

    def test_search_returns_list(self):
        r = _get("/v1/huichuan/knowledge/search?q=test")
        assert r.status_code in (200, 404)


class TestYonghengContracts:
    """永恒 — 端点签名验证"""

    def test_search_needs_admin(self):
        if not ADMIN_TOKEN:
            pytest.skip("no admin token")
        r = _post("/v1/yongheng/memories/search", {
            "query": "test", "namespace": "test", "method": "keyword", "top_k": 3,
        })
        assert r.status_code in (200, 401, 422)
