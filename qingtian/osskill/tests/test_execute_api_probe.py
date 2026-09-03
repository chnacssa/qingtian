"""execute_api probe 确定性路由测试 — 防 LLM 误路由致采购无回复（2026-08-13 小智实测）

背景 1：LLM semantic probe 偶发把"下单"路由到 validate_po（非下单入口，缺 po_id/line_items
校验必然 {ok:false}），导致采购下单无回复。下单是用户直接指令，P0.5 提升为 LLM 之前的
确定性路由（procurement:po_complete，confidence 1.0）。

背景 2：补条款/续答消息（"供应商是…货到付款…抽检"）无"下单/补齐"强词，绕过 P0.5 落 LLM
语义路由，偶发吐纯校验器 action（validate_rfq——返回 {ok,errors} 无自然语言回复）→
skillExecute 拿不到回复 → 用户收不到消息。修复：P0.5.5 续答确定性路由 + P1 LLM action
合法性/对话入口校验（校验器、幻觉 action 弃用落关键词兜底）。

纯逻辑测试，mock skill 路由数据与 LLM，不依赖数据库。验证：
- P0.5 直接下单关键词 → 确定性 po_complete，LLM 不被调用；
- P0.5.5 补条款/续答特征（供应商是/交货日期/货到付款/抽检等）→ 确定性 po_complete，LLM 不被调用；
- 续答路由仅 procurement skill 活跃时触发；履约回复/投标强词不被误路由；
- P1 LLM 路由出校验器（validate_*）或幻觉 action → 弃用，落 P1.5 关键词兜底；
- P1 LLM 路由出合法对话 action → 照常采用。
"""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from osskill.execute_api import (
    ProbeRequest,
    _direct_order_match,
    _order_fill_match,
    _confirm_inquiry_match,
    _inquiry_strong_route,
    _bid_revise_match,
    _bidding_action_hint,
    api_probe_skill,
)


# ── 纯函数真值表：_direct_order_match ──────────────────

@pytest.mark.parametrize("text", [
    "下单",
    "帮我下订单",
    "我要买10台变压器",
    "现在要买一批电缆",
    "下一份采购单",
    "直接采购吧",
    "补齐订单信息",
    "下单，型号YJV22 4×70",
])
def test_direct_order_match_true(text):
    """直接下单意图 → 命中。"""
    assert _direct_order_match(text) is True


@pytest.mark.parametrize("text", [
    "今天电缆价格是多少",
    "YJV22 4×70 多少钱",
    "⏳ 正在生成投标文件（Word）...",
    "帮我写一份标书",
    "客户回访情况怎么样",
    "已交付,物流正常",
    "生成报价单",
    "翻译成英文",
    "",
    "会议室预约",
])
def test_direct_order_match_false(text):
    """非下单意图 → 不误命中。"""
    assert _direct_order_match(text) is False


# ── 纯函数真值表：_order_fill_match（mock skill 路由） ──────────────────

_PROC_ROUTE = [{"name": "procurement", "actions": ["po_complete", "inquiry_create", "validate_rfq"]}]
_NO_PROC_ROUTE = [{"name": "sales", "actions": ["query_quote"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "供应商是H牌供应商，交货日期在12月12日前，交货地点安徽合肥，无指定品牌，支付方式货到付款，物流由供应方承担，需要抽检",
    "货到付款，账期30天",
    "交货日期需要提前，质保要求明确",
    "税率含13个点，不指定运输方式",
])
async def test_order_fill_match_true(text):
    """补条款/续答特征（procurement 活跃）→ 命中。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_PROC_ROUTE):
        assert await _order_fill_match(text) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "已交付,物流正常",          # 履约回复 → 排除
    "标书交货日期怎么填",        # 投标 → 排除
    "今天电缆价格是多少",        # 价格查询 → 无续答特征
    "下单",                     # 下单由 P0.5 接管
    "",
])
async def test_order_fill_match_false(text):
    """非续答意图（procurement 活跃）→ 不误命中。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_PROC_ROUTE):
        assert await _order_fill_match(text) is False


