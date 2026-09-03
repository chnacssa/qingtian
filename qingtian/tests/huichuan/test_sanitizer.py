"""汇川 Phase 1 — 脱敏 + 类型系统单元测试 (无 DB 依赖)

测试范围:
  - sanitizer.py: PII 脱敏各级别
  - models.py: KnowledgeCreate/KnowledgeResponse 新字段
  - database.py: DDL 新增列
"""

import pytest

from huichuan.sanitizer import sanitize, PII_PATTERNS
from huichuan.models import KnowledgeCreate, KnowledgeResponse, KnowledgeUpdate
from huichuan.database import TABLES_SQL


# ═══════════════════════════════════════════════════════
# PII 脱敏
# ═══════════════════════════════════════════════════════


class TestSanitizePII:
    def test_phone_masked(self):
        result = sanitize("联系电话 13812345678 请联络。", level="erp_to_ingest")
        assert "13812345678" not in result
        assert "***" in result

    def test_id_card_masked(self):
        result = sanitize("身份证 110101199001011234 已登记。", level="erp_to_ingest")
        assert "110101199001011234" not in result
        assert "***" in result

    def test_bank_card_masked(self):
        result = sanitize("卡号 6222021234567890123 已绑定。", level="erp_to_ingest")
        assert "6222021234567890123" not in result
        assert "***" in result

    def test_email_masked(self):
        result = sanitize("邮箱 user@company.com 请确认。", level="erp_to_ingest")
        assert "user@company.com" not in result
        assert "***" in result

    def test_multiple_pii_in_one_text(self):
        result = sanitize(
            "联系人: 张三, 电话: 13912345678, 邮箱: zhang@test.com",
            level="erp_to_ingest"
        )
        assert "13912345678" not in result
        assert "zhang@test.com" not in result

    def test_no_pii_passes_through(self):
        text = "油浸式变压器绕组温升限值为 65K，适用于户外环境。"
        result = sanitize(text, level="erp_to_ingest")
        assert result == text

    def test_empty_text_returns_empty(self):
        assert sanitize("") == ""
        assert sanitize(None) is None


class TestSanitizeCJKBoundary:
    """CJK 无空格场景 — b -> (?<!d) 修复验证"""

    def test_phone_adjacent_to_cjk(self):
        """联系人张工电话13800138000 — 无空格也应脱敏"""
        result = sanitize("联系人张工电话13800138000请记录", level="erp_to_ingest")
        assert "13800138000" not in result
        assert "***" in result

    def test_id_card_adjacent_to_cjk(self):
        """身份证110101199001011234已登记 — 无空格也应脱敏"""
        result = sanitize("身份证110101199001011234已登记", level="erp_to_ingest")
        assert "110101199001011234" not in result
        assert "***" in result

    def test_bank_card_adjacent_to_cjk(self):
        """卡号6222021234567890123已绑定 — 无空格也应脱敏"""
        result = sanitize("卡号6222021234567890123已绑定", level="erp_to_ingest")
        assert "6222021234567890123" not in result
        assert "***" in result

    def test_email_adjacent_to_cjk(self):
        """邮箱zhangsan@test.com请确认 — 无空格也应脱敏"""
        result = sanitize("邮箱zhangsan@test.com请确认", level="erp_to_ingest")
        assert "zhangsan@test.com" not in result
        assert "***" in result

    def test_multiple_pii_no_spaces(self):
        """全部PII紧凑排列 — 全应脱敏"""
        result = sanitize(
            "联系人张工手机13800138000身份证110101199001011234卡号6222021234567890123邮箱zhang@test.com",
            level="erp_to_ingest",
        )
        assert "13800138000" not in result
        assert "110101199001011234" not in result
        assert "6222021234567890123" not in result
        assert "zhang@test.com" not in result


class TestSanitizeLevels:
    def test_erp_to_ingest_strips_pii_only(self):
        """erp_to_ingest: 脱敏 PII，但保留 #内部 行"""
        text = "#内部 此报价含回扣\n产品规格: 变压器 100kVA"
        result = sanitize(text, level="erp_to_ingest")
        assert "#内部" in result  # 保留
        assert "变压器" in result   # 保留

    def test_private_to_shared_strips_internal_notes(self):
        """private_to_shared: 脱敏 PII + 去掉 #内部 行"""
        text = "#内部 此报价含回扣\n产品规格: 变压器 100kVA"
        result = sanitize(text, level="private_to_shared")
        assert "#内部" not in result   # 去掉
        assert "变压器" in result      # 保留

    def test_private_to_private_strips_pii_only(self):
        """private_to_private: 只脱敏 PII"""
        text = "#内部备注\n联系人电话 13800001111"
        result = sanitize(text, level="private_to_private")
        assert "#内部备注" in result  # 保留
        assert "13800001111" not in result  # 脱敏


class TestPIIPatterns:
    def test_all_patterns_registered(self):
        assert "phone" in PII_PATTERNS
        assert "id_card" in PII_PATTERNS
        assert "bank" in PII_PATTERNS
        assert "email" in PII_PATTERNS

    def test_phone_pattern_matches_real_numbers(self):
        import re
        pattern = PII_PATTERNS["phone"][0]
        assert re.search(pattern, "13812345678")
        assert re.search(pattern, "15900001111")
        assert not re.search(pattern, "12345678901")  # 不以 1[3-9] 开头

    def test_id_card_pattern_matches_18_digits(self):
        import re
        pattern = PII_PATTERNS["id_card"][0]
        assert re.search(pattern, "110101199001011234")
        assert re.search(pattern, "11010119900101123X")
        assert not re.search(pattern, "12345")  # 太短

    def test_bank_pattern_matches_16_19_digits(self):
        import re
        pattern = PII_PATTERNS["bank"][0]
        assert re.search(pattern, "6222021234567890")       # 16 位
        assert re.search(pattern, "6222021234567890123")    # 19 位
        assert not re.search(pattern, "12345")               # 太短


