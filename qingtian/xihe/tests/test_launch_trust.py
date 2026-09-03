"""羲和 — launch_skill 信任级别接线测试（P1-1）

验证：launch_skill 未显式指定 trust_level 时，启动即从验证链
（本地吊销名单 + verify_skill）读取结果；已吊销的 Skill 即使被
auto_bind 重新拉起也会被 PermissionError 拦截。
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from xihe.agent_runtime import XiheRuntime, ChildProcess


def _make_runtime() -> XiheRuntime:
    runtime = XiheRuntime()
    return runtime


class TestResolveTrustLevel:
    """_resolve_trust_level 纯逻辑测试（不 spawn 子进程）"""

    @pytest.mark.asyncio
    async def test_blacklisted_returns_revoked(self):
        """本地吊销黑名单命中 → revoked（不依赖证书/网络）"""
        runtime = _make_runtime()
        with patch("osskill.market_integration.RevocationManager") as M:
            M.return_value.is_blacklisted.return_value = True
            assert await runtime._resolve_trust_level("evil_skill") == "revoked"

    @pytest.mark.asyncio
    async def test_no_manifest_returns_untrusted(self):
        """A5 (R11): 无 package_dir / skill.json（不可验）→ fail-closed untrusted"""
        runtime = _make_runtime()
        with patch("osskill.market_integration.RevocationManager") as M, \
             patch("common.config.get", return_value=""):
            M.return_value.is_blacklisted.return_value = False
            assert await runtime._resolve_trust_level("bidding") == "untrusted"

    @pytest.mark.asyncio
    async def test_verify_skill_revoked_wins(self):
        """verify_skill 返回 revoked → revoked"""
        runtime = _make_runtime()
        manifest = MagicMock()
        with patch("osskill.market_integration.RevocationManager") as M, \
             patch("common.config.get", return_value="/opt/pkg"), \
             patch("osskill.loader.ManifestLoader.from_package_dir", return_value=manifest), \
             patch("osskill.loader.verify_skill", new=AsyncMock(return_value=("revoked", "revoked: reason"))):
            M.return_value.is_blacklisted.return_value = False
            assert await runtime._resolve_trust_level("bad_skill") == "revoked"

    @pytest.mark.asyncio
    async def test_verify_skill_untrusted_wins(self):
        """verify_skill 返回 untrusted → untrusted（裸奔监管）"""
        runtime = _make_runtime()
        manifest = MagicMock()
        with patch("osskill.market_integration.RevocationManager") as M, \
             patch("common.config.get", return_value="/opt/pkg"), \
             patch("osskill.loader.ManifestLoader.from_package_dir", return_value=manifest), \
             patch("osskill.loader.verify_skill", new=AsyncMock(return_value=("untrusted", "S1 failed: x"))):
            M.return_value.is_blacklisted.return_value = False
            assert await runtime._resolve_trust_level("odd_skill") == "untrusted"

    @pytest.mark.asyncio
    async def test_verify_exception_falls_back_untrusted(self):
        """A5 (R11): 验证链路异常 → fail-closed 默认 untrusted"""
        runtime = _make_runtime()
        with patch("osskill.market_integration.RevocationManager") as M, \
             patch("common.config.get", return_value="/opt/pkg"), \
             patch("osskill.loader.ManifestLoader.from_package_dir", side_effect=Exception("boom")):
            M.return_value.is_blacklisted.return_value = False
            assert await runtime._resolve_trust_level("odd_skill") == "untrusted"


class TestLaunchSkillTrust:
    """launch_skill 信任级别接线（mock _spawn，不真起子进程）"""

    @pytest.mark.asyncio
    async def test_revoked_raises_permission_error(self):
        """验证结果为 revoked → 直接 PermissionError，不再拉起"""
        runtime = _make_runtime()
        with patch.object(runtime, "_resolve_trust_level", new=AsyncMock(return_value="revoked")), \
             patch.object(runtime, "_spawn", new=AsyncMock()) as spawn:
            with pytest.raises(PermissionError, match="吊销"):
                await runtime.launch_skill("bad_skill", agent_id="agent_01")
            spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_trusted_vetted_by_revocation_list(self):
        """P1 (2026-08-27 #8): 显式 trust_level="trusted" 仍须过吊销名单——
        验证结果 revoked 时否决显式声明，禁止被吊销 Skill 经参数拉起"""
        runtime = _make_runtime()
        with patch.object(runtime, "_resolve_trust_level", new=AsyncMock(return_value="revoked")) as resolve, \
             patch.object(runtime, "_spawn", new=AsyncMock()) as spawn:
            with pytest.raises(PermissionError, match="吊销"):
                await runtime.launch_skill(
                    "bidding", agent_id="a1",
                    trust_level="trusted",
                    lifecycle="on_demand",
                )
            # 显式指定也必须执行吊销检查
            resolve.assert_awaited()
            spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_trusted_ok_when_not_revoked(self):
        """显式 trusted 且吊销名单无异常 → 正常拉起（不因检查降级）"""
        runtime = _make_runtime()
        with patch.object(runtime, "_resolve_trust_level", new=AsyncMock(return_value="trusted")), \
             patch.object(runtime, "_spawn", new=AsyncMock()):
            handle = await runtime.launch_skill(
                "bidding", agent_id="a1",
                trust_level="trusted",
                lifecycle="on_demand",
            )
            assert handle is not None

    @pytest.mark.asyncio
    async def test_default_resolves_trust_and_passes_to_child(self):
        """默认（None）走验证链，且 trust_level 透传到 ChildProcess"""
        runtime = _make_runtime()
        spawned = []

        async def fake_spawn(child: ChildProcess, config: dict, version: str):
            spawned.append(child)

        with patch.object(runtime, "_resolve_trust_level", new=AsyncMock(return_value="untrusted")), \
             patch.object(runtime, "_spawn", new=fake_spawn):
            # lifecycle 用 on_demand 避免常驻 slot 干扰
            handle = await runtime.launch_skill(
                "bidding", agent_id="a1",
                lifecycle="on_demand",
            )
            assert spawned, "应已触发 spawn"
            assert spawned[0].trust_level == "untrusted"
            assert handle is not None
