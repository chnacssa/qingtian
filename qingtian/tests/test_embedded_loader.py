"""EmbeddedLoader 测试"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from common.skill_manifest import (
    BackgroundTask,
    LifecycleHooks,
    RouteDecl,
    SkillManifest,
    _parse_manifest,
    canonical_json,
)
from xihe.embedded_loader import EmbeddedLoader, SkillLoadError


# ── 签名辅助 ──
# R11 修复：测试使用独立测试密钥对（非生产信任锚）。
# 测试专用私钥仅存在于本测试文件，生产签名必须用安全渠道的 --key。
from common.crypto import sign as _ed_sign

TEST_PRIVATE_KEY_HEX = "4cfc5c986e10b497cdd865fe6209799b325d5bedbe3887a9a3d9b6bb067d8f34"
TEST_PUBLIC_KEY_HEX = "ccc301cd7bef648bbf31031813cc97d8dc39daf513350923410ffff94ca60a3f"


@pytest.fixture(autouse=True)
def _test_signing_pubkey(monkeypatch):
    """让 EmbeddedLoader._check_security 使用测试公钥验证（与测试签名配对）"""
    monkeypatch.setenv("QINGTIAN_SKILL_SIGN_PUBKEY", TEST_PUBLIC_KEY_HEX)


def _sign_dict(data: dict) -> tuple[str, bytes]:
    """对 skill.json 数据字典签名，返回 (signature_hex, canonical_payload)"""
    raw = dict(data)
    payload = canonical_json(raw).encode("utf-8")
    sig = _ed_sign(bytes.fromhex(TEST_PRIVATE_KEY_HEX), payload)
    return sig.hex(), payload


# ═══════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def mock_app():
    """模拟 FastAPI 应用"""
    app = MagicMock()
    app.routes = []
    return app


@pytest.fixture
def valid_manifest() -> SkillManifest:
    """已签名的 embedded Skill（验证通过）"""
    raw = {
        "name": "test-embedded",
        "display_name": "测试 Embedded Skill",
        "entry": {"class": "TestSkill", "file": "test_skill.py"},
        "runtime": {"mode": "embedded", "lifecycle": "resident"},
        "permissions": ["network", "database"],
        "routes": [
            {"path": "/v1/test/hello", "method": "GET", "handler": "say_hello"},
            {"path": "/v1/test/data", "method": "POST", "handler": "post_data"},
        ],
        "background_tasks": [
            {
                "name": "test_sweeper",
                "interval_seconds": 300,
                "handler": "sweep_test",
                "description": "测试后台任务",
            }
        ],
        "lifecycle": {
            "on_startup": "init_test",
            "on_shutdown": "stop_test",
        },
    }
    sig_hex, payload = _sign_dict(raw)
    m = _parse_manifest(raw)
    m._signature_hex = sig_hex
    m._canonical_payload = payload
    return m


@pytest.fixture
def loader(mock_app) -> EmbeddedLoader:
    """EmbeddedLoader 实例"""
    return EmbeddedLoader(mock_app)


# ═══════════════════════════════════════════════════════════
# _check_security 测试
# ═══════════════════════════════════════════════════════════


class TestCheckSecurity:
    def test_security_pass(self, valid_manifest: SkillManifest, loader: EmbeddedLoader):
        """安全校验通过"""
        loader._check_security(valid_manifest)
        # No exception = pass

    def test_security_fail_no_cert(self, loader: EmbeddedLoader):
        """没有证书签名 → 拒绝"""
        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "runtime": {"mode": "embedded"},
        })
        with pytest.raises(SkillLoadError, match="签名"):
            loader._check_security(m)

    def test_security_fail_system_permission(self, loader: EmbeddedLoader):
        """system 权限 → 拒绝"""
        raw = {
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "runtime": {"mode": "embedded"},
            "permissions": ["system"],
        }
        sig_hex, payload = _sign_dict(raw)
        m = _parse_manifest(raw)
        m._signature_hex = sig_hex
        m._canonical_payload = payload
        with pytest.raises(SkillLoadError, match="system"):
            loader._check_security(m)

    def test_subprocess_security_pass(self, loader: EmbeddedLoader):
        """subprocess 模式签名验证通过（有有效签名时）"""
        raw = {
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "runtime": {"mode": "subprocess"},
            "permissions": ["system"],  # subprocess 允许 system 权限
        }
        sig_hex, payload = _sign_dict(raw)
        m = _parse_manifest(raw)
        m._signature_hex = sig_hex
        m._canonical_payload = payload
        # subprocess 模式有签名应通过安全校验
        loader._check_security(m)


# ═══════════════════════════════════════════════════════════
# _register_routes 测试
# ═══════════════════════════════════════════════════════════


class TestRegisterRoutes:
    def test_register_success(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """路由注册成功"""
        loader._register_routes(valid_manifest)
        # app.include_router 应该被调用
        assert loader._app.include_router.called

    def test_route_conflict_detected(self, mock_app, loader: EmbeddedLoader):
        """路由冲突检测"""
        # 模拟已有路由
        mock_route = MagicMock()
        mock_route.path = "/v1/test/hello"
        mock_route.methods = {"GET"}
        mock_app.routes = [mock_route]

        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "routes": [
                {"path": "/v1/test/hello", "method": "GET", "handler": "say_hello"},
            ],
        })

        with pytest.raises(SkillLoadError, match="Route conflict"):
            loader._register_routes(m)

    def test_route_conflict_different_method_ok(self, mock_app, loader: EmbeddedLoader):
        """不同 HTTP method 不冲突"""
        mock_route = MagicMock()
        mock_route.path = "/v1/test/hello"
        mock_route.methods = {"GET"}
        mock_app.routes = [mock_route]

        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "routes": [
                {"path": "/v1/test/hello", "method": "POST", "handler": "post_hello"},
            ],
        })

        loader._register_routes(m)
        assert loader._app.include_router.called


# ═══════════════════════════════════════════════════════════
# 后台任务注册测试
# ═══════════════════════════════════════════════════════════


class TestRegisterBgTasks:
    @pytest.mark.asyncio
    async def test_register_bg_task(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """后台任务注册"""
        loader._register_bg_tasks(valid_manifest)
        task_name = "test-embedded:test_sweeper"
        assert task_name in loader._bg_tasks
        assert not loader._bg_tasks[task_name].done()

    @pytest.mark.asyncio
    async def test_dedup_bg_tasks(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """重复注册不创建新任务"""
        loader._register_bg_tasks(valid_manifest)
        loader._register_bg_tasks(valid_manifest)
        task_name = "test-embedded:test_sweeper"
        # 应该只有一个
        count = sum(1 for k in loader._bg_tasks if k == task_name)
        assert count == 1

    @pytest.mark.asyncio
    async def test_bg_task_cancelled_on_unload(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """卸载 Skill 时取消后台任务"""
        loader._loaded_skills["test-embedded"] = valid_manifest
        loader._register_bg_tasks(valid_manifest)
        task_name = "test-embedded:test_sweeper"
        assert task_name in loader._bg_tasks

        # 卸载
        await loader.unload_skill("test-embedded")
        assert task_name not in loader._bg_tasks


# ═══════════════════════════════════════════════════════════
# load_skill / unload_skill 测试
# ═══════════════════════════════════════════════════════════


class TestLoadUnloadSkill:
    @pytest.mark.asyncio
    async def test_load_skill_success(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """成功加载 embedded Skill"""
        await loader.load_skill(valid_manifest)
        assert "test-embedded" in loader._loaded_skills
        assert loader._loaded_skills["test-embedded"].name == "test-embedded"

    @pytest.mark.asyncio
    async def test_load_skill_no_cert(self, loader: EmbeddedLoader):
        """未签名 embedded Skill 加载失败"""
        m = _parse_manifest({
            "name": "test",
            "entry": {"class": "X", "file": "x.py"},
            "runtime": {"mode": "embedded"},
        })
        with pytest.raises(SkillLoadError):
            await loader.load_skill(m)
        assert "test" not in loader._loaded_skills

    @pytest.mark.asyncio
    async def test_unload_skill(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """卸载 Skill"""
        await loader.load_skill(valid_manifest)
        assert "test-embedded" in loader._loaded_skills

        await loader.unload_skill("test-embedded")
        assert "test-embedded" not in loader._loaded_skills

    @pytest.mark.asyncio
    async def test_unload_nonexistent(self, loader: EmbeddedLoader):
        """卸载不存在的 Skill 不抛异常"""
        await loader.unload_skill("nonexistent")
        # No exception = pass

    @pytest.mark.asyncio
    async def test_get_loaded_skills(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """查询已加载 Skill"""
        await loader.load_skill(valid_manifest)
        loaded = loader.get_loaded_skills()
        assert "test-embedded" in loaded

    @pytest.mark.asyncio
    async def test_shutdown(self, loader: EmbeddedLoader, valid_manifest: SkillManifest):
        """优雅关闭所有 Skill"""
        await loader.load_skill(valid_manifest)
        await loader.shutdown()
        assert loader._loaded_skills == {}
        assert loader._bg_tasks == {}


# ═══════════════════════════════════════════════════════════
# （原 TestXiheDeployment 已移除 —— R11）
# 该类加载 osskill/implementations/workflow|work_secretary|bidding|procurement|sales
# 的 skill.json，但这些 Skill 属 master 部署树，opensource 发行版不包含，
# 路径不存在导致测试恒红。对应 Skill 不随本发行版发布，测试一并移除。
# ═══════════════════════════════════════════════════════════