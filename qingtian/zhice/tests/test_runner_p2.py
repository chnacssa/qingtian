"""runner P2 (R11) 回归测试

#2 — auto_extract_workflow 回填子查询多版本报错被外层 except 吞掉 → 回填静默失败。
     修复: 逐条 try/except + trace 日志，单条失败继续回填后续行；子查询加
     ORDER BY version DESC LIMIT 1 规避多行。
#3 — _qc_analyze 用 steps[0].get("workflow_id") 取模板 ID，但 zhice.steps 表无
     workflow_id 列 → 恒 None。修复: 改从 zhice.tasks 按 task_id 直查。
"""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
class TestAutoExtractBackfill:
    async def test_backfill_continues_on_single_failure(self):
        """P2 (R11): 第一条回填抛多版本子查询报错，仍继续回填后续行（不再被整段吞掉）"""
        from zhice import runner

        state = {"n": 0, "sqls": []}

        async def fake_execute(sql, *params):
            state["sqls"].append(sql)
            state["n"] += 1
            if state["n"] == 2:  # 第 2 次 = 第一条回填 → 模拟多版本子查询多行报错
                raise Exception("more than one row returned by a subquery used as an expression")
            return "ok"

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=0)  # 同名 workflow 已存在检查 → 0（未存在）
        conn.execute = fake_execute

        task = {"title": "t", "created_by": "u", "task_id": 1,
                "acceptance_criteria": None, "quality_criteria": None, "timeout_minutes": None}

        with patch("zhice.runner._cluster_similar_tasks", new_callable=AsyncMock) as m:
            m.return_value = ["1", "2", "3"]
            await runner._auto_extract_workflow(conn, task, [])

        # INSERT(1) + 3 条回填 = 4 次执行；第 1 条回填失败后仍继续了后续 2 条
        assert state["n"] == 4, "单条回填失败不应中止整个函数"
        backfill_sqls = state["sqls"][1:]
        assert len(backfill_sqls) == 3  # 3 条回填全部尝试（含失败的那条）

        # 子查询应带 ORDER BY version DESC LIMIT 1，规避同名多版本子查询多行报错
        for sql in backfill_sqls:
            assert "ORDER BY version DESC LIMIT 1" in sql

    async def test_workflow_id_backfilled(self):
        """P2 (R11): 回填后 tasks.workflow_id 应指向刚创建的最新版本 workflow"""
        from zhice import runner

        executed = []

        async def fake_execute(sql, *params):
            executed.append((sql, params))

        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=0)
        conn.execute = fake_execute

        task = {"title": "t", "created_by": "u", "task_id": 7,
                "acceptance_criteria": None, "quality_criteria": None, "timeout_minutes": None}

        with patch("zhice.runner._cluster_similar_tasks", new_callable=AsyncMock) as m:
            m.return_value = ["7", "8", "9"]
            await runner._auto_extract_workflow(conn, task, [])

        # 回填 UPDATE 的 $2 应为 similar_ids 里的 tid
        backfills = [(sql, p) for sql, p in executed if "UPDATE" in sql and "workflow_id" in sql]
        assert len(backfills) == 3
        tids = [p[1] for _, p in backfills]
        assert tids == ["7", "8", "9"]


@pytest.mark.asyncio
class TestQcAnalyzeWorkflowId:
    async def test_workflow_id_read_from_tasks_table(self):
        """P2 (R11): steps 无 workflow_id 列，须从 tasks 表按 task_id 查，而非 steps[0].get"""
        from zhice import runner

        conn = AsyncMock()
        fetchval_sqls = []

        async def fake_fetchval(sql, *params):
            fetchval_sqls.append(sql)
            if "verifications" in sql:
                return 0  # engine_recheck 失败数
            if "workflow_id" in sql:
                return 123  # tasks 表里真实的 workflow_id
            raise AssertionError(f"unexpected fetchval SQL: {sql}")

        inserts = []

        async def fake_execute(sql, *params):
            inserts.append((sql, params))

        conn.fetchval = fake_fetchval
        conn.execute = fake_execute

        steps = [{"step_index": 1,
                  "quality_criteria": [{"type": "exists", "field": "result"}],
                  "iteration_log": None}]
        await runner._qc_analyze(conn, task_id=42, steps=steps)

        # 必须发起对 tasks 表的 workflow_id 查询
        wf_sqls = [s for s in fetchval_sqls if "workflow_id" in s and "FROM" in s]
        assert wf_sqls, "应查询 tasks 表的 workflow_id 列"
        assert "tasks" in wf_sqls[0]

        # INSERT task_quality_stats 的 workflow_id 参数应为 123（而非 None）
        assert len(inserts) == 1
        sql, params = inserts[0]
        assert "task_quality_stats" in sql
        assert params[0] == 42    # task_id
        assert params[1] == 123   # workflow_id
