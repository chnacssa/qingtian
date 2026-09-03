"""R11 opensource 深度扫描 P2 收口 — 框架层 9 项修复回归测试。

覆盖（均标注对应待修项）：
  #1  loader.S5 持久化状态从未启用 → 有意义默认路径 + 快照/重启/墙钟回退判定
  #2  cli.deps check 空图恒通过 → 读取 manifest 依赖做多节点 DAG 校验
  #3  execute_api 无脑重试 → 仅传输级瞬态错误重试、业务错误立即透出
  #4  market_integration.verify_local naive/aware 时区比较抛错 → 归一化 UTC
  #5  market_integration License 路径穿越 → skill_name 白名单 + commonpath 校验
  #6  runtime_service._get_license_id 路径穿越 → 同口径清洗
  #7  sast 别名空表漏报 → 聚合真实 alias 表传入 _resolve_alias
  #8  admin_api import_blacklist_file AttributeError → 开源补方法 + hasattr 降级 501
  #9  monitor 本地日期 + ON CONFLICT 覆盖丢数据 → UTC 日期 + 增量加和
"""

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from common.db import get_pool as _real_get_pool  # noqa: F401 (确认可导入)
from common.ipc import IPCError
from xihe.errors import ProcessError

import osskill.loader as loader_mod
import osskill.sast as sast_mod
from osskill.deps import DependencyGraph
from osskill.market_integration import (
    LicenseManager,
    RevocationManager,
    _validate_skill_name,
    _license_file_path,
    _LICENSE_DIR,
    _REVOCATION_DIR,
    _PACKAGE_DIR,
    _LICENSE_STATE_FILE,
)
from osskill.monitor import Monitor
from osskill.execute_api import _is_retryable_exec_error, api_execute_skill, ExecuteRequest


# ════════════════════════════════════════════════════════════
# #1 S5 时钟防回拨（loader.py）
# ════════════════════════════════════════════════════════════


class TestS5ClockSkew:
    """S5 持久化状态启用 + 快照回滚/重启/墙钟回退判定"""

    def _fresh(self, monkeypatch, tmp_path):
        monkeypatch.setattr(loader_mod, "_S5_STATE_FILE", str(tmp_path / "s5.json"))
        monkeypatch.setattr(loader_mod, "_S5_LAST_TIME", 0.0)
        return loader_mod

    def test_default_state_file_is_meaningful(self):
        """R11 前 _S5_STATE_FILE 为空串；现应有意义默认路径（可被 env 覆盖）。"""
        assert loader_mod._S5_STATE_FILE
        assert loader_mod._S5_STATE_FILE.endswith("s5_clock_state.json")

    def test_first_check_passes_and_persists(self, monkeypatch, tmp_path):
        L = self._fresh(monkeypatch, tmp_path)
        ok, err = L._s5_check_clock_skew("demo")
        assert ok and not err
        assert os.path.isfile(L._S5_STATE_FILE)

    def test_second_check_passes(self, monkeypatch, tmp_path):
        L = self._fresh(monkeypatch, tmp_path)
        assert L._s5_check_clock_skew("demo")[0]
        monkeypatch.setattr(L, "_S5_LAST_TIME", time.monotonic())
        ok, err = L._s5_check_clock_skew("demo")
        assert ok and not err

    def test_inprocess_monotonic_rollback_rejected(self, monkeypatch, tmp_path):
        L = self._fresh(monkeypatch, tmp_path)
        now_mono = time.monotonic()
        monkeypatch.setattr(L, "_S5_LAST_TIME", now_mono + 1000)
        ok, err = L._s5_check_clock_skew("demo")
        assert not ok
        assert "rollback" in err

    def test_snapshot_rollback_rejected(self, monkeypatch, tmp_path):
        """monotonic 与墙钟同时回退 → 快照回滚攻击，拒绝。"""
        L = self._fresh(monkeypatch, tmp_path)
        assert L._s5_check_clock_skew("demo")[0]
        now_mono, now_wall = time.monotonic(), time.time()
        monkeypatch.setattr(L, "_S5_LAST_TIME", now_mono)
        L._s5_save_state(now_mono + 1000, now_wall + 1000)
        ok, err = L._s5_check_clock_skew("demo")
        assert not ok
        assert "rollback" in err

    def test_reboot_rebaselines(self, monkeypatch, tmp_path):
        """monotonic 回退但墙钟前进 → 重启场景，重建基准放行。"""
        L = self._fresh(monkeypatch, tmp_path)
        assert L._s5_check_clock_skew("demo")[0]
        now_mono, now_wall = time.monotonic(), time.time()
        monkeypatch.setattr(L, "_S5_LAST_TIME", now_mono)
        L._s5_save_state(now_mono + 1000, now_wall - 1000)
        ok, err = L._s5_check_clock_skew("demo")
        assert ok, err

    def test_wall_clock_only_rollback_rejected(self, monkeypatch, tmp_path):
        """monotonic 未回退但墙钟回退 → 纯墙钟篡改，拒绝。"""
        L = self._fresh(monkeypatch, tmp_path)
        assert L._s5_check_clock_skew("demo")[0]
        now_mono, now_wall = time.monotonic(), time.time()
        monkeypatch.setattr(L, "_S5_LAST_TIME", now_mono)
        L._s5_save_state(now_mono, now_wall + 1000)
        ok, err = L._s5_check_clock_skew("demo")
        assert not ok
        assert "rollback" in err

    def test_fresh_start_with_rolled_back_persisted_wall(self, monkeypatch, tmp_path):
        """新进程冷启动（无内存状态）仍比对持久化墙钟 → 防快照回滚+重启绕过。"""
        L = self._fresh(monkeypatch, tmp_path)
        L._s5_save_state(0.0, time.time() + 1000)
        ok, err = L._s5_check_clock_skew("demo")
        assert not ok
        assert "rollback" in err


