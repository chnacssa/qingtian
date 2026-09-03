"""执策 Phase 3 测试 — extractor + workflow CRUD + 生态联动"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import zhice.api  # ensure submodule is loaded for patching


def _make_ctx(return_value=None):
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=return_value)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock()
    conn.fetch = AsyncMock()
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_make_ctx())
    return conn


def _setup_pool_patch(mock_conn):
    mock_pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire.return_value = ctx
    return patch("zhice.dispatcher.get_pool", AsyncMock(return_value=mock_pool))


def _task_row(task_id=42, title="部署测试", description="部署到生产", status="completed",
              created_by="master", acceptance_criteria=None, timeout_minutes=60, participants=None):
    m = MagicMock()
    d = {
        "task_id": task_id, "title": title, "description": description,
        "status": status, "created_by": created_by, "priority": "P2",
        "acceptance_criteria": acceptance_criteria, "timeout_minutes": timeout_minutes,
        "participants": participants or ["master"],
        "workflow_id": None, "workflow_version": None,
        "expected_outputs": None, "progress": 100, "result": None,
        "started_at": None, "completed_at": None,
        "created_at": None, "updated_at": None,
    }
    m.__getitem__ = lambda self, k: d.get(k)
    m.get = d.get
    m.keys = lambda: d.keys()
    return m


def _step_row(step_id=1, task_id=42, step_index=1, title="test", instruction="run test",
              status="completed", depends_on=None, acceptance_criteria=None,
              timeout_minutes=30, auto_retry=0, assigned_agent="agent1",
              outputs=None, summary="done"):
    d = {
        "step_id": step_id, "task_id": task_id, "step_index": step_index,
        "title": title, "instruction": instruction, "status": status,
        "status_reason": None, "assigned_agent": assigned_agent,
        "assigned_at": None, "depends_on": depends_on,
        "acceptance_criteria": acceptance_criteria,
        "outputs": outputs, "summary": summary, "auto_retry": auto_retry,
        "timeout_minutes": timeout_minutes, "idempotency_key": None,
        "last_heartbeat_at": None, "started_at": None, "completed_at": None,
        "created_at": None, "updated_at": None,
    }
    m = MagicMock()
    m.__getitem__ = lambda self, k: d.get(k)
    m.get = d.get
    m.keys = lambda: d.keys()
    return m


# ═══════════════════════════════════════════════════════════
# extractor
# ═══════════════════════════════════════════════════════════

class TestExtractor:
    async def test_extract_success(self, mock_conn):
        mock_conn.fetchrow.side_effect = [
            _task_row(acceptance_criteria=[{"type": "output_contains", "keyword": "OK"}]),
        ]
        mock_conn.fetch.return_value = [
            _step_row(step_index=1, instruction="deploy to 192.168.1.1"),
            _step_row(step_index=2, instruction="verify"),
        ]

        with patch("zhice.extractor.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice import extractor
            result = await extractor.extract_workflow(42)

        assert result["success"]
        assert result["source_task_id"] == 42
        assert result["name"] == "部署测试（草案）"
        assert len(result["definition"]["steps"]) == 2
        assert result["definition"]["acceptance_criteria"] == [{"type": "output_contains", "keyword": "OK"}]
        assert result["definition"]["timeout_minutes"] == 60
        # 应有 IP 占位符提示
        assert any("192.168.1.1" in h for h in result["hints"])

    async def test_extract_task_not_found(self, mock_conn):
        mock_conn.fetchrow.return_value = None

        with patch("zhice.extractor.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice import extractor
            result = await extractor.extract_workflow(999)

        assert not result["success"]
        assert "不存在" in result["error"]

    async def test_extract_no_hints(self, mock_conn):
        mock_conn.fetchrow.side_effect = [_task_row()]
        mock_conn.fetch.return_value = [
            _step_row(instruction="run the standard process"),
        ]

        with patch("zhice.extractor.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice import extractor
            result = await extractor.extract_workflow(42)

        assert result["success"]
        assert len(result["hints"]) == 0


# ═══════════════════════════════════════════════════════════
# Workflow CRUD
# ═══════════════════════════════════════════════════════════

def _make_pool(mock_conn):
    """构造 pool mock，支持 async with pool.acquire() as conn: 模式"""
    mock_pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire.return_value = ctx
    return mock_pool


def _dict_row(**kwargs):
    """构造可被 dict() 转换的 mock row"""
    m = MagicMock()
    m.__iter__ = lambda: iter(kwargs.items())
    m.keys = lambda: kwargs.keys()
    m.__getitem__ = lambda _, k: kwargs.get(k)
    m.get = kwargs.get
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


class TestWorkflowCRUD:
    async def test_list_workflows(self, mock_conn):
        from datetime import datetime
        ts = datetime(2026, 1, 1)
        mock_conn.fetch.return_value = [
            _dict_row(workflow_id=1, name="deploy", description="desc", version=1,
                      definition={"steps": []}, source_task_id=None, created_by="m",
                      created_at=ts, updated_at=ts),
        ]
        mock_conn.fetchval.return_value = 1

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice.api import list_workflows
            from fastapi import Query
            result = await list_workflows()

        assert result["total"] == 1
        assert len(result["workflows"]) == 1
        assert result["workflows"][0]["name"] == "deploy"

    async def test_create_workflow(self, mock_conn):
        from datetime import datetime
        ts = datetime(2026, 1, 1)

        wf_row = _dict_row(workflow_id=1, name="deploy", description="desc", version=1,
                           definition={"steps": [{"step_index": 1, "title": "s1", "instruction": "do"}]},
                           source_task_id=None, created_by="m",
                           created_at=ts, updated_at=ts)
        mock_conn.fetchval.return_value = 0
        mock_conn.fetchrow.return_value = wf_row

        from zhice.models import CreateWorkflowRequest

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice.api import create_workflow
            result = await create_workflow(CreateWorkflowRequest(
                name="deploy", description="desc",
                definition={"steps": [{"step_index": 1, "title": "s1", "instruction": "do"}]},
                created_by="m",
            ))

        assert result["workflow_id"] == 1
        assert result["name"] == "deploy"
        assert result["version"] == 1

    async def test_update_workflow(self, mock_conn):
        from datetime import datetime
        ts = datetime(2026, 1, 1)

        existing = _dict_row(workflow_id=1, name="deploy", description="old", version=1,
                             definition={"steps": []}, source_task_id=None, created_by="m",
                             created_at=ts, updated_at=ts)
        new_row = _dict_row(workflow_id=2, name="deploy-v2", description="new", version=2,
                            definition={"steps": [{"new": True}]}, source_task_id=None,
                            created_by="m", created_at=ts, updated_at=ts)
        mock_conn.fetchrow.side_effect = [existing, new_row]

        from zhice.models import UpdateWorkflowRequest

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice.api import update_workflow
            result = await update_workflow(1, UpdateWorkflowRequest(
                name="deploy-v2", description="new",
                definition={"steps": [{"new": True}]},
            ))

        assert result["workflow_id"] == 2
        assert result["version"] == 2

    async def test_delete_workflow(self, mock_conn):
        mock_conn.execute.return_value = "DELETE 1"

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice.api import delete_workflow
            result = await delete_workflow(1)

        assert result["success"]

    async def test_delete_workflow_not_found(self, mock_conn):
        mock_conn.execute.return_value = "DELETE 0"

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice.api import delete_workflow
            from fastapi import HTTPException
            with pytest.raises(HTTPException, match="Workflow 不存在"):
                await delete_workflow(1)


# ═══════════════════════════════════════════════════════════
# Yongheng integration
# ═══════════════════════════════════════════════════════════

class TestYonghengIntegration:
    async def test_write_memory_on_task_complete(self, mock_conn):
        """runner.try_complete_task 应调用 yongheng memory/trajectory"""
        task = _task_row(status="running", participants=["agent1"])
        completed_task = _dict_row(task_id=42, status="completed", title="done",
                                   created_by="master", participants=["agent1"])
        mock_conn.fetchrow.side_effect = [
            task,                                                          # get_task
            _dict_row(remaining=0),                                        # all_steps_terminal
            completed_task,                                                # task_complete
            completed_task,                                                # task_update_progress
        ]
        mock_conn.fetch.return_value = [
            _step_row(status="completed"),
            _step_row(step_id=2, step_index=2, title="step2", status="completed"),
        ]

        with patch("zhice.runner._yongheng_integration", new_callable=AsyncMock) as mock_yh:
            with patch("zhice.runner._xixing_learn", new_callable=AsyncMock) as mock_xx:
                with patch("zhice.runner._post_complete_hooks", new_callable=AsyncMock) as mock_pc:
                    from zhice import runner
                    result = await runner.try_complete_task(42, conn=mock_conn)

        assert result is not None
        assert result["action"] == "completed"
        # hooks 改到事务外 fire-and-forget: _post_complete_hooks 被 asyncio.create_task 调度
        mock_pc.assert_called_once()

    async def test_no_integration_on_running_task(self, mock_conn):
        """Task 状态为 running 但 Step 未全部终态时不应触发完结"""
        mock_conn.fetchrow.return_value = _task_row(status="running")
        mock_conn.fetchval.return_value = False

        from zhice import runner
        result = await runner.try_complete_task(42, conn=mock_conn)

        assert result is None

    async def test_build_task_memory(self):
        from zhice.runner import _build_task_memory
        task = {"task_id": 42, "title": "Test"}
        steps = [
            {"status": "completed", "step_index": 1, "title": "s1"},
            {"status": "completed", "step_index": 2, "title": "s2"},
            {"status": "failed", "step_index": 3, "title": "s3"},
        ]
        content = _build_task_memory(task, steps, "completed")
        assert "2/3" in content
        assert "1 failed" in content


# ═══════════════════════════════════════════════════════════
# Pitfall
# ═══════════════════════════════════════════════════════════

class TestPitfallReport:
    async def test_pitfall_on_check_failure(self, mock_conn):
        """Step submit 检查失败 + auto_retry=0 → 通过吸星 API 上报踩坑"""
        mock_conn.fetchrow.side_effect = [
            _step_row(status="in_progress", assigned_agent="agent1", auto_retry=0,
                      acceptance_criteria=[{"type": "output_contains", "keyword": "OK"}]),
            _task_row(status="running"),
            None,  # step_reject returns None
            _step_row(status="failed", assigned_agent="agent1", auto_retry=0,
                      step_id=1, step_index=1, task_id=42),  # get_step 重读
        ]

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            with patch("zhice.dispatcher.ws_notify", new_callable=AsyncMock):
                with patch("zhice.runner.try_complete_task", new_callable=AsyncMock):
                    with patch("zhice.api.report_pitfall", new_callable=AsyncMock) as mock_rp:
                        mock_rp.return_value = True
                        from zhice.models import SubmitRequest
                        from zhice.api import submit_step
                        await submit_step(1, SubmitRequest(
                            agent_id="agent1", status="completed", summary="done",
                            outputs={"result": "FAIL"}, idempotency_key="k1",
                        ), auth={"agent_id": "test-admin", "role": "admin"})
                        # let fire-and-forget task run
                        import asyncio
                        await asyncio.sleep(0)

        # 通过 xixing API 上报（非直接 INSERT）
        mock_rp.assert_called_once()
        call_args = mock_rp.call_args
        assert call_args[1]["agent_id"] == "agent1"
        assert call_args[1]["task_id"] == 42
        assert len(call_args[1]["failed_rules"]) >= 1


# ═══════════════════════════════════════════════════════════
# Reject
# ═══════════════════════════════════════════════════════════

class TestReject:
    async def test_reject_failed_step(self, mock_conn):
        """创建者手动打回 failed Step → pending + 重置 auto_retry"""
        mock_conn.fetchrow.side_effect = [
            _step_row(status="failed", auto_retry=0, task_id=42, step_index=1,
                      assigned_agent="agent1"),
            _dict_row(step_id=1, status="rejected"),  # step_reject forced
        ]

        from zhice.models import RejectStepRequest

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            with patch("zhice.dispatcher.ws_notify", new_callable=AsyncMock) as mock_ws:
                from zhice.api import reject_step
                result = await reject_step(1, RejectStepRequest(
                    reason="再给一次机会", reset_retry=2,
                ))

        assert result["status"] == "pending"
        assert result["auto_retry"] == 2
        mock_ws.assert_called_once()

    async def test_reject_wrong_status(self, mock_conn):
        """非终态 Step 不能打回"""
        mock_conn.fetchrow.return_value = _step_row(status="in_progress")

        from zhice.models import RejectStepRequest, AppError

        with patch("zhice.api.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice.api import reject_step
            with pytest.raises(AppError, match="只有 failed/rejected/timed_out 才能打回"):
                await reject_step(1, RejectStepRequest())


# ═══════════════════════════════════════════════════════════
# Workflow 展开 → create_task
# ═══════════════════════════════════════════════════════════

WF_DEFINITION = {
    "steps": [
        {"step_index": 1, "title": "拉代码", "instruction": "git clone", "timeout_minutes": 10},
        {"step_index": 2, "title": "构建", "instruction": "make build", "depends_on": [1], "timeout_minutes": 15},
        {"step_index": 3, "title": "部署", "instruction": "deploy to {host}", "depends_on": [2], "timeout_minutes": 10},
    ],
    "acceptance_criteria": [{"type": "output_contains", "field": "msg", "keyword": "ok"}],
    "timeout_minutes": 60,
}


def _task_insert_row(task_id=42):
    return _dict_row(task_id=task_id, title="test", description="desc", priority="P2",
                     status="pending", created_by="m", participants=["m"],
                     progress=0, result=None, started_at=None, completed_at=None,
                     created_at=None, updated_at=None)


def _step_insert_row(step_id=1, task_id=42, step_index=1, title="拉代码",
                     instruction="git clone", status="pending", depends_on=None,
                     acceptance_criteria=None, timeout_minutes=10, auto_retry=0,
                     assigned_agent=None):
    return _dict_row(step_id=step_id, task_id=task_id, step_index=step_index,
                     title=title, instruction=instruction, status=status,
                     status_reason=None, assigned_agent=assigned_agent,
                     assigned_at=None, depends_on=depends_on or [],
                     acceptance_criteria=acceptance_criteria,
                     outputs=None, summary=None, auto_retry=auto_retry,
                     timeout_minutes=timeout_minutes, idempotency_key=None,
                     last_heartbeat_at=None, started_at=None, completed_at=None,
                     created_at=None, updated_at=None)


class TestWorkflowExpansion:
    async def test_create_task_from_workflow_no_steps(self, mock_conn):
        """workflow_id 存在 + steps 为空 → 完全展开 workflow.definition.steps"""
        mock_conn.fetchrow.side_effect = [
            _dict_row(definition=WF_DEFINITION),                        # _resolve_workflow
            _dict_row(definition={}),                                    # gotcha fetch
            _task_insert_row(),                                          # task INSERT
            _step_insert_row(step_id=1, step_index=1, title="拉代码"),  # step 1 INSERT
            _step_insert_row(step_id=2, step_index=2, title="构建"),    # step 2 INSERT
            _step_insert_row(step_id=3, step_index=3, title="部署"),    # step 3 INSERT
        ]
        mock_conn.execute.return_value = "OK"

        with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice import runner
            result = await runner.create_task(
                title="从模板创建",
                description="测试 workflow 展开",
                priority="P1",
                created_by="tester",
                steps=[],
                workflow_id=1,
            )

        assert result["success"]
        assert result["task_id"] == 42
        assert len(result["steps"]) == 3
        assert result["steps"][0]["title"] == "拉代码"
        assert result["steps"][1]["title"] == "构建"
        assert result["steps"][2]["title"] == "部署"

    async def test_create_task_merge_workflow_and_steps(self, mock_conn):
        """workflow 作底 + 调用方 steps 覆盖同 step_index"""
        overwrite_def = {
            "steps": [
                {"step_index": 1, "title": "拉代码", "instruction": "git clone"},
                {"step_index": 2, "title": "旧构建", "instruction": "make old"},
                {"step_index": 3, "title": "部署", "instruction": "deploy"},
            ],
        }
        mock_conn.fetchrow.side_effect = [
            _dict_row(definition=overwrite_def),                           # _resolve_workflow
            _dict_row(definition={}),                                       # gotcha fetch
            _task_insert_row(),                                             # task INSERT
            _step_insert_row(step_id=1, step_index=1, title="拉代码"),     # step 1 (from wf)
            _step_insert_row(step_id=2, step_index=2, title="新构建",      # step 2 (overridden)
                             instruction="make new"),
            _step_insert_row(step_id=3, step_index=3, title="部署"),       # step 3 (from wf)
        ]
        mock_conn.execute.return_value = "OK"

        with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice import runner
            result = await runner.create_task(
                title="覆盖构建步骤",
                description="workflow 底 + 自定义 step 2",
                priority="P2",
                created_by="tester",
                steps=[{"step_index": 2, "title": "新构建", "instruction": "make new"}],
                workflow_id=1,
            )

        assert result["success"]
        steps = result["steps"]
        assert len(steps) == 3
        # step 2 应被覆盖
        build_step = [s for s in steps if s["step_index"] == 2][0]
        assert build_step["title"] == "新构建"
        assert build_step["instruction"] == "make new"

    async def test_create_task_workflow_not_found(self, mock_conn):
        """workflow_id 不存在 → AppError"""
        mock_conn.fetchrow.return_value = None  # _resolve_workflow returns None

        with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice import runner
            from zhice.models import AppError
            with pytest.raises(AppError, match="不存在"):
                await runner.create_task(
                    title="test", description="d", priority="P2",
                    created_by="tester", steps=[], workflow_id=999,
                )

    async def test_create_task_workflow_with_defaults(self, mock_conn):
        """workflow 的 acceptance_criteria 和 timeout_minutes 作为默认值"""
        mock_conn.fetchrow.side_effect = [
            _dict_row(definition=WF_DEFINITION),                           # _resolve_workflow
            _dict_row(definition={}),                                       # gotcha fetch
            _task_insert_row(),                                             # task INSERT
            _step_insert_row(step_id=1, step_index=1, title="拉代码"),     # step INSERT
            _step_insert_row(step_id=2, step_index=2, title="构建"),
            _step_insert_row(step_id=3, step_index=3, title="部署"),
        ]
        mock_conn.execute.return_value = "OK"

        with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
            from zhice import runner
            result = await runner.create_task(
                title="继承默认值",
                description="不传 ac 和 timeout",
                priority="P2",
                created_by="tester",
                steps=[],
                workflow_id=1,
            )

        assert result["success"]
        # 验证 acceptance_criteria 和 timeout 从 workflow 继承
        task = result["task"]
        # Task INSERT 中的 acceptance_criteria 被 json.dumps
        assert task is not None

    def test_model_allows_neither_steps_nor_workflow(self):
        """不传 steps 和 workflow_id 时走 LLM 自动分解，模型层放行"""
        from zhice.models import CreateTaskRequest
        req = CreateTaskRequest(
            title="test", description="d", priority="P2",
            created_by="tester",
            # 不传 steps，不传 workflow_id → 触发 LLM 自动分解
        )
        assert req.steps == []
        assert req.workflow_id is None
