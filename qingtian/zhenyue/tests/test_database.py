"""
镇岳 database.py 测试 — DDL 语法验证（静态检查，不连接 PG）
"""

import pytest

from zhenyue.database import SCHEMA, TABLES_SQL, TRIGGERS_SQL
from zhenyue import config as zcfg


class TestSchemaConfig:
    def test_schema_name_is_string(self):
        assert isinstance(SCHEMA, str)
        assert len(SCHEMA) > 0

    def test_schema_name_from_config(self):
        assert SCHEMA == zcfg.get_schema_name()


class TestTablesSQL:
    def test_contains_all_tables(self):
        assert "CREATE TABLE IF NOT EXISTS" in TABLES_SQL
        assert "sign_keys" in TABLES_SQL
        assert "audit_log" in TABLES_SQL
        assert "tokens" in TABLES_SQL
        assert "agents" in TABLES_SQL
        assert "approvals" in TABLES_SQL
        assert "danger_rules" in TABLES_SQL

    def test_contains_schema_creation(self):
        assert f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}" in TABLES_SQL

    def test_contains_extensions(self):
        assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in TABLES_SQL

    def test_total_tables_count(self):
        create_count = TABLES_SQL.count("CREATE TABLE IF NOT EXISTS")
        assert create_count == 11  # sign_keys, audit_log, tokens, agents, agent_keys, danger_rules, approvals, approval_requests, quarantine, guard_rules, agent_reminders

    def test_sign_keys_unique_index(self):
        assert "idx_zt_sign_keys_active" in TABLES_SQL
        assert "WHERE status = 'active'" in TABLES_SQL

    def test_audit_log_has_hash_chain_fields(self):
        assert "prev_hash" in TABLES_SQL
        assert "hash" in TABLES_SQL
        assert "signature" in TABLES_SQL
        assert "sign_key_id" in TABLES_SQL

    def test_all_required_indexes(self):
        required = [
            "idx_zt_sign_keys_active",
            "idx_zt_audit_agent_time",
            "idx_zt_audit_severity_time",
            "idx_zt_audit_action_time",
            "idx_zt_tokens_agent",
            "idx_zt_tokens_hash",
            "idx_zt_agents_status",
            "idx_zt_approvals_status",
            "idx_zt_approvals_agent",
        ]
        for idx in required:
            assert idx in TABLES_SQL, f"Missing index: {idx}"


class TestTriggersSQL:
    def test_contains_enforce_audit_integrity(self):
        assert "enforce_audit_integrity" in TRIGGERS_SQL
        assert "trg_audit_integrity" in TRIGGERS_SQL
        assert "BEFORE INSERT" in TRIGGERS_SQL

    def test_contains_block_audit_mutation(self):
        assert "block_audit_mutation" in TRIGGERS_SQL
        assert "immutable" in TRIGGERS_SQL

    def test_blocks_update_and_delete(self):
        assert "trg_audit_no_delete" in TRIGGERS_SQL
        assert "BEFORE DELETE" in TRIGGERS_SQL
        assert "trg_audit_no_update" in TRIGGERS_SQL
        assert "BEFORE UPDATE" in TRIGGERS_SQL

    def test_hash_validation_in_trigger(self):
        assert "sha256" in TRIGGERS_SQL
        assert "hash mismatch" in TRIGGERS_SQL

    def test_sign_key_validation_in_trigger(self):
        assert "sign_key_id" in TRIGGERS_SQL
        assert "not an active key" in TRIGGERS_SQL


class TestModels:
    def test_import_models(self):
        from zhenyue.models import (
            AppError,
            RegisterAgentRequest, ReviewAgentRequest,
            SendMessageRequest,
            AuditEntryRequest, AuditVerifyResponse,
            ApprovalCallback,
            CreateTokenRequest, CreateTokenResponse,
            ValidateTokenRequest, ValidateTokenResponse,
            RevokeTokenRequest,
            BreakGlassRequest,
        )
        assert RegisterAgentRequest is not None

    def test_app_error(self):
        from zhenyue.models import AppError
        e = AppError("TEST", "test message", 418)
        assert e.code == "TEST"
        assert e.status == 418

    def test_register_agent_request(self):
        from zhenyue.models import RegisterAgentRequest
        req = RegisterAgentRequest(name="test-agent", category="procurement")
        assert req.name == "test-agent"
        assert req.category == "procurement"

    def test_break_glass_request(self):
        from zhenyue.models import BreakGlassRequest
        req = BreakGlassRequest(token="zt_bg_abc123", action="stop_agent", target="agent:bad")
        assert req.action == "stop_agent"
        assert req.target == "agent:bad"
