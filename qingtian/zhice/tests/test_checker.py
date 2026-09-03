"""执策检查规则引擎 — 完整测试"""
import pytest
from zhice import checker


# ══════════════════════════════════════════════════════════
# output_contains
# ══════════════════════════════════════════════════════════

class TestOutputContains:
    def test_pass_simple(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "output_contains", "field": "result", "keyword": "PONG"},
            {"result": "PONG"},
        )
        assert ok
        assert actual == "PONG"
        assert err == ""

    def test_pass_nested_field(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "output_contains", "field": "data.status", "keyword": "ok"},
            {"data": {"status": "ok"}},
        )
        assert ok

    def test_pass_keyword_in_middle(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "output_contains", "field": "msg", "keyword": "success"},
            {"msg": "deploy success!"},
        )
        assert ok

    def test_fail_keyword_not_found(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "output_contains", "field": "result", "keyword": "PONG"},
            {"result": "ERR connection refused"},
        )
        assert not ok
        assert "ERR" in actual

    def test_fail_field_missing(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "output_contains", "field": "nonexistent", "keyword": "x"},
            {"result": "PONG"},
        )
        assert not ok

    def test_fail_none_value(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "output_contains", "field": "result", "keyword": "PONG"},
            {"result": None},
        )
        assert not ok


# ══════════════════════════════════════════════════════════
# file_exists
# ══════════════════════════════════════════════════════════

class TestFileExists:
    def test_pass_exists_required(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "file_exists", "path": "/opt/app/main.py", "required": True},
            {"file_exists": [{"path": "/opt/app/main.py", "exists": True}]},
        )
        assert ok

    def test_pass_not_required_not_exists(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "file_exists", "path": "/tmp/optional", "required": False},
            {"file_exists": [{"path": "/tmp/optional", "exists": False}]},
        )
        assert ok

    def test_fail_required_but_missing(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "file_exists", "path": "/opt/app/main.py", "required": True},
            {"file_exists": [{"path": "/opt/app/main.py", "exists": False}]},
        )
        assert not ok

    def test_fail_not_required_but_exists(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "file_exists", "path": "/tmp/x", "required": False},
            {"file_exists": [{"path": "/tmp/x", "exists": True}]},
        )
        assert not ok

    def test_missing_check_results_key(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "file_exists", "path": "/tmp/x", "required": True},
            {},
        )
        assert not ok


# ══════════════════════════════════════════════════════════
# api_health
# ══════════════════════════════════════════════════════════

class TestApiHealth:
    def test_pass(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "api_health", "url": "http://localhost:1996/health", "expected_status": 200},
            {"api_health": [{"url": "http://localhost:1996/health", "status_code": 200}]},
        )
        assert ok

    def test_fail_wrong_status(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "api_health", "url": "http://localhost:1996/health", "expected_status": 200},
            {"api_health": [{"url": "http://localhost:1996/health", "status_code": 503}]},
        )
        assert not ok

    def test_skip_different_url(self):
        """其他 URL 的检查结果应跳过，不匹配不报错"""
        ok, actual, err, schema_err = checker.check_single(
            {"type": "api_health", "url": "http://a/health", "expected_status": 200},
            {"api_health": [{"url": "http://b/health", "status_code": 200}]},
        )
        assert not ok  # 未找到匹配的 URL 项

    def test_missing_report_fails_closed(self):
        """P2 (R11): api_health 未上报 → fail-closed（不得视为通过），
        原实现误归 ENGINE_AUTO → 未上报直接通过（API 健康从未验证）"""
        ok, actual, err, schema_err = checker.check_single(
            {"type": "api_health", "url": "http://x/health", "expected_status": 200},
            {},  # agent 未上报任何 api_health 结果
        )
        assert not ok
        assert "未上报" in err
        assert not schema_err  # 属正常未验证，不当作协议错误

    def test_missing_report_in_check_all_fails(self):
        """P2 (R11): check_all 中 api_health 未上报 → 整体不通过"""
        result = checker.check_all(
            [{"type": "api_health", "url": "http://x/health", "expected_status": 200}],
            {},
        )
        assert not result["passed"]
        assert len(result["failed_rules"]) == 1
        assert "未上报" in result["failed_rules"][0]["error"]


# ══════════════════════════════════════════════════════════
# db_query
# ══════════════════════════════════════════════════════════

class TestDbQuery:
    def test_pass(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "db_query", "sql": "SELECT COUNT(*) FROM users", "expected_min": 1},
            {"db_query": [{"sql": "SELECT COUNT(*) FROM users", "count": 5}]},
        )
        assert ok

    def test_fail_below_min(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "db_query", "sql": "SELECT COUNT(*) FROM users", "expected_min": 10},
            {"db_query": [{"sql": "SELECT COUNT(*) FROM users", "count": 5}]},
        )
        assert not ok

    def test_pass_exact_boundary(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "db_query", "sql": "SELECT 1", "expected_min": 5},
            {"db_query": [{"sql": "SELECT 1", "count": 5}]},
        )
        assert ok


# ══════════════════════════════════════════════════════════
# run_script
# ══════════════════════════════════════════════════════════

class TestRunScript:
    def test_pass(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "run_script", "script": "tests/smoke.py", "expected_exit_code": 0},
            {"run_script": [{"script": "tests/smoke.py", "exit_code": 0, "stdout": "OK"}]},
        )
        assert ok

    def test_fail_nonzero(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "run_script", "script": "tests/smoke.py", "expected_exit_code": 0},
            {"run_script": [{"script": "tests/smoke.py", "exit_code": 1, "stdout": "FAIL"}]},
        )
        assert not ok


