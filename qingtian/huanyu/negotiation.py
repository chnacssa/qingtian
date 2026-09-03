"""
寰宇 — 谈判状态机
状态流转 + 超时自动化 + 还价上限 + 跨底座双向同步
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from . import config as hcfg

SCHEMA = hcfg.get_schema_name()
logger = logging.getLogger("huanyu.negotiation")


def _now():
    return datetime.now(timezone.utc)


# ── 跨底座同步 ────────────────────────────────────────

async def sync_negotiation_record(data: dict) -> dict:
    """跨底座同步：接收远端推送的谈判记录，upsert 到本地表。

    INSERT 时保留原始 negotiation_id/created_at，UPDATE 时只更新可变字段。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.negotiations (
                    negotiation_id, buyer_id, supplier_id, status,
                    product_category, initial_inquiry, latest_offer,
                    counter_count, max_counters, last_activity_at,
                    expires_at, created_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (negotiation_id) DO UPDATE
                SET status = EXCLUDED.status,
                    latest_offer = EXCLUDED.latest_offer,
                    counter_count = EXCLUDED.counter_count,
                    last_activity_at = EXCLUDED.last_activity_at,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = EXCLUDED.updated_at
                RETURNING negotiation_id::text, status""",
            data["negotiation_id"], data["buyer_id"], data["supplier_id"],
            data.get("status", "active"),
            data.get("product_category", ""),
            json.dumps(data.get("initial_inquiry", {})),
            json.dumps(data.get("latest_offer", {})),
            data.get("counter_count", 0),
            data.get("max_counters", 5),
            data.get("last_activity_at", _now()),
            data.get("expires_at", _now()),
            data.get("created_at", _now()),
            data.get("updated_at", _now()),
        )
    return dict(row)


async def _get_agent_server_host(agent_id: str) -> Optional[str]:
    """查询 Agent 所在底座的 host。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(
            f"SELECT server_host FROM {SCHEMA}.agents WHERE agent_id = $1",
            agent_id,
        )


async def _get_my_host() -> str:
    from common.config import get
    return get("host", "localhost")


async def _replicate_negotiation(negotiation_id: str):
    """将当前谈判记录推送到参与方所在底座（双向同步）。

    双方底座各自维护谈判副本，操作任一方都可读写本地副本。
    推送时带 X-Negotiation-Sync 头，防止接收方再次回推。
    """
    from .signing import sign_peer_message

    # 读取当前完整记录
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.negotiations WHERE negotiation_id = $1",
            negotiation_id,
        )
    if not row:
        return
    record = dict(row)

    my_host = await _get_my_host()
    buyer_host = await _get_agent_server_host(record["buyer_id"]) or ""
    supplier_host = await _get_agent_server_host(record["supplier_id"]) or ""

    # 确定推送到哪个底座：推送到与本地不同的那一方
    target_host = None
    if buyer_host and buyer_host != my_host:
        target_host = buyer_host
    elif supplier_host and supplier_host != my_host:
        target_host = supplier_host

    if not target_host:
        return  # 双方都在本地，无需跨底座

    # 解析 target_host 对应的 peer port
    peer_port = 1996
    try:
        async with pool.acquire() as conn:
            peer_row = await conn.fetchrow(
                f"SELECT port FROM {SCHEMA}.peers WHERE (name = $1 OR peer_id = $1 OR host = $1) AND status = 'active'",
                target_host,
            )
            if peer_row:
                peer_port = peer_row["port"]
    except Exception:
        pass

    # 序列化记录（时间戳 → ISO 字符串）
    sync_data = {}
    for k, v in record.items():
        if isinstance(v, datetime):
            sync_data[k] = v.isoformat()
        else:
            sync_data[k] = v

    payload_str = json.dumps(sync_data, ensure_ascii=False, sort_keys=True, default=str)
    peer_sig = sign_peer_message(payload_str)

    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"http://{target_host}:{peer_port}/peers/negotiation/sync",
                json={"record": sync_data, "peer_sig": peer_sig},
                headers={"X-Negotiation-Sync": "1"},
            )
            resp.raise_for_status()
    except Exception:
        logger.warning("replicate negotiation %s to %s:%s failed",
                       negotiation_id, target_host, peer_port)


# ── 创建 ──────────────────────────────────────────────

