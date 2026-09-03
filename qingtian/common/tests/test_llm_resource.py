"""P2 模型资源层单测（实施文档 §九 test_llm_resource）

三档路由 / 上下文工程（剪枝）/ 输出过滤 / 成本感知（计价+预算守卫）/ llm_batch 并行。
全部 opt-in：mock llm_chat/_call_llm_messages/config 依赖，不触真实网络。
"""

import asyncio

import pytest

from common import llm as llm_mod


def _cfg(model="m"):
    return llm_mod.LLMConfig(api_key="k", base_url="http://x", model=model)


def _pricing():
    return {
        "simple": {"per_1k_in": 0.002, "per_1k_out": 0.006},
        "precise": {"per_1k_in": 0.002, "per_1k_out": 0.008},
        "reasoning": {"per_1k_in": 0.004, "per_1k_out": 0.016},
    }


# ── P2-1: 三档规则路由 ──


def test_detect_simple_keyword():
    assert llm_mod._detect_task_type([{"role": "user", "content": "总结一下这份文档"}]) == llm_mod.TASK_SIMPLE


def test_detect_precise_default():
    assert llm_mod._detect_task_type([{"role": "user", "content": "核对一下金额"}]) == llm_mod.TASK_PRECISE


def test_detect_reasoning_intent_word_and_length():
    msg = "请仔细分析这两家供应商的报价差异，权衡交付周期与总成本，并给出最优的采购决策建议"
    assert len(msg) >= llm_mod.REASONING_MIN_LEN
    assert llm_mod._detect_task_type([{"role": "user", "content": msg}]) == llm_mod.TASK_REASONING


def test_detect_short_intent_word_stays_precise():
    # 意图词命中但输入过短 → 不上 reasoning（防误路由）
    assert llm_mod._detect_task_type([{"role": "user", "content": "分析"}]) == llm_mod.TASK_PRECISE


def test_detect_mixed_task_prefers_simple():
    # 同时含推理词"分析"与简单词"总结/纪要" → 简单档优先（输出形态为摘要，省钱）
    msg = "请分析并总结这三份报价单的差异，然后写一份会议纪要供采购组参考"
    assert llm_mod._detect_task_type([{"role": "user", "content": msg}]) == llm_mod.TASK_SIMPLE


def test_detect_empty_text_precise():
    assert llm_mod._detect_task_type([]) == llm_mod.TASK_PRECISE
    assert llm_mod._detect_task_type([{"role": "system", "content": "sys"}]) == llm_mod.TASK_PRECISE


def test_detect_mode_none_always_precise(monkeypatch):
    monkeypatch.setattr(llm_mod, "_routing_mode", lambda: "none")
    assert llm_mod._detect_task_type([{"role": "user", "content": "总结一下"}]) == llm_mod.TASK_PRECISE


# ── P2-2: 归一化与三档配置 ──


def test_normalize_task_type_alias():
    assert llm_mod._normalize_task_type("chat") == llm_mod.TASK_SIMPLE
    assert llm_mod._normalize_task_type("simple") == llm_mod.TASK_SIMPLE
    assert llm_mod._normalize_task_type("precise") == llm_mod.TASK_PRECISE
    assert llm_mod._normalize_task_type("reasoning") == llm_mod.TASK_REASONING


def test_get_task_model_config_chat_alias_not_raise(monkeypatch):
    def fake_read(path):
        if "reasoning" in path:
            return None
        return _cfg(model=path.rsplit(".", 1)[-1])
    monkeypatch.setattr(llm_mod, "_read_task_model_config", fake_read)
    monkeypatch.setattr(llm_mod, "get_llm_config", lambda *a, **k: _cfg(model="global"))
    primary, backup = llm_mod.get_task_model_config("chat")
    assert primary is not None  # 归一化不报错，且能取到 simple 档


def test_reasoning_unconfigured_degrades_to_precise(monkeypatch):
    configs = {"common.llm.reasoning": None,
               "common.llm.precise": _cfg(model="deepseek-v4-flash")}

    def fake_read(path):
        return configs.get(path)
    monkeypatch.setattr(llm_mod, "_read_task_model_config", fake_read)
    monkeypatch.setattr(llm_mod, "get_llm_config", lambda *a, **k: _cfg(model="global"))
    primary, backup = llm_mod.get_task_model_config("reasoning")
    assert primary is not None
    assert primary.model == "deepseek-v4-flash"


