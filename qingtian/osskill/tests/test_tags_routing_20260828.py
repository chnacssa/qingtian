# -*- coding: utf-8 -*-
"""B 方案（2026-08-28 波哥拍板）回归：Skill tags 确定性路由（P0.8）。

背景：_match_external_skill 原是死代码（全文件零调用）——DB tags 从未被任何运行
路径消费，LLM prompt 构建时 tags 又被丢弃。bid_prep 线上实锤：裸句"技术规范书
整理"被 LLM 语义路由判给 bidding:generate_bid，DB tags 改中文无效。

接线后契约：
1. tag/display_name 唯一明确命中（≥0.7 非模糊）→ LLM 之前确定性拦截；
2. 多候选模糊（ambiguous，差值<0.2）→ 放行 LLM 消歧；
3. 泛词根（0.6 无 boost）不拦截 → 落 LLM/兜底；
4. 强词根（≥0.7 带 action）拦截并携带 matched_action。
"""
import time

import pytest
from unittest.mock import AsyncMock

import osskill.execute_api as ea

# ── 测试用 Skill 路由缓存（键名对齐 list_active_skill_routes 返回结构）──

_BID_PREP = {
    "name": "bid_prep",
    "display_name": "国网设备技术参数特性表提取",
    "description": "投标前处理 — 解嵌套压缩包定位招标文件 + 提取国网设备技术参数特性表",
    "category": "tool",
    "tags": ["bidding", "unpack", "spec-table", "merge", "技术规范", "技术规范书", "特性表"],
    "actions": ["unpack", "select", "format_spec", "format_all"],
}

_BIDDING = {
    "name": "bidding",
    "display_name": "投标文件生成",
    "description": "投标文件生成/修订/评分",
    "category": "enterprise",
    "tags": ["bidding", "scoring", "document", "asset-management"],
    "actions": ["generate_bid", "revise_bid", "evaluate_bid"],
}

_PROCUREMENT = {
    "name": "procurement",
    "display_name": "采购助手",
    "description": "询价/比价/采购",
    "category": "enterprise",
    "tags": ["procurement", "purchase", "rfq", "supplier", "contract"],
    "actions": ["inquiry_create", "inquiry_get", "po_complete"],
}

_SALES = {
    "name": "sales",
    "display_name": "销售智能体",
    "description": "销售报价",
    "category": "enterprise",
    "tags": ["sales", "quotation", "customer", "catalog", "pricing"],
    "actions": ["query_quote", "order_decide"],
}


@pytest.fixture(autouse=True)
def _seed_and_reset():
    """每个用例独立 seed 路由缓存（TTL 内不触 DB），用例后还原。"""
    saved = (ea._SKILL_ROUTE_CACHE, ea._SKILL_ROUTE_CACHE_TS)
    yield
    ea._SKILL_ROUTE_CACHE, ea._SKILL_ROUTE_CACHE_TS = saved


def _seed(skills):
    ea._SKILL_ROUTE_CACHE = skills
    ea._SKILL_ROUTE_CACHE_TS = time.monotonic()


async def _probe(text, skill_name="work_secretary"):
    return await ea.api_probe_skill(skill_name, ea.ProbeRequest(action=text))


# ── 1. tags 命中：LLM 之前确定性拦截（线上实锤主场景）──

@pytest.mark.asyncio
async def test_tag_hit_beats_llm(monkeypatch):
    """「技术规范书整理」tag 命中 bid_prep——即使 LLM 说 bidding 也应被确定性层覆盖。"""
    _seed([_BID_PREP, _BIDDING])
    # LLM mock 成"错误的" bidding:generate_bid（线上实锤行为）——证明拦截发生在 LLM 前
    monkeypatch.setattr(ea, "_llm_semantic_probe", AsyncMock(return_value={
        "matched_skill": "bidding", "matched_action": "generate_bid", "confidence": 0.9}))
    r = await _probe("技术规范书整理")
    assert r["passthrough"] is False
    assert r["target_skill"] == "bid_prep"
    assert r["target_action"] == ""  # tag 命中无显式 action → 网关走 execute 通用语义
    assert r["confidence"] == 0.8


@pytest.mark.asyncio
async def test_tag_hit_partial_word():
    """tag「技术规范」是文本子串即命中（词形包含匹配）。"""
    _seed([_BID_PREP, _PROCUREMENT])
    r = await _probe("批量整理技术规范")
    assert r["passthrough"] is False
    assert r["target_skill"] == "bid_prep"