async def start_negotiation(
    buyer_id: str,
    supplier_id: str,
    product_category: str = "",
    initial_inquiry: Optional[dict] = None,
    max_counters: int = 5,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.negotiations
                (buyer_id, supplier_id, product_category, initial_inquiry, max_counters)
                VALUES ($1,$2,$3,$4,$5)
                RETURNING negotiation_id::text, buyer_id::text, supplier_id::text,
                          status, counter_count, expires_at, created_at""",
            buyer_id, supplier_id, product_category,
            json.dumps(initial_inquiry or {}), max_counters,
        )
    result = dict(row)

    # 跨底座同步：推送到参与方所在底座
    try:
        await _replicate_negotiation(result["negotiation_id"])
    except Exception:
        logger.exception("replicate start_negotiation failed for %s", result["negotiation_id"])

    return result


# ── 状态流转 ──────────────────────────────────────────

async def transition_negotiation(negotiation_id: str, new_status: str) -> dict:
    """手动变更谈判状态（accept/reject/cancel）"""
    valid_transitions = {
        "accepted": ("active",),
        "rejected": ("active",),
        "cancelled": ("active",),
    }
    allowed_from = valid_transitions.get(new_status, ())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""UPDATE {SCHEMA}.negotiations SET status = $1, updated_at = NOW(), last_activity_at = NOW()
                WHERE negotiation_id = $2 AND status = ANY($3)
                RETURNING negotiation_id::text, status, updated_at""",
            new_status, negotiation_id, list(allowed_from),
        )
        if not row:
            return {"status": "error", "error": f"状态不可从当前流转到 {new_status}"}

    result = dict(row)

    # 跨底座同步
    try:
        await _replicate_negotiation(negotiation_id)
    except Exception:
        logger.exception("replicate transition_negotiation failed for %s", negotiation_id)

    return result


async def record_counter(negotiation_id: str, offer: dict) -> dict:
    """记录一次还价，超限则拒绝"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        nego = await conn.fetchrow(
            f"SELECT counter_count, max_counters, status FROM {SCHEMA}.negotiations WHERE negotiation_id = $1",
            negotiation_id,
        )
        if not nego:
            return {"status": "error", "error": "谈判不存在"}
        if nego["status"] != "active":
            return {"status": "error", "error": f"谈判状态 {nego['status']} 不可还价"}
        if nego["counter_count"] >= nego["max_counters"]:
            return {"status": "error", "error": f"已达还价上限（{nego['max_counters']}次），请 accept 或 reject"}

        row = await conn.fetchrow(
            f"""UPDATE {SCHEMA}.negotiations
                SET counter_count = counter_count + 1, latest_offer = $2,
                    last_activity_at = NOW(), updated_at = NOW(),
                    status = 'counter_proposed'
                WHERE negotiation_id = $1
                RETURNING negotiation_id::text, counter_count, max_counters, status""",
            negotiation_id, json.dumps(offer),
        )
    result = dict(row)

    # 跨底座同步
    try:
        await _replicate_negotiation(negotiation_id)
    except Exception:
        logger.exception("replicate record_counter failed for %s", negotiation_id)

    return result


async def accept_counter(negotiation_id: str) -> dict:
    """对方接受当前 counter 报价，谈判自动结束为 accepted"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        nego = await conn.fetchrow(
            f"SELECT status, counter_count FROM {SCHEMA}.negotiations WHERE negotiation_id = $1",
            negotiation_id,
        )
        if not nego:
            return {"status": "error", "error": "谈判不存在"}
        if nego["status"] != "counter_proposed":
            return {"status": "error", "error": f"当前状态 {nego['status']} 不可接受 counter（需 counter_proposed）"}

        row = await conn.fetchrow(
            f"""UPDATE {SCHEMA}.negotiations SET status = 'accepted',
                last_activity_at = NOW(), updated_at = NOW()
                WHERE negotiation_id = $1 AND status = 'counter_proposed'
                RETURNING negotiation_id::text, status, counter_count""",
            negotiation_id,
        )
    result = dict(row)

    try:
        await _replicate_negotiation(negotiation_id)
    except Exception:
        logger.exception("replicate accept_counter failed for %s", negotiation_id)

    return result


