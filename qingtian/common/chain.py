"""G3 提示链 — 通用提示链原语（设计文档 §11.6）

前步输出传后步（initial → 步骤1 → 步骤2 → ... → final）；
fail_fast 短路；LLM 步骤走 common.llm.llm_chat（含 task_type/reasoning）。

示例:
    chain = PromptChain([
        FnStep("去重", dedupe),
        LLMStep("定价", price_prompt, task_type="precise", parse_fn=parse_price),
        FnStep("格式化", format_quote),
    ])
    result = await chain.run(initial=inquiry, ctx={"agent_id": "agent-9"})
    # result = {"outputs": {"去重": ..., "定价": ..., "格式化": ...},
    #           "final": ..., "ok": True, "error": ""}
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from common import llm as llm_mod  # 模块引用，便于测试 monkeypatch

logger = logging.getLogger("common.chain")


class ChainError(Exception):
    """步骤执行失败（含 LLM 空返回）。"""


@dataclass
class ChainStep:
    """提示链步骤基类。name 唯一（outputs 键 + 失败定位）。"""

    name: str
    desc: str = ""

    async def execute(self, prev: Any, ctx: dict) -> Any:
        raise NotImplementedError

    def __repr__(self):
        return f"<{type(self).__name__} {self.name}>"


class FnStep(ChainStep):
    """纯函数步骤。fn(prev, ctx) -> Any（同步或 async 均可）。"""

    def __init__(self, name: str, fn: Callable[..., Any], desc: str = ""):
        super().__init__(name=name, desc=desc)
        if not callable(fn):
            raise TypeError("fn 必须可调用")
        self._fn = fn

    async def execute(self, prev: Any, ctx: dict) -> Any:
        r = self._fn(prev, ctx)
        if inspect.isawaitable(r):
            r = await r
        return r


class LLMStep(ChainStep):
    """LLM 步骤。prompt_fn(prev, ctx) -> str；走 common.llm.llm_chat。

    可选 parse_fn(prev, text) -> Any 做结构化；LLM 返回空/非 str → ChainError。
    """

    def __init__(self, name: str, prompt_fn: Callable[..., Any], desc: str = "",
                 task_type: str | None = None, reasoning: str | None = None,
                 parse_fn: Callable[..., Any] | None = None,
                 max_tokens: int = 2000):
        super().__init__(name=name, desc=desc)
        self._prompt_fn = prompt_fn
        self._task_type = task_type
        self._reasoning = reasoning
        self._parse_fn = parse_fn
        self._max_tokens = max_tokens

    async def execute(self, prev: Any, ctx: dict) -> Any:
        prompt = self._prompt_fn(prev, ctx)
        if inspect.isawaitable(prompt):
            prompt = await prompt
        text = await llm_mod.llm_chat(
            [{"role": "user", "content": str(prompt)}],
            task_type=self._task_type, max_tokens=self._max_tokens,
            reasoning=self._reasoning, caller=f"chain.{self.name}",
        )
        if not isinstance(text, str) or not text.strip():
            raise ChainError(f"{self.name}: LLM 返回空")
        if self._parse_fn:
            parsed = self._parse_fn(prev, text)
            if inspect.isawaitable(parsed):
                parsed = await parsed
            return parsed
        return text


class PromptChain:
    """串行提示链。run() 返回 {outputs, final, ok, error}。

    ctx 为跨步骤共享上下文（dict 引用，步骤可写入），不入链数据流。
    fail_fast=True（默认）：任一步失败立即停止；False：继续并置该步 output=None。
    """

    def __init__(self, steps: list[ChainStep], fail_fast: bool = True):
        names = [s.name for s in steps]
        if len(set(names)) != len(names):
            raise ValueError(f"步骤名必须唯一: {names}")
        self._steps = list(steps)
        self._fail_fast = fail_fast

    async def run(self, initial: Any = None, ctx: dict | None = None) -> dict:
        ctx = ctx or {}
        outputs: dict[str, Any] = {}
        cur = initial
        last_error = ""
        for step in self._steps:
            try:
                cur = await step.execute(cur, ctx)
            except Exception as e:
                last_error = f"{step.name}: {e}"
                logger.warning("链步骤 %s 失败: %s", step.name, e)
                if self._fail_fast:
                    return {"outputs": outputs, "final": None,
                            "ok": False, "error": last_error}
                outputs[step.name] = None
                cur = None
                continue
            outputs[step.name] = cur
        return {"outputs": outputs, "final": cur,
                "ok": not last_error, "error": last_error}
