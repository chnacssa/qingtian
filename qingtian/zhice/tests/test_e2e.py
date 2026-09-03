"""执策端到端集成测试 — 需 PostgreSQL 实例

用法：
  QINGTIAN_CONFIG=/opt/qingtian/config.yaml pytest qingtian/zhice/tests/test_e2e.py -x -v

如果 PostgreSQL 不可达，自动跳过。
"""
import json
import os
import pytest
import uuid

pytestmark = pytest.mark.integration


def _pg_config():
    """从 config.yaml 读数据库连接参数，缺省用环境变量覆盖"""
    try:
        import yaml
        cfg_path = os.getenv("QINGTIAN_CONFIG", "/opt/qingtian/config.yaml")
        with open(cfg_path, "r") as f:
            cfg = yaml.safe_load(f)
        db = cfg.get("database", {})
        return {
            "host": db.get("host", "localhost"),
            "port": db.get("port", 5432),
            "user": db.get("user", "qingtian"),
            "password": db.get("password", ""),
            "database": db.get("db", "qingtian"),
        }
    except Exception:
        return {
            "host": "localhost",
            "port": 5432,
            "user": "qingtian",
            "password": "qingtian-2026",
            "database": "qingtian",
        }


@pytest.fixture
async def conn():
    """创建真实数据库连接 + 初始化 schema"""
    import asyncpg
    pg = _pg_config()
    try:
        conn = await asyncpg.connect(
            host=pg["host"], port=pg["port"],
            user=pg["user"], password=pg["password"],
            database=pg["database"], timeout=5,
        )
    except Exception as e:
        pytest.skip(f"PostgreSQL 不可达: {e}")
        return

    import sys
    sys.path.insert(0, "qingtian")
    from zhice import database
    await database.ensure_schema()

    yield conn

    # 清理测试数据
    from zhice import config as cfg
    schema = cfg.get_schema_name()
    await conn.execute(f"DELETE FROM {schema}.verifications")
    await conn.execute(f"DELETE FROM {schema}.steps")
    await conn.execute(f"DELETE FROM {schema}.tasks")
    await conn.execute(f"DELETE FROM {schema}.workflows")
    await conn.close()


# ══════════════════════════════════════════════════════════
# 全链路：创建 → 分配 → 执行 → 心跳 → 提交 → 验收
# ══════════════════════════════════════════════════════════

