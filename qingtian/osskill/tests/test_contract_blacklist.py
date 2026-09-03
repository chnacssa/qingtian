"""阻断2 契约对齐 — 社区版 RevocationManager 黑板名单 key 对齐测试。

此前 sync_from_server 以 skill_id 为 key，而 is_blacklisted/add_entry/remove_entry
按 skill_name 操作 → 在线同步的条目永远匹配不上本地方法（吊销名单永远空）。
对齐后优先 skill_name 作为 key（后端 /revocations 现已同时返回 skill_id+skill_name）。
"""

import json
import os
import tempfile

# 必须在 import market_integration 前设置（_DATA_DIR 是 import 期常量）
os.environ["QINGTIAN_SKILL_DATA_DIR"] = tempfile.mkdtemp(prefix="qt_blacklist_")

import pytest  # noqa: E402

from osskill.market_integration import RevocationManager  # noqa: E402


class _FakeGateway:
    """模拟 MarketGateway.fetch_revocations 返回，后端契约：skill_id + skill_name。"""

    async def fetch_revocations(self):
        return {"revocations": [
            {"skill_id": "sid_a", "skill_name": "表格大师", "version": "1.0.0", "reason": "内部"},
            {"skill_id": "sid_b", "skill_name": "", "version": "2.0.0", "reason": "内部"},
            {"skill_id": "sid_c", "skill_name": "", "version": "", "reason": "内部"},
        ]}


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """隔离本地黑板名单文件，避免用例间串扰。"""
    from osskill import market_integration as mi
    blacklist_file = mi._REVOCATION_DIR / "blacklist.json"
    if blacklist_file.exists():
        blacklist_file.unlink()
    yield
    if blacklist_file.exists():
        blacklist_file.unlink()


@pytest.mark.asyncio
async def test_sync_from_server_keys_by_skill_name(clean_state):
    """同步后条目以 skill_name 为 key（缺名时退回 skill_id 兼容存量）"""
    mgr = RevocationManager(gateway=_FakeGateway())
    count = await mgr.sync_from_server()

    assert count == 3
    # skill_name 命中
    assert mgr.is_blacklisted("表格大师")
    assert not mgr.is_blacklisted("sid_a")  # 不再按 skill_id
    # 缺 skill_name → 退回 skill_id:version / skill_id
    assert mgr.is_blacklisted("sid_b:2.0.0")
    assert mgr.is_blacklisted("sid_c")


@pytest.mark.asyncio
async def test_sync_from_server_idempotent(clean_state):
    """重复同步不产生重复条目"""
    mgr = RevocationManager(gateway=_FakeGateway())
    assert await mgr.sync_from_server() == 3
    assert await mgr.sync_from_server() == 0


def test_local_add_remove_uses_same_skill_name_key(clean_state):
    """本地 add_entry/remove_entry 与在线同步共用 skill_name 契约"""
    mgr = RevocationManager(gateway=_FakeGateway())
    mgr.add_entry("bidding", "security_vuln", severity="high")
    assert mgr.is_blacklisted("bidding")
    assert mgr.get_blacklist_entry("bidding")["severity"] == "high"

    mgr.remove_entry("bidding")
    assert not mgr.is_blacklisted("bidding")


def test_local_blacklist_persists_loaded(clean_state):
    """在线同步 + 本地持久化可被新实例加载（跨重启吊销不丢）"""
    mgr = RevocationManager(gateway=_FakeGateway())
    mgr.add_entry("合同助手", "license_fraud", severity="critical")
    del mgr

    mgr2 = RevocationManager(gateway=_FakeGateway())
    assert mgr2.is_blacklisted("合同助手")
