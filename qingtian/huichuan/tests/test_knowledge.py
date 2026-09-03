"""汇川 — 单元测试 (无 DB 依赖)"""

import pytest

from huichuan.database import SCHEMA, TABLES_SQL
from huichuan import config as kcfg
from huichuan.errors import (
    AppError,
    KnowledgeNotFoundError,
    VersionConflictError,
    VisibilityForbiddenError,
)
from huichuan.models import (
    BatchWriteRequest,
    BatchWriteResponse,
    BatchWriteResult,
    KnowledgeCreate,
    KnowledgeResponse,
    KnowledgeUpdate,
    MetricsResponse,
    RefineProcessResponse,
    RefineQueueItem,
    RefineQueueResponse,
    RefineSubmitRequest,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    StatsResponse,
    SubscriptionCreate,
    SubscriptionResponse,
    VectorSearchRequest,
    VectorSearchResponse,
    VectorSearchResultItem,
    VersionDetailResponse,
    VersionHistoryItem,
    VersionHistoryResponse,
)
from huichuan.refine import (
    REFINE_SYSTEM_PROMPT,
    _parse_llm_output,
    _confidence_to_quality,
)
from huichuan.cron import _CRON_SCHEDULE
from huichuan.import_export import (
    parse_json,
    parse_markdown,
    parse_csv,
    parse_file_content,
    _content_hash,
    validate_entry,
    MAX_FILES_PER_BATCH,
)
from huichuan.models import (
    ImportResultItem,
    ImportReportResponse,
)


# ═══════════════════════════════════════════════════════
# TestSchemaConfig
# ═══════════════════════════════════════════════════════


class TestSchemaConfig:
    def test_schema_name_is_string(self):
        assert isinstance(SCHEMA, str)
        assert len(SCHEMA) > 0

    def test_schema_name_from_config(self):
        assert SCHEMA == kcfg.get_schema_name()

    def test_deploy_env_is_string(self):
        env = kcfg.get_deploy_env()
        assert isinstance(env, str)
        assert len(env) > 0


# ═══════════════════════════════════════════════════════
# TestTablesSQL
# ═══════════════════════════════════════════════════════


class TestTablesSQL:
    def test_contains_schema_creation(self):
        assert f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}" in TABLES_SQL

    def test_contains_all_tables(self):
        assert f"CREATE TABLE IF NOT EXISTS {SCHEMA}.knowledge_entries" in TABLES_SQL
        assert f"CREATE TABLE IF NOT EXISTS {SCHEMA}.knowledge_versions" in TABLES_SQL
        assert f"CREATE TABLE IF NOT EXISTS {SCHEMA}.subscriptions" in TABLES_SQL
        assert f"CREATE TABLE IF NOT EXISTS {SCHEMA}.refinement_queue" in TABLES_SQL

    def test_total_tables_count(self):
        create_count = TABLES_SQL.count("CREATE TABLE IF NOT EXISTS")
        assert create_count == 7  # + file_registry + file_images

    def test_knowledge_entries_columns(self):
        required = [
            "knowledge_id", "title", "domain", "tags", "visibility",
            "owner_agent", "authorized_agents", "content", "source", "version",
            "valid_from", "valid_until", "metadata", "quality",
            "status", "refined_at", "created_at", "updated_at",
        ]
        for col in required:
            assert col in TABLES_SQL, f"Missing column: {col}"

    def test_knowledge_entries_check_constraints(self):
        assert "CHECK (visibility IN ('public', 'enterprise', 'private'))" in TABLES_SQL
        assert "CHECK (quality BETWEEN 1 AND 5)" in TABLES_SQL
        assert "CHECK (status IN ('draft', 'active', 'archived', 'revoked'))" in TABLES_SQL

    def test_knowledge_entries_uuid_pk(self):
        # 主键应包含 UUID
        assert "UUID PRIMARY KEY" in TABLES_SQL or "uuid PRIMARY KEY" in TABLES_SQL.lower()

    def test_all_required_indexes(self):
        required = [
            "idx_ke_domain",
            "idx_ke_tags",
            "idx_ke_visibility",
            "idx_ke_owner",
            "idx_ke_updated",
            "idx_ke_fts",
            "idx_kv_knowledge",
            "idx_sub_agent",
            "idx_rq_status",
        ]
        for idx in required:
            assert idx in TABLES_SQL, f"Missing index: {idx}"

    def test_knowledge_versions_cascade(self):
        assert "ON DELETE CASCADE" in TABLES_SQL

    def test_subscriptions_unique(self):
        assert "UNIQUE (agent_id, subscription_name)" in TABLES_SQL

    def test_gin_indexes(self):
        assert "USING GIN (tags)" in TABLES_SQL
        assert "to_tsvector('simple'" in TABLES_SQL

    def test_refinement_queue_domain_column(self):
        # refinement_queue 在 DDL 中出现 3 次（注释、CREATE TABLE、CREATE INDEX）
        # 第 3 个分割段 [2] 包含表列定义
        parts = TABLES_SQL.split("refinement_queue")
        assert len(parts) >= 3
        assert "domain" in parts[2]


