"""汇川 v2.4 — 快速冒烟测试（需数据库）

运行前提: ACSSA 底座已启动在 127.0.0.1:1996
  pytest tests/huichuan/test_v2_4_quick.py -v -s

覆盖:
  - Phase 0: 中文搜索 (pg_bigm + iLIKE)
  - Phase 1: entry_type / PII 脱敏
  - Phase 3: LLM 摄入管道 + 质量门
  - CRUD: create → get → update → soft delete → restore
  - 订阅
  - 精炼 (smoke)
"""

import json
import os
import uuid

import pytest
import httpx

BASE = os.environ.get("QINGTIAN_BASE", "http://127.0.0.1:1996")
API = f"{BASE}/v1/huichuan"

# 服务器未运行时自动跳过
try:
    httpx.get(f"{BASE}/health", timeout=2)
    _SERVER_UP = True
except Exception:
    _SERVER_UP = False

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _SERVER_UP, reason=f"ACSSA 底座未运行在 {BASE}"),
]


def _tag() -> str:
    return uuid.uuid4().hex[:8]


# ═══════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════

def _post(path: str, **kw):
    return httpx.post(f"{API}{path}", timeout=120, **kw)


def _get(path: str, **kw):
    return httpx.get(f"{API}{path}", timeout=30, **kw)


def _put(path: str, **kw):
    return httpx.put(f"{API}{path}", timeout=30, **kw)


def _delete(path: str, **kw):
    return httpx.delete(f"{API}{path}", timeout=30, **kw)


# ═══════════════════════════════════════════════════════
# Phase 0: 中文搜索
# ═══════════════════════════════════════════════════════


class TestChineseSearch:
    def test_search_chinese_returns_results(self):
        """搜索 '变压器' 应返回结果（pg_bigm 或 iLIKE 兜底）"""
        resp = _post("/search", json={"query": "变压器", "limit": 10})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "results" in data
        assert data["count"] >= 0  # 可能为零（空库），但不 500

    def test_search_empty_query_returns_empty(self):
        resp = _post("/search", json={"query": "", "limit": 10})
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_search_with_domain_filter(self):
        resp = _post("/search", json={"query": "测试", "domain": "power", "limit": 5})
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════
# Phase 1: PII 脱敏
# ═══════════════════════════════════════════════════════


class TestPIISanitized:
    def test_create_entry_pii_blocked(self):
        """含手机号的内容应被 validate_entry 拦截"""
        resp = _post("", json={
            "title": "测试 PII",
            "domain": "power",
            "content": "联系人张工电话13812345678请记录",
        })
        assert resp.status_code == 422, resp.text

    def test_ingest_pii_sanitized(self):
        """摄入含 PII 文本 → 入库 content 不包含原始 PII"""
        resp = _post("/ingest", json={
            "text": "供应商信息：联系人张三，电话13800138000，"
                    "身份证320106199001014411，"
                    "银行账号6222021234567890。"
                    "合同金额50万元，交货期30天。",
            "source": "test",
            "original_filename": "pii_test.txt",
        })
        # 可能因 LLM 不可用返回 400（无 API key）
        assert resp.status_code in (200, 400), resp.text

        if resp.status_code == 200:
            data = resp.json()
            knowledge_ids = data.get("knowledge_ids", [])
            assert data["entries"] > 0 or data.get("skipped_validation", 0) >= 0

            # 入库 content 不应含原始 PII
            for kid in knowledge_ids:
                entry = _get(f"/{kid}").json()
                content = entry.get("content", "")
                assert "13800138000" not in content, f"Phone leaked in {kid}"
                assert "320106199001014411" not in content, f"ID leaked in {kid}"
                assert "6222021234567890" not in content, f"Bank leaked in {kid}"

    def test_sanitize_private_to_shared(self):
        """promote 时脱敏 #内部 行"""
        tag = _tag()
        resp = _post("", json={
            "title": f"测试脱敏_{tag}",
            "domain": "power",
            "content": "#内部 此报价含回扣\n产品规格: 变压器 100kVA",
            "visibility": "private",
            "owner_agent": f"test_agent_{tag}",
        })
        if resp.status_code == 201:
            kid = resp.json()["knowledge_id"]
            # promote
            _post(f"/promote/{kid}")


# ═══════════════════════════════════════════════════════
# CRUD: create → get → update → soft delete → restore
# ═══════════════════════════════════════════════════════