@pytest.mark.asyncio
async def test_order_fill_match_requires_procurement_skill():
    """procurement 未活跃 → 续答特征不命中（sales 部署不误路由）。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_NO_PROC_ROUTE):
        assert await _order_fill_match("货到付款") is False


# ── 纯函数真值表：_confirm_inquiry_match（确认询价确定性路由，2026-08-15） ──────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "确认询价",                                     # 纯确认短句（≤12字，_complete_order 确认分支）
    "确认并询价",
    "确认下单询价",
    "确认发出询价",
    "确认后询价",
    "发起询价",
    "开始询价",
    "发出询价",
    "就去询价",
    "可以询价",
    "确认询价：YJV22 电缆 500米",                     # 带参数长消息（走 po_complete 正常下单流程）
    "回复确认询价",                                   # 短语嵌句中
])
async def test_confirm_inquiry_match_true(text):
    """确认询价短语（procurement 活跃）→ 命中。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_PROC_ROUTE):
        assert await _confirm_inquiry_match(text) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "下单",                       # 下单由 P0.5 接管
    "询价 10 台变压器",            # 裸询价、无确认短语 → 不劫持（落 inquiry_create）
    "询价：YJV22 4×70 电缆多少钱",  # 大师实锤原句：询价+价格词 → 不劫持（P0.6 路由 inquiry_create）
    "已交付,物流正常",             # 履约回复 → 排除
    "标书交货日期怎么填",          # 投标 → 排除
    "今天电缆价格是多少",          # 价格查询
    "会议室预约",                 # 无关日常
    "",
])
async def test_confirm_inquiry_match_false(text):
    """非确认询价意图（procurement 活跃）→ 不误命中。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_PROC_ROUTE):
        assert await _confirm_inquiry_match(text) is False


@pytest.mark.asyncio
async def test_confirm_inquiry_match_requires_procurement_skill():
    """procurement 未活跃（销售服）→ 确认询价短语不劫持（不误路由到采购）。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_NO_PROC_ROUTE):
        assert await _confirm_inquiry_match("确认询价") is False


# ── 纯函数真值表：_inquiry_strong_route（询价强词+价格词 → 采购询价，2026-08-14 大师实锤） ──────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "询价：YJV22-0.6/1KV 4*95的电力电缆多少钱？",   # 大师实锤原句：询价+多少钱
    "询价，YJV22 4×70 电缆现在的价格是多少",          # 询价+价格/是多少
    "报价：这个型号单价多少",                          # 报价+单价
    "比价，变压器市场价多少",                          # 比价+市场价
    "采购电缆，现价多少",                              # 采购+现价
])
async def test_inquiry_strong_route_true(text):
    """询价/报价/比价/采购/招标强词 + 价格词（procurement 活跃）→ 路由采购询价。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_PROC_ROUTE):
        assert await _inquiry_strong_route(text) == "procurement:inquiry_create"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "今天电缆价格是多少",        # 纯价格词、无询价强词 → 不劫持（落 query_quote）
    "询价 10 台变压器",          # 询价强词、无价格词 → 不劫持（落原 856 行）
    "标书交货日期怎么填",        # 投标强词（招标/标书）→ 排除
    "已交付,物流正常",           # 履约回复 → 排除
    "",
    "会议室预约",                # 无关日常
])
async def test_inquiry_strong_route_false(text):
    """无询价强词+价格词组合 / 投标履约强词 → 不路由采购询价。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_PROC_ROUTE):
        assert await _inquiry_strong_route(text) is None


@pytest.mark.asyncio
async def test_inquiry_strong_route_requires_procurement_skill():
    """procurement 未活跃（销售服）→ 询价强词+价格词不劫持（销售服 query_quote 查自家目录正确）。"""
    with patch("osskill.execute_api._load_skill_routes", new_callable=AsyncMock, return_value=_NO_PROC_ROUTE):
        assert await _inquiry_strong_route("询价：YJV22 4×95电缆多少钱") is None