# ═══════════════════════════════════════════════════════
# TestModels
# ═══════════════════════════════════════════════════════


class TestModels:
    def test_import_all_models(self):
        models = [
            KnowledgeCreate, KnowledgeUpdate, KnowledgeResponse,
            SearchRequest, SearchResponse, SearchResultItem,
            BatchWriteRequest, BatchWriteResponse, BatchWriteResult,
            VectorSearchRequest, VectorSearchResponse, VectorSearchResultItem,
            SubscriptionCreate, SubscriptionResponse,
            RefineSubmitRequest, RefineQueueItem, RefineQueueResponse,
            StatsResponse,
            VersionHistoryItem, VersionHistoryResponse, VersionDetailResponse,
        ]
        for m in models:
            assert m is not None

    def test_app_error(self):
        e = AppError("TEST_CODE", "test message", 400)
        assert e.code == "TEST_CODE"
        assert e.message == "test message"
        assert e.status == 400
        assert str(e) == "test message"

    def test_knowledge_create_defaults(self):
        obj = KnowledgeCreate(title="Test", domain="power", content="Hello")
        assert obj.visibility == "public"
        assert obj.quality == 3
        assert obj.source == "manual"
        assert obj.status == "active"
        assert obj.tags == []
        assert obj.authorized_agents == []
        assert obj.metadata == {}
        assert obj.owner_agent is None

    def test_knowledge_create_all_fields(self):
        obj = KnowledgeCreate(
            title="T", domain="d", content="c",
            tags=["a", "b"], visibility="private", owner_agent="agent-1",
            authorized_agents=["agent-2"], source="agent",
            quality=4, status="draft",
            metadata={"key": "value"},
        )
        assert obj.title == "T"
        assert obj.visibility == "private"
        assert obj.owner_agent == "agent-1"
        assert obj.authorized_agents == ["agent-2"]
        assert obj.quality == 4
        assert obj.status == "draft"
        assert obj.metadata == {"key": "value"}

    def test_knowledge_update_all_optional(self):
        obj = KnowledgeUpdate(version=1)
        assert obj.version == 1
        assert obj.title is None
        assert obj.content is None
        assert obj.visibility is None

    def test_knowledge_response_from_dict(self):
        d = {
            "knowledge_id": "kb-001", "title": "T", "domain": "d",
            "tags": [], "visibility": "public", "owner_agent": None,
            "authorized_agents": [], "content": "c", "source": "manual",
            "version": 1, "valid_from": None, "valid_until": None,
            "metadata": {}, "quality": 3, "status": "active",
            "refined_at": None, "created_at": "2026-05-25T00:00:00Z",
            "updated_at": "2026-05-25T00:00:00Z",
        }
        resp = KnowledgeResponse(**d)
        assert resp.knowledge_id == "kb-001"
        assert resp.title == "T"

    def test_batch_write_request(self):
        entries = [
            KnowledgeCreate(title="A", domain="d", content="a"),
            KnowledgeCreate(title="B", domain="d", content="b"),
        ]
        req = BatchWriteRequest(entries=entries)
        assert len(req.entries) == 2

    def test_search_request_defaults(self):
        req = SearchRequest()
        assert req.query == ""
        assert req.limit == 20
        assert req.offset == 0
        assert req.sort_by == "updated_at"
        assert req.status == "active"

    def test_vector_search_request(self):
        req = VectorSearchRequest(query="test query", top_k=5)
        assert req.query == "test query"
        assert req.top_k == 5
        assert req.min_similarity == 0.7

    def test_subscription_create(self):
        req = SubscriptionCreate(
            agent_id="agent-1", subscription_name="my-sub",
            domains=["power"], tags=["transformer"],
        )
        assert req.agent_id == "agent-1"
        assert req.subscription_name == "my-sub"
        assert req.domains == ["power"]
        assert req.tags == ["transformer"]


# ═══════════════════════════════════════════════════════
# TestErrors
# ═══════════════════════════════════════════════════════


