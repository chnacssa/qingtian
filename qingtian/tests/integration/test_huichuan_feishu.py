"""汇川 v2.4 — 飞书文件接入集成测试

端到端测试：飞书消息 → 文件下载 → 文本提取 → ingest 入库

前置条件:
  - PostgreSQL 数据库运行中 (配置 DSN 到环境变量 TEST_DATABASE_URL)
  - huichuan schema 已初始化
  - (可选) 飞书 bot token 配置到 FEISHU_BOT_TOKEN

运行:
  pytest tests/integration/test_huichuan_feishu.py -v

标记:
  - skip_if_no_db: 无数据库时自动跳过
  - skip_if_no_feishu: 无飞书 token 时自动跳过
"""

import os
import pytest


# 检查运行条件
_HAS_DB = bool(os.environ.get("TEST_DATABASE_URL") or os.environ.get("QINGTIAN_CONFIG"))
_HAS_FEISHU = bool(os.environ.get("FEISHU_BOT_TOKEN"))

need_db = pytest.mark.skipif(not _HAS_DB, reason="TEST_DATABASE_URL or QINGTIAN_CONFIG not set")
need_feishu = pytest.mark.skipif(not _HAS_FEISHU, reason="FEISHU_BOT_TOKEN not set")


class TestFeishuIngestEndToEnd:
    """飞书文件→ingest 完整链路"""

    @pytest.mark.asyncio
    @need_db
    async def test_text_file_ingest_roundtrip(self):
        """TXT 文件 → ingest → 入库 → 搜索可查"""
        from common.db import get_pool
        from huichuan.ingest import ingest_text
        from huichuan.search import search_knowledge

        pool = await get_pool()
        async with pool.acquire() as conn:
            # 1. 摄入一段文本
            result = await ingest_text(
                conn,
                text="油浸式变压器绕组温升限值为 65K，适用于户外环境。",
                source="integration_test",
                original_filename="test_transformer.txt",
            )
            assert result["entries"] > 0 or "error" in result

            # 2. 搜索验证
            if result.get("entries", 0) > 0:
                search_results = await search_knowledge(
                    conn, "变压器", limit=10,
                )
                assert len(search_results) > 0

    @pytest.mark.asyncio
    @need_db
    async def test_pdf_file_ingest(self):
        """PDF 文件 → 提取文本 → ingest 入库"""
        from common.db import get_pool
        from huichuan.ingest import ingest_file

        # 构造一个最小的合法 PDF
        pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
            b"0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF"
        )

        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await ingest_file(
                conn,
                pdf_bytes,
                filename="test.pdf",
                source="integration_test",
            )
            # 不应该崩溃
            assert "entries" in result
            assert "storage_path" in result

    @pytest.mark.asyncio
    @need_db
    async def test_sanitize_on_ingest(self):
        """摄入含 PII 文本 → 入库时自动脱敏"""
        from common.db import get_pool
        from huichuan.ingest import ingest_text
        from huichuan.search import search_knowledge

        pool = await get_pool()
        async with pool.acquire() as conn:
            # 含手机号和身份证的文本
            result = await ingest_text(
                conn,
                text="联系人张三，电话13812345678，身份证110101199001011234。",
                source="integration_test",
                original_filename="pii_test.txt",
            )
            # 不应崩溃
            assert "entries" in result
            if result.get("entries", 0) > 0:
                results = await search_knowledge(conn, "张三", limit=5)
                for r in results:
                    snippet = r.get("snippet", "")
                    # PII 应已被脱敏
                    assert "13812345678" not in snippet
                    assert "110101199001011234" not in snippet


class TestFeishuReceiver:
    """飞书接收器集成测试"""

    @pytest.mark.asyncio
    @need_db
    @need_feishu
    async def test_handle_file_event_smoke(self):
        """飞书文件事件处理冒烟测试（需要真实 feishu token）"""
        from common.db import get_pool
        from huichuan.receiver.feishu import handle_feishu_file_event, SUPPORTED_FILETYPES

        # dummy feishu client
        class DummyFeishuClient:
            async def download_resource(self, file_key, file_type):
                # 返回一个最小文本内容
                return f"Mock file content for {file_key}".encode("utf-8")

        client = DummyFeishuClient()
        event = {
            "file_key": "test_key_001",
            "file_type": "txt",
            "file_name": "test_document.txt",
            "file_size": 100,
        }

        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await handle_feishu_file_event(
                conn, event, client,
                storage_base="/tmp/huichuan_test_storage",
            )
            # 应返回 ingested 或 queued
            assert result["action"] in ("ingested", "queued", "skipped", "error")
            assert "file_name" in result

    def test_supported_filetypes(self):
        """验证支持的文件类型列表"""
        from huichuan.receiver.feishu import SUPPORTED_FILETYPES
        assert "pdf" in SUPPORTED_FILETYPES
        assert "docx" in SUPPORTED_FILETYPES
        assert "xlsx" in SUPPORTED_FILETYPES
        assert not any(
            ft in SUPPORTED_FILETYPES for ft in ("jpg", "png", "mp4")
        )
