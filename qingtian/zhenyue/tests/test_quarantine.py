"""
quarantine.py 单元测试
删除隔离区 — quarantine_file, restore_file 流程
"""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from zhenyue.quarantine import list_quarantine, purge_expired, quarantine_file, restore_file


class TestQuarantineFile:
    @pytest.mark.asyncio
    async def test_quarantine_file_not_found(self):
        """文件不存在 → error"""
        result = await quarantine_file("agent-1", "test", "/nonexistent/path.txt")
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_path_traversal_agent_id(self):
        """P1 (R11): agent_id 含路径穿越 → 拒绝（不写入隔离区外路径）"""
        result = await quarantine_file("../escape", "test", "/nonexistent/path.txt")
        assert result["status"] == "error"
        assert "Invalid agent_id" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_empty_agent_id(self):
        """agent_id 为空 → 拒绝"""
        result = await quarantine_file("", "test", "/nonexistent/path.txt")
        assert result["status"] == "error"
        assert "Invalid agent_id" in result["error"]

    @pytest.mark.asyncio
    async def test_db_failure_keeps_file_in_place(self, mock_conn, mock_pool):
        """P1 (R11): DB 写入失败 → 文件不被移动（原实现先 move 后 INSERT → 文件丢失）"""
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"data")
            original_path = f.name

        mock_conn.execute.side_effect = Exception("DB down")
        try:
            with (
                patch("zhenyue.quarantine.get_pool", return_value=mock_pool),
                patch("zhenyue.quarantine.QUARANTINE_BASE", tempfile.mkdtemp()),
            ):
                result = await quarantine_file("agent-1", "test", original_path)
            assert result["status"] == "error"
            # 文件必须仍在原位置
            assert os.path.exists(original_path) is True
        finally:
            if os.path.exists(original_path):
                os.remove(original_path)

    @pytest.mark.asyncio
    async def test_quarantine_file_success(self, mock_conn, mock_pool):
        """成功隔离文件"""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"test data")
            original_path = f.name

        try:
            with (
                patch("zhenyue.quarantine.get_pool", return_value=mock_pool),
                patch("zhenyue.quarantine.QUARANTINE_BASE", tempfile.mkdtemp()),
            ):
                result = await quarantine_file(
                    agent_id="agent-1",
                    source="test",
                    original_path=original_path,
                    metadata={"reason": "test"},
                )
                assert result["status"] == "ok"
                assert result["quarantine_id"] is not None
                assert result["quarantine_path"] is not None
                # 文件已被移动
                assert os.path.exists(original_path) is False
                assert os.path.exists(result["quarantine_path"]) is True
                # 验证 DB 写入了记录
                mock_conn.execute.assert_awaited_once()
        finally:
            if os.path.exists(original_path):
                os.remove(original_path)

    @pytest.mark.asyncio
    async def test_restore_file_success(self, mock_conn, mock_pool):
        """成功恢复隔离文件"""
        quarantine_dir = tempfile.mkdtemp()
        original_dir = tempfile.mkdtemp()
        original_path = os.path.join(original_dir, "test.txt")

        with open(original_path, "w") as f:
            f.write("test data")

        quarantine_path = os.path.join(quarantine_dir, "quarantined_test.txt")
        os.rename(original_path, quarantine_path)

        quarantine_id = "test-quarantine-uuid"

        # mock DB 查询返回
        mock_conn.fetchrow.return_value = {
            "quarantine_id": quarantine_id,
            "agent_id": "agent-1",
            "source": "test",
            "original_path": original_path,
            "quarantine_path": quarantine_path,
            "original_size": 9,
            "status": "quarantined",
            "expires_at": None,
            "restored_at": None,
            "metadata": {},
            "created_at": None,
        }

        with patch("zhenyue.quarantine.get_pool", return_value=mock_pool):
            result = await restore_file(quarantine_id)
            assert result["status"] == "ok"
            assert result["original_path"] == original_path
            # 文件已恢复
            assert os.path.exists(original_path) is True

        # 清理
        if os.path.exists(original_path):
            os.remove(original_path)
        os.rmdir(original_dir)
        os.rmdir(quarantine_dir)

    @pytest.mark.asyncio
    async def test_restore_not_found(self, mock_conn, mock_pool):
        """隔离记录不存在 → error"""
        mock_conn.fetchrow.return_value = None

        with patch("zhenyue.quarantine.get_pool", return_value=mock_pool):
            result = await restore_file("nonexistent-id")
            assert result["status"] == "error"
            assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_restore_already_restored(self, mock_conn, mock_pool):
        """已恢复的记录 → error"""
        mock_conn.fetchrow.return_value = {
            "quarantine_id": "id",
            "status": "restored",
            "quarantine_path": "/tmp/somefile",
            "original_path": "/tmp/orig",
        }

        with patch("zhenyue.quarantine.get_pool", return_value=mock_pool):
            result = await restore_file("id")
            assert result["status"] == "error"
            assert "not 'quarantined'" in result["error"]

    @pytest.mark.asyncio
    async def test_list_quarantine(self, mock_conn, mock_pool):
        """列出隔离文件"""
        mock_conn.fetch.return_value = [
            {"quarantine_id": "q1", "agent_id": "agent-1", "status": "quarantined"},
            {"quarantine_id": "q2", "agent_id": "agent-1", "status": "quarantined"},
        ]

        with patch("zhenyue.quarantine.get_pool", return_value=mock_pool):
            items = await list_quarantine(agent_id="agent-1")
            assert len(items) == 2

    @pytest.mark.asyncio
    async def test_purge_expired(self, mock_conn, mock_pool):
        """清理过期隔离文件"""
        # purge_expired 的 DB 查询由 mock 控制返回空列表
        mock_conn.fetch.return_value = []

        with patch("zhenyue.quarantine.get_pool", return_value=mock_pool):
            count = await purge_expired(max_age_days=0)
            assert count == 0
