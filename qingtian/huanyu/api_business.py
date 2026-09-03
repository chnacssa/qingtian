"""
业务路由 — 协商/签约/PO/评价（企业版）
从 api_rest.py 迁移的业务接口。
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from . import messaging as msg_svc
from . import negotiation as negosvc
from .models import (
    AgreementResponse, CreateAgreementRequest,
    NegotiationResponse, StartNegotiationRequest,
    SubmitRatingRequest,
)

logger = logging.getLogger(__name__)

business_router = APIRouter(tags=["Business"])


# ── 谈判 ────────────────────────────────────────

@business_router.post("/negotiations", response_model=NegotiationResponse)
async def start_negotiation(req: StartNegotiationRequest):
    try:
        nego = await negosvc.start_negotiation(
            buyer_id=req.buyer_id, supplier_id=req.supplier_id,
            product_category=req.product_category, initial_inquiry=req.initial_inquiry,
        )
        return NegotiationResponse(**nego)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@business_router.get("/negotiations")
async def list_negotiations(agent_id: str = Query(default="")):
    negos = await negosvc.list_negotiations(agent_id=agent_id if agent_id else None)
    return {"negotiations": negos}


@business_router.get("/negotiations/{nego_id}")
async def get_negotiation(nego_id: str):
    nego = await negosvc.get_negotiation(nego_id)
    if not nego:
        raise HTTPException(status_code=404, detail="Negotiation not found")
    return nego


@business_router.post("/negotiations/{nego_id}/transition")
async def transition_negotiation(nego_id: str, req: dict):
    ok = await negosvc.transition_negotiation(nego_id, req.get("state", ""))
    return {"status": "ok" if ok.get("status") != "error" else "error"}


@business_router.post("/negotiations/{nego_id}/counter")
async def submit_counter(nego_id: str, req: dict):
    ok = await negosvc.record_counter(negotiation_id=nego_id, offer=req.get("details", {}))
    return {"status": "ok" if ok.get("status") != "error" else "error"}


@business_router.post("/negotiations/{nego_id}/counter/accept")
async def accept_counter(nego_id: str):
    ok = await negosvc.accept_counter(nego_id)
    return {"status": "ok" if ok.get("status") != "error" else "error"}


@business_router.post("/negotiations/{nego_id}/counter/reject")
async def reject_counter(nego_id: str):
    ok = await negosvc.reject_counter(nego_id)
    return {"status": "ok" if ok.get("status") != "error" else "error"}


# ── 协议 ────────────────────────────────────────

@business_router.post("/agreements", response_model=AgreementResponse)
async def create_agreement(req: CreateAgreementRequest):
    try:
        ag = await negosvc.create_agreement(
            negotiation_id=req.negotiation_id,
            buyer_id=req.buyer_id,
            supplier_id=req.supplier_id,
            product=req.product,
            quantity=req.quantity,
            unit_price=req.unit_price,
            total_price=req.total_price,
            terms=req.terms,
            buyer_finance_ain=req.buyer_finance_ain,
            seller_finance_ain=req.seller_finance_ain,
        )
        if ag.get("status") == "error":
            raise HTTPException(status_code=400, detail=ag.get("error", "create agreement failed"))
        return AgreementResponse(**ag)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@business_router.get("/agreements")
async def list_agreements(agent_id: str = Query(default="")):
    ags = await negosvc.list_agreements(agent_id=agent_id if agent_id else None)
    return {"agreements": ags}


@business_router.get("/agreements/{agreement_id}")
async def get_agreement(agreement_id: str):
    ag = await negosvc.get_agreement(agreement_id)
    if not ag:
        raise HTTPException(status_code=404, detail="Agreement not found")
    return ag


@business_router.post("/agreements/{agreement_id}/sign")
async def sign_agreement(agreement_id: str, req: dict = None):
    """签署协议 — 请求体须含 signer_id（买方或卖方 agent_id）"""
    signer_id = (req or {}).get("signer_id", "")
    if not signer_id:
        raise HTTPException(status_code=400, detail="signer_id is required")
    ok = await negosvc.sign_agreement(agreement_id, signer_id)
    return {"status": "ok" if ok.get("status") != "error" else "error"}


@business_router.post("/agreements/{agreement_id}/transition")
async def transition_agreement(agreement_id: str, req: dict):
    ok = await negosvc.transition_agreement(agreement_id, req.get("state", ""))
    return {"status": "ok" if ok.get("status") != "error" else "error"}


# ── 评分 ────────────────────────────────────────

@business_router.post("/ratings")
async def submit_rating(req: SubmitRatingRequest):
    ok = await negosvc.submit_rating(
        from_agent=req.from_agent, to_agent=req.to_agent,
        agreement_id=req.agreement_id, score=req.score if isinstance(req.score, (int, float)) else 3.0,
        comment=req.comment,
    )
    return {"status": "ok" if ok.get("status") != "error" else "error"}


@business_router.get("/ratings/{agent_id}")
async def get_ratings(agent_id: str):
    ratings = await negosvc.get_agent_ratings(agent_id)
    return {"agent_id": agent_id, "ratings": ratings}


@business_router.get("/rank/suppliers")
async def rank_suppliers(category: str = Query(default="")):
    rank = await negosvc.rank_suppliers(buyer_industry=category if category else None)
    return {"category": category, "suppliers": rank}


# ── 一站式自动流程 ──────────────────────────────────


@business_router.post("/auto-flow")
async def auto_flow(req: dict):
    """一站式询价→采购→逐轮谈判。

    用户只需要传一次需求，系统自动完成：
    1. 询价广播 → 所有 biz:seller 收到
    2. 采购报价 → 所有 biz:seller 返回市场价
    3. 逐轮谈判 → 和每个供应商自动 3-5 轮
    4. 汇总结果 → 返回各家最终报价

    请求体:
    ```json
    {
        "buyer_agent": "proc-agent-01",
        "product_category": "阀门",
        "items": [{"sku": "A286-B25", "name": "DN25法兰球阀", "qty": 100, "base_price": 100.0}],
        "target_price": 110.0,        // 采购方目标单价
        "category": "阀门",            // 品类（加价率用）
        "max_rounds": 5,              // 最大谈判轮数
        "seller_ids": []              // 可选：指定 seller agent_id 列表，为空则广播所有
    }
    ```

    返回:
    ```json
    {
        "inquiry": {...},
        "quotes": [{...}, ...],
        "negotiations": [{...}, ...],
        "results": [{...}, ...]
    }
    ```
    """
    buyer = req.get("buyer_agent", "")
    product_cat = req.get("product_category", "")
    items = req.get("items", [])
    target_price = req.get("target_price", 0)
    category = req.get("category", "其他")
    max_rounds = req.get("max_rounds", 5)
    seller_ids = req.get("seller_ids", [])

    if not buyer:
        raise HTTPException(status_code=400, detail="buyer_agent is required")

    result = {"status": "ok", "buyer": buyer, "product_category": product_cat}

    # ── 阶段一：询价广播 ──
    inquiry_payload = {
        "type": "INQUIRY",
        "items": [
            {"sku": i.get("sku", ""), "name": i.get("name", ""), "qty": i.get("qty", 1)}
            for i in items
        ],
        "product_category": product_cat,
    }
    inquiry_result = await msg_svc.broadcast_to_category(
        from_agent=buyer,
        target_category="biz:seller",
        message_type="inquiry",
        payload=inquiry_payload,
        priority="high",
    )
    result["inquiry"] = inquiry_result

    # ── 阶段二：向找到的供应商发正式采购需求 ──
    purchase_payload = {
        "type": "PURCHASE",
        "items": items,
        "product_category": product_cat,
        "target_price": target_price,
    }
    purchase_result = await msg_svc.broadcast_to_category(
        from_agent=buyer,
        target_category="biz:seller",
        message_type="info",
        payload=purchase_payload,
        priority="high",
    )
    result["quotes"] = purchase_result

    # ── 阶段三：逐个自动谈判 ──
    # 找出所有 seller
    from common.db import get_pool
    from . import config as hcfg
    pool = await get_pool()
    async with pool.acquire() as conn:
        if seller_ids:
            rows = await conn.fetch(
                f"SELECT agent_id FROM {hcfg.get_schema_name()}.agents "
                f"WHERE agent_id = ANY($1::text[]) AND status = 'active'",
                seller_ids,
            )
        else:
            rows = await conn.fetch(
                f"SELECT agent_id FROM {hcfg.get_schema_name()}.agents "
                f"WHERE category = 'biz:seller' AND status = 'active'",
            )
    sellers = [r["agent_id"] for r in rows]

    negotiation_results = []
    for seller in sellers:
        base_price = items[0].get("base_price", 100) if items else 100
        try:
            nego_result = await negosvc.auto_negotiate(
                buyer_id=buyer,
                supplier_id=seller,
                product_category=category,
                base_price=base_price,
                target_price=target_price or base_price * 1.1,
                max_rounds=max_rounds,
                initial_inquiry=inquiry_payload,
            )
            negotiation_results.append(nego_result)
        except Exception as e:
            logger.exception("auto_negotiate with %s failed", seller)
            negotiation_results.append({
                "supplier": seller,
                "status": "error",
                "error": str(e),
            })

    result["negotiations"] = negotiation_results

    # ── 汇总 ──
    summaries = []
    for nr in negotiation_results:
        summaries.append({
            "supplier": nr.get("supplier", nr.get("negotiation_id", "")),
            "negotiation_id": nr.get("negotiation_id", ""),
            "rounds": len(nr.get("rounds", [])),
            "final_price": nr.get("final_price", 0),
            "final_total": nr.get("final_total", 0),
            "status": nr.get("status", "unknown"),
        })
    result["results"] = summaries

    return result
