"""
吸星 database.py 测试 — DDL 语法验证（静态检查，不连接 PG）
"""

import pytest

from xixing.database import SCHEMA, TABLES_SQL
from xixing import config as xcfg


class TestSchemaConfig:
    def test_schema_name_is_string(self):
        assert isinstance(SCHEMA, str)
        assert len(SCHEMA) > 0

    def test_schema_name_from_config(self):
        assert SCHEMA == xcfg.get_schema_name()


class TestTablesSQL:
    def test_contains_all_tables(self):
        assert "CREATE TABLE IF NOT EXISTS" in TABLES_SQL
        assert "sources" in TABLES_SQL
        assert "collection_runs" in TABLES_SQL
        assert "knowledge_items" in TABLES_SQL
        assert "xizhenji" in TABLES_SQL
        assert "scan_results" in TABLES_SQL
        assert "distillation_runs" in TABLES_SQL

    def test_contains_schema_creation(self):
        assert f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}" in TABLES_SQL

    def test_total_tables_count(self):
        create_count = TABLES_SQL.count("CREATE TABLE IF NOT EXISTS")
        assert create_count == 7

    def test_sources_has_reputation(self):
        assert "reputation" in TABLES_SQL
        assert "consecutive_errors" in TABLES_SQL

    def test_knowledge_items_unique_hash(self):
        assert "content_hash" in TABLES_SQL
        assert "UNIQUE" in TABLES_SQL

    def test_collection_runs_fk(self):
        assert "REFERENCES" in TABLES_SQL
        assert f"{SCHEMA}.sources" in TABLES_SQL

    def test_all_required_indexes(self):
        required = [
            "idx_cr_source",
            "idx_cr_status",
            "idx_ki_source",
            "idx_ki_category",
            "idx_ki_injected",
            "idx_xz_severity",
            "idx_xz_resolved",
            "idx_scan_date",
        ]
        for idx in required:
            assert idx in TABLES_SQL, f"Missing index: {idx}"

    def test_scan_results_has_actionable(self):
        assert "actionable" in TABLES_SQL
        assert "action_taken" in TABLES_SQL

    def test_xizhenji_has_injection_flag(self):
        assert "injected_to_yongheng" in TABLES_SQL


class TestModels:
    def test_import_all_models(self):
        from xixing.models import (
            AppError,
            SourceCreate, SourceUpdate, SourceResponse,
            CollectRequest, CollectionResult, CollectResponse,
            IngestRequest, GateResult, IngestResponse,
            IngestToYonghengRequest,
            LearnRequest, LearnResponse,
            XizhenjiCreate, XizhenjiUpdate, XizhenjiResponse,
            ReportPitfallRequest,
            ScanRequest, ScanResultItem, ScanResponse,
            DistillRequest, DistillResponse,
        )
        assert SourceCreate is not None
        assert XizhenjiCreate is not None
        assert DistillRequest is not None

    def test_app_error(self):
        from xixing.models import AppError
        e = AppError("VALIDATION", "content too short", 400)
        assert e.code == "VALIDATION"
        assert e.status == 400

    def test_source_create_validation(self):
        from xixing.models import SourceCreate
        src = SourceCreate(
            id="test-source",
            name="Test Source",
            url="https://example.com",
            source_type="research",
        )
        assert src.id == "test-source"
        assert src.schedule == "daily"

    def test_xizhenji_create_validation(self):
        from xixing.models import XizhenjiCreate
        xz = XizhenjiCreate(
            title="Config file broken",
            description="Gateway failed to start after config change",
            severity="critical",
            tags=["config", "gateway"],
        )
        assert xz.severity == "critical"
        assert "gateway" in xz.tags

    def test_collect_request_optional_source_ids(self):
        from xixing.models import CollectRequest
        req = CollectRequest(source_ids=["clawhub-skills", "arxiv-llm-agents"])
        assert len(req.source_ids) == 2

    # ── Skill 提案模型 ─────────────────────────────────

    def test_evolve_request_defaults(self):
        from xixing.models import EvolveRequest
        req = EvolveRequest()
        assert req.dry_run is False
        assert req.full_scan is False

    def test_evolve_request_dry_run(self):
        from xixing.models import EvolveRequest
        req = EvolveRequest(dry_run=True, full_scan=True)
        assert req.dry_run is True
        assert req.full_scan is True

    def test_evolve_response(self):
        from xixing.models import EvolveResponse
        proposals = [
            {"name": "skill_a", "display_name": "技能A", "description": "test", "frequency": 15},
            {"name": "skill_b", "display_name": "技能B", "description": "test", "frequency": 20},
        ]
        resp = EvolveResponse(proposals=proposals, total=2, dry_run=False)
        assert resp.total == 2
        assert len(resp.proposals) == 2
        assert resp.dry_run is False

    def test_evolve_response_dry_run(self):
        from xixing.models import EvolveResponse
        resp = EvolveResponse(proposals=[], total=0, dry_run=True)
        assert resp.dry_run is True
        assert resp.proposals == []