async def reject_counter(negotiation_id: str) -> dict:
    """对方拒绝当前 counter 报价，谈判回到 active 状态（可继续还价）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        nego = await conn.fetchrow(
            f"SELECT status FROM {SCHEMA}.negotiations WHERE negotiation_id = $1",
            negotiation_id,
        )
        if not nego:
            return {"status": "error", "error": "谈判不存在"}
        if nego["status"] != "counter_proposed":
            return {"status": "error", "error": f"当前状态 {nego['status']} 不可拒绝 counter（需 counter_proposed）"}

        row = await conn.fetchrow(
            f"""UPDATE {SCHEMA}.negotiations SET status = 'active',
                last_activity_at = NOW(), updated_at = NOW()
                WHERE negotiation_id = $1 AND status = 'counter_proposed'
                RETURNING negotiation_id::text, status, counter_count""",
            negotiation_id,
        )
    result = dict(row)

    try:
        await _replicate_negotiation(negotiation_id)
    except Exception:
        logger.exception("replicate reject_counter failed for %s", negotiation_id)

    return result


# ── 自动谈判 ──────────────────────────────────────────

# 简易自动价格策略（按物料品类）
_AUTO_PRICE_MARKUP: dict[str, float] = {
    "阀门": 1.15,       # 基准价 +15%
    "管道": 1.12,
    "法兰": 1.10,
    "仪表": 1.20,
    "泵": 1.18,
    "电缆": 1.10,
    "标准件": 1.08,
    "钢材": 1.05,
    "其他": 1.10,
}


def _calc_quote_price(base_price: float, category: str, round_num: int) -> float:
    """根据品类加价率和谈判轮次计算报价。

    第 1 轮：按品类加价率报价
    第 2 轮：降 3%（让步）
    第 3 轮：再降 2%
    第 4 轮：再降 1%
    第 5 轮及之后：保持不变（底线）
    """
    markup = _AUTO_PRICE_MARKUP.get(category, 1.10)
    price = base_price * markup
    # 逐轮让步（与 docstring 对齐：第4轮累计降6%后触底）
    concession = {1: 0.0, 2: 0.03, 3: 0.05, 4: 0.06, 5: 0.06}.get(round_num, 0.06)
    return round(price * (1 - concession), 2)


def _calc_buyer_bid(target_price: float, round_num: int) -> float:
    """计算采购方出价。

    第 1 轮：target_price * 0.90（出价低 10%）
    第 2 轮：target_price * 0.93
    第 3 轮：target_price * 0.95
    第 4 轮：target_price * 0.97
    第 5 轮及之后：target_price * 0.98（接近成交）
    """
    bid_ratio = {1: 0.90, 2: 0.93, 3: 0.95, 4: 0.97, 5: 0.98}.get(round_num, 0.98)
    return round(target_price * bid_ratio, 2)


async def auto_negotiate(
    buyer_id: str,
    supplier_id: str,
    product_category: str,
    base_price: float,
    target_price: float,
    max_rounds: int = 5,
    initial_inquiry: Optional[dict] = None,
) -> dict:
    """两个 Agent 之间自动完成 3-5 轮谈判。

    Args:
        buyer_id: 采购方 agent_id
        supplier_id: 销售方 agent_id
        product_category: 物料品类（用于计算加价率）
        base_price: 基准价（供应商成本参考）
        target_price: 采购方目标价
        max_rounds: 最大谈判轮数
        initial_inquiry: 初始询价信息

    Returns:
        dict: {
            "negotiation_id": ...,
            "rounds": [...],
            "final_price": ...,
            "status": "accepted" | "rejected" | "max_rounds_reached",
        }
    """
    # 1. 创建谈判
    # 每轮产生 2 次还价（采购出价 + 供应商还价），预算 = 轮数 × 2，
    # 避免最后一轮前就触及 counter 上限产生幻影 round 条目。
    nego = await start_negotiation(
        buyer_id=buyer_id,
        supplier_id=supplier_id,
        product_category=product_category,
        initial_inquiry=initial_inquiry,
        max_counters=max_rounds * 2,
    )
    nego_id = nego["negotiation_id"]
    rounds: list[dict] = []

    # 2. 逐轮自动出价/还价
    for round_num in range(1, max_rounds + 1):
        # 采购方出价
        buyer_price = _calc_buyer_bid(target_price, round_num)
        buyer_offer = {
            "round": round_num,
            "side": "buyer",
            "unit_price": buyer_price,
            "total_price": buyer_price * (initial_inquiry or {}).get("quantity", 1),
            "note": f"采购方第{round_num}轮出价",
        }
        counter1 = await record_counter(nego_id, buyer_offer)
        if counter1.get("status") == "error":
            break  # 已达还价上限，提前结束
        rounds.append(buyer_offer)

        if round_num == max_rounds:
            # 最后一轮：采购方出价后自动转入 accepted
            await asyncio.sleep(0.1)
            # 检查 state：如果 supplier 还没 accept，买家 accept
            nego_cur = await get_negotiation(nego_id)
            if nego_cur and nego_cur.get("status") == "counter_proposed":
                await accept_counter(nego_id)
            break

        # 供应商还价
        seller_price = _calc_quote_price(base_price, product_category, round_num)
        seller_offer = {
            "round": round_num + 1,
            "side": "supplier",
            "unit_price": seller_price,
            "total_price": seller_price * (initial_inquiry or {}).get("quantity", 1),
            "note": f"供应商第{round_num + 1}轮报价",
        }
        counter2 = await record_counter(nego_id, seller_offer)
        if counter2.get("status") == "error":
            break  # 已达还价上限，提前结束
        rounds.append(seller_offer)

    # 3. 获取最终状态
    final_nego = await get_negotiation(nego_id)
    final_price = rounds[-1]["unit_price"] if rounds else 0
    status = final_nego.get("status", "active") if final_nego else "unknown"

    if status == "accepted":
        return {
            "negotiation_id": nego_id,
            "rounds": rounds,
            "final_price": final_price,
            "final_total": final_price * (initial_inquiry or {}).get("quantity", 1),
            "status": "accepted",
        }
    else:
        # 尝试走 accept 兜底（仅在 accept 成功时报告 accepted）
        try:
            res = await accept_counter(nego_id)
            if res.get("status") == "error":
                return {
                    "negotiation_id": nego_id,
                    "rounds": rounds,
                    "final_price": final_price,
                    "status": status,
                }
            return {
                "negotiation_id": nego_id,
                "rounds": rounds,
                "final_price": final_price,
                "status": "accepted",
            }
        except Exception:
            return {
                "negotiation_id": nego_id,
                "rounds": rounds,
                "final_price": final_price,
                "status": status,
            }


# ── 超时自动化（定时任务调用） ──────────────────────────

async def expire_stale_negotiations() -> int:
    """将到期的 active 谈判标记为 expired"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""UPDATE {SCHEMA}.negotiations SET status = 'expired', updated_at = NOW()
                WHERE status = 'active' AND expires_at < NOW()
                RETURNING negotiation_id::text""",
        )
    return len(rows)


async def notify_silent_negotiations() -> list[dict]:
    """查找超 3 天无活动的谈判，返回列表供通知"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT negotiation_id::text, buyer_id::text, supplier_id::text,
                       product_category, last_activity_at, expires_at
                FROM {SCHEMA}.negotiations
                WHERE status = 'active' AND last_activity_at < NOW() - INTERVAL '3 days'""",
        )
    return [dict(r) for r in rows]


async def notify_expiring_soon() -> list[dict]:
    """查找 1 天内到期的谈判"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT negotiation_id::text, buyer_id::text, supplier_id::text,
                       product_category, expires_at
                FROM {SCHEMA}.negotiations
                WHERE status = 'active'
                  AND expires_at > NOW()
                  AND expires_at < NOW() + INTERVAL '1 day'""",
        )
    return [dict(r) for r in rows]


# ── 查询 ──────────────────────────────────────────────

async def get_negotiation(negotiation_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.negotiations WHERE negotiation_id = $1",
            negotiation_id,
        )
        return dict(row) if row else None


async def list_negotiations(agent_id: Optional[str] = None) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_id:
            rows = await conn.fetch(
                f"""SELECT negotiation_id::text, buyer_id::text, supplier_id::text,
                           product_category, status, counter_count, expires_at, created_at
                    FROM {SCHEMA}.negotiations
                    WHERE (buyer_id = $1 OR supplier_id = $1)
                    ORDER BY created_at DESC""",
                agent_id,
            )
        else:
            rows = await conn.fetch(
                f"""SELECT negotiation_id::text, buyer_id::text, supplier_id::text,
                           product_category, status, counter_count, expires_at, created_at
                    FROM {SCHEMA}.negotiations
                    ORDER BY created_at DESC""",
            )
        return [dict(r) for r in rows]


# ── 协议 ──────────────────────────────────────────────

async def create_agreement(
    negotiation_id: str,
    buyer_id: str,
    supplier_id: str,
    product: str,
    quantity: str,
    unit_price: str,
    total_price: str,
    terms: Optional[dict] = None,
    buyer_finance_ain: str = "",
    seller_finance_ain: str = "",
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 先确认谈判状态
            nego = await conn.fetchrow(
                f"SELECT status FROM {SCHEMA}.negotiations WHERE negotiation_id = $1",
                negotiation_id,
            )
            if not nego:
                return {"status": "error", "error": "谈判不存在"}
            if nego["status"] != "accepted":
                return {"status": "error", "error": "谈判未被接受，无法创建协议"}

            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.agreements
                    (negotiation_id, buyer_id, supplier_id, product, quantity, unit_price, total_price, terms,
                     buyer_finance_ain, seller_finance_ain)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    RETURNING agreement_id::text, negotiation_id::text, product, quantity, total_price, status,
                              buyer_finance_ain, seller_finance_ain, created_at""",
                negotiation_id, buyer_id, supplier_id, product, quantity,
                unit_price, total_price, json.dumps(terms or {}),
                buyer_finance_ain, seller_finance_ain,
            )
            # 更新谈判状态（与协议创建同事务，失败自动回滚）
            await conn.execute(
                f"UPDATE {SCHEMA}.negotiations SET status = 'accepted', updated_at = NOW() WHERE negotiation_id = $1",
                negotiation_id,
            )
    # zhenyue 审计: 协议创建 → severity: critical
    try:
        from zhenyue.audit_service import write_audit
        async with pool.acquire() as audit_conn:
            await write_audit(audit_conn, {
                "agent_id": buyer_id,
                "action": "create_agreement",
                "target_type": "agreement",
                "target_id": str(row["agreement_id"]),
                "severity": "critical",
                "detail": json.dumps({
                    "negotiation_id": negotiation_id,
                    "supplier_id": supplier_id,
                    "product": product,
                    "quantity": quantity,
                    "total_price": total_price,
                }),
            })
    except Exception:
        logger.exception("zhenyue audit write failed for create_agreement %s", row["agreement_id"])
    return dict(row)