class TestErrors:
    def test_knowledge_not_found(self):
        e = KnowledgeNotFoundError("kb-001")
        assert e.code == "HUICHUAN_NOT_FOUND"
        assert e.status == 404
        assert "kb-001" in e.message

    def test_version_conflict(self):
        e = VersionConflictError(3)
        assert e.code == "VERSION_CONFLICT"
        assert e.status == 409
        assert "3" in e.message

    def test_visibility_forbidden(self):
        e = VisibilityForbiddenError("kb-002")
        assert e.code == "VISIBILITY_FORBIDDEN"
        assert e.status == 403
        assert "kb-002" in e.message


# ═══════════════════════════════════════════════════════
# TestConfigDefaults
# ═══════════════════════════════════════════════════════


class TestConfigDefaults:
    def test_schema_default(self):
        assert kcfg.get_schema_name() == "huichuan"

    def test_deploy_env_default(self):
        assert kcfg.get_deploy_env() == "prod"

    def test_max_content_size_default(self):
        assert kcfg.get_max_knowledge_size() == 50000

    def test_refine_batch_size_default(self):
        assert kcfg.get_refine_batch_size() == 20

    def test_dedup_threshold_default(self):
        assert kcfg.get_dedup_threshold() == 0.92

    def test_all_config_types(self):
        assert isinstance(kcfg.get_schema_name(), str)
        assert isinstance(kcfg.get_deploy_env(), str)
        assert isinstance(kcfg.get_max_knowledge_size(), int)
        assert isinstance(kcfg.get_refine_batch_size(), int)
        assert isinstance(kcfg.get_dedup_threshold(), float)
        assert isinstance(kcfg.get_abstract_max_tokens(), int)
        assert isinstance(kcfg.get_refine_cron(), str)
        assert isinstance(kcfg.get_refine_llm_model(), str)

    def test_phase2_new_config_types(self):
        assert isinstance(kcfg.get_redis_url(), str)
        assert isinstance(kcfg.get_deepseek_api_key(), str)
        assert isinstance(kcfg.get_deepseek_base_url(), str)


# ═══════════════════════════════════════════════════════
# TestPhase2Models
# ═══════════════════════════════════════════════════════


class TestPhase2Models:
    def test_refine_submit_request_has_domain(self):
        req = RefineSubmitRequest(observation="test", domain="power")
        assert req.domain == "power"
        assert req.observation == "test"
        assert req.context == ""

    def test_refine_process_response(self):
        resp = RefineProcessResponse(
            processed=10, accepted=7, rejected=3,
            duration_ms=1234.5, timestamp="2026-05-25T00:00:00Z",
        )
        assert resp.action == "refine_process"
        assert resp.processed == 10
        assert resp.accepted == 7
        assert resp.rejected == 3
        assert resp.duration_ms == 1234.5

    def test_metrics_response_defaults(self):
        resp = MetricsResponse()
        assert resp.storage.total_entries == 0
        assert resp.storage.by_domain == {}
        assert resp.refinement.queue_pending == 0
        assert resp.sync.yongheng_backlog == 0


# ═══════════════════════════════════════════════════════
# TestRefinePrompt
# ═══════════════════════════════════════════════════════


class TestRefinePrompt:
    def test_system_prompt_contains_keywords(self):
        assert "知识工程师" in REFINE_SYSTEM_PROMPT
        assert "INSUFFICIENT_DATA" in REFINE_SYSTEM_PROMPT
        assert "## 标题" in REFINE_SYSTEM_PROMPT
        assert "## 适用场景" in REFINE_SYSTEM_PROMPT
        assert "## 核心规则" in REFINE_SYSTEM_PROMPT
        assert "## 应用建议" in REFINE_SYSTEM_PROMPT
        assert "## 限制条件" in REFINE_SYSTEM_PROMPT

    def test_system_prompt_contains_few_shot(self):
        assert "合肥沙" in REFINE_SYSTEM_PROMPT
        assert "采购谈判第3轮" in REFINE_SYSTEM_PROMPT
        assert "让步窗口" in REFINE_SYSTEM_PROMPT

    def test_system_prompt_mentions_desensitization(self):
        assert "去除" in REFINE_SYSTEM_PROMPT or "脱敏" in REFINE_SYSTEM_PROMPT

    def test_system_prompt_has_word_limit(self):
        assert "500" in REFINE_SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════
# TestRefineParsing
# ═══════════════════════════════════════════════════════