# ── api_probe_skill 集成：P0.6 询价强词确定性路由 ──────────────────

# LLM 把"询价：XX多少钱"路由到 query_quote（LLM prompt 355 行"查某产品价格→query_quote"劫持场景）
_LLM_PRICE_ROUTE = {
    "matched_skill": "sales",
    "matched_action": "query_quote",
    "confidence": 0.9,
    "params": {},
}


@pytest.mark.asyncio
async def test_probe_inquiry_strong_beats_llm_price_route():
    """bug 场景：LLM 把"询价：XX多少钱"路由到 query_quote，P0.6 必须先拦截为 inquiry_create 且不调 LLM。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_LLM_PRICE_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="询价：YJV22-0.6/1KV 4*95的电力电缆多少钱？"))
    assert result["ok"] is True
    assert result["passthrough"] is False
    assert result["intent"] == "procurement:inquiry_create"
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "inquiry_create"
    # P0.6 在 LLM 之前返回 → LLM 不应被调用（确定性路由真正绕过 LLM）
    mock_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_inquiry_strong_sales_deployment_keeps_query_quote():
    """销售服（无 procurement）→ P0.6 不触发，"询价：XX多少钱"落 LLM 合法路由 query_quote（查自家目录）。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_NO_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_LLM_PRICE_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="询价：YJV22 4×95电缆多少钱"))
    mock_llm.assert_awaited_once()
    assert result["target_skill"] == "sales"
    assert result["target_action"] == "query_quote"


# ── api_probe_skill 集成：P0.5 直接下单确定性路由 ──────────────────

# 复现小智实测 bug 场景：LLM 把下单误路由到 validate_po（高置信）
_BAD_LLM_VALIDATE_PO_ROUTE = {
    "matched_skill": "procurement",
    "matched_action": "validate_po",
    "confidence": 0.9,
    "params": {},
}

# 合法对话入口 action（询价）
_GOOD_LLM_INQUIRY_ROUTE = {
    "matched_skill": "procurement",
    "matched_action": "inquiry_create",
    "confidence": 0.9,
    "params": {"product_category": "变压器", "quantity": 10},
}


@pytest.mark.parametrize("text", [
    "下单",
    "帮我下订单",
    "我要买10台变压器",
    "现在要买一批电缆",
    "直接采购吧",
    "补齐订单信息",
])
@pytest.mark.asyncio
async def test_probe_order_keyword_beats_llm(text):
    """bug 场景：LLM 误路由 validate_po，P0.5 必须先拦截为 po_complete 且不调 LLM。"""
    with patch(
        "osskill.execute_api._llm_semantic_probe",
        new_callable=AsyncMock,
        return_value=_BAD_LLM_VALIDATE_PO_ROUTE,
    ) as mock_llm:
        result = await api_probe_skill("procurement", ProbeRequest(action=text))
    assert result["ok"] is True
    assert result["passthrough"] is False
    assert result["intent"] == "procurement:po_complete"
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "po_complete"
    assert result["confidence"] == 1.0
    # P0.5 在 LLM 之前返回 → LLM 不应被调用（确定性路由真正绕过 LLM）
    mock_llm.assert_not_awaited()


# ── api_probe_skill 集成：P0.5.5 补条款/续答确定性路由 ──────────────────