async def get_agreement(agreement_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.agreements WHERE agreement_id = $1",
            agreement_id,
        )
        return dict(row) if row else None


async def list_agreements(agent_id: Optional[str] = None) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_id:
            rows = await conn.fetch(
                f"""SELECT agreement_id::text, negotiation_id::text, buyer_id::text,
                           supplier_id::text, product, quantity, total_price, status,
                           buyer_finance_ain, seller_finance_ain, created_at
                    FROM {SCHEMA}.agreements
                    WHERE buyer_id = $1 OR supplier_id = $1
                    ORDER BY created_at DESC""",
                agent_id,
            )
        else:
            rows = await conn.fetch(
                f"""SELECT agreement_id::text, negotiation_id::text, buyer_id::text,
                           supplier_id::text, product, quantity, total_price, status,
                           buyer_finance_ain, seller_finance_ain, created_at
                    FROM {SCHEMA}.agreements
                    ORDER BY created_at DESC""",
            )
        return [dict(r) for r in rows]


async def sign_agreement(agreement_id: str, signer_id: str) -> dict:
    """签署协议 — 协议状态从 active 变为 signed"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        agr = await conn.fetchrow(
            f"SELECT status, buyer_id, supplier_id FROM {SCHEMA}.agreements WHERE agreement_id = $1",
            agreement_id,
        )
        if not agr:
            return {"status": "error", "error": "协议不存在"}
        if agr["status"] != "active":
            return {"status": "error", "error": f"协议状态 {agr['status']} 不可签署"}
        if signer_id not in (agr["buyer_id"], agr["supplier_id"]):
            return {"status": "error", "error": "只有买方或卖方可以签署"}

        row = await conn.fetchrow(
            f"""UPDATE {SCHEMA}.agreements SET status = 'signed', updated_at = NOW()
                WHERE agreement_id = $1 AND status = 'active'
                RETURNING agreement_id::text, status, product, quantity, total_price""",
            agreement_id,
        )
    result = dict(row)

    try:
        from zhenyue.audit_service import write_audit
        async with pool.acquire() as audit_conn:
            await write_audit(audit_conn, {
                "agent_id": signer_id,
                "action": "sign_agreement",
                "target_type": "agreement",
                "target_id": agreement_id,
                "severity": "high",
                "detail": json.dumps({"signer": signer_id}),
            })
    except Exception:
        logger.exception("zhenyue audit write failed for sign_agreement %s", agreement_id)

    return result


async def transition_agreement(agreement_id: str, new_status: str) -> dict:
    """协议状态流转（completed/cancelled/disputed）"""
    valid_transitions = {
        "completed": ("active", "signed"),
        "cancelled": ("active", "signed"),
        "disputed": ("active", "signed"),
    }
    allowed_from = valid_transitions.get(new_status, ())

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""UPDATE {SCHEMA}.agreements SET status = $1, updated_at = NOW()
                WHERE agreement_id = $2 AND status = ANY($3)
                RETURNING agreement_id::text, status, product, quantity, total_price""",
            new_status, agreement_id, list(allowed_from),
        )
        if not row:
            return {"status": "error", "error": f"状态不可从当前流转到 {new_status}"}
    return dict(row)


