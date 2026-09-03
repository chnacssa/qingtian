"""存储配额 — 测试"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osskill.storage_quota import (
    StorageQuota,
    quota_from_skill_json,
    QUOTA_LEVELS,
    get_storage_quota,
)


class TestQuotaFromSkillJson:
    """测试 quota_from_skill_json"""

    def test_default_quota(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            b, level = quota_from_skill_json(tmpdir)
            assert b == 500 * 1024 * 1024
            assert level == "medium"

    def test_tiny_quota(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import json
            with open(os.path.join(tmpdir, "skill.json"), "w") as f:
                json.dump({"resources": {"storage_mb": 50}}, f)
            b, level = quota_from_skill_json(tmpdir)
            assert b == 50 * 1024 * 1024
            assert level == "tiny"

    def test_unlimited(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import json
            with open(os.path.join(tmpdir, "skill.json"), "w") as f:
                json.dump({"resources": {"storage_mb": 0}}, f)
            b, level = quota_from_skill_json(tmpdir)
            assert b == 0
            assert level == "unlimited"


class TestStorageQuota:
    """测试 StorageQuota"""

    def setup_method(self):
        self.q = StorageQuota()
        self.home = "/tmp/test_skill_home"
        self.q.register(self.home, quota_bytes=1024 * 1024, level="small")

    def test_try_write_sync_tracks_usage(self):
        allowed, msg = self.q.try_write_sync(self.home, 100 * 1024)
        assert allowed
        assert self.q._states[self.home].usage_bytes == 100 * 1024

    def test_try_write_sync_over_limit(self):
        allowed, msg = self.q.try_write_sync(self.home, 2 * 1024 * 1024)
        assert not allowed

    def test_release_sync_rolls_back(self):
        self.q.try_write_sync(self.home, 100 * 1024)
        assert self.q._states[self.home].usage_bytes == 100 * 1024
        self.q.release_sync(self.home, 50 * 1024)
        assert self.q._states[self.home].usage_bytes == 50 * 1024

    def test_try_write_sync_unlimited(self):
        home = "/tmp/unlimited_write"
        self.q.register(home, quota_bytes=0, level="unlimited")
        allowed, msg = self.q.try_write_sync(home, 999 * 1024 * 1024)
        assert allowed

    def test_try_write_sync_unregistered(self):
        allowed, msg = self.q.try_write_sync("/nonexistent", 999 * 1024)
        assert allowed

    def test_expand_invalid_ratio(self):
        ok, msg = self.q.expand_storage(self.home, ratio=5.0)
        assert not ok

    def test_expand_twice_fails(self):
        self.q.expand_storage(self.home, ratio=2.0, duration_hours=1)
        ok, msg = self.q.expand_storage(self.home, ratio=2.0, duration_hours=1)
        assert not ok

    def test_unregister_cleans_up(self):
        self.q.register("/tmp/cleanup", quota_bytes=100)
        assert "/tmp/cleanup" in self.q._states
        self.q.unregister("/tmp/cleanup")
        assert "/tmp/cleanup" not in self.q._states

    def test_calibrate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.q.register(tmpdir, quota_bytes=10 * 1024 * 1024)
            with open(os.path.join(tmpdir, "test.dat"), "wb") as f:
                f.write(b"x" * 1024)
            actual = self.q._du(tmpdir)
            assert actual >= 1024

    def test_singleton(self):
        q1 = get_storage_quota()
        q2 = get_storage_quota()
        assert q1 is q2

    def test_unlimited_always_passes(self):
        home = "/tmp/unlimited"
        self.q.register(home, quota_bytes=0, level="unlimited")
        st = self.q._states[home]
        effective = 0 if st.quota_bytes == 0 else st.quota_bytes
        assert effective == 0  # 0 = 不限

    def test_unregistered_returns_none(self):
        st = self.q.get_state("/nonexistent")
        assert st is None

    def test_get_state(self):
        st = self.q.get_state(self.home)
        assert st is not None
        assert st.quota_bytes == 1024 * 1024
        assert st.level == "small"


class TestStorageQuotaAtomic:
    """测试原子配额方法"""
    def test_check_under_limit(self):
        q = StorageQuota()
        q.register("/test", quota_bytes=1024 * 1024)
        allowed, msg = q.check_write("/test", 500 * 1024)
        assert allowed

    def test_check_over_limit(self):
        q = StorageQuota()
        q.register("/test", quota_bytes=1024 * 1024)
        allowed, msg = q.check_write("/test", 2 * 1024 * 1024)
        assert not allowed

    def test_check_after_usage_released(self):
        q = StorageQuota()
        q.register("/test", quota_bytes=1024 * 1024)
        q.try_write_sync("/test", 900 * 1024)
        q.release_sync("/test", 900 * 1024)
        allowed, msg = q.check_write("/test", 950 * 1024)
        assert allowed

    def test_expand_storage_quota(self):
        q = StorageQuota()
        q.register("/test", quota_bytes=1024 * 1024)
        q.expand_storage("/test", ratio=2.0, duration_hours=1)
        allowed, msg = q.check_write("/test", 1500 * 1024)
        assert allowed

    def test_unlimited_always_passes(self):
        q = StorageQuota()
        q.register("/ulimit", quota_bytes=0, level="unlimited")
        allowed, msg = q.check_write("/ulimit", 999 * 1024 * 1024)
        assert allowed

    def test_unregistered_always_passes(self):
        q = StorageQuota()
        allowed, msg = q.check_write("/nonexistent", 999 * 1024)
        assert allowed


class TestQuotaLevels:
    def test_all_levels_defined(self):
        assert "tiny" in QUOTA_LEVELS
        assert "small" in QUOTA_LEVELS
        assert "medium" in QUOTA_LEVELS
        assert "large" in QUOTA_LEVELS
        assert "unlimited" in QUOTA_LEVELS

    def test_levels_monotonic(self):
        values = [QUOTA_LEVELS[k] for k in ["tiny", "small", "medium", "large", "unlimited"]]
        assert values[0] < values[1] < values[2] < values[3]
