"""CognitionRunner 单测（实施文档 §7.1 T1-T7）

llm_call 用假 LLM 注入，不依赖底座，纯内存可跑。
T7 测 llm_chat 的 cot 分支，mock 掉真实 LLM 调用。
"""

import pytest

import common.llm as llm_mod
from common.cognition import (CognitionRunner, run_with_replay, MAX_RETRY,
                              sample_consistency)


# ── 假 LLM 与工具 ──────────────────────────


class FakeLLM:
    """按预设决策队列出牌，队列耗尽后返回 final_answer。"""

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = 0

    async def __call__(self, goal, history, tools_desc, system_prompt=""):
        self.calls += 1
        if self._decisions:
            return self._decisions.pop(0)
        return {"thought": "默认收尾", "action": "final_answer",
                "action_input": {"summary": "done"}, "tokens": 1}


def decision(action, tokens=1, action_input=None):
    return {"thought": f"think:{action}", "action": action,
            "action_input": action_input or {}, "tokens": tokens}


async def tool_ok(params):
    return {"ok": True, "data": 42}


async def tool_boom(params):
    raise ValueError("boom!")


async def tool_loop(params):
    return {"ok": True}


# ── T1: 工具全部成功，第 1 步 final_answer 收敛 ──


@pytest.mark.asyncio
async def test_t1_first_step_converge():
    llm = FakeLLM([decision("final_answer", action_input={"summary": "ok"})])
    runner = CognitionRunner(llm, tools={})
    result = await runner.run("目标")
    assert result["success"] is True
    assert result["answer"] == "ok"
    assert len(result["steps"]) == 1


# ── T2: 未知动作 → LLM 可见错误 → 纠错后收敛 ──


@pytest.mark.asyncio
async def test_t2_unknown_action_then_recover():
    llm = FakeLLM([
        decision("no_such_tool"),
        decision("final_answer", action_input={"summary": "ok"}),
    ])
    runner = CognitionRunner(llm, tools={})
    result = await runner.run("目标")
    assert result["success"] is True
    assert len(result["steps"]) == 2
    assert "未知动作" in result["steps"][0]["error"]
    assert result["steps"][1]["action"] == "final_answer"


# ── T3: 工具抛异常 → observation 记录 error，循环继续 ──


@pytest.mark.asyncio
async def test_t3_tool_exception_recorded():
    llm = FakeLLM([
        decision("boom"),
        decision("final_answer", action_input={"summary": "ok"}),
    ])
    runner = CognitionRunner(llm, tools={"boom": tool_boom})
    result = await runner.run("目标")
    assert result["success"] is True
    assert result["steps"][0]["error"] != ""
    assert "boom" in result["steps"][0]["error"]


# ── T4: 步数耗尽未收敛 ──


@pytest.mark.asyncio
async def test_t4_steps_exhausted():
    # 一直返回 loop，永不出 final_answer → 8 步耗尽
    llm = FakeLLM([decision("loop")] * 20)
    runner = CognitionRunner(llm, tools={"loop": tool_loop}, max_steps=8)
    result = await runner.run("目标")
    assert result["success"] is False
    assert "步" in result["error"]
    assert len(result["steps"]) == 8


# ── T5: token 超预算强制收尾 ──


@pytest.mark.asyncio
async def test_t5_token_budget_enforced():
    # 每轮上报 6 token，max_tokens=10 → 第 2 轮累计 12 >= 10 触发收尾
    llm = FakeLLM([decision("loop", tokens=6), decision("loop", tokens=6)])
    runner = CognitionRunner(llm, tools={"loop": tool_loop}, max_tokens=10)
    result = await runner.run("目标")
    assert result["success"] is False
    assert "token" in result["error"]
    assert result["tokens_used"] >= 10


@pytest.mark.asyncio
async def test_t5b_final_answer_not_killed_by_budget():
    # 收尾决策即使累计超预算也应放行执行（合法收尾不应被预算误杀）
    llm = FakeLLM([decision("loop", tokens=6),
                   decision("final_answer", tokens=6,
                            action_input={"summary": "ok"})])
    runner = CognitionRunner(llm, tools={"loop": tool_loop}, max_tokens=10)
    result = await runner.run("目标")
    assert result["success"] is True
    assert result["steps"][-1]["action"] == "final_answer"