async def create_po(
    agreement_id: str,
    buyer_id: str,
    supplier_id: str,
    product: str = "",
    quantity: str = "",
    unit_price: str = "",
    total_price: str = "",
    delivery_date: str = "",
    payment_terms: str = "",
) -> dict:
    """从已签署协议生成采购订单（PO）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 确认协议已签署
        agr = await conn.fetchrow(
            f"SELECT status, product, quantity, unit_price, total_price "
            f"FROM {SCHEMA}.agreements WHERE agreement_id = $1",
            agreement_id,
        )
        if not agr:
            return {"status": "error", "error": "协议不存在"}
        if agr["status"] not in ("active", "signed"):
            return {"status": "error", "error": f"协议状态 {agr['status']} 不可生成 PO（需 active 或 signed）"}

        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.purchase_orders
                (agreement_id, buyer_id, supplier_id, product, quantity, unit_price,
                 total_price, delivery_date, payment_terms, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'issued')
                RETURNING po_id::text, agreement_id::text, status, product, quantity,
                          total_price, delivery_date, created_at""",
            agreement_id, buyer_id, supplier_id,
            product or agr["product"], quantity or agr["quantity"],
            unit_price or agr["unit_price"], total_price or agr["total_price"],
            delivery_date, payment_terms,
        )
    result = dict(row)

    try:
        from zhenyue.audit_service import write_audit
        async with pool.acquire() as audit_conn:
            await write_audit(audit_conn, {
                "agent_id": buyer_id,
                "action": "create_po",
                "target_type": "purchase_order",
                "target_id": str(row["po_id"]),
                "severity": "critical",
                "detail": json.dumps({"agreement_id": agreement_id, "total_price": result["total_price"]}),
            })
    except Exception:
        logger.exception("zhenyue audit write failed for create_po %s", row["po_id"])

    return result