# ════════════════════════════════════════════════════════════
# #3 execute_api 重试策略（仅瞬态错误重试）
# ════════════════════════════════════════════════════════════


class TestExecuteRetryClassification:
    def test_retryable_transient(self):
        assert _is_retryable_exec_error(ProcessError("IPC call timed out"))
        assert _is_retryable_exec_error(ConnectionError("pipe closed"))
        assert _is_retryable_exec_error(OSError("broken pipe"))
        assert _is_retryable_exec_error(asyncio.TimeoutError())
        assert _is_retryable_exec_error(EOFError("pipe closed"))

    def test_not_retryable_business(self):
        assert not _is_retryable_exec_error(IPCError("Error calling 'execute': boom"))
        assert not _is_retryable_exec_error(ValueError("bad params"))
        assert not _is_retryable_exec_error(RuntimeError("skill failed"))


class _FakeRuntime:
    def __init__(self, handle):
        self._handle = handle
        self.launches = 0
        self.gets = 0

    async def get_handle(self, skill_name, agent_id):
        self.gets += 1
        return self._handle

    async def launch_skill(self, skill_name, agent_id="", **kw):
        self.launches += 1
        return self._handle

    async def check_skill_access(self, skill_name, agent_id):
        return True


class _FakeRequest:
    class _State:
        agent_id = "agent-1"

    headers = {}
    state = _State()


class TestExecuteRetryBehavior:
    @pytest.fixture
    def _setup(self, monkeypatch):
        monkeypatch.setattr("osskill.execute_api._EXECUTE_BACKOFF_BASE_S", 0.0)
        saved = getattr(api_execute_skill, "_runtime", None)
        yield
        api_execute_skill._runtime = saved

    @pytest.mark.asyncio
    async def test_business_error_not_retried(self, _setup):
        calls = {"n": 0}

        async def exec_business(params):
            calls["n"] += 1
            raise IPCError("Error calling 'execute': boom")

        handle = SimpleNamespace(execute=exec_business)
        api_execute_skill._runtime = _FakeRuntime(handle)

        from fastapi import HTTPException
        body = ExecuteRequest(agent_id="agent-1", params={"action": "do"})
        with pytest.raises(HTTPException) as ei:
            await api_execute_skill("demo_skill", body, _FakeRequest())
        assert ei.value.status_code == 500
        assert calls["n"] == 1  # 业务错误绝不重试

    @pytest.mark.asyncio
    async def test_transient_error_retried_then_success(self, _setup):
        calls = {"n": 0}

        async def exec_transient(params):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ProcessError("IPC call 'execute' timed out")
            return {"ok": True}

        runtime = _FakeRuntime(SimpleNamespace(execute=exec_transient))
        api_execute_skill._runtime = runtime

        body = ExecuteRequest(agent_id="agent-1", params={"action": "do"})
        result = await api_execute_skill("demo_skill", body, _FakeRequest())
        assert result == {"ok": True}
        assert calls["n"] == 3
        assert runtime.gets >= 2  # 重试前重新获取句柄

    @pytest.mark.asyncio
    async def test_transient_error_exhausts_attempts(self, _setup):
        calls = {"n": 0}

        async def exec_always_fail(params):
            calls["n"] += 1
            raise ConnectionError("pipe closed")

        api_execute_skill._runtime = _FakeRuntime(SimpleNamespace(execute=exec_always_fail))

        from fastapi import HTTPException
        body = ExecuteRequest(agent_id="agent-1", params={"action": "do"})
        with pytest.raises(HTTPException) as ei:
            await api_execute_skill("demo_skill", body, _FakeRequest())
        assert ei.value.status_code == 500
        assert calls["n"] == 3  # 有上限