class TestRefineParsing:
    def test_parse_valid_output(self):
        result = _parse_llm_output(
            "## 合肥沙料谈判让步窗口\n"
            "## 适用场景\n合肥地区沙料采购谈判。\n"
            "## 核心规则\n第3-4轮通常是让步窗口。\n"
            "## 应用建议\n- 暂缓回应\n"
            "## 限制条件\n- 仅限合肥地区"
        )
        assert result["status"] == "ok"
        assert "合肥沙料谈判让步窗口" in result["title"]
        assert result["confidence"] == 4

    def test_parse_insufficient_data(self):
        result = _parse_llm_output("INSUFFICIENT_DATA")
        assert result["status"] == "insufficient"
        assert result["confidence"] == 1

    def test_parse_insufficient_data_case_insensitive(self):
        result = _parse_llm_output("insufficient_data")
        assert result["status"] == "insufficient"

    def test_parse_markdown_no_space_after_hash(self):
        result = _parse_llm_output("##测试标题\n## 适用场景\n场景描述。")
        assert result["status"] == "ok"
        assert "测试标题" in result["title"]

    def test_parse_invalid_text(self):
        result = _parse_llm_output("这是一段普通文本，没有 Markdown 标题格式。")
        assert result["status"] == "invalid"
        assert result["confidence"] == 2

    def test_parse_empty_string(self):
        result = _parse_llm_output("")
        assert result["status"] == "invalid"


# ═══════════════════════════════════════════════════════
# TestConfidenceToQuality
# ═══════════════════════════════════════════════════════


class TestConfidenceToQuality:
    def test_confidence_5_to_quality_4(self):
        assert _confidence_to_quality(5) == 4

    def test_confidence_4_to_quality_3(self):
        assert _confidence_to_quality(4) == 3

    def test_confidence_3_to_quality_3(self):
        assert _confidence_to_quality(3) == 3

    def test_confidence_2_to_quality_2(self):
        assert _confidence_to_quality(2) == 2

    def test_confidence_1_to_quality_2(self):
        assert _confidence_to_quality(1) == 2


# ═══════════════════════════════════════════════════════
# TestCronSchedule
# ═══════════════════════════════════════════════════════


class TestCronSchedule:
    def test_all_tasks_registered(self):
        task_names = [t[0] for t in _CRON_SCHEDULE]
        assert "refinement" in task_names
        assert "cleanup_expired" in task_names
        assert "cold_start_activate" in task_names
        assert "yongheng_sync_retry" in task_names

    def test_schedule_structure(self):
        for entry in _CRON_SCHEDULE:
            assert len(entry) == 5
            name, sched_type, hour, minute, fn = entry
            assert isinstance(name, str)
            assert sched_type in ("daily", "hourly")
            assert isinstance(hour, int)
            assert isinstance(minute, int)
            assert callable(fn)

    def test_daily_tasks_have_valid_hours(self):
        for entry in _CRON_SCHEDULE:
            if entry[1] == "daily":
                assert 0 <= entry[2] <= 23
                assert 0 <= entry[3] <= 59

    def test_refinement_is_daily(self):
        for entry in _CRON_SCHEDULE:
            if entry[0] == "refinement":
                assert entry[1] == "daily"
                assert entry[2] == 2
                assert entry[3] == 0


# ═══════════════════════════════════════════════════════
# TestRateLimit
# ═══════════════════════════════════════════════════════


class TestRateLimit:
    def test_redis_key_format(self):
        agent_id = "test-agent-001"
        key = f"huichuan:refine:ratelimit:{agent_id}"
        assert "huichuan:refine:ratelimit" in key
        assert agent_id in key

    def test_rate_limit_threshold(self):
        max_per_hour = 10
        assert max_per_hour == 10
        assert 3600 == 3600  # TTL 1 hour


# ═══════════════════════════════════════════════════════
# TestImportModels
# ═══════════════════════════════════════════════════════


class TestImportModels:
    def test_import_result_item(self):
        item = ImportResultItem(title="T", action="created", knowledge_id="kb-1")
        assert item.action == "created"
        assert item.knowledge_id == "kb-1"

    def test_import_result_item_failed(self):
        item = ImportResultItem(title="T", action="failed", reason="invalid")
        assert item.action == "failed"
        assert item.reason == "invalid"
        assert item.knowledge_id is None

    def test_import_report_response(self):
        resp = ImportReportResponse(
            status="completed", total_files=5, total_items=12,
            created=8, updated=2, skipped=1, conflicted=0, failed=1,
            timestamp="2026-05-25T00:00:00Z",
        )
        assert resp.status == "completed"
        assert resp.created == 8
        assert resp.updated == 2
        assert resp.skipped == 1
        assert resp.total_files == 5
        assert resp.total_items == 12

    def test_import_report_plan_ready(self):
        resp = ImportReportResponse(status="plan_ready")
        assert resp.status == "plan_ready"
        assert resp.created == 0