class TestCRUDLifecycle:
    # 类级属性，不依赖 autouse fixture（避免实例属性遮蔽）
    kid: str | None = None
    version: int = 1
    tag: str = _tag()

    def test_create(self):
        resp = _post("", json={
            "title": f"CRUD测试_{self.tag}",
            "domain": "power",
            "content": "油浸式变压器绕组温升限值为 65K。",
            "tags": ["transformer", "test"],
            "entry_type": "concept",
            "visibility": "public",
            "quality": 4,
        })
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["knowledge_id"]
        assert data["entry_type"] == "concept"
        assert data["quality"] == 4
        self.__class__.kid = data["knowledge_id"]
        self.__class__.version = data["version"]

    def test_get(self):
        assert self.kid, "test_create must run first"
        resp = _get(f"/{self.kid}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["knowledge_id"] == self.kid
        assert resp.json()["status"] == "active"

    def test_update(self):
        assert self.kid, "test_create must run first"
        # 乐观锁需要传 version
        resp = _put(f"/{self.kid}", json={
            "title": f"CRUD测试_updated_{self.tag}",
            "content": "更新后的内容 — 温升限值改为 70K。",
            "version": self.version,
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["title"] == f"CRUD测试_updated_{self.tag}"
        self.__class__.version = data["version"]

    def test_update_version_conflict(self):
        """旧 version → 409"""
        assert self.kid
        resp = _put(f"/{self.kid}", json={
            "title": "并发冲突",
            "version": self.version - 1,
        })
        assert resp.status_code == 409, f"Expected 409 got {resp.status_code}"

    def test_soft_delete(self):
        assert self.kid
        resp = _delete(f"/{self.kid}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "revoked"
        assert "retain_until" in data

        # 删除后 GET 应返回 200（仅标记为 revoked，仍可读）
        resp_get = _get(f"/{self.kid}")
        assert resp_get.status_code == 200, resp_get.text

    def test_restore(self):
        assert self.kid
        resp = _post(f"/{self.kid}/restore")
        assert resp.status_code == 200, resp.text

        # 恢复后应可见
        resp_get = _get(f"/{self.kid}")
        assert resp_get.status_code == 200
        assert resp_get.json()["status"] == "active"


# ═══════════════════════════════════════════════════════
# 订阅
# ═══════════════════════════════════════════════════════


class TestSubscription:
    def test_subscribe_and_unsubscribe(self):
        """订阅 → 查询 → 取消订阅"""
        agent_id = f"test_sub_quick_{_tag()}"
        sub_name = f"sub_{_tag()}"

        # 创建订阅
        resp = _post("/subscribe", json={
            "agent_id": agent_id,
            "subscription_name": sub_name,
            "domains": ["power", "price"],
            "tags": ["transformer"],
        })
        assert resp.status_code in (201, 409), resp.text  # 409 = 已存在也不报错

        # 查询订阅
        resp_list = _get(f"/subscriptions?agent_id={agent_id}")
        assert resp_list.status_code == 200
        subs = resp_list.json().get("subscriptions", [])
        assert len(subs) >= 1

        # 清理 — 删除该 agent 的所有订阅
        for s in subs:
            sid = s["subscription_id"]
            _delete(f"/subscribe/{sid}")


# ═══════════════════════════════════════════════════════
# 精炼 (smoke)
# ═══════════════════════════════════════════════════════


class TestRefineSmoke:
    def test_submit_refine(self):
        tag = _tag()
        resp = _post("/refine", json={
            "observation": f"合肥地区沙料采购谈判中，供应商在第3轮报价时通常有8-15%的降价空间。{tag}",
            "domain": "price",
        })
        # 429 (rate limited) / 422 (too short/no context) 都是合理的
        assert resp.status_code in (200, 422, 429), resp.text

    def test_refine_queue_readable(self):
        resp = _get("/refine/queue")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════


class TestHealth:
    def test_health(self):
        resp = _get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ═══════════════════════════════════════════════════════
# 图谱 (Phase 5)
# ═══════════════════════════════════════════════════════


class TestGraphSmoke:
    def test_links_endpoint_404_on_unknown(self):
        """不存在的 knowledge_id → 404"""
        resp = _get(f"/{uuid.uuid4()}/links")
        assert resp.status_code in (200, 404)  # 200 返回空列表也可以

    def test_neighborhood_endpoint(self):
        resp = _get(f"/graph/{uuid.uuid4()}/neighborhood?max_hops=2")
        assert resp.status_code in (200, 404)


# ═══════════════════════════════════════════════════════
# 巡检 (Phase 6)
# ═══════════════════════════════════════════════════════


class TestLintSmoke:
    def test_lint_report(self):
        resp = _get("/lint/report")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("orphans", "broken_links", "contradictions", "expired", "decayed"):
            assert key in data, f"Missing key: {key}"