# ════════════════════════════════════════════════════════════
# #4 market_integration.verify_local 时区归一化
# ════════════════════════════════════════════════════════════


class TestVerifyLocalTimezone:
    @pytest.fixture
    def _dirs(self, monkeypatch, tmp_path):
        monkeypatch.setattr("osskill.market_integration._LICENSE_DIR", tmp_path / "licenses")
        monkeypatch.setattr("osskill.market_integration._PACKAGE_DIR", tmp_path / "packages")
        monkeypatch.setattr("osskill.market_integration._REVOCATION_DIR", tmp_path / "revocations")
        monkeypatch.setattr("osskill.market_integration._LICENSE_STATE_FILE", tmp_path / "license_state.json")
        return tmp_path

    def test_naive_future_not_expired(self, _dirs):
        mgr = LicenseManager()
        mgr.save_license("naive_skill", {"expires_at": "2099-12-31T23:59:59"})
        valid, reason = mgr.verify_local("naive_skill")
        assert valid and reason == "ok"

    def test_naive_past_expired(self, _dirs):
        mgr = LicenseManager()
        mgr.save_license("naive_skill", {"expires_at": "2020-01-01T00:00:00"})
        valid, reason = mgr.verify_local("naive_skill")
        assert not valid and reason == "expired"

    def test_z_suffix_aware_expired(self, _dirs):
        mgr = LicenseManager()
        mgr.save_license("z_skill", {"expires_at": "2020-01-01T00:00:00Z"})
        valid, reason = mgr.verify_local("z_skill")
        assert not valid and reason == "expired"

    def test_invalid_expires_at_is_expired(self, _dirs):
        mgr = LicenseManager()
        mgr.save_license("bad_skill", {"expires_at": "not-a-date"})
        valid, reason = mgr.verify_local("bad_skill")
        assert not valid and reason == "expired"


# ════════════════════════════════════════════════════════════
# #5 market_integration License 路径穿越
# ════════════════════════════════════════════════════════════


class TestLicensePathTraversal:
    def test_validate_skill_name_rejects_unsafe(self):
        for bad in ["", ".", "..", "../../etc/passwd", "a/b", "a\\b", "../x", "..%2f"]:
            with pytest.raises(ValueError):
                _validate_skill_name(bad)

    def test_validate_skill_name_accepts_safe(self):
        assert _validate_skill_name("bidding") == "bidding"
        assert _validate_skill_name("my_skill_v2") == "my_skill_v2"

    def test_license_file_path_stays_in_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr("osskill.market_integration._LICENSE_DIR", tmp_path)
        p = _license_file_path("demo")
        assert str(p).startswith(str(tmp_path.resolve()))

    def test_save_license_rejects_traversal(self, monkeypatch, tmp_path):
        monkeypatch.setattr("osskill.market_integration._LICENSE_DIR", tmp_path)
        mgr = LicenseManager()
        with pytest.raises(ValueError):
            mgr.save_license("../../evil", {"expires_at": "2099-01-01T00:00:00Z"})

    def test_load_license_rejects_traversal(self, monkeypatch, tmp_path):
        monkeypatch.setattr("osskill.market_integration._LICENSE_DIR", tmp_path)
        mgr = LicenseManager()
        with pytest.raises(ValueError):
            mgr.load_license("../../etc/passwd")


# ════════════════════════════════════════════════════════════
# #6 runtime_service._get_license_id 路径穿越
# ════════════════════════════════════════════════════════════


