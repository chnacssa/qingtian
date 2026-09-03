"""license /sync 验签与 tier 判定 — 单元测试（2026-08-27 P0-1 / P1-3 / P1-4 修复）

P0-1: SIGN_KEY 未配置时 /v1/license/sync 必须 fail-closed 拒绝（原实现跳过校验
      直接放行，默认部署任意人 POST 即可给客户机提权 pro）。
P1-3: registered_at 缺失不再永久 pro（保守 free）。
P1-4: enterprise 计划不再被卷入 90 天赠礼分支。
"""

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.license_api import client_router
from common import license as lic


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(client_router)
    return app


class TestSyncFailClosed:
    """P0-1: /sync 验签 fail-closed"""

    def test_missing_sign_key_rejected(self):
        """HUANYU_SIGN_KEY 未配置 → 拒绝处理（不更新缓存不写文件）"""
        app = _make_app()
        # /sync 内 SIGN_KEY 为函数内 from common.license 导入 → patch 源模块
        with patch("common.license.SIGN_KEY", ""):
            resp = TestClient(app).post("/v1/license/sync", json={
                "enterprise_id": "ent-x", "plan": "pro",
                "signature": "whatever",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is False
            assert "fail-closed" in body["reason"] or "SIGN_KEY" in body["reason"]

    def test_bad_signature_rejected(self):
        """配了 key 但签名错 → 拒绝"""
        app = _make_app()
        with patch("common.license.SIGN_KEY", "secret"):
            resp = TestClient(app).post("/v1/license/sync", json={
                "enterprise_id": "ent-x", "plan": "pro",
                "signature": "0" * 64,
            })
            assert resp.json()["ok"] is False

    def test_valid_signature_accepted_and_resigns(self, tmp_path):
        """P1-5: 合法签名 → 接受；写回 license.yaml 时顶层 HMAC 必须重算
        （否则下次 load_license 验签必失配，整机降级 free——推送一次毁一次）"""
        key = "secret"
        lic_path = tmp_path / "license.yaml"
        # 预置一份已验签通过的本地 license
        import yaml
        base = {"plan": "free", "enterprise_id": "ent-x"}
        base["signature"] = hmac.new(
            key.encode(), __import__("json").dumps(base, sort_keys=True).encode(),
            hashlib.sha256,
        ).hexdigest()
        lic_path.write_text(yaml.dump(base), encoding="utf-8")

        app = _make_app()
        payload = "ent-x:pro"
        sig = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        with patch("common.license.SIGN_KEY", key), \
             patch("common.license.LICENSE_PATH", str(lic_path)), \
             patch("common.license._cloud_cache", {}):
            # 重置 mtime 缓存强制重读
            lic._license_mtime = 0
            resp = TestClient(app).post("/v1/license/sync", json={
                "enterprise_id": "ent-x", "plan": "pro", "signature": sig,
            })
            assert resp.json()["ok"] is True

            # 写回后必须仍能通过验签（重算过的顶层签名）
            lic._license_mtime = 0  # 强制重读
            reloaded = lic.load_license()
            assert reloaded.get("plan") != "free" or "modules" in reloaded, \
                "写回后 load_license 不应降级 free（顶层签名须重算）"


class TestEffectiveTierGift:
    """P1-3 / P1-4: 赠礼分支修正"""

    @pytest.mark.asyncio
    async def test_missing_registered_at_downgrades_free(self):
        """registered_at 缺失 → 保守 free（原实现永久 pro）"""
        with patch("common.license.get_plan", return_value="free"):
            tier = await lic.get_effective_tier(None, enterprise_id="ent-x")
            assert tier == "free"

    @pytest.mark.asyncio
    async def test_recent_registration_gets_gift(self):
        """free 用户注册 30 天 → 赠礼 pro 不受影响"""
        recent = datetime.now(timezone.utc) - timedelta(days=30)
        with patch("common.license.get_plan", return_value="free"):
            tier = await lic.get_effective_tier(recent, enterprise_id="ent-x")
            assert tier == "pro"

    @pytest.mark.asyncio
    async def test_enterprise_plan_skips_gift_branch(self):
        """P1-4: enterprise 计划不走赠礼分支——直接进云端复核路径"""
        old = datetime.now(timezone.utc) - timedelta(days=365)
        cloud_calls = []

        async def fake_cloud(eid, module="bidding"):
            cloud_calls.append(eid)
            return {"plan": "enterprise"}

        with patch("common.license.get_plan", return_value="enterprise"), \
             patch("common.license._check_cloud", new=fake_cloud), \
             patch("common.license._cloud_cache", {}):
            tier = await lic.get_effective_tier(old, enterprise_id="ent-x")
            assert tier == "enterprise"
            assert cloud_calls, "enterprise 必须走云端复核而非赠礼判定"