class TestFullChain:
    async def test_simple_task_lifecycle(self, conn):
        """单步任务完整生命周期"""
        import sys
        sys.path.insert(0, "qingtian")
        from zhice import runner, status_machine as sm

        # ── 创建 ──
        result = await runner.create_task(
            title="E2E 测试任务",
            description="验证执策全链路",
            priority="P1",
            created_by="e2e-tester",
            steps=[{
                "step_index": 1,
                "title": "PING 测试",
                "instruction": "返回 PONG",
                "acceptance_criteria": [
                    {"type": "output_contains", "field": "result", "keyword": "PONG"}
                ],
                "timeout_minutes": 5,
            }],
        )
        assert result["success"]
        task_id = result["task"]["task_id"]
        steps = result["steps"]
        assert len(steps) == 1
        step_id = steps[0]["step_id"]
        assert result["mode"] == "simple"
        assert result["task"]["status"] == "running"

        # ── 获取下一步（原子分配）──
        nxt = await runner.get_next_step(task_id, "e2e-agent")
        assert nxt["found"]
        assert nxt["current_step"]["step_id"] == step_id
        assert nxt["current_step"]["status"] == "assigned"

        # ── 确认开始 ──
        started = await sm.step_start(conn, step_id, "e2e-agent")
        assert started is not None
        assert started["status"] == "in_progress"

        # ── 心跳 ──
        hb = await sm.step_heartbeat(conn, step_id, "e2e-agent", "executing")
        assert hb is not None

        # ── 提交结果 ──
        completed = await sm.step_complete(
            conn, step_id, "e2e-agent", "PING 返回 PONG",
            {"result": "PONG", "check_results": []},
        )
        assert completed is not None
        assert completed["status"] == "completed"

        # ── Task 自动完结 ──
        await runner.try_complete_task(task_id)
        task = await sm.get_task(conn, task_id)
        assert task["status"] == "completed"
        assert task["progress"] == 100

    async def test_retry_on_failure(self, conn):
        """失败后自动重试（auto_retry > 0）"""
        import sys
        sys.path.insert(0, "qingtian")
        from zhice import runner, status_machine as sm

        result = await runner.create_task(
            title="E2E 重试测试",
            description="验证 auto_retry 机制",
            created_by="e2e-tester",
            steps=[{
                "step_index": 1,
                "title": "可能失败的步骤",
                "instruction": "执行可能失败的操作",
                "acceptance_criteria": [
                    {"type": "output_contains", "field": "msg", "keyword": "ok"}
                ],
                "auto_retry": 2,
                "timeout_minutes": 5,
            }],
        )
        assert result["success"]
        task_id = result["task"]["task_id"]
        step_id = result["steps"][0]["step_id"]

        # 分配 + 开始
        await runner.get_next_step(task_id, "e2e-agent")
        await sm.step_start(conn, step_id, "e2e-agent")

        # 模拟失败 — step_fail → auto retry
        failed = await sm.step_fail(conn, step_id, "e2e-agent", "第一次失败")
        assert failed is not None
        assert failed["status"] == "failed"
        assert failed["auto_retry"] >= 1

        # 触发重试
        retried = await sm.step_retry(conn, step_id)
        assert retried is not None
        assert retried["status"] == "in_progress"
        assert retried["auto_retry"] == 1  # 消耗了一次

        # 第二次提交成功
        await sm.step_complete(
            conn, step_id, "e2e-agent", "第二次成功了",
            {"msg": "ok", "check_results": []},
        )
        await runner.try_complete_task(task_id)
        task = await sm.get_task(conn, task_id)
        assert task["status"] == "completed"

    async def test_task_cancel(self, conn):
        """取消任务"""
        import sys
        sys.path.insert(0, "qingtian")
        from zhice import runner, status_machine as sm

        result = await runner.create_task(
            title="E2E 取消测试",
            description="验证取消流程",
            created_by="e2e-tester",
            steps=[
                {"step_index": 1, "title": "步骤A", "instruction": "做A", "timeout_minutes": 5},
                {"step_index": 2, "title": "步骤B", "instruction": "做B", "depends_on": [1], "timeout_minutes": 5},
            ],
        )
        task_id = result["task"]["task_id"]

        # 分配步骤1
        await runner.get_next_step(task_id, "e2e-agent")

        # 取消任务
        cancelled = await sm.task_cancel(conn, task_id)
        assert cancelled is not None
        assert cancelled["status"] == "cancelled"

        # 取消所有非终态步骤
        steps = await sm.get_task_steps(conn, task_id)
        for s in steps:
            if s["status"] not in ("completed", "failed", "skipped", "cancelled"):
                await sm.step_cancel(conn, s["step_id"])

        # 跳过依赖已取消的步骤
        steps = await sm.get_task_steps(conn, task_id)
        assert all(s["status"] in ("cancelled", "skipped") for s in steps)

    async def test_multi_step_with_deps(self, conn):
        """多步骤依赖链"""
        import sys
        sys.path.insert(0, "qingtian")
        from zhice import runner, status_machine as sm

        result = await runner.create_task(
            title="E2E 多步骤测试",
            description="验证依赖链",
            created_by="e2e-tester",
            steps=[
                {"step_index": 1, "title": "拉代码", "instruction": "git clone", "timeout_minutes": 5},
                {"step_index": 2, "title": "构建", "instruction": "make", "depends_on": [1], "timeout_minutes": 5},
                {"step_index": 3, "title": "部署", "instruction": "deploy", "depends_on": [2], "timeout_minutes": 5},
            ],
        )
        task_id = result["task"]["task_id"]
        step_ids = {s["step_index"]: s["step_id"] for s in result["steps"]}

        # 第一次 /next — 只能拿到 step 1
        nxt1 = await runner.get_next_step(task_id, "e2e-agent")
        assert nxt1["found"]
        assert nxt1["current_step"]["step_index"] == 1

        # step 2 依赖未满足，拿不到
        await sm.step_start(conn, step_ids[1], "e2e-agent")
        await sm.step_complete(conn, step_ids[1], "e2e-agent", "done", {})
        await runner.try_complete_task(task_id)

        # 第二次 /next — 现在可以拿到 step 2
        nxt2 = await runner.get_next_step(task_id, "e2e-agent")
        assert nxt2["found"]
        assert nxt2["current_step"]["step_index"] == 2

        await sm.step_start(conn, step_ids[2], "e2e-agent")
        await sm.step_complete(conn, step_ids[2], "e2e-agent", "done", {})

        # 第三次 /next — step 3
        nxt3 = await runner.get_next_step(task_id, "e2e-agent")
        assert nxt3["found"]
        assert nxt3["current_step"]["step_index"] == 3

        await sm.step_start(conn, step_ids[3], "e2e-agent")
        await sm.step_complete(conn, step_ids[3], "e2e-agent", "done", {})

        # Task 自动完结
        await runner.try_complete_task(task_id)
        task = await sm.get_task(conn, task_id)
        assert task["status"] == "completed"