@pytest.mark.asyncio
async def test_tag_hit_same_skill_returns_intent():
    """probe 调用方即命中 skill 本身 → 返回 intent 不带 target_skill（!!指令!! 同款模式）。"""
    _seed([_BID_PREP])
    r = await _probe("整理技术规范", skill_name="bid_prep")
    assert r["passthrough"] is False
    assert r["intent"] == "execute"
    assert "target_skill" not in r


# ── 2. ambiguous：多候选模糊 → 放行 LLM 消歧 ──

@pytest.mark.asyncio
async def test_ambiguous_tag_vs_root_falls_to_llm(monkeypatch):
    """「投标文件技术规范」：tag"技术规范"(bid_prep 0.8) vs 词根"投标"(bidding 0.8)
    同分模糊 → 不拦截，LLM 消歧。"""
    _seed([_BID_PREP, _BIDDING])
    monkeypatch.setattr(ea, "_llm_semantic_probe", AsyncMock(return_value={
        "matched_skill": "bidding", "matched_action": "generate_bid", "confidence": 0.85}))
    r = await _probe("投标文件技术规范怎么写")
    assert r["target_skill"] == "bidding"  # LLM 的结果透传（未被 0.8/0.8 模糊命中劫持）


@pytest.mark.asyncio
async def test_ambiguous_shared_tag_falls_to_llm(monkeypatch):
    """bid_prep 与 bidding 共享英文 tag"bidding"→ 双 0.8 模糊 → 放行 LLM。"""
    _seed([_BID_PREP, _BIDDING])
    monkeypatch.setattr(ea, "_llm_semantic_probe", AsyncMock(return_value={
        "matched_skill": "bidding", "matched_action": "generate_bid", "confidence": 0.7}))
    r = await _probe("bidding 文件处理")
    assert r["target_skill"] == "bidding"


# ── 3. 泛词根（0.6 无 boost）不拦截 ──

@pytest.mark.asyncio
async def test_weak_root_not_intercepted(monkeypatch):
    """「生成」词根 0.6（无 boost）低于 0.7 门槛 → 不拦，落 LLM/兜底。"""
    _seed([_BIDDING])  # generate_bid 词根 generate→"生成"
    monkeypatch.setattr(ea, "_llm_semantic_probe", AsyncMock(return_value=None))
    monkeypatch.setattr(ea, "_keyword_fallback", AsyncMock(return_value=None))
    r = await _probe("生成一个工作汇报文档")
    assert r["passthrough"] is True  # 泛词根不拦截


# ── 4. 强词根（≥0.7 带 action）拦截并携带 action ──

@pytest.mark.asyncio
async def test_strong_root_carries_action(monkeypatch):
    """「询价」词根 0.6+0.25=0.85 ≥0.7 → 拦截 procurement 且带 matched_action。"""
    _seed([_PROCUREMENT])
    monkeypatch.setattr(ea, "_llm_semantic_probe", AsyncMock(return_value=None))
    r = await _probe("询价 10 台变压器")
    assert r["passthrough"] is False
    assert r["target_skill"] == "procurement"
    assert r["target_action"] == "inquiry_create"


# ── 5. display_name 匹配 ──

@pytest.mark.asyncio
async def test_display_name_match(monkeypatch):
    """display_name="销售智能体" → short_name"销售" in "帮我销售导入产品" → 0.7 拦截。"""
    _seed([_SALES])
    monkeypatch.setattr(ea, "_llm_semantic_probe", AsyncMock(return_value=None))
    r = await _probe("帮我销售导入产品")
    assert r["passthrough"] is False
    assert r["target_skill"] == "sales"


# ── 6. LLM 消歧上下文带 tags ──

@pytest.mark.asyncio
async def test_llm_prompt_carries_tags(monkeypatch):
    """skills_short 应带 tags（B 方案：LLM 兜底消歧也看得见运营方路由意图）。"""
    _seed([_BID_PREP, _BIDDING])
    prompts = []

    async def _fake_llm_call(*args, **kwargs):
        prompts.append(kwargs.get("prompt") or "")
        return None  # 三次全 None → _llm_semantic_probe 返回 None

    monkeypatch.setattr("common.llm.llm_call_json", _fake_llm_call)
    # 走 ambiguous 场景（tag 0.8 vs 词根 0.8）放行到 LLM
    await _probe("投标文件技术规范怎么写")
    assert prompts, "LLM 未被调用"
    assert any("技术规范" in p for p in prompts)  # tags 已进 LLM 上下文
