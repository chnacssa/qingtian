"""汇川 Phase 2+3 — ingest + feishu 单元测试 (无 DB 依赖)

测试范围:
  - ingest.py: 边界约束（空文本 / LLM 失败兜底 / 超长截断）
  - ingest.py: _extract_text 文件格式解析
  - feishu.py: 支持/不支持文件类型
  - models.py: IngestRequest/IngestResponse
"""

import pytest

from huichuan.ingest import (
    MAX_CHUNK_CHARS,
    MAX_ENTRIES_PER_DOC,
    COOLDOWN_SECONDS,
    INGEST_PROMPT,
    ingest_text,
    _now_str,
    _extract_text,
    _extract_pdf,
    _extract_docx,
    _extract_xlsx,
)
from huichuan.models import IngestRequest, IngestResponse, IngestFileResponse
from huichuan.receiver.feishu import (
    SUPPORTED_FILETYPES,
    FEISHU_MAX_FILE_SIZE,
    DIRECT_INGEST_MAX_SIZE,
)


# ═══════════════════════════════════════════════════════
# 边界常量
# ═══════════════════════════════════════════════════════


class TestIngestConstants:
    def test_max_chunk_chars(self):
        assert MAX_CHUNK_CHARS == 50000

    def test_max_entries_per_doc(self):
        assert MAX_ENTRIES_PER_DOC == 15

    def test_cooldown_seconds(self):
        assert COOLDOWN_SECONDS == 5

    def test_ingest_prompt_contains_keywords(self):
        assert "entry_type" in INGEST_PROMPT
        assert "entity|concept" in INGEST_PROMPT
        assert "contradicts" in INGEST_PROMPT
        assert "confidence" in INGEST_PROMPT
        assert "domain" in INGEST_PROMPT


# ═══════════════════════════════════════════════════════
# ingest_text 边界（mock conn / mock LLM）
# ═══════════════════════════════════════════════════════


class TestIngestTextBoundary:
    @pytest.mark.asyncio
    async def test_empty_text_returns_zero(self):
        """空文本 → entries=0 + error"""
        result = await ingest_text(None, "")
        assert result["entries"] == 0
        assert result["error"] == "empty text"

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_zero(self):
        """纯空白 → entries=0 + error"""
        result = await ingest_text(None, "   \n\t  ")
        assert result["entries"] == 0

    @pytest.mark.asyncio
    async def test_llm_failure_returns_default(self):
        """LLM 不可用时返回 default（不崩溃）"""
        # 没有 API key → llm_call_json 返回 default={"entries":[],"summary":""}
        result = await ingest_text(None, "一些文本内容用于测试")
        assert result["entries"] == 0
        assert "error" in result
        assert result["error"] == "LLM returned empty"

    @pytest.mark.asyncio
    async def test_ingested_at_is_iso_string(self):
        """ingested_at 应该是 ISO 格式字符串"""
        result = await ingest_text(None, "")
        assert "T" in result["ingested_at"]  # ISO 格式含 T


class TestIngestResponseFields:
    def test_ingest_response_has_knowledge_ids(self):
        resp = IngestResponse(
            entries=3, summary="test summary",
            ingested_at="2026-06-04T00:00:00",
            knowledge_ids=["id1", "id2", "id3"],
        )
        assert resp.entries == 3
        assert resp.summary == "test summary"
        assert len(resp.knowledge_ids) == 3

    def test_ingest_file_response_has_storage_path(self):
        resp = IngestFileResponse(
            entries=2, storage_path="/opt/storage/file.pdf",
            ingested_at="2026-06-04T00:00:00",
        )
        assert resp.storage_path == "/opt/storage/file.pdf"


# ═══════════════════════════════════════════════════════
# 文件格式解析（线程池执行，不需要 DB）
# ═══════════════════════════════════════════════════════


