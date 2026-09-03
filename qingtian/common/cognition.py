"""认知原语 — ReAct 循环器（思考→行动→观察）

设计文档《擎天Agent设计模式改良》§3.1。opt-in：不改变任何现有调用路径。

结构:
  - StepRecord: 单步轨迹（thought/action/action_input/observation/error）
  - CognitionRunner: 思考→行动→观察 循环（max_steps + max_tokens 双保险）
  - run_with_replay: 失败→反思→修正重试（最多 MAX_RETRY 次）

llm_call 用"回调注入"而非内置 common.llm，保证零依赖可单测。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:  # 仅类型标注，避免运行时依赖（goal.py 零依赖纯内存）
    from common.goal import Goal

logger = logging.getLogger("common.cognition")

# 动作名常量
ACTION_FINAL = "final_answer"   # 内置收尾动作
ACTION_LLM = "llm_chat"         # 内置临时问答
ACTION_RECALL = "recall"        # 内置记忆召回（接永恒）


@dataclass
class StepRecord:
    thought: str                        # LLM 推理
    action: str                         # 动作名
    action_input: dict                  # 动作参数
    observation: Any                    # 动作结果（失败为 error 字符串）
    error: str = ""                     # 执行异常摘要（空 = 成功）

    def to_dict(self) -> dict:
        obs = self.observation
        if not isinstance(obs, (str, int, float, bool)) or obs is None:
            obs = json.dumps(obs, ensure_ascii=False, default=str)[:500]
        return {
            "thought": self.thought,
            "action": self.action,
            "action_input": self.action_input,
            "observation": obs,
            "error": self.error,
        }


ToolFn = Callable[[dict], Awaitable[Any]]


class CognitionRunner:
    """思考→行动→观察 循环器。

    Args:
        llm_call: async (goal, history, tools_desc) -> dict
                  返回 {"thought": str, "action": str, "action_input": dict,
                        "tokens": int}（tokens 为该轮 LLM 消耗 token 数，
                  由适配层上报，供 token 预算判定用。见 common/llm.py llm_call_react）
        tools: {动作名: async callable(params)->Any}
        max_steps: 循环上限（默认 8，防死循环）
        max_tokens: 本轮总 token 预算（含历史，超了强制收尾）
        system_prompt: 追加到 ReAct 提示词的领域说明
        trace_hook: G1 可选 async (traj: dict) -> None，run() 结束时回调执行轨迹
                    （含 goal/context/steps/tokens_used/success/error/answer）。
                    不直接连 DB，持久化由业务侧（xihe/osskill）注入 hook 完成。
    """

    def __init__(self, llm_call, tools: dict[str, ToolFn],
                 max_steps: int = 8, max_tokens: int = 4000,
                 system_prompt: str = "", trace_hook=None):
        self._llm_call = llm_call
        self._tools = dict(tools)
        self._tools.setdefault(ACTION_FINAL, self._builtin_final)
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._steps: list[StepRecord] = []
        self._token_used = 0
        self._trace_hook = trace_hook
        # run() 内暂存的本轮输入（供 _finish 的 trace_hook / goal_obj 用）
        self._goal = ""
        self._context: dict = {}
        self._goal_obj = None

    async def _builtin_final(self, params: dict) -> dict:
        """内置收尾：返回总结，触发循环终止。"""
        return {"ok": True, "answer": params.get("summary", ""),
                "data": params.get("data")}

    async def run(self, goal: str, context: dict | None = None,
                  goal_obj: Goal | None = None) -> dict:
        """执行循环。返回 {answer, steps, tokens_used, success, error}。

        context 并入 system_prompt 供 LLM 决策（含 run_with_replay 的复盘注入
        `_replay`，让重试轮能看到上一轮失败原因）。

        goal_obj: G2 可选。每步执行后 update_progress(步数/上限)，
                  终局按结果 complete()/fail()（状态转换通过 persist_hook 落库）。
        """
        context = dict(context or {})
        # 每次 run 全新状态：防止 run_with_replay 多轮重试累积步骤/token
        self._steps = []
        self._token_used = 0
        self._goal = goal
        self._context = context
        self._goal_obj = goal_obj
        if goal_obj is not None and hasattr(goal_obj, "start"):
            try:
                goal_obj.start()  # pending -> running（重试轮已是 running，幂等跳过）
            except Exception as e:
                logger.warning("goal start 异常: %s", e)
        if not goal:
            return await self._finish(False, error="目标为空")
        tools_desc = self._format_tools()
        sys_prompt = self._system_prompt
        if context:
            ctx_text = "；".join(f"{k}={v}" for k, v in context.items())
            sys_prompt = (f"{sys_prompt}\n\n[上下文] {ctx_text}"
                          if sys_prompt else f"[上下文] {ctx_text}")
        for _ in range(self._max_steps):
            # 1. LLM 出 thought+action（适配层上报 tokens，累计预算）
            decision = await self._llm_call(
                goal, [s.to_dict() for s in self._steps], tools_desc,
                system_prompt=sys_prompt,
            )
            if decision is None:
                return await self._finish(False, error="LLM 未返回决策")
            self._token_used += int(decision.get("tokens", 0) or 0)

            thought = decision.get("thought", "")
            action = decision.get("action", "")
            action_input = decision.get("action_input", {}) or {}
            action_input = action_input if isinstance(action_input, dict) else {}

            # 2. 执行动作
            tool = self._tools.get(action)
            if tool is None:
                record = StepRecord(thought, action, action_input,
                                    observation="", error=f"未知动作: {action}")
                self._steps.append(record)
                self._tick_progress()
                continue  # 让 LLM 看见错误后纠错

            error = ""
            try:
                observation = await tool(action_input)
            except Exception as e:
                observation = ""
                error = str(e)[:200]
            record = StepRecord(thought, action, action_input, observation, error)
            self._steps.append(record)
            self._tick_progress()

            # 3. 终止判定：final_answer 优先放行（收尾决策已产生，不应被预算误杀）
            if action == ACTION_FINAL and not error:
                answer = observation.get("answer") if isinstance(observation, dict) else observation
                return await self._finish(True, answer=answer)

            # 4. 预算守卫：非收尾步骤累计超预算才强制终止
            if self._token_used >= self._max_tokens:
                return await self._finish(False, error=f"token 超预算({self._token_used})")

        # 5. 步数耗尽
        return await self._finish(False, error=f"超过 {self._max_steps} 步未收敛")

    def _tick_progress(self):
        """G2：每执行一步后刷新 goal 进度（单调、夹取 0-1）。"""
        go = self._goal_obj
        if go is not None and hasattr(go, "update_progress"):
            try:
                go.update_progress(len(self._steps) / self._max_steps)
            except Exception as e:
                logger.warning("goal 进度更新异常: %s", e)

    async def _finish(self, success: bool, answer=None, error=""):
        """统一收尾：goal 终局状态 + trace_hook 轨迹回调。"""
        result = {
            "answer": answer,
            "steps": [s.to_dict() for s in self._steps],
            "tokens_used": self._token_used,
            "success": success,
            "error": error,
        }
        # G2：终局按最终结果 complete/fail（最后一次 _finish 决定终态）
        if self._goal_obj is not None:
            try:
                if success:
                    self._goal_obj.complete()
                else:
                    self._goal_obj.fail(error)
            except Exception as e:
                logger.warning("goal 终局更新异常: %s", e)
        # G1：执行轨迹回调（不直接连 DB，落库由业务侧 hook 完成）
        if self._trace_hook:
            try:
                await self._trace_hook({
                    "goal": self._goal,
                    "context": self._context,
                    "steps": result["steps"],
                    "tokens_used": result["tokens_used"],
                    "success": success,
                    "error": error,
                    "answer": answer,
                })
            except Exception as e:
                logger.warning("trace_hook 异常: %s", e)
        return result

    def _format_tools(self) -> str:
        lines = []
        for name, fn in self._tools.items():
            doc = (getattr(fn, "__doc__") or "").strip().split("\n")[0]
            lines.append(f"- {name}: {doc}")
        return "\n".join(lines)

    # token 预算已由 run() 循环内从 decision["tokens"] 累计，无需单独 add_tokens。
    # （设计文档验收项"token 超限强制收尾"由循环顶部判定保证。）


# ── 失败复盘钩子（设计文档 §3.2） ──
MAX_RETRY = 2


async def run_with_replay(runner: CognitionRunner, goal: str,
                          context: dict | None = None,
                          goal_obj: Goal | None = None) -> dict:
    """失败→反思→修正重试（最多 MAX_RETRY 次）。

    首次失败后，追加一步"复盘"：把失败信息注入下一轮提示词，
    让 LLM 修正动作再跑。仍失败则返回错误（由业务侧 on_execution_failure 兜底）。

    goal_obj 透传：中间轮推进度，终局只按最终结果更新一次（最后一次 run 的
    complete/fail 决定终态）。
    """
    result = await runner.run(goal, context, goal_obj)
    for _ in range(MAX_RETRY):
        if result["success"]:
            return result
        logger.warning("ReAct 失败（%s），复盘重试...", result["error"])
        context = {**(context or {}), "_replay": result["error"]}
        result = await runner.run(goal, context, goal_obj)
    return result


# ── 自洽性采样（设计文档 §4.3 reasoning=self_consistency） ──
# Phase 1：n 次独立采样 + 逐字段多数投票。零依赖：llm_call 由调用方注入。

async def sample_consistency(llm_call: Callable[[int], Awaitable[dict | None]],
                             n: int = 3) -> dict:
    """自洽性决策：n 次独立 LLM 采样，逐字段多数投票。

    Args:
        llm_call: async (attempt: int) -> dict | None，第 attempt 次采样的决策
                  （如 {"thought", "action", "action_input"}）。返回 None/空 dict 的
                  采样丢弃，不参与投票。
        n: 采样次数（默认 3，文档 §4.3）。

    Returns:
        投票后的决策 dict（逐字段取多数值）；无有效采样时返回 {}。
        调用方可按需剔除如 tokens 这类不计票字段后再传入。
    """
    samples = []
    for i in range(n):
        r = await llm_call(i)
        if isinstance(r, dict) and r:
            samples.append(r)
    if not samples:
        return {}

    def _key(v) -> str:
        # dict/list 不可哈希 → 序列化后作投票键
        return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)

    voted: dict = {}
    for key in samples[0]:
        counter: dict[str, int] = {}
        for s in samples:
            if key not in s:
                continue
            k = _key(s[key])
            counter[k] = counter.get(k, 0) + 1
        if counter:
            best = max(counter.items(), key=lambda kv: kv[1])[0]
            for s in samples:
                if key in s and _key(s[key]) == best:
                    voted[key] = s[key]
                    break
    return voted