# ═══════════════════════════════════════════════════════
# 模型新字段
# ═══════════════════════════════════════════════════════


class TestKnowledgeCreateNewFields:
    def test_entry_type_default(self):
        obj = KnowledgeCreate(title="T", domain="d", content="c")
        assert obj.entry_type == "entity"

    def test_entry_type_custom(self):
        obj = KnowledgeCreate(title="T", domain="d", content="c", entry_type="concept")
        assert obj.entry_type == "concept"

    def test_original_filename_default_none(self):
        obj = KnowledgeCreate(title="T", domain="d", content="c")
        assert obj.original_filename is None

    def test_original_filename_set(self):
        obj = KnowledgeCreate(
            title="T", domain="d", content="c",
            original_filename="变压器标准.pdf",
        )
        assert obj.original_filename == "变压器标准.pdf"

    def test_original_storage_path(self):
        obj = KnowledgeCreate(
            title="T", domain="d", content="c",
            original_storage_path="/opt/qingtian/huichuan/storage/2026/06/uuid.pdf",
        )
        assert obj.original_storage_path == "/opt/qingtian/huichuan/storage/2026/06/uuid.pdf"

    def test_original_file_sha256(self):
        obj = KnowledgeCreate(
            title="T", domain="d", content="c",
            original_file_sha256="a1b2c3d4e5f6",
        )
        assert obj.original_file_sha256 == "a1b2c3d4e5f6"


class TestKnowledgeUpdateNewFields:
    def test_entry_type_optional(self):
        obj = KnowledgeUpdate(version=1)
        assert obj.entry_type is None

    def test_entry_type_set(self):
        obj = KnowledgeUpdate(version=1, entry_type="comparison")
        assert obj.entry_type == "comparison"

    def test_original_filename_update(self):
        obj = KnowledgeUpdate(version=1, original_filename="new_name.pdf")
        assert obj.original_filename == "new_name.pdf"


class TestKnowledgeResponseNewFields:
    def test_entry_type_in_response(self):
        d = {
            "knowledge_id": "kb-001", "title": "T", "domain": "d",
            "tags": [], "visibility": "public", "owner_agent": None,
            "authorized_agents": [], "content": "c", "source": "manual",
            "version": 1, "valid_from": None, "valid_until": None,
            "metadata": {}, "entry_type": "concept",
            "quality": 3, "status": "active",
            "refined_at": None,
            "created_at": "2026-05-25T00:00:00Z",
            "updated_at": "2026-05-25T00:00:00Z",
        }
        resp = KnowledgeResponse(**d)
        assert resp.entry_type == "concept"

    def test_entry_type_defaults_to_entity(self):
        d = {
            "knowledge_id": "kb-001", "title": "T", "domain": "d",
            "tags": [], "visibility": "public", "owner_agent": None,
            "authorized_agents": [], "content": "c", "source": "manual",
            "version": 1, "valid_from": None, "valid_until": None,
            "metadata": {}, "quality": 3, "status": "active",
            "refined_at": None,
            "created_at": "2026-05-25T00:00:00Z",
            "updated_at": "2026-05-25T00:00:00Z",
        }
        resp = KnowledgeResponse(**d)
        assert resp.entry_type == "entity"

    def test_original_filename_in_response(self):
        d = {
            "knowledge_id": "kb-001", "title": "T", "domain": "d",
            "tags": [], "visibility": "public", "owner_agent": None,
            "authorized_agents": [], "content": "c", "source": "manual",
            "version": 1, "valid_from": None, "valid_until": None,
            "metadata": {}, "original_filename": "file.pdf",
            "quality": 3, "status": "active",
            "refined_at": None,
            "created_at": "2026-05-25T00:00:00Z",
            "updated_at": "2026-05-25T00:00:00Z",
        }
        resp = KnowledgeResponse(**d)
        assert resp.original_filename == "file.pdf"


# ═══════════════════════════════════════════════════════
# DDL 新列
# ═══════════════════════════════════════════════════════


class TestDDLNewColumns:
    def test_entry_type_in_ddl(self):
        assert "entry_type" in TABLES_SQL
        assert "CHECK (entry_type IN ('entity','concept','comparison','query','source'))" in TABLES_SQL

    def test_original_filename_in_ddl(self):
        assert "original_filename" in TABLES_SQL

    def test_original_storage_path_in_ddl(self):
        assert "original_storage_path" in TABLES_SQL

    def test_original_file_sha256_in_ddl(self):
        assert "original_file_sha256" in TABLES_SQL

    def test_entry_type_column_position(self):
        """entry_type 应在 metadata 之后、quality 之前"""
        meta_pos = TABLES_SQL.find("metadata")
        et_pos = TABLES_SQL.find("entry_type")
        quality_pos = TABLES_SQL.find("quality")
        assert meta_pos < et_pos < quality_pos