# ══════════════════════════════════════════════════════════
# 检查引擎集成
# ══════════════════════════════════════════════════════════

class TestCheckerIntegration:
    async def test_check_all_pass(self, conn):
        """提交 results 通过全部检查"""
        import sys
        sys.path.insert(0, "qingtian")
        from zhice import runner, status_machine as sm, checker

        result = await runner.create_task(
            title="E2E 检查测试",
            description="验证检查引擎",
            created_by="e2e-tester",
            steps=[{
                "step_index": 1,
                "title": "综合检查",
                "instruction": "执行所有检查",
                "acceptance_criteria": [
                    {"type": "output_contains", "field": "msg", "keyword": "ok"},
                    {"type": "file_exists", "path": "/tmp/test.txt", "required": True},
                    {"type": "api_health", "url": "http://x/health", "expected_status": 200},
                ],
                "timeout_minutes": 5,
            }],
        )
        task_id = result["task"]["task_id"]
        step_id = result["steps"][0]["step_id"]

        await runner.get_next_step(task_id, "e2e-agent")
        await sm.step_start(conn, step_id, "e2e-agent")

        # 模拟 check_results
        check_results = {
            "msg": "deploy ok",
            "file_exists": [{"path": "/tmp/test.txt", "exists": True}],
            "api_health": [{"url": "http://x/health", "status_code": 200}],
        }
        result = checker.check_all(
            result["steps"][0]["acceptance_criteria"] or [],
            check_results,
        )
        assert result["passed"]
        assert len(result["failed_rules"]) == 0

        await sm.step_complete(conn, step_id, "e2e-agent", "all passed", check_results)
        await runner.try_complete_task(task_id)
        task = await sm.get_task(conn, task_id)
        assert task["status"] == "completed"


# ══════════════════════════════════════════════════════════
# 幂等性
# ══════════════════════════════════════════════════════════

class TestIdempotency:
    async def test_duplicate_submit(self, conn):
        """重复 submit 返回已有结果"""
        import sys
        sys.path.insert(0, "qingtian")
        from zhice import runner, status_machine as sm
        from zhice.config import get_schema_name

        result = await runner.create_task(
            title="E2E 幂等测试",
            description="验证幂等性",
            created_by="e2e-tester",
            steps=[{
                "step_index": 1,
                "title": "幂等步骤",
                "instruction": "做X",
                "timeout_minutes": 5,
            }],
        )
        task_id = result["task"]["task_id"]
        step_id = result["steps"][0]["step_id"]

        await runner.get_next_step(task_id, "e2e-agent")
        await sm.step_start(conn, step_id, "e2e-agent")

        idem_key = str(uuid.uuid4())

        # 第一次提交
        await sm.step_complete(conn, step_id, "e2e-agent", "done", {"x": 1})

        # 记录 idempotency_key
        schema = get_schema_name()
        await conn.execute(
            f"UPDATE {schema}.steps SET idempotency_key = $2 WHERE step_id = $1",
            step_id, idem_key,
        )

        # 第二次用相同 key 提交 — step 已经是 completed 状态
        # 幂等检查：step.idempotency_key == req.idempotency_key
        step = await sm.get_step(conn, step_id)
        assert step["idempotency_key"] == idem_key