def test_simple_unconfigured_uses_precise_backup(monkeypatch):
    configs = {"common.llm.simple": None, "common.llm.chat": None,
               "common.llm.precise": _cfg(model="deepseek-v4-flash")}

    def fake_read(path):
        return configs.get(path)
    monkeypatch.setattr(llm_mod, "_read_task_model_config", fake_read)
    monkeypatch.setattr(llm_mod, "get_llm_config", lambda *a, **k: _cfg(model="global"))
    primary, backup = llm_mod.get_task_model_config("simple")
    # simple 未配 → primary 落空，backup 用 precise 兜底（llm_chat 会走 backup）
    assert backup is not None
    assert backup.model == "deepseek-v4-flash"


# ── P2-3: 上下文工程（剪枝） ──


def test_prune_under_budget_unchanged():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert llm_mod._prune_messages(msgs, 100) is msgs


def test_prune_keeps_system_and_last_drops_middle():
    msgs = [
        {"role": "system", "content": "系统" * 10},   # 10 tok
        {"role": "user", "content": "m1旧" * 10},     # 15 tok
        {"role": "user", "content": "m2中" * 10},     # 15 tok
        {"role": "user", "content": "L" * 20},        # 10 tok
    ]
    got = llm_mod._prune_messages(msgs, budget=25)
    # system + 尾部保留，中部历史被丢弃
    assert got[0]["role"] == "system"
    assert got[0]["content"] == "系统" * 10
    assert got[-1]["content"] == "L" * 20
    assert len(got) == 2
    total = sum(llm_mod._estimate_tokens(str(m.get("content", ""))) for m in got)
    assert total <= 25


def test_prune_truncates_when_only_system_and_last_over_budget():
    msgs = [{"role": "system", "content": "S" * 100}, {"role": "user", "content": "L" * 100}]
    got = llm_mod._prune_messages(msgs, budget=20)
    assert got  # 至少保留一条（可能丢 system，保尾部）
    total = sum(llm_mod._estimate_tokens(str(m.get("content", ""))) for m in got)
    assert total <= 20
    assert any("…[截断]…" in str(m.get("content", "")) for m in got)


# ── P2-4: 输出过滤 ──


def test_output_filter_custom_sensitive_word():
    f = llm_mod.make_output_filter(sensitive_words=["机密"])
    ok, reason = f("文件包含机密内容")
    assert ok is False and "机密" in reason


def test_output_filter_default_word():
    f = llm_mod.make_output_filter()
    ok, _ = f("user password: hunter2")
    assert ok is False


def test_output_filter_max_chars():
    f = llm_mod.make_output_filter(max_chars=10)
    ok, reason = f("x" * 20)
    assert ok is False and "超长" in reason


def test_output_filter_pass():
    f = llm_mod.make_output_filter(sensitive_words=["机密"], max_chars=100)
    ok, reason = f("正常报价文本")
    assert ok is True and reason == ""