# ── T6: run_with_replay 首轮失败 → 复盘重试成功 ──


@pytest.mark.asyncio
async def test_t6_replay_recovers():
    calls = {"n": 0}

    async def fake_llm(goal, history, tools_desc, system_prompt=""):
        calls["n"] += 1
        # 首次 run 8 步全 loop → 步数耗尽；复盘 run 第 9 次起 final_answer
        if calls["n"] >= 9:
            return {"thought": "复盘修正", "action": "final_answer",
                    "action_input": {"summary": "ok"}, "tokens": 1}
        return decision("loop")

    runner = CognitionRunner(fake_llm, tools={"loop": tool_loop}, max_steps=8)
    result = await run_with_replay(runner, "目标")
    assert result["success"] is True
    assert calls["n"] >= 9  # 确实走了复盘重试路径


# ── T7: reasoning="cot" 不改返回结构，仅追加思考提示 ──


class _FakeCfg:
    is_valid = staticmethod(lambda: True)
    model = "fake-model"


async def _fake_call_messages(cfg, messages, max_tokens=2000, temperature=0, timeout=30, **kwargs):
    captured["messages"] = messages
    return "答案"


@pytest.mark.asyncio
async def test_t7_cot_appends_prompt(monkeypatch):
    global captured
    captured = {}
    monkeypatch.setattr(llm_mod, "_call_llm_messages", _fake_call_messages)
    monkeypatch.setattr(llm_mod, "get_task_model_config",
                        lambda task_type: (_FakeCfg(), None))
    monkeypatch.setattr(llm_mod, "get_llm_config", lambda *a, **k: _FakeCfg())
    import common.metrics as metrics_mod
    monkeypatch.setattr(metrics_mod, "record_llm_call", lambda *a, **k: None)

    # cot：追加思考提示，返回仍是 str
    result = await llm_mod.llm_chat(
        [{"role": "user", "content": "问题"}], reasoning="cot")
    assert isinstance(result, str)
    assert result == "答案"
    assert captured["messages"][-1]["content"].endswith(llm_mod._COT_PROMPT)

    # 无 reasoning：不追加，行为不变
    await llm_mod.llm_chat([{"role": "user", "content": "问题2"}])
    assert captured["messages"][-1]["content"] == "问题2"

    # 多模态 content 为 list：cot 不崩溃、不追加（C1-4 守卫）
    await llm_mod.llm_chat(
        [{"role": "user", "content": [{"type": "text", "text": "看图"}]}],
        reasoning="cot")
    assert captured["messages"][-1]["content"] == [{"type": "text", "text": "看图"}]


# ── T8: context 注入到达 LLM；多次 run 状态不累积 ──


@pytest.mark.asyncio
async def test_t8_context_injected_and_state_reset():
    seen = []

    async def fake_llm(goal, history, tools_desc, system_prompt=""):
        seen.append(system_prompt)
        return decision("final_answer", action_input={"summary": "ok"})

    runner = CognitionRunner(fake_llm, tools={})
    result = await runner.run("目标", context={"inquiry_id": "Q-1", "agent_id": "agent-9"})
    assert result["success"] is True
    assert "inquiry_id=Q-1" in seen[0]
    assert "agent_id=agent-9" in seen[0]

    # 第二次 run 状态全新（steps/token 不累积、上一轮上下文不残留）
    result2 = await runner.run("目标2", context={"round": 2})
    assert len(result2["steps"]) == 1
    assert result2["tokens_used"] == 1
    assert "round=2" in seen[1]
    assert "Q-1" not in seen[1]


# ── T9: run_with_replay 复盘注入确实到达 LLM ──


@pytest.mark.asyncio
async def test_t9_replay_injection_reaches_llm():
    seen = []

    async def fake_llm(goal, history, tools_desc, system_prompt=""):
        seen.append(system_prompt)
        return decision("loop")

    runner = CognitionRunner(fake_llm, tools={"loop": tool_loop}, max_steps=2)
    result = await run_with_replay(runner, "目标")
    assert result["success"] is False
    # 首轮之后的重试轮，system_prompt 必须携带 _replay 复盘注入
    assert any("_replay" in p for p in seen[1:])


# ── T10: 空 goal 提前返回失败 ──