_PO_COLUMNS = ("po_id::text", "agreement_id::text", "buyer_id::text", "supplier_id::text",
               "product", "quantity", "unit_price", "total_price",
               "delivery_date", "payment_terms", "status", "created_at")


async def list_pos(agent_id: str = "") -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_id:
            rows = await conn.fetch(
                f"SELECT {', '.join(_PO_COLUMNS)} FROM {SCHEMA}.purchase_orders "
                f"WHERE buyer_id = $1 OR supplier_id = $1 ORDER BY created_at DESC",
                agent_id,
            )
        else:
            rows = await conn.fetch(
                f"SELECT {', '.join(_PO_COLUMNS)} FROM {SCHEMA}.purchase_orders ORDER BY created_at DESC",
            )
        return [dict(r) for r in rows]


async def get_po(po_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {', '.join(_PO_COLUMNS)} FROM {SCHEMA}.purchase_orders WHERE po_id = $1", po_id,
        )
        return dict(row) if row else None


async def transition_po(po_id: str, new_status: str) -> dict:
    """PO 状态流转（confirmed/fulfilled/cancelled）"""
    valid_transitions = {
        "confirmed": ("issued",),
        "fulfilled": ("confirmed",),
        "cancelled": ("draft", "issued"),
    }
    allowed_from = valid_transitions.get(new_status, ())
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""UPDATE {SCHEMA}.purchase_orders SET status = $1, updated_at = NOW()
                WHERE po_id = $2 AND status = ANY($3)
                RETURNING po_id::text, status, product, total_price""",
            new_status, po_id, list(allowed_from),
        )
        if not row:
            return {"status": "error", "error": f"状态不可从当前流转到 {new_status}"}
    return dict(row)


# ── 评分 ──────────────────────────────────────────────

async def submit_rating(
    from_agent: str,
    to_agent: str,
    score: float,
    agreement_id: Optional[str] = None,
    dimensions: Optional[dict] = None,
    comment: str = "",
) -> dict:
    # 兜底 clamp（DB CHECK 约束为 1.0-5.0，越界直接 500）
    score = max(1.0, min(float(score), 5.0))

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 去重：同一协议（from/to/agreement）只允许评一次
        if agreement_id:
            existing = await conn.fetchrow(
                f"SELECT rating_id FROM {SCHEMA}.ratings "
                f"WHERE from_agent = $1 AND to_agent = $2 AND agreement_id = $3",
                from_agent, to_agent, agreement_id,
            )
            if existing:
                return {"status": "error", "error": "该协议已评分，请勿重复提交"}

        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.ratings (from_agent, to_agent, agreement_id, score, dimensions, comment)
                VALUES ($1,$2,$3,$4,$5,$6)
                RETURNING rating_id::text, from_agent::text, to_agent::text, score, created_at""",
            from_agent, to_agent, agreement_id, score,
            json.dumps(dimensions or {}), comment,
        )
    # zhenyue 审计: 评分提交 → severity: medium
    try:
        from zhenyue.audit_service import write_audit
        async with pool.acquire() as audit_conn:
            await write_audit(audit_conn, {
                "agent_id": from_agent,
                "action": "submit_rating",
                "target_type": "rating",
                "target_id": str(row["rating_id"]),
                "severity": "medium",
                "detail": json.dumps({
                    "to_agent": to_agent,
                    "score": score,
                    "agreement_id": agreement_id,
                    "comment": comment,
                }),
            })
    except Exception:
        logger.exception("zhenyue audit write failed for submit_rating %s", row["rating_id"])
    return dict(row)