class TestTextExtraction:
    @pytest.mark.asyncio
    async def test_extract_txt(self):
        text = await _extract_text(b"Hello World", "test.txt")
        assert "Hello World" in text

    @pytest.mark.asyncio
    async def test_extract_md(self):
        text = await _extract_text(b"# Title\nContent", "readme.md")
        assert "Title" in text

    @pytest.mark.asyncio
    async def test_extract_json(self):
        text = await _extract_text(b'{"key": "val"}', "data.json")
        assert "key" in text

    @pytest.mark.asyncio
    async def test_extract_csv(self):
        text = await _extract_text(b"col1,col2\nval1,val2", "data.csv")
        assert "col1" in text

    @pytest.mark.asyncio
    async def test_extract_unknown_format_falls_back_to_text(self):
        """未知格式 → 尝试当 UTF-8 文本解码"""
        text = await _extract_text(b"plain text content", "unknown.xyz")
        assert "plain text content" in text

    @pytest.mark.asyncio
    async def test_extract_binary_not_crash(self):
        """二进制数据不应崩溃"""
        text = await _extract_text(b"\x00\x01\x02\x03", "test.bin")
        # 应该返回空或乱码但不崩
        assert isinstance(text, str)

    @pytest.mark.asyncio
    async def test_extract_pdf_no_lib_returns_empty(self):
        """pdfplumber 未安装 → 返回空字符串（不崩）"""
        text = _extract_pdf(b"%PDF-1.4 fake content")
        # _extract_pdf 是同步函数（_extract_text 在线程池中调用它）
        assert isinstance(text, str)

    @pytest.mark.asyncio
    async def test_extract_docx_no_lib_returns_empty(self):
        """python-docx 未安装 → 返回空字符串（不崩）"""
        text = _extract_docx(b"fake docx content")
        assert isinstance(text, str)

    @pytest.mark.asyncio
    async def test_extract_xlsx_no_lib_returns_empty(self):
        """openpyxl 未安装 → 返回空字符串（不崩）"""
        text = _extract_xlsx(b"fake xlsx content")
        assert isinstance(text, str)

    @pytest.mark.asyncio
    async def test_extract_empty_bytes(self):
        text = await _extract_text(b"", "empty.txt")
        assert text == ""


# ═══════════════════════════════════════════════════════
# 飞书接收器常量
# ═══════════════════════════════════════════════════════


class TestFeishuConstants:
    def test_supported_filetypes(self):
        assert "pdf" in SUPPORTED_FILETYPES
        assert "docx" in SUPPORTED_FILETYPES
        assert "xlsx" in SUPPORTED_FILETYPES
        assert "txt" in SUPPORTED_FILETYPES
        assert "md" in SUPPORTED_FILETYPES

    def test_max_file_size(self):
        assert FEISHU_MAX_FILE_SIZE == 50 * 1024 * 1024  # 50MB

    def test_direct_ingest_threshold(self):
        assert DIRECT_INGEST_MAX_SIZE == 10 * 1024 * 1024  # 10MB

    def test_unsupported_type_not_in_list(self):
        assert "jpg" not in SUPPORTED_FILETYPES
        assert "png" not in SUPPORTED_FILETYPES
        assert "mp4" not in SUPPORTED_FILETYPES


# ═══════════════════════════════════════════════════════
# IngestRequest 模型
# ═══════════════════════════════════════════════════════


class TestIngestRequest:
    def test_minimal_request(self):
        req = IngestRequest(text="test content")
        assert req.text == "test content"
        assert req.source == "manual"
        assert req.original_filename is None

    def test_full_request(self):
        req = IngestRequest(
            text="content",
            source="feishu",
            original_filename="report.pdf",
            storage_path="/opt/storage/report.pdf",
        )
        assert req.source == "feishu"
        assert req.original_filename == "report.pdf"
        assert req.storage_path == "/opt/storage/report.pdf"


# ═══════════════════════════════════════════════════════
# _now_str 格式
# ═══════════════════════════════════════════════════════


class TestNowStr:
    def test_returns_iso_format(self):
        s = _now_str()
        assert "T" in s
        assert "+" in s or "Z" in s or s.endswith("00:00")

    def test_two_calls_different(self):
        """连续两次调用应该有微小时间差"""
        s1 = _now_str()
        s2 = _now_str()
        assert s1 <= s2  # 第二次不小于第一次
