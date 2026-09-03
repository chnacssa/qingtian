"""
backup.py 单元测试
配置自动备份 — backup_file, list_backups 流程
"""

import os
import tempfile
from unittest.mock import patch

import pytest

from zhenyue.backup import _cleanup_old, backup_file, list_backups


class TestBackupFile:
    def test_backup_file_creates_backup(self):
        """backup_file 创建备份文件"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 源文件在子目录中，避免与 BACKUP_BASE 路径冲突
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir, exist_ok=True)
            src_path = os.path.join(src_dir, "config.yaml")
            with open(src_path, "w") as f:
                f.write("key: value\n")

            with patch("zhenyue.backup.BACKUP_BASE", tmpdir):
                backup_path = backup_file(src_path)

                assert os.path.exists(backup_path)
                assert backup_path.endswith("_config.yaml")

                with open(backup_path, "r") as f:
                    content = f.read()
                assert content == "key: value\n"

    def test_backup_file_not_found(self):
        """源文件不存在 → FileNotFoundError"""
        with patch("zhenyue.backup.BACKUP_BASE", tempfile.mkdtemp()):
            with pytest.raises(FileNotFoundError):
                backup_file("/nonexistent/path.yaml")

    def test_backup_creates_latest_symlink(self):
        """备份后创建 latest 软链（不支持软链的平台跳过）"""
        import platform
        if platform.system().lower() == "windows":
            pytest.skip("Windows does not support unprivileged symlinks")

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir, exist_ok=True)
            src_path = os.path.join(src_dir, "config.yaml")
            with open(src_path, "w") as f:
                f.write("test")

            with patch("zhenyue.backup.BACKUP_BASE", tmpdir):
                backup_path = backup_file(src_path)
                link_path = os.path.join(tmpdir, "config.yaml", "latest")
                assert os.path.islink(link_path)
                assert os.path.realpath(link_path) == os.path.realpath(backup_path)

    def test_backup_creates_versioned(self):
        """多次备份创建不同版本"""
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            os.makedirs(src_dir, exist_ok=True)
            src_path = os.path.join(src_dir, "config.yaml")
            with open(src_path, "w") as f:
                f.write("v1")

            with patch("zhenyue.backup.BACKUP_BASE", tmpdir):
                backup_file(src_path)
                time.sleep(1.1)  # 确保时间戳不同

                # 修改内容后再次备份
                with open(src_path, "w") as f:
                    f.write("v2")
                backup_file(src_path)

                # 验证有两个版本
                backups = list_backups("config.yaml")
                assert len(backups) == 2

    def test_backup_cleanup_old(self):
        """_cleanup_old 保留最近 MAX_VERSIONS 个"""
        with tempfile.TemporaryDirectory() as tmpdir:
            name = "test.yaml"
            dest = os.path.join(tmpdir, name)
            os.makedirs(dest, exist_ok=True)

            # 创建 35 个备份（超过 MAX_VERSIONS=30）
            for i in range(35):
                ts = f"20260101_{i:06d}"
                fname = f"{ts}_{name}"
                with open(os.path.join(dest, fname), "w") as f:
                    f.write(f"backup {i}")

            with (
                patch("zhenyue.backup.BACKUP_BASE", tmpdir),
                patch("zhenyue.backup.MAX_VERSIONS", 30),
            ):
                _cleanup_old(name)

            remaining = [f for f in os.listdir(dest) if f.endswith(f"_{name}")]
            assert len(remaining) <= 30


class TestListBackups:
    def test_list_backups_returns_sorted(self):
        """list_backups 返回按时间倒序的备份列表"""
        with tempfile.TemporaryDirectory() as tmpdir:
            name = "config.yaml"
            dest = os.path.join(tmpdir, name)
            os.makedirs(dest, exist_ok=True)

            # 创建备份
            for i in range(3):
                ts = f"20260101_{i:06d}"
                fname = f"{ts}_{name}"
                with open(os.path.join(dest, fname), "w") as f:
                    f.write(f"backup {i}")

            with patch("zhenyue.backup.BACKUP_BASE", tmpdir):
                backups = list_backups(name)
                assert len(backups) == 3
                # 验证倒序
                for i in range(len(backups) - 1):
                    assert backups[i]["timestamp"] >= backups[i + 1]["timestamp"]

    def test_list_backups_no_backup_dir(self):
        """备份目录不存在 → 空列表"""
        with patch("zhenyue.backup.BACKUP_BASE", "/tmp/nonexistent_backup_dir"):
            backups = list_backups("config.yaml")
            assert backups == []

    def test_list_backups_returns_path_and_filename(self):
        """备份列表返回完整路径和文件名"""
        with tempfile.TemporaryDirectory() as tmpdir:
            name = "config.yaml"
            dest = os.path.join(tmpdir, name)
            os.makedirs(dest, exist_ok=True)

            fname = f"20260101_120000_{name}"
            with open(os.path.join(dest, fname), "w") as f:
                f.write("data")

            with patch("zhenyue.backup.BACKUP_BASE", tmpdir):
                backups = list_backups(name)
                assert len(backups) == 1
                assert backups[0]["filename"] == fname
                assert backups[0]["path"].endswith(fname)
                # timestamp 是完整的 "_{name}" 前缀部分
                assert "20260101_120000" in backups[0]["timestamp"]