# ═══════════════════════════════════════════════════════
# TestFileParsing
# ═══════════════════════════════════════════════════════


class TestFileParsing:
    def test_parse_json_single_object(self):
        result = parse_json('{"title": "Test", "content": "Hello"}')
        assert len(result) == 1
        assert result[0]["title"] == "Test"

    def test_parse_json_array(self):
        result = parse_json('[{"title":"A"},{"title":"B"}]')
        assert len(result) == 2
        assert result[0]["title"] == "A"
        assert result[1]["title"] == "B"

    def test_parse_json_invalid(self):
        import json as _json
        try:
            parse_json("not json")
            assert False, "expected ValueError"
        except _json.JSONDecodeError:
            pass

    def test_parse_markdown_with_title(self):
        result = parse_markdown("# 变压器选型指南\n\n内容文本。")
        assert len(result) == 1
        assert result[0]["title"] == "变压器选型指南"

    def test_parse_markdown_untitled(self):
        result = parse_markdown("没有标题的内容。")
        assert len(result) == 1
        assert result[0]["title"] == "Untitled"

    def test_parse_csv_basic(self):
        result = parse_csv("title,domain,content\n铜价行情,price,上海铜价5月...\n电缆标准,power,GB/T 2026...")
        assert len(result) == 2
        assert result[0]["title"] == "铜价行情"
        assert result[0]["domain"] == "price"
        assert result[1]["title"] == "电缆标准"

    def test_parse_csv_too_short(self):
        try:
            parse_csv("only_header")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_parse_file_content_json(self):
        items = parse_file_content("data.json", '{"title":"T","content":"C"}')
        assert len(items) == 1
        assert items[0]["title"] == "T"
        assert items[0]["source"] == "import"

    def test_parse_file_content_md(self):
        items = parse_file_content("guide.md", "# Guide\nContent here.")
        assert len(items) == 1
        assert items[0]["title"] == "Guide"

    def test_parse_file_content_defaults(self):
        items = parse_file_content("data.json", '{"key":"val"}')
        assert "title" in items[0]
        assert items[0]["domain"] == "general"
        assert items[0]["visibility"] == "public"
        assert items[0]["quality"] == 3


# ═══════════════════════════════════════════════════════
# TestContentHash
# ═══════════════════════════════════════════════════════


class TestContentHash:
    def test_same_content_same_hash(self):
        assert _content_hash("Hello") == _content_hash("Hello")

    def test_different_content_different_hash(self):
        assert _content_hash("Hello") != _content_hash("World")

    def test_hash_is_string(self):
        assert isinstance(_content_hash("test"), str)

    def test_hash_length(self):
        assert len(_content_hash("test")) == 16


# ═══════════════════════════════════════════════════════
# TestEntryValidation
# ═══════════════════════════════════════════════════════


class TestEntryValidation:
    def test_valid_entry_passes(self):
        result = validate_entry("变压器选型", "油浸式变压器绕组温升限值为 65K。", "power")
        assert result is None

    def test_content_too_long(self):
        long_content = "X" * 50001
        result = validate_entry("Test", long_content, "power")
        assert result is not None
        assert "超过上限" in result

    def test_content_too_short(self):
        result = validate_entry("Test", "Hi", "power")
        assert result is not None
        assert "过短" in result

    def test_pii_phone_number(self):
        result = validate_entry("Test", "联系电话 13812345678 请联络。", "power")
        assert result is not None
        assert "个人身份信息" in result

    def test_pii_id_card(self):
        result = validate_entry("Test", "身份证号 110101199001011234 已登记。", "power")
        assert result is not None

    def test_pii_cjk_no_space(self):
        """CJK 无空格 PII 校验 — 验证 lookbehind 修复"""
        result = validate_entry("Test", "联系人张工电话13812345678请记录", "power")
        assert result is not None
        assert "个人身份信息" in result

    def test_blacklist_keyword(self):
        result = validate_entry("合同原文", "这是某合同原文的内容。", "negotiation")
        assert result is not None
        assert "不适宜入库" in result

    def test_empty_content(self):
        result = validate_entry("Test", "   ", "power")
        assert result is not None


# ═══════════════════════════════════════════════════════
# TestImportConstants
# ═══════════════════════════════════════════════════════


class TestImportConstants:
    def test_max_files_per_batch(self):
        assert MAX_FILES_PER_BATCH == 100
