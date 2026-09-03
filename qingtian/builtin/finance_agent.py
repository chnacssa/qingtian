"""
infra:finance — 财务对账 Agent

处理 payment_notify → 银联查账 → 自动充值。
走 bus 接收 Siku 的 billing 事件。
"""

import asyncio
import logging
from typing import Optional

from common.db import get_pool
from siku import account_service as siku_acct
from siku.config import get_schema_name as _siku_schema

logger = logging.getLogger("builtin.finance")

AGENT_ID = "infra:finance-01"
SCHEMA = _siku_schema()
# 余额不足时多充 100 元（10000 分），防止再次触发告警
RECHARGE_BUFFER_FEN = 10000


async def handle_billing_event(event: dict):
    """处理司库账单事件"""
    payload = event.get("payload", {})
    event_type = event.get("type", "")

    if event_type == "billing_alert":
        # 余额不足 → 查账 → 自动充值
        agent_id = payload.get("agent_id", "")
        if not agent_id:
            return

        balance = await _check_balance(agent_id)
        if balance is not None and balance < 0:
            amount = abs(balance) + RECHARGE_BUFFER_FEN  # 多充 100 元防止再次触发
            result = await _auto_recharge(agent_id, amount)
            logger.info("Auto-recharge %s: amount=%d result=%s", agent_id, amount, result)

    elif event_type == "payment_received":
        agent_id = payload.get("agent_id", "")
        amount = payload.get("amount", 0)
        logger.info("Payment received: %s +%d", agent_id, amount)

    elif event_type == "fee_due":
        agent_id = payload.get("agent_id", "")
        days_left = payload.get("days_left", 0)
        if days_left <= 3:
            logger.warning("Fee due soon: %s in %d days", agent_id, days_left)


async def _check_balance(agent_id: str) -> Optional[int]:
    """查可用余额（单位：分）"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            account = await siku_acct.get_account(conn, agent_id)
            if not account:
                return None
            return account["available_fen"]
    except Exception as e:
        logger.error("Balance check failed for %s: %s", agent_id, e)
        return None


async def _auto_recharge(agent_id: str, amount_fen: int) -> dict:
    """自动充值（委托司库 account_service，维持哈希链记账）"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await siku_acct.recharge(
                conn, agent_id, amount_fen, remark="自动充值-余额不足"
            )
            if result.get("status") == "ok":
                return {"status": "ok", "balance": result["balance_after"]}
            return {"status": "error", "error": result.get("error", "recharge failed")}
    except ValueError:
        return {"status": "error", "error": "account not found"}
    except Exception as e:
        logger.error("Auto-recharge failed for %s: %s", agent_id, e)
        return {"status": "error", "error": str(e)}


async def run():
    """finance Agent 主循环"""
    logger.info("Finance agent %s started", AGENT_ID)
    # 每日对账任务
    while True:
        await asyncio.sleep(3600)  # 每小时检查
        try:
            await _daily_reconciliation()
        except Exception as e:
            logger.error("Daily reconciliation failed: %s", e)


async def _daily_reconciliation():
    """每日凌晨对账"""
    logger.info("Daily reconciliation started")
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 查昨日所有交易（金额单位：分）
            rows = await conn.fetch(
                f"""SELECT agent_id, SUM(amount_fen) as total_fen
                    FROM {SCHEMA}.transactions
                    WHERE created_at >= NOW() - INTERVAL '1 day'
                    GROUP BY agent_id"""
            )
            for row in rows:
                total_yuan = float(row["total_fen"] or 0) / 100
                logger.debug("  %s: yesterday total %.2f 元", row["agent_id"], total_yuan)
    except Exception as e:
        logger.error("Reconciliation query failed: %s", e)
