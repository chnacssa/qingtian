"""agent_daemon db_query 命令注入加固测试（P2 R11）

原实现 `psql -c "{sql}"` + shell=True，恶意 SQL（如 `"; rm -rf /; "`）可执行任意
shell 命令。修复后: _validate_readonly_sql 白名单拦截注入 SQL，_run_db_query 以
shell=False + argv 列表传给 psql。本文件验证两类行为:
  1. 注入载荷（分号多语句 / $(...) / 反引号 / 写关键字 / 非 SELECT）一律被拒绝
  2. 合法只读 SELECT 以"参数列表、无 shell"方式执行，SQL 仅作为单个 argv
"""
import subprocess
from unittest.mock import patch

from zhice.agent_daemon import _validate_readonly_sql, _run_db_query


# ══════════════════════════════════════════════════════════
# _validate_readonly_sql — 白名单校验
# ══════════════════════════════════════════════════════════

class TestValidateReadonlySql:
    def test_accepts_simple_select(self):
        assert _validate_readonly_sql("SELECT COUNT(*) FROM users") is None

    def test_accepts_lowercase_select_with_where(self):
        assert _validate_readonly_sql(
            "select count(*) from tasks where status = 'done'"
        ) is None

    def test_accepts_leading_whitespace(self):
        assert _validate_readonly_sql("  SELECT 1  ") is None

    def test_rejects_semicolon_multi_statement(self):
        err = _validate_readonly_sql("SELECT 1; DROP TABLE tasks")
        assert err is not None
        assert "分号" in err

    def test_rejects_shell_payload_via_quote_break(self):
        """原漏洞利用方式: 闭合双引号后拼任意 shell 命令"""
        err = _validate_readonly_sql('SELECT 1"; rm -rf /; "')
        assert err is not None

    def test_rejects_command_substitution(self):
        err = _validate_readonly_sql("SELECT 1 $(id)")
        assert err is not None
        assert "shell" in err

    def test_rejects_backtick_substitution(self):
        err = _validate_readonly_sql("SELECT 1 `id`")
        assert err is not None

    def test_rejects_comment_hiding_semicolon(self):
        err = _validate_readonly_sql("SELECT 1 --; DROP TABLE tasks")
        assert err is not None

    def test_rejects_non_select_insert(self):
        err = _validate_readonly_sql("INSERT INTO tasks VALUES (1)")
        assert err is not None

    def test_rejects_non_select_update(self):
        err = _validate_readonly_sql("UPDATE tasks SET workflow_id = 1")
        assert err is not None

    def test_rejects_with_cte(self):
        """WITH ... 开头非 SELECT，保守白名单同样拒绝"""
        err = _validate_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x")
        assert err is not None

    def test_rejects_empty(self):
        assert _validate_readonly_sql("") is not None
        assert _validate_readonly_sql("   ") is not None


# ══════════════════════════════════════════════════════════
# _run_db_query — 无 shell 执行
# ══════════════════════════════════════════════════════════

class TestRunDbQueryNoShell:
    @patch("subprocess.run")
    def test_calls_subprocess_without_shell_as_argv_list(self, mock_run):
        """核心断言: argv 列表传参（非字符串拼接）+ 不启用 shell + psql -c 单命令"""
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="42\n", stderr=""
        )
        count = _run_db_query("SELECT COUNT(*) FROM users")
        assert count == 42

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert isinstance(cmd, list)                                # argv 列表而非字符串
        assert cmd[0] == "psql"
        assert cmd[-2] == "-c"                                      # psql -c 单命令模式
        assert cmd[-1] == "SELECT COUNT(*) FROM users"              # SQL 作为单个参数
        assert kwargs.get("shell") in (None, False)                 # 不启用 shell

    @patch("subprocess.run")
    def test_injection_sql_never_reaches_subprocess(self, mock_run):
        """恶意 SQL 在到达 subprocess 之前被白名单拦截，subprocess 不被调用"""
        payloads = [
            "SELECT 1; DROP TABLE tasks",
            'SELECT 1"; rm -rf /; "',
            "SELECT 1 $(id)",
            "SELECT 1 `id`",
            "DELETE FROM tasks",
        ]
        for payload in payloads:
            mock_run.reset_mock()
            assert _run_db_query(payload) == 0
            mock_run.assert_not_called()
