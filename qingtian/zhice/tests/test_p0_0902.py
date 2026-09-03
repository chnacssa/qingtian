"""执策 — 9-2 P0-1 修复回归（review 2026-08-30-执策.md 补审实锤）。

  1. daemon 消费四端点 fail-closed 鉴权 + 自报 agent_id 归属断言
     （/tasks/{id}/next、/steps/{id}/start、/steps/{id}/heartbeat、/steps/{id}/submit）
  2. _resolve_exec_type：缺省/无效 exec_type 不再默认 shell → manual
  3. daemon 客户端缺省 exec_type 同步 manual
"""

import pytest
from fastapi import HTTPException

from zhice.api import (
    _require_daemon_agent,
    get_next_step,
    heartbeat,
    start_step,
    submit_step,
)
from zhice.models import HeartbeatRequest, StartStepRequest, SubmitRequest
from zhice.runner import _resolve_exec_type
from zhice import agent_daemon


# ═══════════════════════════════════════════════════════
# 1. _resolve_exec_type — 缺省/无效不再落 shell
# ═══════════════════════════════════════════════════════


def test_exec_type_missing_http_instruction_autodetects_http():
    """缺省 + HTTP 方法前缀 → http（保留既有便利行为）。"""
    assert _resolve_exec_type({"instruction": "GET http://10.0.0.1/api/health"}) == "http"


def test_exec_type_missing_plain_instruction_defaults_manual():
    """P0 核心：缺省 + 普通指令 → manual（原实现落 shell = 远程 RCE 缺省面）。"""
    assert _resolve_exec_type({"instruction": "git pull origin main"}) == "manual"
    assert _resolve_exec_type({"instruction": ""}) == "manual"
    assert _resolve_exec_type({}) == "manual"


def test_exec_type_invalid_value_falls_to_manual():
    """非法值（bash/sh/大写误拼等不合法项）→ manual，绝不透传给 daemon。"""
    assert _resolve_exec_type({"exec_type": "bash", "instruction": "x"}) == "manual"
    assert _resolve_exec_type({"exec_type": "sh", "instruction": "x"}) == "manual"
    assert _resolve_exec_type({"exec_type": "  ", "instruction": "x"}) == "manual"


def test_exec_type_valid_values_case_insensitive():
    """合法值大小写不敏感原样采纳。"""
    assert _resolve_exec_type({"exec_type": "SHELL", "instruction": "x"}) == "shell"
    assert _resolve_exec_type({"exec_type": "Http", "instruction": "x"}) == "http"
    assert _resolve_exec_type({"exec_type": "skill", "instruction": "s:a"}) == "skill"
    assert _resolve_exec_type({"exec_type": "script", "instruction": "run.py"}) == "script"
    assert _resolve_exec_type({"exec_type": "manual", "instruction": "x"}) == "manual"


def test_exec_type_explicit_shell_wins_over_autodetect():
    """显式声明优先：exec_type=shell + HTTP 样指令不再被自动识别改判 http。"""
    assert _resolve_exec_type(
        {"exec_type": "shell", "instruction": "GET http://x"}
    ) == "shell"


# ═══════════════════════════════════════════════════════
# 2. _require_daemon_agent — 归属断言
# ═══════════════════════════════════════════════════════


def test_daemon_auth_admin_passthrough():
    """admin token 放行任意自报 agent（管理监控通道，同 ws 口径）。"""
    _require_daemon_agent({"agent_id": "zt_admin_1", "role": "admin"}, "any-agent")


def test_daemon_auth_internal_ipc_passthrough():
    """内部 IPC（网关注入）放行。"""
    _require_daemon_agent({"agent_id": "internal-ipc", "role": "admin"}, "any-agent")


def test_daemon_auth_matching_agent_with_prefix_normalization():
    """token 归属与自报一致（含平台前缀归一化，如 feishu:ou_xxx vs ou_xxx）→ 放行。"""
    _require_daemon_agent({"agent_id": "feishu:ou_abc", "role": "agent"}, "ou_abc")
    _require_daemon_agent({"agent_id": "foo", "role": "agent"}, "foo")


def test_daemon_auth_mismatch_rejected():
    """持 A 的 token 自报 B → 403（抢/交步骤冒充面）。"""
    with pytest.raises(HTTPException) as ei:
        _require_daemon_agent({"agent_id": "attacker", "role": "agent"}, "victim")
    assert ei.value.status_code == 403


# ═══════════════════════════════════════════════════════
# 3. 端点接线 — 拒绝路径先于 DB（直接调端点函数验 403 早退）
# ═══════════════════════════════════════════════════════

_ATTACKER_AUTH = {"agent_id": "attacker", "role": "agent"}


@pytest.mark.asyncio
async def test_next_step_rejects_mismatched_identity():
    """归属不符 → 403 且不触引擎（不碰 DB）。"""
    with pytest.raises(HTTPException) as ei:
        await get_next_step(1, "victim", auth=_ATTACKER_AUTH)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_start_step_rejects_mismatched_identity():
    with pytest.raises(HTTPException) as ei:
        await start_step(1, StartStepRequest(agent_id="victim"), auth=_ATTACKER_AUTH)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_heartbeat_rejects_mismatched_identity():
    with pytest.raises(HTTPException) as ei:
        await heartbeat(
            1, HeartbeatRequest(agent_id="victim"), auth=_ATTACKER_AUTH,
        )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_submit_step_rejects_mismatched_identity():
    with pytest.raises(HTTPException) as ei:
        await submit_step(
            1,
            SubmitRequest(agent_id="victim", idempotency_key="k1"),
            auth=_ATTACKER_AUTH,
        )
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_next_step_admin_passes_gate_to_engine(monkeypatch):
    """admin 放行后正常进引擎（mock 引擎返回 found=False 证接线通）。"""
    async def _fake_next(task_id, agent_id):
        return {"found": False, "task_status": "running", "progress": "",
                "upcoming_steps": []}

    monkeypatch.setattr("zhice.api.runner.get_next_step", _fake_next)
    resp = await get_next_step(
        7, "some-agent", auth={"agent_id": "zt_admin_1", "role": "admin"},
    )
    assert resp.current_step is None
    assert resp.task_status == "running"


# ═══════════════════════════════════════════════════════
# 4. daemon 客户端缺省
# ═══════════════════════════════════════════════════════


def test_daemon_client_default_exec_type_is_manual():
    """agent_daemon 分发前的缺省值必须是 manual（纵深防御）。"""
    import inspect
    src = inspect.getsource(agent_daemon)
    assert 'step.get("exec_type", "manual")' in src
    assert 'step.get("exec_type", "shell")' not in src