# ── 供应商排序 (QACP v0.6 §6.2) ──────────────────────

# C-Level 信任权重 — 认证深度决定排序权重，与 Tier（付费等级）正交
C_LEVEL_WEIGHT = {"C0": 0.3, "C1": 0.6, "C2": 1.0, "C3": 1.5}

# base_score 子因子权重（初始值，后续根据数据调优）
BASE_SCORE_WEIGHTS = {
    "industry_match": 0.30,
    "scale_match": 0.30,
    "price_score": 0.20,
    "quality_score": 0.20,
}


def compute_base_score(
    industry_match: float = 0.0,
    scale_match: float = 0.0,
    price_score: float = 0.5,
    quality_score: float = 0.5,
) -> float:
    """计算基本面得分 base_score ∈ [0, 1]

    industry_match: 行业匹配度 [0,1] — 同 ISIC 大类=1.0, 相关大类=0.5, 不相关=0
    scale_match:    规模匹配度 [0,1] — 产能 ≤ 5x 需求=1.0, >5x 递减至 0.3
    price_score:    价格得分 [0,1] — 报价/预算比归一化
    quality_score:  质量得分 [0,1] — 资质/历史履约综合
    """
    w = BASE_SCORE_WEIGHTS
    return (
        w["industry_match"] * min(industry_match, 1.0) +
        w["scale_match"] * min(scale_match, 1.0) +
        w["price_score"] * min(price_score, 1.0) +
        w["quality_score"] * min(quality_score, 1.0)
    )


def compute_final_rank(
    base_score: float,
    c_level: str = "C0",
    reputation: float = 3.0,
) -> float:
    """计算最终排序分 final_rank = base_score × c_level_weight × reputation

    c_level_weight: C0=0.3 / C1=0.6 / C2=1.0 / C3=1.5
    reputation:     [1,5] from agent_rating_summary.avg_score, default 3.0
    """
    weight = C_LEVEL_WEIGHT.get(c_level, C_LEVEL_WEIGHT["C0"])
    return round(base_score * weight * reputation, 4)