async def _stub_llm_env(monkeypatch, fake_call):
    """mock 网络与配置依赖，使 llm_chat 可离线运行。"""
    monkeypatch.setattr(llm_mod, "_call_llm_messages", fake_call)
    monkeypatch.setattr(llm_mod, "get_task_model_config",
                        lambda tt: (_cfg(), None))
    monkeypatch.setattr(llm_mod, "get_llm_config", lambda *a, **k: _cfg(model="global"))
    import common.metrics as m
    monkeypatch.setattr(m, "record_llm_call", lambda *a, **k: None)
    monkeypatch.setattr(m, "record_llm_cost", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_llm_chat_output_filter_fails_over(monkeypatch):
    async def fake_call(cfg, messages, max_tokens=2000, temperature=0, timeout=30, **kw):
        return "含机密内容"
    await _stub_llm_env(monkeypatch, fake_call)
    f = llm_mod.make_output_filter(sensitive_words=["机密"])
    # 过滤失败 → 主备/全局兜底全被滤 → RuntimeError（不计成功）
    with pytest.raises(RuntimeError, match="机密"):
        await llm_mod.llm_chat([{"role": "user", "content": "x"}], output_filter=f)


@pytest.mark.asyncio
async def test_llm_chat_context_budget_prunes_before_call(monkeypatch):
    captured = {}

    async def fake_call(cfg, messages, max_tokens=2000, temperature=0, timeout=30, **kw):
        captured["messages"] = messages
        return "ok"
    await _stub_llm_env(monkeypatch, fake_call)
    msgs = [
        {"role": "system", "content": "系统" * 10},
        {"role": "user", "content": "m1旧" * 10},
        {"role": "user", "content": "m2中" * 10},
        {"role": "user", "content": "L" * 20},
    ]
    await llm_mod.llm_chat(msgs, context_budget=25)
    got = captured["messages"]
    assert got[0]["content"] == "系统" * 10
    assert got[-1]["content"] == "L" * 20
    assert len(got) == 2


# ── P2-5: 成本感知 ──


def test_compute_cost_by_tier(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_pricing", _pricing)
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    assert abs(llm_mod._compute_cost("simple", usage) - 0.008) < 1e-9
    assert abs(llm_mod._compute_cost("precise", usage) - 0.01) < 1e-9
    assert abs(llm_mod._compute_cost("reasoning", usage) - 0.02) < 1e-9


def test_compute_cost_unknown_tier_uses_precise(monkeypatch):
    monkeypatch.setattr(llm_mod, "get_pricing", _pricing)
    usage = {"prompt_tokens": 1000, "completion_tokens": 1000}
    assert abs(llm_mod._compute_cost("bogus", usage) - 0.01) < 1e-9


def test_budget_precheck_blocks_when_exceeded(monkeypatch):
    llm_mod.reset_daily_budget_for_test()
    monkeypatch.setattr(llm_mod, "_daily_budget_limit", lambda: 1.0)
    llm_mod._budget_spend(0.6)
    llm_mod._budget_spend(0.4)
    with pytest.raises(llm_mod.BudgetExceededError):
        llm_mod._budget_precheck()


def test_budget_precheck_allows_within_limit(monkeypatch):
    llm_mod.reset_daily_budget_for_test()
    monkeypatch.setattr(llm_mod, "_daily_budget_limit", lambda: 1.0)
    llm_mod._budget_spend(0.5)
    llm_mod._budget_precheck()  # 不抛错


@pytest.mark.asyncio
async def test_llm_chat_track_cost_records_metrics(monkeypatch):
    cost_records = []

    async def fake_call(cfg, messages, max_tokens=2000, temperature=0, timeout=30,
                        usage_box=None, **kw):
        if usage_box is not None:
            usage_box["usage"] = {"prompt_tokens": 1000, "completion_tokens": 1000}
        return "ok"
    await _stub_llm_env(monkeypatch, fake_call)
    import common.metrics as m
    monkeypatch.setattr(m, "record_llm_cost", lambda model, cost: cost_records.append(cost))
    monkeypatch.setattr(llm_mod, "get_pricing", _pricing)
    llm_mod.reset_daily_budget_for_test()
    monkeypatch.setattr(llm_mod, "_daily_budget_limit", lambda: 0.0)  # 不限

    await llm_mod.llm_chat([{"role": "user", "content": "x"}], track_cost=True)
    assert len(cost_records) == 1
    assert abs(cost_records[0] - 0.01) < 1e-9  # precise: (1000*0.002+1000*0.008)/1000


# ── P2-6: LLM 并行化 ──


@pytest.mark.asyncio
async def test_llm_batch_ordered_concurrent(monkeypatch):
    inflight = 0
    max_inflight = 0

    async def fake_chat(messages, **kw):
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return f"R:{messages[0]['content']}"
    monkeypatch.setattr(llm_mod, "llm_chat", fake_chat)

    out = await llm_mod.llm_batch(["a", "b", "c", "d"], caller="t")
    assert out == ["R:a", "R:b", "R:c", "R:d"]
    assert max_inflight == 4  # 并发，非串行


@pytest.mark.asyncio
async def test_llm_batch_single_failure_returns_none(monkeypatch):
    async def fake_chat(messages, **kw):
        if messages[0]["content"] == "b":
            raise RuntimeError("boom")
        return f"R:{messages[0]['content']}"
    monkeypatch.setattr(llm_mod, "llm_chat", fake_chat)

    out = await llm_mod.llm_batch(["a", "b", "c"], caller="t")
    assert out == ["R:a", None, "R:c"]


@pytest.mark.asyncio
async def test_llm_batch_empty(monkeypatch):
    async def fake_chat(messages, **kw):
        return "x"
    monkeypatch.setattr(llm_mod, "llm_chat", fake_chat)
    assert await llm_mod.llm_batch([], caller="t") == []
