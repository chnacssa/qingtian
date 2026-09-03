"""
database.py 测试
DDL 语法验证（不连接 PG，做静态检查）
"""

import pytest

from huanyu.database import SCHEMA, TABLES_SQL
from huanyu import config as hcfg


class TestSchemaConfig:
    def test_schema_name_is_string(self):
        assert isinstance(SCHEMA, str)
        assert len(SCHEMA) > 0

    def test_schema_name_from_config(self):
        assert SCHEMA == hcfg.get_schema_name()


class TestTablesSQL:
    def test_contains_all_tables(self):
        """DDL 包含全部 9 张核心表"""
        assert "CREATE TABLE IF NOT EXISTS" in TABLES_SQL
        assert "agents" in TABLES_SQL
        assert "messages" in TABLES_SQL
        assert "negotiations" in TABLES_SQL
        assert "agreements" in TABLES_SQL
        assert "ratings" in TABLES_SQL
        assert "topic_subscriptions" in TABLES_SQL
        assert "peers" in TABLES_SQL
        assert "audit_log" in TABLES_SQL
        assert "cert_revocations" in TABLES_SQL

    def test_contains_schema_creation(self):
        assert f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}" in TABLES_SQL

    def test_contains_rating_view(self):
        assert "agent_rating_summary" in TABLES_SQL
        assert "DROP VIEW IF EXISTS" in TABLES_SQL

    def test_agents_table_constraints(self):
        assert "CHECK (category IN ('biz:buyer','biz:seller','biz:broker','biz:inspector'" in TABLES_SQL
        assert "CHECK (status IN ('active','inactive','suspended','deleted'))" in TABLES_SQL

    def test_agents_unique_constraint(self):
        assert "UNIQUE (name, server_host)" in TABLES_SQL

    def test_messages_table_new_fields(self):
        """v2.5 新增字段"""
        assert "delivery_status" in TABLES_SQL
        assert "idempotency_key" in TABLES_SQL
        assert "delivered" in TABLES_SQL or "pending" in TABLES_SQL

    def test_messages_idempotency_index(self):
        assert "idx_messages_idempotency" in TABLES_SQL

    def test_messages_delivery_index(self):
        assert "idx_messages_delivery" in TABLES_SQL

    def test_messages_check_constraints(self):
        assert "CHECK (message_type IN (" in TABLES_SQL
        assert "CHECK (priority IN ('low','normal','high','urgent'))" in TABLES_SQL
        assert "CHECK (status IN ('unread','read','archived'))" in TABLES_SQL
        assert "CHECK (delivery_status IN ('local','pending','sent','hub_acked','delivered','failed','dead','cross_org'))" in TABLES_SQL

    def test_negotiations_max_counters(self):
        """max_counters 有默认值"""
        assert "max_counters" in TABLES_SQL
        assert "DEFAULT 5" in TABLES_SQL

    def test_peers_unique_constraint(self):
        assert "UNIQUE (host, port)" in TABLES_SQL

    def test_heartbeat_interval_default(self):
        """心跳间隔不再硬编码 '5 minutes'"""
        assert "300 seconds" in TABLES_SQL

    def test_all_required_indexes(self):
        required_indexes = [
            "idx_agents_category", "idx_agents_status", "idx_agents_host", "idx_agents_name",
            "idx_messages_from", "idx_messages_to", "idx_messages_negotiation",
            "idx_negotiations_buyer", "idx_negotiations_supplier", "idx_negotiations_expires",
            "idx_agreements_buyer", "idx_agreements_supplier",
            "idx_ratings_agent",
            "idx_topic_sub_topic",
            "idx_peers_status",
        ]
        for idx in required_indexes:
            assert idx in TABLES_SQL, f"Missing index: {idx}"

    def test_topic_subscriptions_unique(self):
        assert "UNIQUE (agent_id, topic)" in TABLES_SQL

    def test_total_tables_count(self):
        """9 张核心表（含 audit_log + cert_revocations）"""
        create_count = TABLES_SQL.count("CREATE TABLE IF NOT EXISTS")
        assert create_count >= 10  # tables may grow with new features

    def test_audit_log_table(self):
        """审计表具备哈希链防篡改结构"""
        assert "audit_log" in TABLES_SQL
        assert "prev_hash" in TABLES_SQL
        assert "row_hash" in TABLES_SQL
        assert "idx_audit_actor" in TABLES_SQL
        assert "idx_audit_hash" in TABLES_SQL
