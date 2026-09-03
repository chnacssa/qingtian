"""
yongheng database.py 测试 — DDL 语法验证（静态检查，不连接 PG）
"""

import pytest

from yongheng.database import SCHEMA, TABLES_SQL
from yongheng import config as ycfg


class TestSchemaConfig:
    def test_schema_name_is_string(self):
        assert isinstance(SCHEMA, str)
        assert len(SCHEMA) > 0

    def test_schema_name_from_config(self):
        assert SCHEMA == ycfg.get_schema_name()


class TestTablesSQL:
    def test_contains_all_tables(self):
        assert "CREATE TABLE IF NOT EXISTS" in TABLES_SQL
        assert "memories" in TABLES_SQL
        assert "trajectories" in TABLES_SQL
        assert "profiles" in TABLES_SQL
        assert "digests" in TABLES_SQL
        assert "tokens" in TABLES_SQL

    def test_contains_schema_creation(self):
        assert f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}" in TABLES_SQL

    def test_contains_vector_extension(self):
        assert "CREATE EXTENSION IF NOT EXISTS vector" in TABLES_SQL

    def test_total_tables_count(self):
        create_count = TABLES_SQL.count("CREATE TABLE IF NOT EXISTS")
        assert create_count == 5

    def test_memories_embedding_index(self):
        assert "idx_memories_embedding" in TABLES_SQL
        assert "ivfflat" in TABLES_SQL

    def test_memories_fts_index(self):
        assert "idx_memories_fts" in TABLES_SQL
        assert "GIN" in TABLES_SQL

    def test_trajectories_unique(self):
        assert "UNIQUE (namespace, date)" in TABLES_SQL

    def test_all_required_indexes(self):
        required = [
            "idx_memories_namespace", "idx_memories_type", "idx_memories_timestamp",
            "idx_memories_protected", "idx_memories_consolidated",
            "idx_trajectories_namespace_date",
            "idx_digests_namespace_date",
            "idx_yh_tokens_namespace", "idx_yh_tokens_hash",
        ]
        for idx in required:
            assert idx in TABLES_SQL, f"Missing index: {idx}"


class TestModels:
    def test_import_models(self):
        from yongheng.models import (
            WriteMemoryRequest, SearchRequest, ContextRequest,
            ProfileResponse, ConsolidateRequest, SessionStartRequest,
            AppError,
        )
        assert WriteMemoryRequest is not None

    def test_app_error(self):
        from yongheng.models import AppError
        e = AppError("TEST", "test message", 418)
        assert e.code == "TEST"
        assert e.status == 418

    def test_write_memory_request_validation(self):
        from yongheng.models import WriteMemoryRequest
        req = WriteMemoryRequest(namespace="test:agent1", content="hello world")
        assert req.namespace == "test:agent1"