@pytest.mark.parametrize("text", [
    "供应商是H牌供应商，交货日期在12月12日前，交货地点安徽合肥，无指定品牌，支付方式货到付款，物流由供应方承担，需要抽检",
    "货到付款，月结账期30天",
    "交货日期需要提前，抽检标准要明确",
])
@pytest.mark.asyncio
async def test_probe_order_fill_routes_to_po_complete(text):
    """补条款/续答特征 → 确定性路由 po_complete，不经 LLM（复现小智场景）。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_BAD_LLM_VALIDATE_PO_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action=text))
    assert result["ok"] is True
    assert result["passthrough"] is False
    assert result["intent"] == "procurement:po_complete"
    assert result["target_action"] == "po_complete"
    assert result["confidence"] == 1.0
    mock_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_order_fill_skipped_when_no_procurement_skill():
    """procurement 未活跃 → 续答特征不路由 po_complete，走 LLM（sales 部署不误路由）。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_NO_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_GOOD_LLM_INQUIRY_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="货到付款"))
    mock_llm.assert_awaited_once()
    # LLM 合法对话 action 照常采用
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "inquiry_create"


@pytest.mark.asyncio
async def test_probe_order_fill_not_triggered_by_fulfillment():
    """履约回复（已交付,物流正常）不被续答路由误拦 → 落履约路由。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch("osskill.execute_api._resolve_fulfillment_skill", new_callable=AsyncMock, return_value="procurement"):
            with patch(
                "osskill.execute_api._llm_semantic_probe",
                new_callable=AsyncMock,
                return_value=_BAD_LLM_VALIDATE_PO_ROUTE,
            ) as mock_llm:
                result = await api_probe_skill("procurement", ProbeRequest(action="已交付,物流正常"))
    mock_llm.assert_awaited_once()
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "fulfillment_reply"


@pytest.mark.asyncio
async def test_probe_order_fill_not_triggered_by_bidding():
    """投标消息（标书交货日期）不被续答路由误拦 → 落 bidding。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_BAD_LLM_VALIDATE_PO_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="标书交货日期怎么填"))
    mock_llm.assert_awaited_once()
    assert result["target_skill"] == "bidding"
    assert result["target_action"] == "evaluate_bid"


# ── api_probe_skill 集成：P0.5.6 确认询价确定性路由（2026-08-15） ──────────────────

@pytest.mark.parametrize("text", [
    "确认询价",
    "确认并询价",
    "发起询价",
    "开始询价：500米电缆YJV22",
])
@pytest.mark.asyncio
async def test_probe_confirm_inquiry_routes_to_po_complete(text):
    """bug 场景：LLM 把确认询价路由到 inquiry_create，P0.5.6 必须先拦截为 po_complete 且不调 LLM。
    （劫持后果：_complete_order 确认分支不执行 → 用户收不到"已按你的确认发起询价"。）"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_GOOD_LLM_INQUIRY_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action=text))
    assert result["ok"] is True
    assert result["passthrough"] is False
    assert result["intent"] == "procurement:po_complete"
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "po_complete"
    assert result["confidence"] == 1.0
    # P0.5.6 在 LLM 之前返回 → LLM 不应被调用（确定性路由真正绕过 LLM）
    mock_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_confirm_inquiry_skipped_when_no_procurement_skill():
    """procurement 未活跃（销售服）→ 确认询价短语不劫持 po_complete，走 LLM 合法路由。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_NO_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_GOOD_LLM_INQUIRY_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="确认询价"))
    mock_llm.assert_awaited_once()
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "inquiry_create"


@pytest.mark.asyncio
async def test_probe_confirm_inquiry_skipped_for_bare_inquiry():
    """裸询价（无确认短语）不被 P0.5.6 误拦 → LLM 合法路由 inquiry_create 照常采用。
    （2026-08-14 大师实锤"询价+价格词"场景必须保持 inquiry_create，不被确认路由吞掉。）"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_GOOD_LLM_INQUIRY_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="询价 10 台变压器"))
    mock_llm.assert_awaited_once()
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "inquiry_create"
    assert result.get("params", {}).get("product_category") == "变压器"


# ── api_probe_skill 集成：P1 LLM action 合法性/对话入口校验 ──────────────────

@pytest.mark.asyncio
async def test_probe_llm_validator_action_discarded_to_fallback():
    """LLM 把询价解析成校验器 validate_rfq → 弃用，落关键词兜底 inquiry_create（不再 failed 丢回复）。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_BAD_LLM_VALIDATE_PO_ROUTE,  # validate_po 同属校验器系列
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="询价 10 台变压器"))
    mock_llm.assert_awaited_once()
    assert result["passthrough"] is False
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "inquiry_create"