@pytest.mark.asyncio
async def test_t10_empty_goal_rejected():
    runner = CognitionRunner(FakeLLM([]), tools={})
    result = await runner.run("")
    assert result["success"] is False
    assert "目标为空" in result["error"]


# ── T11: sample_consistency 逐字段多数投票 ──


@pytest.mark.asyncio
async def test_t11_sample_consistency_votes():
    def make(action):
        return {"thought": "t", "action": action, "action_input": {"k": "v"}}

    async def fake_llm(i):
        return make("A") if i != 2 else make("B")

    decision = await sample_consistency(fake_llm, n=3)
    assert decision["action"] == "A"          # 2/3 多数
    assert decision["thought"] == "t"
    assert decision["action_input"] == {"k": "v"}

    # 全部采样无效 → 返回空 dict
    async def fake_none(i):
        return None
    assert await sample_consistency(fake_none, n=3) == {}


# ── T13: trace_hook（G1）run 结束时回调执行轨迹 ──


@pytest.mark.asyncio
async def test_t13_trace_hook_fires_on_finish():
    captured = {}

    async def trace_hook(traj):
        captured.update(traj)

    llm = FakeLLM([decision("final_answer", action_input={"summary": "ok"})])
    runner = CognitionRunner(llm, tools={}, trace_hook=trace_hook)
    result = await runner.run("目标", context={"round": 1})
    assert result["success"] is True
    assert captured["goal"] == "目标"
    assert captured["context"] == {"round": 1}
    assert captured["success"] is True
    assert captured["answer"] == "ok"
    assert len(captured["steps"]) == 1
    assert captured["tokens_used"] == 1


@pytest.mark.asyncio
async def test_t13_trace_hook_fires_on_failure():
    captured = {}

    async def trace_hook(traj):
        captured.update(traj)

    runner = CognitionRunner(FakeLLM([]), tools={}, trace_hook=trace_hook)
    result = await runner.run("")
    assert result["success"] is False
    assert captured["success"] is False
    assert "目标为空" in captured["error"]


# ── T14: goal_obj（G2）每步推进度 + 终局 complete/fail ──


class FakeGoal:
    """轻量假 Goal：只验证 update_progress/complete/fail 被正确调用。"""

    def __init__(self):
        self.progress = 0.0
        self.status = "pending"
        self.terminal = None
        self.progress_calls = 0

    def update_progress(self, ratio):
        self.progress_calls += 1
        self.progress = max(self.progress, min(ratio, 1.0))

    def complete(self):
        self.status = "done"
        self.terminal = "complete"
        self.progress = 1.0  # 与真实 Goal 语义一致：终局 progress=1.0

    def fail(self, error):
        self.status = "failed"
        self.terminal = "fail"


@pytest.mark.asyncio
async def test_t14_goal_progress_and_complete():
    llm = FakeLLM([decision("loop"), decision("final_answer",
                                              action_input={"summary": "ok"})])
    runner = CognitionRunner(llm, tools={"loop": tool_loop}, max_steps=4)
    goal = FakeGoal()
    result = await runner.run("目标", goal_obj=goal)
    assert result["success"] is True
    assert goal.status == "done"
    assert goal.terminal == "complete"
    assert goal.progress == 1.0
    assert goal.progress_calls >= 2  # 每步执行后都更新


@pytest.mark.asyncio
async def test_t14_goal_fail_on_exhaustion():
    llm = FakeLLM([decision("loop")] * 20)
    runner = CognitionRunner(llm, tools={"loop": tool_loop}, max_steps=3)
    goal = FakeGoal()
    result = await runner.run("目标", goal_obj=goal)
    assert result["success"] is False
    assert goal.status == "failed"
    assert goal.terminal == "fail"


@pytest.mark.asyncio
async def test_t12_react_consistency_tokens_sum(monkeypatch):
    calls = []

    async def fake_react(goal, history, tools_desc, system_prompt="", caller="cognition"):
        calls.append(1)
        return {"thought": "t", "action": "final_answer",
                "action_input": {"summary": "ok"}, "tokens": 5}

    monkeypatch.setattr(llm_mod, "llm_call_react", fake_react)
    d = await llm_mod.llm_call_react_consistency("目标", [], "tools")
    assert d["action"] == "final_answer"
    assert d["tokens"] == 15        # 3 次 × 5
    assert len(calls) == 3          # n=3 独立采样
