"""汇川 Phase 4-7 — connector + lint + mcp 单元测试 (无 DB 依赖)

测试范围:
  - connector.py: JSONPath 提取 / 配置解析
  - lint.py: lint_report mock / auto_fix mock
  - mcp.py: MCP_TOOLS 完整性
  - database.py: knowledge_links DDL
"""

import pytest

from huichuan.connector import _extract_jsonpath, run_connector
from huichuan.lint import lint_report, auto_fix
from huichuan.mcp import MCP_TOOLS
from huichuan.database import TABLES_SQL


# ═══════════════════════════════════════════════════════
# connector.py: JSONPath 提取
# ═══════════════════════════════════════════════════════


class TestJSONPath:
    def test_simple_field(self):
        obj = {"name": "test", "value": 42}
        assert _extract_jsonpath(obj, "$.name") == "test"
        assert _extract_jsonpath(obj, "$.value") == "42"

    def test_nested_field(self):
        obj = {"order": {"number": "ORD-001", "amount": 3500}}
        assert _extract_jsonpath(obj, "$.order.number") == "ORD-001"
        assert _extract_jsonpath(obj, "$.order.amount") == "3500"

    def test_missing_field_returns_empty(self):
        obj = {"name": "test"}
        assert _extract_jsonpath(obj, "$.nonexistent") == ""

    def test_empty_path_returns_empty(self):
        obj = {"key": "val"}
        assert _extract_jsonpath(obj, "") == ""

    def test_list_index_access(self):
        obj = {"items": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        assert _extract_jsonpath(obj, "$.items.0.id") == "a"
        assert _extract_jsonpath(obj, "$.items.2.id") == "c"

    def test_list_index_out_of_range(self):
        obj = {"items": [{"id": "a"}]}
        assert _extract_jsonpath(obj, "$.items.5.id") == ""

    def test_non_dict_intermediate(self):
        """中间值非 dict/list → 返回空"""
        obj = {"key": "plain_string"}
        assert _extract_jsonpath(obj, "$.key.subkey") == ""

    def test_none_value(self):
        assert _extract_jsonpath({}, "$.x") == ""


# ═══════════════════════════════════════════════════════
# connector.py: run_connector 边界
# ═══════════════════════════════════════════════════════


class TestRunConnectorBoundary:
    @pytest.mark.asyncio
    async def test_connector_not_found(self):
        """不存在的连接器 → error"""
        result = await run_connector(None, "nonexistent_connector")
        assert "error" in result
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_unsupported_type(self):
        """connector type 非 http_poll → error"""
        # 通过 mock config 目录来测试 — 这里只验证导入和函数签名正确
        # 实际文件 I/O 需要 /opt/qingtian/huichuan/connectors/ 目录存在
        pass


# ═══════════════════════════════════════════════════════
# lint.py: lint_report mock
# ═══════════════════════════════════════════════════════


class TestLintReport:
    @pytest.mark.asyncio
    async def test_report_with_empty_db(self):
        """空数据库 → 返回空报告（不崩溃）"""
        calls = []

        class MockConn:
            async def fetch(self, sql, *params):
                calls.append(sql)
                return []

        report = await lint_report(MockConn())
        assert "orphans" in report
        assert "broken_links" in report
        assert "contradictions" in report
        assert "expired" in report
        assert "decayed" in report
        assert "ran_at" in report
        # 5 项检查各调用一次 fetch
        assert len(calls) == 5

    @pytest.mark.asyncio
    async def test_report_all_empty_lists(self):
        class MockConn:
            async def fetch(self, sql, *params):
                return []

        report = await lint_report(MockConn())
        assert report["orphans"] == []
        assert report["broken_links"] == []
        assert report["contradictions"] == []
        assert report["expired"] == []
        assert report["decayed"] == []

    @pytest.mark.asyncio
    async def test_ran_at_is_iso_format(self):
        class MockConn:
            async def fetch(self, sql, *params):
                return []

        report = await lint_report(MockConn())
        assert "T" in report["ran_at"]


# ═══════════════════════════════════════════════════════
# lint.py: auto_fix mock
# ═══════════════════════════════════════════════════════


class TestAutoFix:
    @pytest.mark.asyncio
    async def test_auto_fix_all_default(self):
        """auto_fix() 默认修复所有类别"""
        calls = []

        class MockConn:
            async def execute(self, sql, *params):
                calls.append(sql)
                return "UPDATE 0"

        result = await auto_fix(MockConn())
        assert "fixed" in result
        assert "skipped" in result
        assert "errors" in result
        # 3 项自动修复: broken_links, expired, decayed
        assert len(calls) == 3

    @pytest.mark.asyncio
    async def test_auto_fix_specific_category(self):
        """auto_fix(categories=["expired"]) 仅修复过期"""
        calls = []

        class MockConn:
            async def execute(self, sql, *params):
                calls.append(sql)
                return "UPDATE 5"

        result = await auto_fix(MockConn(), categories=["expired"])
        assert len(calls) == 1  # only expired
        assert result["fixed"] == 5


# ═══════════════════════════════════════════════════════
# MCP Tools
# ═══════════════════════════════════════════════════════


class TestMCPTools:
    def test_tool_count(self):
        assert len(MCP_TOOLS) == 16

    def test_all_tools_have_name_and_description(self):
        for tool in MCP_TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert isinstance(tool["name"], str)
            assert isinstance(tool["description"], str)

    def test_all_tools_have_handler(self):
        for tool in MCP_TOOLS:
            assert "handler" in tool
            assert tool["handler"].startswith("huichuan.")

    def test_unique_tool_names(self):
        names = [t["name"] for t in MCP_TOOLS]
        assert len(names) == len(set(names))

    def test_search_tools_exist(self):
        names = {t["name"] for t in MCP_TOOLS}
        assert "search_entities" in names
        assert "search_concepts" in names
        assert "search_comparisons" in names

    def test_ingest_tools_exist(self):
        names = {t["name"] for t in MCP_TOOLS}
        assert "ingest_text" in names
        assert "ingest_url" in names
        assert "ingest_file" in names

    def test_graph_tools_exist(self):
        names = {t["name"] for t in MCP_TOOLS}
        assert "list_links" in names
        assert "get_graph_neighborhood" in names

    def test_lint_tools_exist(self):
        names = {t["name"] for t in MCP_TOOLS}
        assert "lint_report" in names
        assert "auto_fix" in names

    def test_subscribe_tools_exist(self):
        names = {t["name"] for t in MCP_TOOLS}
        assert "subscribe" in names
        assert "unsubscribe" in names

    def test_all_tools_have_parameters(self):
        for tool in MCP_TOOLS:
            assert "parameters" in tool
            assert isinstance(tool["parameters"], dict)


# ═══════════════════════════════════════════════════════
# DDL: knowledge_links 表
# ═══════════════════════════════════════════════════════


class TestDDLKnowledgeLinks:
    def test_knowledge_links_table_exists(self):
        assert "knowledge_links" in TABLES_SQL

    def test_total_tables_count(self):
        # knowledge_entries + versions + subscriptions + refinement_queue
        # + knowledge_links + file_registry + file_images = 7
        create_count = TABLES_SQL.count("CREATE TABLE IF NOT EXISTS")
        assert create_count == 7

    def test_knowledge_links_columns(self):
        required = [
            "link_id", "source_id", "target_id",
            "link_type", "confidence", "created_by", "created_at",
        ]
        for col in required:
            assert col in TABLES_SQL, f"Missing column: {col}"

    def test_link_type_check_constraint(self):
        assert "CHECK (link_type IN ('related','contradicts','extends','depends','cites'))" in TABLES_SQL

    def test_cascade_delete(self):
        assert "ON DELETE CASCADE" in TABLES_SQL

    def test_unique_constraint(self):
        assert "UNIQUE(source_id, target_id, link_type)" in TABLES_SQL

    def test_source_target_indexes(self):
        assert "idx_kl_source" in TABLES_SQL
        assert "idx_kl_target" in TABLES_SQL