class TestGetLicenseIdTraversal:
    @pytest.fixture
    def _config(self, monkeypatch, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr(
            "common.config.get",
            lambda key, default=None: str(data_dir) if key == "skill.data_dir" else default,
        )
        return data_dir

    @pytest.mark.asyncio
    async def test_traversal_returns_none(self, _config):
        from osskill.runtime_service import RuntimeService
        svc = RuntimeService()
        assert await svc._get_license_id("../../etc/passwd") is None

    @pytest.mark.asyncio
    async def test_valid_name_reads_license_id(self, _config):
        from osskill.runtime_service import RuntimeService
        (_config / "demo.license").write_text(
            json.dumps({"license_id": "lic_123"}), encoding="utf-8")
        svc = RuntimeService()
        assert await svc._get_license_id("demo") == "lic_123"


# ════════════════════════════════════════════════════════════
# #7 sast 别名解析（聚合真实 alias 表）
# ════════════════════════════════════════════════════════════


class TestSastAliasResolution:
    def test_scan_passes_real_aliases(self, tmp_path, monkeypatch):
        """scan_directory 必须把聚合的 alias 表传给 _resolve_alias（而非空 dict）。"""
        (tmp_path / "skill.json").write_text(
            json.dumps({"name": "alias_skill", "permissions": []}), encoding="utf-8")
        (tmp_path / "main.py").write_text(
            "import requests as req\n"
            "req.get('http://example.com')\n", encoding="utf-8")

        seen_aliases = []
        real = sast_mod._resolve_alias

        def spy(call, aliases):
            seen_aliases.append(dict(aliases))
            return real(call, aliases)

        monkeypatch.setattr(sast_mod, "_resolve_alias", spy)
        report = sast_mod.scan_directory(str(tmp_path))

        assert "network" in report.detected_permissions
        assert any(d.get("req") == "requests" for d in seen_aliases), (
            "_resolve_alias 应收到含 req→requests 的真实 alias 表，实际: %r" % seen_aliases
        )

    def test_resolve_alias_through_scan(self, tmp_path):
        """别名调用经解析后命中 network 规则（requests.get 精确匹配）。"""
        (tmp_path / "skill.json").write_text(
            json.dumps({"name": "alias_skill", "permissions": []}), encoding="utf-8")
        (tmp_path / "main.py").write_text(
            "import requests as req\n"
            "req.get('http://example.com')\n", encoding="utf-8")
        report = sast_mod.scan_directory(str(tmp_path))
        assert "network" in report.detected_permissions


# ════════════════════════════════════════════════════════════
# #2 cli.deps check 空图恒通过
# ════════════════════════════════════════════════════════════


class _CliPool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *a, **k):
        return self._rows


class TestDepsCheck:
    @pytest.fixture
    def _pkg(self, monkeypatch, tmp_path):
        skill_dir = tmp_path / "demo"
        skill_dir.mkdir()
        monkeypatch.setattr(
            "common.config.get",
            lambda key, default=None: str(tmp_path) if key == "skill.package_dir" else default,
        )
        return skill_dir

    @pytest.mark.asyncio
    async def test_no_deps_reports_explicitly(self, _pkg, monkeypatch, capsys):
        from osskill.cli import _cmd_deps_check
        (_pkg / "skill.json").write_text(
            json.dumps({"name": "demo", "dependencies": {"skills": {}}}), encoding="utf-8")
        await _cmd_deps_check("demo")
        out = capsys.readouterr().out
        assert "无依赖可校验" in out
        assert "通过" not in out  # 不再假装通过

    @pytest.mark.asyncio
    async def test_missing_dep_reported(self, _pkg, monkeypatch, capsys):
        from osskill.cli import _cmd_deps_check
        (_pkg / "skill.json").write_text(
            json.dumps({"name": "demo",
                        "dependencies": {"skills": {"missing_dep": ">=1.0.0"}}}),
            encoding="utf-8")
        monkeypatch.setattr("common.db.get_pool", AsyncMock(return_value=_CliPool([])))
        await _cmd_deps_check("demo")
        out = capsys.readouterr().out
        assert "missing_dep" in out
        assert "depends on" in out

    @pytest.mark.asyncio
    async def test_satisfied_deps_pass(self, _pkg, monkeypatch, capsys):
        from osskill.cli import _cmd_deps_check
        (_pkg / "skill.json").write_text(
            json.dumps({"name": "demo",
                        "dependencies": {"skills": {"dep1": ">=1.0.0"}}}),
            encoding="utf-8")
        rows = [{"name": "demo", "version": "1.0.0"}, {"name": "dep1", "version": "2.0.0"}]
        monkeypatch.setattr("common.db.get_pool", AsyncMock(return_value=_CliPool(rows)))
        await _cmd_deps_check("demo")
        out = capsys.readouterr().out
        assert "依赖检查通过" in out