@pytest.mark.asyncio
async def test_probe_llm_hallucinated_action_discarded_to_fallback():
    """LLM 幻觉不存在的 action（procurement 无此 enum）→ 弃用，落关键词兜底。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value={"matched_skill": "procurement", "matched_action": "nonexistent_action", "confidence": 0.9},
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="询价 10 台变压器"))
    mock_llm.assert_awaited_once()
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "inquiry_create"


@pytest.mark.asyncio
async def test_probe_llm_valid_action_adopted():
    """LLM 路由出合法对话 action（inquiry_create）→ 照常采用，带参数透传。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_GOOD_LLM_INQUIRY_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action="询价 10 台变压器"))
    mock_llm.assert_awaited_once()
    assert result["passthrough"] is False
    assert result["target_skill"] == "procurement"
    assert result["target_action"] == "inquiry_create"
    assert result.get("params", {}).get("product_category") == "变压器"


# ── api_probe_skill 集成：非下单/续答消息 → LLM 或关键词兜底 ──────────────────

@pytest.mark.parametrize("text,expect_skill,expect_action", [
    ("帮我写一份标书", "bidding", "generate_bid"),          # 写 → generate 强信号
    ("今天电缆价格是多少", "sales", "query_quote"),          # 价格 → query_quote
    ("生成报价单", "procurement", "inquiry_create"),         # 报价 → inquiry_create
    ("会议室预约", None, None),                              # 日常 → passthrough
])
@pytest.mark.asyncio
async def test_probe_non_order_llm_result_validation(text, expect_skill, expect_action):
    """非下单/续答消息不被确定性路由拦截；LLM 结果若是校验器/幻觉 action → 弃用落关键词兜底。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_BAD_LLM_VALIDATE_PO_ROUTE,
        ) as mock_llm:
            result = await api_probe_skill("procurement", ProbeRequest(action=text))
    mock_llm.assert_awaited_once()
    if expect_skill is None:
        assert result["passthrough"] is True
    else:
        assert result["passthrough"] is False
        assert result["target_skill"] == expect_skill
        assert result["target_action"] == expect_action


# ── 纯函数真值表：_bid_revise_match（修标书确定性路由，2026-08-14） ──────────────────

@pytest.mark.parametrize("text,record_id,feedback", [
    ("修标书 ID：123 第3章补充安全措施", 123, "第3章补充安全措施"),
    ("修改标书，ID 456，报价表改成总价100万", 456, "报价表改成总价100万"),
    ("修订标书：编号 789，技术方案补一段", 789, "技术方案补一段"),
    ("标书ID123第3章补充安全措施", 123, "标书第3章补充安全措施"),  # 弱信号：编号+补充，无"修标书"强词
    ("改标书 ID：88 第2章售后服务不满意", 88, "第2章售后服务不满意"),
    ("修标书ID为123第3章补充安全措施", 123, "第3章补充安全措施"),  # P3-1："ID为" 形态
    ("标书ID号456第3章补充安全措施", 456, "标书第3章补充安全措施"),  # P3-1："ID号" 形态
])
def test_bid_revise_match_true(text, record_id, feedback):
    """『修标书』意图（强词/弱信号）→ 命中 bidding:revise_bid，提取 record_id + user_feedback。"""
    r = _bid_revise_match(text)
    assert r is not None
    assert r["skill"] == "bidding"
    assert r["action"] == "revise_bid"
    assert r["params"]["record_id"] == record_id
    assert r["params"]["user_feedback"] == feedback


@pytest.mark.parametrize("text", [
    "帮我写一份标书",             # 生成 → 无修订强词/编号
    "评标打分，分析这份标书",     # 评分 → 无修订强词/编号
    "⏳ 正在生成投标文件（Word）...",  # 进度消息 → 无修订词
    "今天电缆价格是多少",          # 价格查询
    "下单",                       # 采购下单
    "",
    "修订采购方案",               # P1-②：修订强词但无投标上下文 → 不劫持
    "修正方案编号88的报价",       # P1-②：编号在但无投标上下文 → 不劫持
    "标书，分析一下评分",         # 评分意图 → 无修订词
])
def test_bid_revise_match_false(text):
    """非修订意图 → 不误命中。"""
    assert _bid_revise_match(text) is None


# ── 纯函数真值表：_bidding_action_hint（关键词兜底判别修订/生成/评分） ──────────────────

@pytest.mark.parametrize("text,action", [
    ("修标书 ID：123 第3章补充安全措施", "revise_bid"),
    ("修改标书，第2章改一下", "revise_bid"),
    ("帮我写一份标书", "generate_bid"),
    ("生成投标文件", "generate_bid"),
    ("评标打分", "evaluate_bid"),
    ("分析这份标书", "evaluate_bid"),
    ("修订采购方案", "evaluate_bid"),      # P1-②：无投标上下文 → 不判修订
    ("标书，报价补充一下", "revise_bid"),   # P1-②：有投标上下文 + 补充 → 判修订
])
def test_bidding_action_hint_revise_first(text, action):
    """修订意图优先于生成/评分（避免"修标书"被生成词误吞）。"""
    assert _bidding_action_hint(text) == action


# ── api_probe_skill 集成：P0.7 修标书确定性路由 ──────────────────

# 复现潜在 bug 场景：LLM 把"修标书"误路由到生成/评分（高置信）
_BAD_LLM_REVISE_ROUTE = {
    "matched_skill": "bidding",
    "matched_action": "evaluate_bid",
    "confidence": 0.9,
    "params": {},
}


@pytest.mark.parametrize("text,record_id", [
    ("修标书 ID：123 第3章补充安全措施", 123),
    ("修改标书，ID 456，报价表改成总价100万", 456),
])
@pytest.mark.asyncio
async def test_probe_bid_revise_beats_llm(text, record_id):
    """bug 场景：LLM 把修标书误路由评分/生成，P0.7 必须先拦截为 bidding:revise_bid 且不调 LLM。"""
    with patch(
        "osskill.execute_api._llm_semantic_probe",
        new_callable=AsyncMock,
        return_value=_BAD_LLM_REVISE_ROUTE,
    ) as mock_llm:
        result = await api_probe_skill("bidding", ProbeRequest(action=text))
    assert result["ok"] is True
    assert result["passthrough"] is False
    assert result["intent"] == "bidding:revise_bid"
    assert result["target_skill"] == "bidding"
    assert result["target_action"] == "revise_bid"
    assert result["confidence"] == 1.0
    # 参数携带：record_id + user_feedback 供 bidding._revise_bid 直接使用
    assert result.get("params", {}).get("record_id") == record_id
    assert result.get("params", {}).get("user_feedback")
    # P0.7 在 LLM 之前返回 → LLM 不应被调用（确定性路由真正绕过 LLM）
    mock_llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_generate_bid_not_hijacked_by_revise_route():
    """生成意图（帮我写一份标书）不被修订路由误拦 → LLM 坏路由弃用 → 关键词兜底 generate_bid。"""
    with patch(
        "osskill.execute_api._load_skill_routes",
        new_callable=AsyncMock,
        return_value=_PROC_ROUTE,
    ):
        with patch(
            "osskill.execute_api._llm_semantic_probe",
            new_callable=AsyncMock,
            return_value=_BAD_LLM_VALIDATE_PO_ROUTE,  # 校验器坏路由 → 弃用 → 关键词兜底
        ) as mock_llm:
            result = await api_probe_skill("bidding", ProbeRequest(action="帮我写一份标书"))
    mock_llm.assert_awaited_once()
    assert result["target_skill"] == "bidding"
    assert result["target_action"] == "generate_bid"
