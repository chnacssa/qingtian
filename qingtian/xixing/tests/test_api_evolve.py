"""
吸星 API — /skills/evolve 路由单元测试
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xixing.api import router
from xixing.models import EvolveRequest, EvolveResponse

# 创建独立 app 挂载吸星路由
app = FastAPI()
app.include_router(router)

client = TestClient(app)


class MockPool:
    """模拟 asyncpg 连接池"""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestEvolveRoute:
    def test_evolve_not_management(self):
        """非 management 角色返回 403"""
        with patch("common.config.is_management", return_value=False):
            resp = client.post("/v1/xixing/skills/evolve", json={})
            assert resp.status_code == 403
            data = resp.json()
            assert "FORBIDDEN" in str(data)

    def test_evolve_management_dry_run(self):
        """management 角色 dry_run=True"""
        mock_pool = MockPool()
        with patch("common.config.is_management", return_value=True), \
             patch("xixing.api.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("xixing.distiller._generate_skill_proposals", AsyncMock(return_value=[])):
            resp = client.post("/v1/xixing/skills/evolve", json={"dry_run": True})
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 0
            assert data["dry_run"] is True
            assert data["proposals"] == []

    def test_evolve_with_proposals(self):
        """正常返回提案列表"""
        mock_proposals = [
            {
                "name": "steel_query",
                "display_name": "钢材查询",
                "description": "test",
                "category": "cost",
                "frequency": 15,
                "sample_queries": ["q1"],
                "knowledge_categories": ["钢材"],
            }
        ]
        mock_pool = MockPool()
        with patch("common.config.is_management", return_value=True), \
             patch("xixing.api.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("xixing.distiller._generate_skill_proposals", AsyncMock(return_value=mock_proposals)):
            resp = client.post("/v1/xixing/skills/evolve", json={"dry_run": False})
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] == 1
            assert data["proposals"][0]["name"] == "steel_query"
            assert data["dry_run"] is False

    def test_evolve_internal_error(self):
        """生成失败时返回 500"""
        mock_pool = MockPool()
        with patch("common.config.is_management", return_value=True), \
             patch("xixing.api.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("xixing.distiller._generate_skill_proposals", AsyncMock(side_effect=Exception("DB crash"))):
            resp = client.post("/v1/xixing/skills/evolve", json={})
            assert resp.status_code == 500
            data = resp.json()
            assert "EVOLVE_FAILED" in str(data)


class TestEvolveRouteDefaultDryRun:
    """不传 body 时默认 dry_run=False"""

    def test_default_dry_run_false(self):
        mock_pool = MockPool()
        with patch("common.config.is_management", return_value=True), \
             patch("xixing.api.get_pool", AsyncMock(return_value=mock_pool)), \
             patch("xixing.distiller._generate_skill_proposals", AsyncMock(return_value=[])):
            resp = client.post("/v1/xixing/skills/evolve")
            assert resp.status_code == 200
            data = resp.json()
            assert data["dry_run"] is False