# ════════════════════════════════════════════════════════════
# #8 admin_api import_blacklist_file（开源补方法 + hasattr 降级）
# ════════════════════════════════════════════════════════════


class TestAdminBlacklistImport:
    def test_revocation_manager_has_import_blacklist_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("osskill.market_integration._REVOCATION_DIR", tmp_path)
        mgr = RevocationManager()
        assert hasattr(mgr, "import_blacklist_file")
        # 委托 import_file：无签名的吊销文件应抛 ValueError（走同一校验路径）
        bad = tmp_path / "bad.revoke.json"
        bad.write_text('{"revocations": []}', encoding="utf-8")
        with pytest.raises(ValueError):
            mgr.import_blacklist_file(str(bad))

    @pytest.mark.asyncio
    async def test_missing_capability_returns_501(self):
        import osskill.admin_api as aa
        from osskill.admin_api import admin_import_blacklist, BlacklistImportRequest
        saved = aa._revocation_service
        try:
            aa._revocation_service = SimpleNamespace()  # 无 import_blacklist_file
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as ei:
                await admin_import_blacklist(BlacklistImportRequest(content='{"revocations": []}'))
            assert ei.value.status_code == 501
        finally:
            aa._revocation_service = saved


# ════════════════════════════════════════════════════════════
# #9 monitor 本地日期 + ON CONFLICT 覆盖丢数据
# ════════════════════════════════════════════════════════════


class _MonConn:
    def __init__(self):
        self.executions = []

    async def execute(self, sql, *params):
        self.executions.append((sql, params))
        return "OK 1"


class _MonPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _acm():
            yield self._conn
        return _acm()


class TestMonitorFlush:
    @pytest.fixture
    def _pool(self, monkeypatch):
        conn = _MonConn()
        monkeypatch.setattr("osskill.monitor.get_pool", AsyncMock(return_value=_MonPool(conn)))
        return conn

    @pytest.mark.asyncio
    async def test_utc_date_and_incremental_accumulation(self, _pool):
        from datetime import datetime, timezone
        m = Monitor()
        m.before_call("skill_a"); m.after_call("skill_a", success=True)
        m.before_call("skill_a"); m.after_call("skill_a", success=False, error="boom")

        await m.flush_to_db()
        sql, params = _pool.executions[0]
        # ON CONFLICT 改增量加和（非覆盖）
        assert "invoke_count = skill_usage_stats.invoke_count + EXCLUDED.invoke_count" in sql
        assert "success_count = skill_usage_stats.success_count + EXCLUDED.success_count" in sql
        # 首次 flush 写全量
        assert params[0] == "skill_a"
        assert params[2] == 2   # invoke
        assert params[3] == 1   # success
        # UTC 日期
        assert params[6] == datetime.now(timezone.utc).date()

        # 二次 flush：只写增量
        _pool.executions.clear()
        m.before_call("skill_a"); m.after_call("skill_a", success=True)
        await m.flush_to_db()
        _, params = _pool.executions[0]
        assert params[2] == 1   # 仅增量
        assert params[3] == 1

    @pytest.mark.asyncio
    async def test_no_new_data_skips(self, _pool):
        m = Monitor()
        m.before_call("skill_b"); m.after_call("skill_b", success=True)
        await m.flush_to_db()
        _pool.executions.clear()
        await m.flush_to_db()
        assert _pool.executions == []

    @pytest.mark.asyncio
    async def test_reset_clears_baseline(self, _pool):
        m = Monitor()
        m.before_call("skill_c"); m.after_call("skill_c", success=True)
        await m.flush_to_db()
        m.reset("skill_c")
        _pool.executions.clear()
        m.before_call("skill_c"); m.after_call("skill_c", success=True)
        await m.flush_to_db()
        _, params = _pool.executions[0]
        assert params[2] == 1  # reset 后全量写入