async def rank_suppliers(
    buyer_industry: str = "",
    buyer_scale: str = "",
    buyer_budget: float = 0.0,
    required_c_level: str = "C0",
    supplier_ids: Optional[list[str]] = None,
    limit: int = 20,
) -> list[dict]:
    """按 QACP v0.6 §6.2 公式对供应商排序

    final_rank = base_score × c_level_weight × reputation

    排序流程：
    1. 从 agents 表查询候选供应商（按 industry + c_level 过滤）
    2. 逐供应商计算 base_score（行业匹配 + 规模匹配 + 价格 + 质量）
    3. 查 agent_rating_summary 获取 reputation
    4. 加权得出 final_rank，降序排列
    5. 返回 Top N

    C_LEVEL_RESTRICTED: 当供应商 c_level < required_c_level 时自动排除
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. 候选池查询
        conditions = ["status = 'active'", "category = 'biz:seller'"]
        params = []
        idx = 1

        if buyer_industry:
            conditions.append(f"industry = ${idx}")
            params.append(buyer_industry)
            idx += 1

        conditions.append(f"c_level >= ${idx}")
        params.append(required_c_level)
        idx += 1

        if supplier_ids:
            placeholders = ", ".join(f"${i}" for i in range(idx, idx + len(supplier_ids)))
            conditions.append(f"agent_id::text IN ({placeholders})")
            params.extend(supplier_ids)
            idx += len(supplier_ids)

        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"SELECT agent_id::text, name, industry, c_level, scale "
            f"FROM {SCHEMA}.agents WHERE {where}",
            *params,
        )
        candidates = [dict(r) for r in rows]

        if not candidates:
            return []

        # 2. 批量查 reputation
        agent_ids = [c["agent_id"] for c in candidates]
        placeholders = ", ".join(f"${i}" for i in range(1, len(agent_ids) + 1))
        rating_rows = await conn.fetch(
            f"SELECT agent_id::text, avg_score, total_ratings "
            f"FROM {SCHEMA}.agent_rating_summary "
            f"WHERE agent_id::text IN ({placeholders})",
            *agent_ids,
        )
        rep_map: dict[str, float] = {r["agent_id"]: float(r["avg_score"] or 3.0) for r in rating_rows}

        # 3. 逐供应商计算排名
        scale_map = {"micro": 1, "small": 2, "medium": 3, "large": 4}

        def _scale_match(sup_scale: str) -> float:
            """规模匹配：同规模=1.0, 差1级=0.8, 差2级=0.5, 差3级=0.3"""
            if not buyer_scale or not sup_scale:
                return 0.5
            diff = abs(scale_map.get(buyer_scale, 2) - scale_map.get(sup_scale, 2))
            return {0: 1.0, 1: 0.8, 2: 0.5, 3: 0.3}.get(diff, 0.3)

        def _industry_match(sup_industry: str) -> float:
            """行业匹配：同行业=1.0, 任一为空=0.5, 不同=0.2"""
            if not buyer_industry or not sup_industry:
                return 0.5
            return 1.0 if buyer_industry == sup_industry else 0.2

        ranked = []
        for c in candidates:
            base = compute_base_score(
                industry_match=_industry_match(c.get("industry", "")),
                scale_match=_scale_match(c.get("scale", "")),
                # price_score 和 quality_score 默认 0.5（需谈判后才更新）
            )
            rep = rep_map.get(c["agent_id"], 3.0)
            final = compute_final_rank(base, c.get("c_level", "C0"), rep)

            ranked.append({
                "agent_id": c["agent_id"],
                "name": c["name"],
                "industry": c.get("industry", ""),
                "c_level": c.get("c_level", "C0"),
                "scale": c.get("scale", ""),
                "base_score": round(base, 4),
                "c_level_weight": C_LEVEL_WEIGHT.get(c.get("c_level", "C0"), 0.3),
                "reputation": round(rep, 2),
                "final_rank": final,
            })

        ranked.sort(key=lambda x: x["final_rank"], reverse=True)
        return ranked[:limit]


async def get_agent_ratings(agent_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.ratings WHERE to_agent = $1 ORDER BY created_at DESC",
            agent_id,
        )
        summary = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.agent_rating_summary WHERE agent_id = $1",
            agent_id,
        )
    return {
        "agent_id": agent_id,
        "avg_score": float(summary["avg_score"]) if summary and summary["avg_score"] else None,
        "total_ratings": summary["total_ratings"] if summary else 0,
        "unique_raters": summary["unique_raters"] if summary else 0,
        "ratings": [dict(r) for r in rows],
    }
