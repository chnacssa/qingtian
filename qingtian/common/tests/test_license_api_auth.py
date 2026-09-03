"""A7 (R11): license_api 订阅管理端点角色校验 — 单元测试

验证 /v1/license/subscriptions GET/POST 必须携带有效 X-Admin-Token；
无令牌一律 401，防止任意访问者开通/吊销企业订阅。
"""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.license_api import router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def _admin_env():
    os.environ["ZHENYUE_ADMIN_TOKEN"] = "test-admin-token"
    yield
    os.environ.pop("ZHENYUE_ADMIN_TOKEN", None)


def test_list_subscriptions_requires_admin():
    """无 X-Admin-Token → 401"""
    app = _make_app()
    with patch("common.license_api.get_pool"):
        resp = TestClient(app).get("/v1/license/subscriptions")
        assert resp.status_code == 401


def test_upsert_subscription_requires_admin():
    """无 X-Admin-Token → 401"""
    app = _make_app()
    with patch("common.license_api.get_pool"):
        resp = TestClient(app).post("/v1/license/subscriptions", json={})
        assert resp.status_code == 401


def test_list_with_valid_admin_token_passes():
    """有效 X-Admin-Token → 200（走业务逻辑）"""
    app = _make_app()
    pool = AsyncMock()
    pool.fetch.return_value = []
    with patch("common.license_api.get_pool", return_value=pool):
        resp = TestClient(app).get(
            "/v1/license/subscriptions",
            headers={"X-Admin-Token": "test-admin-token"},
        )
        assert resp.status_code == 200
        assert resp.json()["subscriptions"] == []


def test_upsert_with_valid_admin_token_passes():
    """有效 X-Admin-Token → 业务逻辑可达（mock DB 调用）"""
    app = _make_app()
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    with (
        patch("common.license_api.get_pool", return_value=pool),
        patch("common.license_api.push_sync_to_client", new=AsyncMock()),
    ):
        resp = TestClient(app).post(
            "/v1/license/subscriptions",
            json={
                "enterprise_name": "测试企业",
                "module": "bidding",
                "plan": "pro",
                "start_date": "2026-01-01",
                "end_date": "2027-01-01",
                "amount": 1000,
            },
            headers={"X-Admin-Token": "test-admin-token"},
        )
        # 名称未匹配到企业 → 404（说明已通过鉴权，进入业务层）
        assert resp.status_code == 404
        assert "未找到企业" in resp.json()["detail"]