# ══════════════════════════════════════════════════════════
# manual_review
# ══════════════════════════════════════════════════════════

class TestManualReview:
    def test_always_needs_review(self):
        ok, actual, err, schema_err = checker.check_single(
            {"type": "manual_review", "reviewer": "大师"},
            {},
        )
        assert not ok
        assert err == "needs_review"
        assert not schema_err  # manual_review is not a schema error


# ══════════════════════════════════════════════════════════
# schema 校验
# ══════════════════════════════════════════════════════════

class TestSchemaValidation:
    def test_missing_required_field(self):
        err = checker.validate_check_results_schema("file_exists", {"path": "/tmp/x"})
        assert err is not None
        assert "exists" in err

    def test_wrong_type(self):
        err = checker.validate_check_results_schema("api_health", {
            "url": "http://x", "status_code": "not_int"
        })
        assert err is not None
        assert "int" in err

    def test_status_code_out_of_range(self):
        err = checker.validate_check_results_schema("api_health", {
            "url": "http://x", "status_code": 999
        })
        assert err is not None

    def test_exit_code_out_of_range(self):
        err = checker.validate_check_results_schema("run_script", {
            "script": "test.py", "exit_code": 300
        })
        assert err is not None

    def test_sql_too_long(self):
        err = checker.validate_check_results_schema("db_query", {
            "sql": "x" * 501, "count": 1
        })
        assert err is not None

    def test_pass_valid(self):
        err = checker.validate_check_results_schema("file_exists", {
            "path": "/tmp/x", "exists": True
        })
        assert err is None

    def test_non_agent_report_skipped(self):
        err = checker.validate_check_results_schema("output_contains", {})
        assert err is None

    def test_schema_error_flag_on_bad_input(self):
        """schema 校验失败时应返回 schema_error=True"""
        ok, actual, err, schema_err = checker.check_single(
            {"type": "file_exists", "path": "/tmp/x", "required": True},
            {"file_exists": [{"path": "/tmp/x"}]},  # 缺少 exists 字段
        )
        assert not ok
        assert schema_err
        assert "exists" in err

    def test_no_schema_error_on_real_failure(self):
        """真实检查失败时 schema_error 应为 False"""
        ok, actual, err, schema_err = checker.check_single(
            {"type": "file_exists", "path": "/tmp/x", "required": True},
            {"file_exists": [{"path": "/tmp/x", "exists": False}]},
        )
        assert not ok
        assert not schema_err


# ══════════════════════════════════════════════════════════
# check_all — 集成测试
# ══════════════════════════════════════════════════════════

class TestCheckAll:
    def test_all_pass(self):
        result = checker.check_all(
            [
                {"type": "output_contains", "field": "msg", "keyword": "ok"},
                {"type": "file_exists", "path": "/tmp/a", "required": True},
            ],
            {
                "msg": "deploy ok",
                "file_exists": [{"path": "/tmp/a", "exists": True}],
            },
        )
        assert result["passed"]
        assert len(result["failed_rules"]) == 0
        assert len(result["results"]) == 2

    def test_partial_failure(self):
        result = checker.check_all(
            [
                {"type": "output_contains", "field": "msg", "keyword": "ok"},
                {"type": "file_exists", "path": "/tmp/a", "required": True},
            ],
            {
                "msg": "FAILED",
                "file_exists": [{"path": "/tmp/a", "exists": False}],
            },
        )
        assert not result["passed"]
        assert len(result["failed_rules"]) == 2

    def test_one_pass_one_fail(self):
        result = checker.check_all(
            [
                {"type": "output_contains", "field": "msg", "keyword": "ok"},
                {"type": "file_exists", "path": "/tmp/a", "required": True},
            ],
            {
                "msg": "deploy ok",
                "file_exists": [{"path": "/tmp/a", "exists": False}],
            },
        )
        assert not result["passed"]
        assert len(result["failed_rules"]) == 1

    def test_empty_criteria(self):
        result = checker.check_all([], {})
        assert result["passed"]
        assert len(result["failed_rules"]) == 0

    def test_mixed_types(self):
        result = checker.check_all(
            [
                {"type": "output_contains", "field": "status", "keyword": "ok"},
                {"type": "api_health", "url": "http://x/health", "expected_status": 200},
                {"type": "run_script", "script": "test.py", "expected_exit_code": 0},
            ],
            {
                "status": "all systems ok",
                "api_health": [{"url": "http://x/health", "status_code": 200}],
                "run_script": [{"script": "test.py", "exit_code": 0}],
            },
        )
        assert result["passed"]
        assert len(result["results"]) == 3


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════

class TestHelpers:
    def test_is_engine_auto(self):
        assert checker.is_engine_auto("output_contains")
        assert checker.is_engine_auto("manual_review")
        assert not checker.is_engine_auto("api_health")  # P2 (R11): api_health 属 agent-report
        assert not checker.is_engine_auto("file_exists")

    def test_is_agent_report(self):
        assert checker.is_agent_report("file_exists")
        assert checker.is_agent_report("api_health")  # P2 (R11)
        assert checker.is_agent_report("db_query")
        assert checker.is_agent_report("run_script")
        assert not checker.is_agent_report("output_contains")
