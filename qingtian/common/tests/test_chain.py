"""G3 提示链单测（实施文档 §九 test_chain）

FnStep 串行 / LLMStep（mock llm_chat）输出传后步 / fail_fast 短路 / parse_fn。
"""

import pytest

from common import llm as llm_mod
from common.chain import FnStep, LLMStep, PromptChain


def _plus1(prev, ctx):
    return (prev or 0) + 1


# ── G3-1: FnStep 串行 ──


@pytest.mark.asyncio
async def test_fn_serial_chain():
    chain = PromptChain([FnStep("a", _plus1), FnStep("b", _plus1)])
    r = await chain.run(initial=1)
    assert r["ok"] is True
    assert r["final"] == 3
    assert r["outputs"]["a"] == 2
    assert r["outputs"]["b"] == 3


@pytest.mark.asyncio
async def test_ctx_shared_across_steps():
    seen = {}

    def record(prev, ctx):
        seen.update(ctx)
        return prev

    chain = PromptChain([FnStep("r", record)])
    await chain.run(initial=0, ctx={"agent_id": "a1", "inquiry_id": "Q-1"})
    assert seen == {"agent_id": "a1", "inquiry_id": "Q-1"}


# ── G3-2: LLMStep ──


@pytest.mark.asyncio
async def test_llm_step_passes_output_forward(monkeypatch):
    calls = []

    async def fake_chat(messages, **kwargs):
        calls.append(kwargs)
        return "报价文本"

    monkeypatch.setattr(llm_mod, "llm_chat", fake_chat)
    chain = PromptChain([
        FnStep("norm", lambda p, c: "输入"),
        LLMStep("pricing", lambda prev, ctx: f"定价 {prev}", task_type="precise"),
    ])
    r = await chain.run()
    assert r["ok"] is True
    assert r["outputs"]["pricing"] == "报价文本"
    assert calls[0]["task_type"] == "precise"
    assert calls[0]["caller"] == "chain.pricing"


@pytest.mark.asyncio
async def test_llm_step_parse_fn_structures_output(monkeypatch):
    async def fake_chat(messages, **kwargs):
        return '{"price": 100}'

    monkeypatch.setattr(llm_mod, "llm_chat", fake_chat)
    step = LLMStep("p", lambda p, c: "x",
                   parse_fn=lambda prev, text: {"price": 100})
    r = await PromptChain([step]).run()
    assert r["outputs"]["p"] == {"price": 100}


# ── G3-3: 失败处理 ──


@pytest.mark.asyncio
async def test_llm_empty_fails_fast(monkeypatch):
    async def fake_chat(messages, **kwargs):
        return ""

    monkeypatch.setattr(llm_mod, "llm_chat", fake_chat)
    chain = PromptChain([LLMStep("p", lambda p, c: "x"),
                         FnStep("after", lambda p, c: "never")])
    r = await chain.run()
    assert r["ok"] is False
    assert "p" in r["error"]
    assert "after" not in r["outputs"]


@pytest.mark.asyncio
async def test_fn_failure_fail_fast_stops():
    def boom(prev, ctx):
        raise ValueError("boom")

    chain = PromptChain([FnStep("a", boom), FnStep("b", _plus1)])
    r = await chain.run(initial=1)
    assert r["ok"] is False
    assert "a" in r["error"]
    assert "b" not in r["outputs"]


@pytest.mark.asyncio
async def test_no_fail_fast_continues_with_none():
    def boom(prev, ctx):
        raise ValueError("boom")

    chain = PromptChain(
        [FnStep("a", boom), FnStep("b", _plus1)], fail_fast=False)
    r = await chain.run(initial=1)
    assert r["ok"] is False
    assert r["outputs"]["a"] is None
    assert r["outputs"]["b"] == 1


@pytest.mark.asyncio
async def test_duplicate_step_name_rejected():
    with pytest.raises(ValueError):
        PromptChain([FnStep("a", _plus1), FnStep("a", _plus1)])
