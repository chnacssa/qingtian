"""执策自动分解测试 — llm_decompose_task + _validate_decomposed_steps"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# 所有需要真实 LLM API 的测试都必须 mock API key
FAKE_API_KEY = "sk-test-decompose-key"


def _patch_api_key():
    """确保 llm_decompose_task 不会因为缺 API key 而跳过"""
    return patch("zhice.runner.cfg.get_llm_api_key", return_value=FAKE_API_KEY)


# ══════════════════════════════════════════════════════════
# _validate_decomposed_steps — 纯函数，无需 mock
# ══════════════════════════════════════════════════════════

from zhice.runner import _validate_decomposed_steps


class TestValidateDecomposedSteps:
    def test_valid_minimal_single_step(self):
        steps = [{"step_index": 1, "title": "Do X", "instruction": "Run X", "exec_type": "shell"}]
        assert _validate_decomposed_steps(steps) is None

    def test_valid_multi_step_with_deps(self):
        steps = [
            {"step_index": 1, "title": "Step A", "instruction": "Do A", "exec_type": "shell", "depends_on": []},
            {"step_index": 2, "title": "Step B", "instruction": "Do B", "exec_type": "shell", "depends_on": [1]},
            {"step_index": 3, "title": "Step C", "instruction": "Do C", "exec_type": "shell", "depends_on": [2]},
        ]
        assert _validate_decomposed_steps(steps) is None

    def test_valid_with_acceptance_criteria(self):
        steps = [{
            "step_index": 1,
            "title": "Deploy",
            "instruction": "Deploy to production",
            "exec_type": "shell",
            "acceptance_criteria": [
                {"type": "api_health", "url": "http://localhost/health", "expected_status": 200}
            ],
            "timeout_minutes": 10,
        }]
        assert _validate_decomposed_steps(steps) is None

    def test_empty_steps(self):
        assert _validate_decomposed_steps([]) is not None

    def test_not_list(self):
        assert _validate_decomposed_steps({"steps": []}) is not None

    def test_missing_required_field(self):
        steps = [{"step_index": 1, "title": "Do X"}]  # 缺 instruction
        err = _validate_decomposed_steps(steps)
        assert err is not None
        assert "instruction" in err

    def test_duplicate_step_index(self):
        steps = [
            {"step_index": 1, "title": "A", "instruction": "Do A", "exec_type": "shell"},
            {"step_index": 1, "title": "B", "instruction": "Do B", "exec_type": "shell"},
        ]
        assert _validate_decomposed_steps(steps) is not None

    def test_invalid_step_index_zero(self):
        steps = [{"step_index": 0, "title": "X", "instruction": "Do X", "exec_type": "shell"}]
        assert _validate_decomposed_steps(steps) is not None

    def test_invalid_depends_on_self(self):
        steps = [{"step_index": 1, "title": "X", "instruction": "Do X", "exec_type": "shell", "depends_on": [1]}]
        assert _validate_decomposed_steps(steps) is not None

    def test_invalid_depends_on_greater(self):
        steps = [
            {"step_index": 1, "title": "A", "instruction": "Do A", "exec_type": "shell", "depends_on": [2]},
            {"step_index": 2, "title": "B", "instruction": "Do B", "exec_type": "shell"},
        ]
        assert _validate_decomposed_steps(steps) is not None

    def test_empty_title(self):
        steps = [{"step_index": 1, "title": "", "instruction": "Do X", "exec_type": "shell"}]
        assert _validate_decomposed_steps(steps) is not None

    def test_empty_instruction(self):
        steps = [{"step_index": 1, "title": "X", "instruction": "  ", "exec_type": "shell"}]
        assert _validate_decomposed_steps(steps) is not None

    def test_title_too_long(self):
        steps = [{"step_index": 1, "title": "X" * 300, "instruction": "Do X", "exec_type": "shell"}]
        assert _validate_decomposed_steps(steps) is not None

    def test_too_many_steps(self):
        steps = [
            {"step_index": i, "title": f"S{i}", "instruction": f"Do {i}", "exec_type": "shell"}
            for i in range(1, 22)  # 21 > MAX_DECOMPOSE_STEPS(20)
        ]
        assert _validate_decomposed_steps(steps) is not None

    def test_acceptance_criteria_missing_type(self):
        steps = [{
            "step_index": 1,
            "title": "X",
            "instruction": "Do X",
            "exec_type": "shell",
            "acceptance_criteria": [{"field": "result"}],  # 缺 type
        }]
        assert _validate_decomposed_steps(steps) is not None


# ══════════════════════════════════════════════════════════
# llm_decompose_task — mock httpx
# ══════════════════════════════════════════════════════════

SAMPLE_DECOMPOSE_RESPONSE = [
    {"step_index": 1, "title": "拉取代码", "instruction": "git pull origin master", "exec_type": "shell",
     "depends_on": [], "acceptance_criteria": [{"type": "file_exists", "path": "/opt/app/main.py", "required": True}],
     "timeout_minutes": 5},
    {"step_index": 2, "title": "重启服务", "instruction": "systemctl restart myapp", "exec_type": "shell",
     "depends_on": [1], "timeout_minutes": 3},
]


class FakeResponse:
    def __init__(self, status=200, content=""):
        self._status = status
        self._content = content

    def raise_for_status(self):
        if self._status >= 400:
            raise Exception(f"HTTP {self._status}")

    def json(self):
        return {
            "choices": [{"message": {"content": self._content}}],
        }


@pytest.mark.asyncio
async def test_llm_decompose_success():
    """LLM 返回合法 JSON → 成功解析"""
    fake_resp = FakeResponse(content=json.dumps(SAMPLE_DECOMPOSE_RESPONSE))
    with _patch_api_key():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_resp
            from zhice.runner import llm_decompose_task
            steps, err = await llm_decompose_task("部署应用", "部署应用到生产环境")
    assert err is None
    assert steps is not None
    assert len(steps) == 2
    assert steps[0]["title"] == "拉取代码"


@pytest.mark.asyncio
async def test_llm_decompose_single_step():
    """简单任务 → 1 个步骤"""
    single_step = [{"step_index": 1, "title": "PING", "instruction": "运行 ping 测试", "exec_type": "shell",
                     "acceptance_criteria": [{"type": "output_contains", "field": "result", "keyword": "ok"}]}]
    fake_resp = FakeResponse(content=json.dumps(single_step))
    with _patch_api_key():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_resp
            from zhice.runner import llm_decompose_task
            steps, err = await llm_decompose_task("检查状态", "检查服务状态")
    assert err is None
    assert len(steps) == 1


@pytest.mark.asyncio
async def test_llm_decompose_json_with_markdown_fence():
    """LLM 返回 markdown 代码块包裹的 JSON"""
    content = "```json\n" + json.dumps(SAMPLE_DECOMPOSE_RESPONSE) + "\n```"
    fake_resp = FakeResponse(content=content)
    with _patch_api_key():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_resp
            from zhice.runner import llm_decompose_task
            steps, err = await llm_decompose_task("部署", "部署")
    assert err is None
    assert len(steps) == 2


@pytest.mark.asyncio
async def test_llm_decompose_invalid_json():
    """LLM 返回非法 JSON → 返回错误"""
    fake_resp = FakeResponse(content="这不是 JSON 格式")
    with _patch_api_key():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_resp
            from zhice.runner import llm_decompose_task
            steps, err = await llm_decompose_task("X", "Y")
    assert steps is None
    assert err is not None
    assert "非 JSON" in err


@pytest.mark.asyncio
async def test_llm_decompose_validation_fails():
    """LLM 返回合法 JSON 但 depends_on 引用自身 → 校验失败"""
    bad_steps = [{"step_index": 1, "title": "X", "instruction": "Do X", "exec_type": "shell", "depends_on": [1]}]
    fake_resp = FakeResponse(content=json.dumps(bad_steps))
    with _patch_api_key():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_resp
            from zhice.runner import llm_decompose_task
            steps, err = await llm_decompose_task("X", "Y")
    assert steps is None
    assert err is not None
    assert "校验失败" in err


@pytest.mark.asyncio
async def test_llm_decompose_timeout():
    """LLM 调用超时 → 返回错误"""
    import httpx
    with _patch_api_key():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.TimeoutException("timeout")
            from zhice.runner import llm_decompose_task
            steps, err = await llm_decompose_task("X", "Y")
    assert steps is None
    assert "超时" in err


@pytest.mark.asyncio
async def test_llm_decompose_no_api_key():
    """没有 API Key → 返回错误"""
    with patch("zhice.runner.cfg.get_llm_api_key", return_value=""):
        from zhice.runner import llm_decompose_task
        steps, err = await llm_decompose_task("X", "Y")
    assert steps is None
    assert "API Key" in err


@pytest.mark.asyncio
async def test_llm_decompose_dict_wrapped():
    """LLM 返回 {"steps": [...]} 包装格式"""
    content = json.dumps({"steps": SAMPLE_DECOMPOSE_RESPONSE})
    fake_resp = FakeResponse(content=content)
    with _patch_api_key():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = fake_resp
            from zhice.runner import llm_decompose_task
            steps, err = await llm_decompose_task("部署", "部署应用")
    assert err is None
    assert len(steps) == 2


# ══════════════════════════════════════════════════════════
# create_task — 自动分解入口集成测试
# ══════════════════════════════════════════════════════════

def _make_pool(mock_conn):
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire.return_value = ctx
    return pool


def _task_dict(**overrides):
    """构建一个类似 asyncpg Row 的 task dict"""
    d = {
        "task_id": 99, "title": "T", "description": "D", "priority": "P2",
        "status": "pending", "created_by": "master",
        "participants": ["master"], "acceptance_criteria": None,
        "expected_outputs": None, "timeout_minutes": 60,
        "workflow_id": None, "workflow_version": None,
        "progress": 0, "result": None,
        "started_at": None, "completed_at": None,
        "created_at": None, "updated_at": None,
    }
    d.update(overrides)
    return _dict_row(d)


def _step_dict(**overrides):
    """构建一个类似 asyncpg Row 的 step dict"""
    d = {
        "step_id": 1, "task_id": 99, "step_index": 1, "title": "S",
        "instruction": "Do S", "status": "pending", "status_reason": None,
        "assigned_agent": None, "assigned_at": None, "depends_on": [],
        "acceptance_criteria": None, "expected_outputs": None,
        "outputs": None, "summary": None, "auto_retry": 0,
        "timeout_minutes": 30, "idempotency_key": None,
        "last_heartbeat_at": None, "started_at": None,
        "completed_at": None, "created_at": None, "updated_at": None,
    }
    d.update(overrides)
    return _dict_row(d)


def _dict_row(d: dict):
    """将普通 dict 包装成支持 get/__getitem__/keys 的 mock row"""
    return d  # asyncpg Row 可直接用 dict


@pytest.mark.asyncio
async def test_create_task_auto_decompose_success():
    """不传 steps 和 workflow_id → LLM 自动分解 → 创建成功"""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_make_transaction_ctx())
    # fetchrow 依次返回: task INSERT, step1 INSERT, step2 INSERT
    conn.fetchrow = AsyncMock(side_effect=[
        _task_dict(),
        _step_dict(step_index=1, title="检查状态"),
        _step_dict(step_id=2, step_index=2, title="重启"),
    ])
    conn.execute = AsyncMock()

    fake_resp = FakeResponse(content=json.dumps([
        {"step_index": 1, "title": "检查状态", "instruction": "执行 health check", "exec_type": "shell",
         "acceptance_criteria": [{"type": "output_contains", "field": "status", "keyword": "ok"}]},
        {"step_index": 2, "title": "重启", "instruction": "执行重启", "exec_type": "shell", "depends_on": [1]},
    ]))

    with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(conn))):
        with _patch_api_key():
            with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
                mock_post.return_value = fake_resp
                from zhice import runner
                result = await runner.create_task(
                    title="部署新版本",
                    description="将新版本部署到生产环境并验证",
                    priority="P2",
                    created_by="master",
                    steps=[],
                )
    assert result["success"] is True
    assert result["mode"] == "complex"
    assert mock_post.called


@pytest.mark.asyncio
async def test_create_task_auto_decompose_fallback():
    """LLM 分解失败 → 返回 error"""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_make_transaction_ctx())
    conn.fetchrow = AsyncMock()

    with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(conn))):
        with patch("zhice.runner._check_clarity", new_callable=AsyncMock) as mock_clarity:
            mock_clarity.return_value = {"needs_clarification": False}
            with patch("zhice.runner.llm_decompose_task", new_callable=AsyncMock) as mock_decomp:
                mock_decomp.return_value = (None, "模拟 LLM 错误")
                from zhice import runner
                result = await runner.create_task(
                    title="X", description="Y", priority="P2",
                    created_by="master", steps=[],
                )
    assert result["success"] is False
    assert "自动分解失败" in result["error"]


@pytest.mark.asyncio
async def test_create_task_with_steps_no_decompose():
    """传了 steps → 不触发自动分解（直接使用）"""
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_make_transaction_ctx())
    conn.fetchrow = AsyncMock(side_effect=[
        _task_dict(),
        _step_dict(step_index=1, title="手动步骤"),
    ])
    conn.execute = AsyncMock()

    with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(conn))):
        with patch("zhice.runner.llm_decompose_task", new_callable=AsyncMock) as mock_decomp:
            from zhice import runner
            result = await runner.create_task(
                title="X", description="Y", priority="P2",
                created_by="master",
                steps=[{"step_index": 1, "title": "手动步骤", "instruction": "手动执行", "exec_type": "shell"}],
            )
    assert result["success"] is True
    mock_decomp.assert_not_called()


@pytest.mark.asyncio
async def test_create_task_auto_decompose_empty_result():
    """LLM 返回空列表 → 返回 error"""
    conn = AsyncMock()
    with patch("zhice.runner.get_pool", AsyncMock(return_value=_make_pool(conn))):
        with patch("zhice.runner._check_clarity", new_callable=AsyncMock) as mock_clarity:
            mock_clarity.return_value = {"needs_clarification": False}
            with patch("zhice.runner.llm_decompose_task", new_callable=AsyncMock) as mock_decomp:
                mock_decomp.return_value = ([], None)
                from zhice import runner
                result = await runner.create_task(
                    title="X", description="Y", priority="P2",
                    created_by="master", steps=[],
                )
    assert result["success"] is False
    assert "空步骤" in result["error"]


def _make_transaction_ctx():
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock()
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx
