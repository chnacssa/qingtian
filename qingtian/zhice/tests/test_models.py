"""执策 Pydantic 模型验证测试"""
import pytest
from zhice.models import (
    AppError,
    CreateTaskRequest,
    StartStepRequest,
    HeartbeatRequest,
    SubmitRequest,
    IssueRequest,
    StepDef,
    AcceptanceCriterion,
    VALID_CHECK_TYPES,
)


class TestAppError:
    def test_basic(self):
        err = AppError("TEST", "msg", 400)
        assert err.code == "TEST"
        assert err.message == "msg"
        assert err.status == 400

    def test_default_status(self):
        err = AppError("X", "y")
        assert err.status == 400

    def test_str(self):
        err = AppError("A", "hello")
        assert str(err) == "hello"


class TestAcceptanceCriterion:
    def test_output_contains(self):
        ac = AcceptanceCriterion(type="output_contains", field="result", keyword="PONG")
        assert ac.type == "output_contains"
        assert ac.field == "result"

    def test_file_exists(self):
        ac = AcceptanceCriterion(type="file_exists", path="/tmp/x", required=True)
        assert ac.path == "/tmp/x"
        assert ac.required is True

    def test_api_health(self):
        ac = AcceptanceCriterion(type="api_health", url="http://x/health", expected_status=200)
        assert ac.expected_status == 200

    def test_invalid_type_allowed_by_field(self):
        """Pydantic 不校验 type 枚举——由 checker 层校验"""
        ac = AcceptanceCriterion(type="bad_type")
        assert ac.type == "bad_type"


class TestStepDef:
    def test_minimal(self):
        s = StepDef(step_index=1, title="测试", instruction="做某事")
        assert s.step_index == 1
        assert s.auto_retry == 0
        assert s.depends_on is None

    def test_with_deps(self):
        s = StepDef(step_index=3, title="步骤3", instruction="做", depends_on=[1, 2])
        assert s.depends_on == [1, 2]

    def test_auto_retry_bounds(self):
        s = StepDef(step_index=1, title="x", instruction="y", auto_retry=5)
        assert s.auto_retry == 5

        with pytest.raises(Exception):
            StepDef(step_index=1, title="x", instruction="y", auto_retry=11)

    def test_step_index_positive(self):
        with pytest.raises(Exception):
            StepDef(step_index=0, title="x", instruction="y")

    def test_empty_title(self):
        with pytest.raises(Exception):
            StepDef(step_index=1, title="", instruction="y")


class TestCreateTaskRequest:
    def test_simple(self):
        req = CreateTaskRequest(
            title="测试任务",
            description="描述",
            created_by="小智",
            steps=[StepDef(step_index=1, title="第一步", instruction="做X")],
        )
        assert req.priority == "P2"
        assert len(req.steps) == 1

    def test_complex(self):
        req = CreateTaskRequest(
            title="复杂任务",
            description="多步骤",
            created_by="小智",
            priority="P1",
            steps=[
                StepDef(step_index=1, title="拉代码", instruction="git clone"),
                StepDef(step_index=2, title="部署", instruction="deploy", depends_on=[1]),
                StepDef(step_index=3, title="验证", instruction="test", depends_on=[2]),
            ],
            timeout_minutes=60,
        )
        assert req.priority == "P1"
        assert len(req.steps) == 3
        assert req.timeout_minutes == 60

    def test_no_steps(self):
        """不传 steps 和 workflow_id 时走 LLM 自动分解，模型层不拒绝"""
        req = CreateTaskRequest(title="x", description="y", created_by="z", steps=[])
        assert req.steps == []
        assert req.workflow_id is None

    def test_invalid_priority(self):
        with pytest.raises(Exception):
            CreateTaskRequest(
                title="x", description="y", created_by="z",
                steps=[StepDef(step_index=1, title="a", instruction="b")],
                priority="P5",
            )

    def test_duplicate_step_indices(self):
        """允许创建时 step_index 重复——runner 层校验"""
        req = CreateTaskRequest(
            title="x", description="y", created_by="z",
            steps=[
                StepDef(step_index=1, title="a", instruction="b"),
                StepDef(step_index=1, title="c", instruction="d"),
            ],
        )
        assert len(req.steps) == 2


class TestHeartbeatRequest:
    def test_default(self):
        req = HeartbeatRequest(agent_id="小智")
        assert req.status_reason == "executing"

    def test_waiting_input(self):
        req = HeartbeatRequest(agent_id="小智", status_reason="waiting_input")
        assert req.status_reason == "waiting_input"

    def test_blocked(self):
        req = HeartbeatRequest(agent_id="小智", status_reason="blocked")
        assert req.status_reason == "blocked"

    def test_invalid_reason(self):
        with pytest.raises(Exception):
            HeartbeatRequest(agent_id="小智", status_reason="unknown")


class TestSubmitRequest:
    def test_minimal(self):
        req = SubmitRequest(
            agent_id="小智",
            idempotency_key="550e8400-e29b-41d4-a716-446655440000",
        )
        assert req.status == "completed"

    def test_failed(self):
        req = SubmitRequest(
            agent_id="小智",
            status="failed",
            summary="执行失败",
            idempotency_key="key-123",
        )
        assert req.status == "failed"


class TestIssueRequest:
    def test_valid_types(self):
        for t in ("blocked_by_dependency", "need_clarification", "resource_insufficient"):
            req = IssueRequest(agent_id="x", issue_type=t, description="desc")
            assert req.issue_type == t

    def test_invalid_type(self):
        with pytest.raises(Exception):
            IssueRequest(agent_id="x", issue_type="bad", description="desc")


class TestStartStepRequest:
    def test_ok(self):
        req = StartStepRequest(agent_id="小智")
        assert req.agent_id == "小智"

    def test_empty_agent(self):
        with pytest.raises(Exception):
            StartStepRequest(agent_id="")
