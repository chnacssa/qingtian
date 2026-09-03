"""
司库 — REST API 路由
余额 / 充值 / 扣款 / 年费 / 发票 / 定价 / 哈希链校验
"""

import base64
import calendar
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from common.config import get as root_get
from common.db import get_pool
from huanyu.config import get_schema_name as _huanyu_schema
from huanyu.ed25519_utils import sign_message
from . import config as cfg
from . import account_service as acct
from .audit import query_finance_audit, verify_finance_audit_chain, write_finance_audit
from .finance_agent import _agent_ain, _agent_private_key
from .models import (
    RechargeRequest, DeductRequest, CheckBalanceRequest,
    AnnualPayRequest,
    InvoiceRequest, InvoiceIssueRequest, InvoiceRejectRequest, InvoiceVoidRequest,
)
from .auth import auth_dependency, require_admin, require_agent_or_admin, verify_agent_ownership

logger = logging.getLogger("siku.api")

router = APIRouter(prefix="/v1/siku", tags=["司库"])
SCHEMA = cfg.get_schema_name()
HUANYU_SCHEMA = _huanyu_schema()


def _add_one_year(dt: datetime) -> datetime:
    """时间戳加 1 年；闰日（2/29）在目标年不存在时回落 2/28。

    P2 (R11): datetime.replace(year+1) 在 2 月 29 日抛 ValueError →
    年费续费在闰日触发 500 时间炸弹。非法日期时按目标年该月实际天数回落月末。
    """
    try:
        return dt.replace(year=dt.year + 1)
    except ValueError:
        last_day = calendar.monthrange(dt.year + 1, dt.month)[1]
        return dt.replace(year=dt.year + 1, day=last_day)


# ══════════════════════════════════════════════════════════
# 账户
# ══════════════════════════════════════════════════════════

@router.get("/accounts/{agent_id}")
async def get_balance(agent_id: str, auth=require_agent_or_admin()):
    verify_agent_ownership(auth, agent_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        account = await acct.get_account(conn, agent_id)
        if not account:
            raise HTTPException(404, "账户不存在")
        return account


@router.post("/accounts/recharge")
async def recharge(req: RechargeRequest, auth=require_admin()):
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可充值")
    operator_id = auth["agent_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                result = await acct.recharge(
                    conn, req.agent_id, req.amount_fen,
                    req.idempotency_key, req.remark,
                )
            except ValueError as e:
                raise HTTPException(404, str(e))
            # 记录管理员操作
            await conn.execute(
                f"INSERT INTO {SCHEMA}.admin_operations "
                f"(operator_id, action, target_agent_id, amount_fen, txn_id, detail) "
                f"VALUES ($1,'recharge',$2,$3,$4,$5)",
                operator_id, req.agent_id, req.amount_fen,
                result.get("txn_id"),
                json.dumps({"idempotency_key": req.idempotency_key, "remark": req.remark}, ensure_ascii=False),
            )

            # 审计日志（写入失败不影响主流程）
            try:
                await write_finance_audit(conn, {
                    "agent_id": operator_id,
                    "action": "admin_recharge",
                    "event_type": "balance",
                    "target_id": req.agent_id,
                    "amount_fen": req.amount_fen,
                    "severity": "high",
                    "detail": {
                        "txn_id": result.get("txn_id"),
                        "idempotency_key": req.idempotency_key,
                        "remark": req.remark,
                    },
                })
            except Exception:
                pass
    return result


@router.post("/accounts/deduct")
async def deduct(req: DeductRequest, auth=require_agent_or_admin()):
    verify_agent_ownership(auth, req.agent_id)

    operator_id = auth["agent_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            try:
                result = await acct.deduct(
                    conn, req.agent_id, req.amount_fen,
                    req.fee_type, req.reference_id, req.idempotency_key,
                )
            except ValueError as e:
                raise HTTPException(404, str(e))

            # 审计日志
            try:
                await write_finance_audit(conn, {
                    "agent_id": operator_id,
                    "action": "account_deduct",
                    "event_type": req.fee_type or "general",
                    "target_id": req.agent_id,
                    "amount_fen": req.amount_fen,
                    "severity": "high",
                    "detail": {
                        "fee_type": req.fee_type, "reference_id": req.reference_id,
                        "idempotency_key": req.idempotency_key,
                        "txn_id": result.get("txn_id"),
                    },
                })
            except Exception:
                pass

    if result.get("error") == "INSUFFICIENT_BALANCE":
        return JSONResponse(status_code=402, content=result)
    return result


@router.post("/accounts/check")
async def check_balance(req: CheckBalanceRequest, auth=require_agent_or_admin()):
    verify_agent_ownership(auth, req.agent_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        return await acct.check_balance(conn, req.agent_id, req.required_fen)


# ══════════════════════════════════════════════════════════
# 流水
# ══════════════════════════════════════════════════════════

@router.get("/transactions/{agent_id}")
async def get_transactions(
    agent_id: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    auth=require_agent_or_admin(),
):
    verify_agent_ownership(auth, agent_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        txns = await acct.get_transactions(conn, agent_id, limit, offset)
    return {"agent_id": agent_id, "transactions": txns, "count": len(txns)}


# ══════════════════════════════════════════════════════════
# 哈希链校验
# ══════════════════════════════════════════════════════════

@router.get("/chain/verify")
async def verify_chain(
    agent_id: str = Query(..., min_length=1),
    auth=require_admin(),
):
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可校验哈希链")
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await acct.verify_chain(conn, agent_id)
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ══════════════════════════════════════════════════════════
# 年费
# ══════════════════════════════════════════════════════════

@router.get("/annual/status/{agent_id}")
async def annual_status(agent_id: str, auth=require_agent_or_admin()):
    verify_agent_ownership(auth, agent_id)

    pool = await get_pool()
    result: dict = {}
    # P1-1（9-1 修复日）：pay_to 的 finance 查询此前在 async with 块外用已释放
    # conn（215 释放→230 复用）——pay_to 恒空 + 连接复用串扰。全部挪进块内。
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT agent_id, free_months, first_paid_at, expires_at, is_expired, expired_at, "
            f"created_at, updated_at FROM {SCHEMA}.annual_fee_status WHERE agent_id = $1",
            agent_id,
        )
        if not row:
            raise HTTPException(404, "年费状态不存在，可能非 biz:seller 角色")

        result = dict(row)
        in_free = result["first_paid_at"] is None
        result["in_free_period"] = in_free
        now = datetime.now(timezone.utc)
        delta = (result["expires_at"] - now).days if result["expires_at"] else 0
        result["days_until_expiry"] = max(0, delta)

        # pay_to: 返回本底座 infra:finance agent 的收款信息
        result["pay_to"] = {}
        try:
            my_host = root_get("host", "localhost")
            finance_row = await conn.fetchrow(
                f"SELECT agent_id, ain, name FROM {HUANYU_SCHEMA}.agents "
                f"WHERE category = 'infra:finance' AND server_host = $1 AND status = 'active' "
                f"ORDER BY created_at LIMIT 1",
                my_host,
            )
            if finance_row:
                result["pay_to"] = {
                    "ain": finance_row["ain"] or "",
                    "name": finance_row["name"],
                    "payment_channels": cfg.get_payment_info(),
                }
        except Exception:
            pass

    return result


@router.post("/annual/pay")
async def annual_pay(req: AnnualPayRequest, auth=require_agent_or_admin()):
    verify_agent_ownership(auth, req.agent_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        agent = await conn.fetchrow(
            f"SELECT agent_id, category, status FROM {HUANYU_SCHEMA}.agents WHERE agent_id = $1",
            req.agent_id,
        )
        if not agent:
            raise HTTPException(404, "Agent 不存在")
        if agent["category"] != "biz:seller":
            raise HTTPException(400, "年费仅对 biz:seller 角色开放")

        afs = await conn.fetchrow(
            f"SELECT agent_id, free_months, first_paid_at, expires_at, is_expired "
            f"FROM {SCHEMA}.annual_fee_status WHERE agent_id = $1",
            req.agent_id,
        )
        if not afs:
            raise HTTPException(404, "年费状态不存在")

        annual_fee = cfg.get_annual_fee_fen()
        now = datetime.now(timezone.utc)

        if afs["expires_at"] and afs["expires_at"] > now:
            new_expires = _add_one_year(afs["expires_at"])
        else:
            new_expires = _add_one_year(now)

        async with conn.transaction():
            if req.request_id:
                # 客户端带 request_id：强幂等，重试携带相同值即去重
                idem_key = f"annual_pay:{req.agent_id}:{req.request_id}"
            else:
                # 无 request_id：退化为按计费周期幂等（一年只能缴一次），
                # 防旧秒级时间戳 key 在跨秒重试/双击时被重复扣费。
                period = new_expires.year
                idem_key = f"annual_pay:{req.agent_id}:{period}"
            result = await acct.deduct(
                conn, req.agent_id, annual_fee,
                fee_type="annual_fee", idempotency_key=idem_key,
            )
            if result.get("error") == "INSUFFICIENT_BALANCE":
                raise HTTPException(402, "余额不足，无法缴纳年费")
            if result.get("already_processed"):
                # P1 (R19): 幂等命中 —— 本请求已缴过费。若继续执行 UPDATE 会免费
                # 再延期一年（旧逻辑漏洞），故直接返回当前状态，不再延期/激活。
                return {
                    "status": "already_processed",
                    "txn_id": result.get("txn_id"),
                    "expires_at": (afs["expires_at"].isoformat()
                                   if afs["expires_at"] else None),
                    "first_paid_at": (afs["first_paid_at"].isoformat()
                                      if afs["first_paid_at"] else None),
                    "status_restored": False,
                }

            first_paid = afs["first_paid_at"] or now
            await conn.execute(
                f"UPDATE {SCHEMA}.annual_fee_status SET "
                f"first_paid_at = COALESCE(first_paid_at, NOW()), "
                f"expires_at = $1, is_expired = false, expired_at = NULL, "
                f"updated_at = NOW() WHERE agent_id = $2",
                new_expires, req.agent_id,
            )

            if agent["status"] == "inactive":
                await conn.execute(
                    f"UPDATE {HUANYU_SCHEMA}.agents SET status = 'active', updated_at = NOW() "
                    f"WHERE agent_id = $1",
                    req.agent_id,
                )
                status_restored = True
            else:
                status_restored = False

            # 审计日志 — 年费缴纳
            await write_finance_audit(conn, {
                "agent_id": auth["agent_id"],
                "action": "annual_fee_pay",
                "event_type": "annual_fee",
                "target_id": req.agent_id,
                "amount_fen": annual_fee,
                "severity": "high",
                "detail": {
                    "txn_id": result.get("txn_id"),
                    "new_expires_at": new_expires.isoformat(),
                    "first_paid_at": first_paid.isoformat() if first_paid else None,
                    "status_restored": status_restored,
                },
            })

    return {
        "status": "ok",
        "txn_id": result.get("txn_id"),
        "expires_at": new_expires.isoformat(),
        "first_paid_at": first_paid.isoformat() if first_paid else None,
        "status_restored": status_restored,
    }


# ══════════════════════════════════════════════════════════
# 定价 & 收款信息
# ══════════════════════════════════════════════════════════

@router.get("/pricing")
async def pricing():
    return {
        "cert": {
            "C1": cfg.get_cert_price_fen("c1"),
            "C2": cfg.get_cert_price_fen("c2"),
            "C3": cfg.get_cert_price_fen("c3"),
        },
        "annual_fee": cfg.get_annual_fee_fen(),
        "unit": "fen (1元 = 100分)",
    }


@router.get("/payment-info")
async def payment_info(auth=require_agent_or_admin()):
    if auth["role"] not in ("admin", "agent"):
        raise HTTPException(403, "需要 agent 或 admin 角色")
    info = cfg.get_payment_info()
    info["note"] = "扫码转账时请在备注栏填写企业全称，转账后联系管理员确认到账"
    return info


@router.get("/admin/agents/lookup")
async def lookup_agent_by_company(
    company_name: str = Query(..., min_length=1, description="企业全称或部分关键字"),
    auth=require_admin(),
):
    """管理员对账：按企业名查 agent，快速确认充值归属"""
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可用")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT agent_id::text, name, company_name, category, status, c_level, server_host "
            f"FROM {HUANYU_SCHEMA}.agents "
            f"WHERE company_name ILIKE $1 OR name ILIKE $1 "
            f"ORDER BY company_name LIMIT 20",
            f"%{company_name}%",
        )
    return {"query": company_name, "agents": [dict(r) for r in rows], "count": len(rows)}


@router.get("/admin/operations")
async def admin_operations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth=require_admin(),
):
    """管理员操作记录 — 谁在什么时间对哪个 agent 做了什么"""
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可用")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT op_id, operator_id, action, target_agent_id, amount_fen, txn_id, detail, created_at "
            f"FROM {SCHEMA}.admin_operations "
            f"ORDER BY created_at DESC LIMIT $1 OFFSET $2",
            limit, offset,
        )
    return {"operations": [dict(r) for r in rows], "count": len(rows)}


# ══════════════════════════════════════════════════════════
# 发票
# ══════════════════════════════════════════════════════════

@router.post("/invoices/request", status_code=201)
async def request_invoice(req: InvoiceRequest, auth=require_agent_or_admin()):
    verify_agent_ownership(auth, req.agent_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"INSERT INTO {SCHEMA}.invoices "
                f"(agent_id, title, tax_number, amount_fen, related_txn_ids, remark) "
                f"VALUES ($1,$2,$3,$4,$5,$6) "
                f"RETURNING invoice_id, agent_id, status, title, amount_fen, created_at",
                req.agent_id, req.title, req.tax_number, req.amount_fen,
                req.related_txn_ids, req.remark,
            )
    result = dict(row)
    return result


@router.get("/invoices/by-agent/{agent_id}")
async def list_invoices(
    agent_id: str,
    status: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    auth=require_agent_or_admin(),
):
    verify_agent_ownership(auth, agent_id)

    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                f"SELECT invoice_id, agent_id, title, amount_fen, status, file_url, "
                f"file_hash, issuer, issued_at, reject_reason, remark, created_at "
                f"FROM {SCHEMA}.invoices "
                f"WHERE agent_id = $1 AND status = $2 "
                f"ORDER BY created_at DESC LIMIT $3 OFFSET $4",
                agent_id, status, limit, offset,
            )
        else:
            rows = await conn.fetch(
                f"SELECT invoice_id, agent_id, title, amount_fen, status, file_url, "
                f"file_hash, issuer, issued_at, reject_reason, remark, created_at "
                f"FROM {SCHEMA}.invoices "
                f"WHERE agent_id = $1 "
                f"ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                agent_id, limit, offset,
            )
    return {"agent_id": agent_id, "invoices": [dict(r) for r in rows], "count": len(rows)}


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: int, auth=require_agent_or_admin()):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT invoice_id, agent_id, invoice_type, title, tax_number, amount_fen, "
            f"related_txn_ids, status, file_url, file_hash, issuer, issued_at, "
            f"reject_reason, remark, created_at, updated_at "
            f"FROM {SCHEMA}.invoices WHERE invoice_id = $1",
            invoice_id,
        )
    if not row:
        raise HTTPException(404, "发票不存在")

    verify_agent_ownership(auth, row["agent_id"])
    return dict(row)


@router.post("/invoices/issue")
async def issue_invoice(req: InvoiceIssueRequest, auth=require_admin()):
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可开具发票")
    operator_id = auth["agent_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"UPDATE {SCHEMA}.invoices SET "
                f"status = 'issued', file_url = $1, file_hash = $2, "
                f"issuer = COALESCE(NULLIF($3,''), issuer), "
                f"issued_at = NOW(), updated_at = NOW() "
                f"WHERE invoice_id = $4 AND status = 'pending' "
                f"RETURNING invoice_id, agent_id, status, issued_at",
                req.file_url, req.file_hash, req.issuer, req.invoice_id,
            )
            if row:
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.admin_operations "
                    f"(operator_id, action, target_agent_id, detail) "
                    f"VALUES ($1,'invoice_issue',$2,$3)",
                    operator_id, row["agent_id"],
                    json.dumps({"invoice_id": req.invoice_id, "file_url": req.file_url}, ensure_ascii=False),
                )
    if not row:
        raise HTTPException(400, "发票不存在或状态非 pending")
    return {"status": "ok", "invoice_id": row["invoice_id"],
            "invoice_status": row["status"], "issued_at": row["issued_at"].isoformat()}


@router.post("/invoices/reject")
async def reject_invoice(req: InvoiceRejectRequest, auth=require_admin()):
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可驳回发票")
    operator_id = auth["agent_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"UPDATE {SCHEMA}.invoices SET "
                f"status = 'rejected', reject_reason = $1, updated_at = NOW() "
                f"WHERE invoice_id = $2 AND status = 'pending' "
                f"RETURNING invoice_id, agent_id, status",
                req.reject_reason, req.invoice_id,
            )
            if row:
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.admin_operations "
                    f"(operator_id, action, target_agent_id, detail) "
                    f"VALUES ($1,'invoice_reject',$2,$3)",
                    operator_id, row["agent_id"],
                    json.dumps({"invoice_id": req.invoice_id, "reason": req.reject_reason}, ensure_ascii=False),
                )
    if not row:
        raise HTTPException(400, "发票不存在或状态非 pending")
    return {"status": "ok", "invoice_id": row["invoice_id"], "invoice_status": row["status"]}


@router.post("/invoices/void")
async def void_invoice(req: InvoiceVoidRequest, auth=require_admin()):
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可作废发票")
    operator_id = auth["agent_id"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                f"UPDATE {SCHEMA}.invoices SET "
                f"status = 'voided', reject_reason = $1, updated_at = NOW() "
                f"WHERE invoice_id = $2 AND status = 'issued' "
                f"RETURNING invoice_id, agent_id, status",
                req.reason, req.invoice_id,
            )
            if row:
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.admin_operations "
                    f"(operator_id, action, target_agent_id, detail) "
                    f"VALUES ($1,'invoice_void',$2,$3)",
                    operator_id, row["agent_id"],
                    json.dumps({"invoice_id": req.invoice_id, "reason": req.reason}, ensure_ascii=False),
                )
    if not row:
        raise HTTPException(400, "发票不存在或状态非 issued")
    return {"status": "ok", "invoice_id": row["invoice_id"], "invoice_status": row["status"]}


# ══════════════════════════════════════════════════════════
# 财务 Agent — 身份验证
# ══════════════════════════════════════════════════════════

@router.get("/finance/audit")
async def finance_audit_log(
    agent_id: str = Query(default=""),
    action: str = Query(default=""),
    event_type: str = Query(default=""),
    severity: str = Query(default=""),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    auth=require_admin(),
):
    """查询财务审计日志（仅管理员）。支持按 agent/action/event/severity 过滤。"""
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可查询审计日志")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await query_finance_audit(
            conn, agent_id=agent_id, action=action,
            event_type=event_type, severity=severity,
            limit=limit, offset=offset,
        )
    return {"audit_records": rows, "count": len(rows)}


@router.get("/finance/audit/verify")
async def finance_audit_verify(auth=require_admin()):
    """校验 finance_audit 哈希链完整性（仅管理员）。"""
    if auth["role"] != "admin":
        raise HTTPException(403, "仅管理员可校验审计链")

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await verify_finance_audit_chain(conn)
    return result


@router.post("/finance/verify-challenge")
async def finance_verify_challenge(data: dict, auth=require_agent_or_admin()):
    """Ed25519 challenge-response 身份验证。

    远程会计发来 nonce，本会计用 Ed25519 私钥签名后返回。
    供跨底座会计之间验证身份用。
    """
    ain = data.get("ain", "")
    nonce = data.get("nonce", "")
    if not ain or not nonce or len(nonce) < 16:
        raise HTTPException(400, "Missing or invalid ain/nonce")

    if _agent_private_key is None:
        raise HTTPException(500, "Finance agent private key not available")

    # P1 (R18): 签名绑定领域前缀，防 Ed25519 预言机 —— 否则调用方可拿任意消息
    # 让本会计签名，若他处用同一密钥验证交易哈希等敏感内容即被借刀签名。
    challenge = f"finance-challenge:{ain}:{nonce}"
    signature = sign_message(_agent_private_key, challenge)

    # 审计日志 — challenge 响应
    pool = await get_pool()
    async with pool.acquire() as audit_conn:
        await write_finance_audit(audit_conn, {
            "agent_id": _agent_ain or "unknown",
            "action": "challenge_response",
            "event_type": "challenge",
            "target_id": ain,
            "severity": "info",
            "detail": {"nonce": nonce[:16] + "...", "caller_ain": ain},
        })

    return {"ain": _agent_ain or "", "signature": signature, "nonce": nonce}


# ══════════════════════════════════════════════════════════
# IM 通道回调验签 & 测试
# ══════════════════════════════════════════════════════════

# P1 (R17): IM 回调此前无任何鉴权 —— 攻击者伪造 open_id/FromUserName 即可触发
# approve/reject 等操作并写审计。修复：按平台签名算法 fail-closed 验签；
# 平台 token 未配置时一律拒绝（不静默放行）。


def _sha1_hex(*parts: str) -> str:
    """微信系回调验签核心: SHA1(sorted([...]))"""
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def _verify_wechat_style(token: str, raw_body: str, query_params: dict,
                         data: dict, signature_field: str, include_body: bool) -> str:
    """SHA1(sorted([token, timestamp, nonce(, body)])) 验签 — wechat/wecom 通用。

    返回错误信息（空串 = 通过）。
    """
    timestamp = query_params.get("timestamp", "") or data.get("timestamp", "") or ""
    nonce = query_params.get("nonce", "") or data.get("nonce", "") or ""
    if not (timestamp and nonce):
        return "缺少 timestamp/nonce 签名参数"
    try:
        if abs(int(timestamp) - int(time.time())) > 300:
            return "回调时间戳超时"
    except ValueError:
        return "回调时间戳非法"

    signature = query_params.get(signature_field, "") or data.get(signature_field, "")
    if not signature:
        return f"缺少 {signature_field} 签名参数"

    parts = [token, timestamp, nonce]
    if include_body:
        parts.append(raw_body)
    if hmac.compare_digest(_sha1_hex(*parts), signature):
        return ""
    return "回调签名不匹配"


def _verify_im_callback(channel: str, data: dict, raw_body: str,
                        query_params: dict, headers: dict) -> str:
    """验证 IM 平台回调签名，返回错误信息（空串 = 通过）。

    各平台算法：
      - wechat(公众号): signature = SHA1(sorted([token, timestamp, nonce]))
      - wecom(企业微信): msg_signature = SHA1(sorted([token, timestamp, nonce, body]))
      - feishu: X-Lark-Signature = base64(HMAC-SHA256(encrypt_key, ts+nonce+raw_body))
                或事件体 header.token == verify_token
    平台 token 未配置 → fail-closed 拒绝（无法证明回调来自平台）。
    """
    ch_cfg = cfg.get_im_channel_config(channel)
    token = ch_cfg.get("token", "") or ch_cfg.get("verify_token", "")
    encrypt_key = ch_cfg.get("encrypt_key", "")

    if channel == "feishu":
        if not token and not encrypt_key:
            return "飞书 verify_token/encrypt_key 未配置，拒绝回调"
        # 事件体内嵌 header.token 校验（新事件格式）
        header = data.get("header", {})
        body_token = header.get("token", "") if isinstance(header, dict) else ""
        if token and body_token and hmac.compare_digest(str(body_token), token):
            return ""
        # 新签名头: base64(HMAC-SHA256(encrypt_key, ts+nonce+raw_body))
        if encrypt_key:
            sig = headers.get("x-lark-signature", "")
            ts = headers.get("x-lark-request-timestamp", "")
            nonce = headers.get("x-lark-request-nonce", "")
            if sig and ts and nonce:
                try:
                    if abs(int(ts) - int(time.time())) > 300:
                        return "回调时间戳超时"
                except ValueError:
                    return "回调时间戳非法"
                expected = base64.b64encode(
                    hmac.new(encrypt_key.encode(), f"{ts}{nonce}{raw_body}".encode(),
                             hashlib.sha256).digest()
                ).decode()
                if hmac.compare_digest(expected, sig):
                    return ""
        return "飞书回调签名不匹配"

    if channel == "wechat":
        if not token:
            return "微信 token 未配置，拒绝回调"
        return _verify_wechat_style(token, raw_body, query_params, data,
                                    "signature", include_body=False)

    if channel == "wecom":
        if not token:
            return "企微 token 未配置，拒绝回调"
        return _verify_wechat_style(token, raw_body, query_params, data,
                                    "msg_signature", include_body=True)

    return f"未知通道 {channel}"


async def _read_callback_json(request: Request) -> tuple[dict, str]:
    """读取回调原始体（验签用）+ 解析 JSON，返回 (data, raw_body)"""
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    if not raw_body:
        return {}, raw_body
    try:
        return json.loads(raw_body), raw_body
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(400, "回调体非 JSON")


@router.post("/im/callback/feishu")
async def im_feishu_callback(request: Request):
    """飞书卡片按钮回调 — 人类财务在飞书卡片中点击按钮触发。

    飞书开放平台配此 URL: POST /v1/siku/im/callback/feishu
    """
    data, raw_body = await _read_callback_json(request)

    # URL 验证握手：有配置 token 时校验内嵌 token，防伪造回显
    challenge = data.get("challenge", "")
    if challenge:
        ch_cfg = cfg.get_im_channel_config("feishu")
        verify_token = ch_cfg.get("verify_token", "")
        body_token = data.get("token", "")
        if verify_token and body_token and not hmac.compare_digest(str(body_token), verify_token):
            raise HTTPException(401, "飞书 URL 验证 token 不匹配")
        return {"challenge": challenge}

    err = _verify_im_callback("feishu", data, raw_body,
                              dict(request.query_params), dict(request.headers))
    if err:
        logger.warning("IM feishu 回调拒绝: %s", err)
        raise HTTPException(401, err)

    action_value = {}
    try:
        action_data = data.get("action", {}).get("value", "{}")
        if isinstance(action_data, str):
            action_value = json.loads(action_data)
        else:
            action_value = action_data
    except (json.JSONDecodeError, TypeError):
        pass

    action = action_value.get("action", "")
    user_id = data.get("open_id", data.get("user_id", ""))
    msg_id = action_value.get("message_id", "")
    value = action_value.get("value", "")

    result = await _handle_im_action("feishu", action, user_id, msg_id, value)
    return {"status": "ok", "result": result}


@router.post("/im/callback/wecom")
async def im_wecom_callback(request: Request):
    """企业微信回调 — 人类财务在企微群中 @机器人 回复。

    企微应用配此 URL: POST /v1/siku/im/callback/wecom
    """
    data, raw_body = await _read_callback_json(request)

    err = _verify_im_callback("wecom", data, raw_body,
                              dict(request.query_params), dict(request.headers))
    if err:
        logger.warning("IM wecom 回调拒绝: %s", err)
        raise HTTPException(401, err)

    text = ""
    user_id = data.get("FromUserName", data.get("user_id", ""))

    if "Msg" in data and isinstance(data["Msg"], dict):
        text = data["Msg"].get("Content", data["Msg"].get("Text", {}).get("Content", ""))
    elif "Content" in data:
        text = str(data["Content"])

    action, query = _parse_im_text(text)
    result = await _handle_im_action("wecom", action, user_id, "", query)
    return {"status": "ok", "result": result}


@router.post("/im/callback/wechat")
async def im_wechat_callback(request: Request):
    """微信回调（公众号 / 企微互通）。

    公众号配此 URL: POST /v1/siku/im/callback/wechat
    """
    data, raw_body = await _read_callback_json(request)

    err = _verify_im_callback("wechat", data, raw_body,
                              dict(request.query_params), dict(request.headers))
    if err:
        logger.warning("IM wechat 回调拒绝: %s", err)
        raise HTTPException(401, err)

    text = ""
    user_id = data.get("FromUserName", data.get("user_id", ""))

    if "Content" in data:
        text = str(data["Content"])
    elif "Msg" in data:
        text = str(data["Msg"].get("Content", ""))

    action, query = _parse_im_text(text)
    result = await _handle_im_action("wechat", action, user_id, "", query)
    return {"status": "ok", "result": result}


@router.post("/im/test-notify")
async def im_test_notify(data: dict, auth=require_admin()):
    """测试 IM 通道连通性 — 发送一条测试消息到所有已启用通道。

    POST /v1/siku/im/test-notify
    Body: {"channel": "feishu", "title": "测试", "content": "手动触发通知"}
    """
    from .chat_channel import chat_notifier, ChatPayload

    title = data.get("title", "司库 IM 通道测试")
    content = data.get("content", "如果你看到这条消息，说明 IM 通道配置正确。")
    severity = data.get("severity", "info")

    payload = ChatPayload(
        title=title, content=content, severity=severity,
        metadata={"test": "true", "operator": auth.get("agent_id", "")},
    )

    if data.get("channel"):
        from .chat_channel import FeishuChannel, WeComChannel, WeChatChannel
        ch_name = data["channel"]
        ch_map = {
            "feishu": FeishuChannel(cfg.get_im_channel_config("feishu")),
            "wecom": WeComChannel(cfg.get_im_channel_config("wecom")),
            "wechat": WeChatChannel(cfg.get_im_channel_config("wechat")),
        }
        ch = ch_map.get(ch_name)
        if ch is None:
            raise HTTPException(400, f"未知通道: {ch_name}")
        ok = await ch.send(payload)
        if not ok:
            raise HTTPException(502, f"{ch_name} 发送失败，检查 webhook 配置")
        return {"status": "ok", "channel": ch_name, "sent": True}

    results = await chat_notifier.notify(payload)
    if not any(results.values()):
        return {"status": "warning", "message": "没有启用的 IM 通道或所有通道发送失败", "results": results}
    return {"status": "ok", "results": results}


# ══════════════════════════════════════════════════════════
# IM 指令解析
# ══════════════════════════════════════════════════════════

def _parse_im_text(text: str) -> tuple[str, str]:
    """解析人类财务人员从 IM 发来的文本指令。

    返回 (action, query)，如 ("balance", "某公司")。
    """
    text = text.strip()
    patterns = [
        (r"^余额\s*(.*)", "balance"),
        (r"^流水\s*(.*)", "transactions"),
        (r"^通过\s*(.*)", "approve"),
        (r"^拒绝\s*(.*)", "reject"),
        (r"^发票\s*(.*)", "invoice"),
    ]
    for pattern, action in patterns:
        m = re.match(pattern, text)
        if m:
            return action, m.group(1).strip()
    return "unknown", text


async def _im_approve_pending_recharge(user_id: str, message_id: str) -> dict:
    """IM 人审通过待审充值单 → 执行入账（P0 接线，9-1 修复日）。

    幂等键与 Path B 自动入账完全一致（finance_agent:recharge:{message_id}）：
    重复 approve / 与其他路径入账互斥，account_service 唯一幂等索引兜底。
    """
    from huanyu import messaging as hmessaging
    from .finance_agent import _maybe_notify
    from .models import PaymentConfirmPayload

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.pending_recharges WHERE message_id = $1",
            message_id,
        )
        if not row:
            return {"status": "not_found", "note": f"无此待审充值单: {message_id}"}
        if row["status"] == "approved":
            return {"status": "already_approved",
                    "note": f"该充值单已通过（操作人 {row['decided_by']}）"}
        if row["status"] == "rejected":
            return {"status": "already_rejected",
                    "note": f"该充值单已被拒绝（操作人 {row['decided_by']}），不可再通过"}

        try:
            async with conn.transaction():
                recharge_result = await acct.recharge(
                    conn, row["payer_agent_id"], row["amount_fen"],
                    idempotency_key=f"finance_agent:recharge:{message_id}",
                    remark=f"im_approve/{row['payment_channel']}/{row['voucher_number']}",
                )
        except ValueError as e:
            logger.exception("IM approve 入账失败: %s", message_id)
            await write_finance_audit(conn, {
                "agent_id": user_id, "action": "im_approve_recharge_fail",
                "event_type": "im_human_action", "target_id": row["payer_agent_id"],
                "amount_fen": row["amount_fen"], "severity": "critical",
                "detail": {"error": str(e), "message_id": message_id},
            })
            return {"status": "error", "note": f"入账失败: {e}"}

        await conn.execute(
            f"UPDATE {SCHEMA}.pending_recharges "
            f"SET status='approved', decided_at=NOW(), decided_by=$2 "
            f"WHERE message_id=$1 AND status='pending'",
            message_id, user_id,
        )

        already = recharge_result.get("already_processed", False)
        txn_id = recharge_result.get("txn_id")
        await write_finance_audit(conn, {
            "agent_id": user_id, "action": "im_approve_recharge_ok",
            "event_type": "im_human_action", "target_id": row["payer_agent_id"],
            "amount_fen": row["amount_fen"], "severity": "high",
            "detail": {
                "txn_id": str(txn_id), "company_name": row["company_name"],
                "already_processed": already, "message_id": message_id,
            },
        })

        # 通知付款方 payment_confirm（语义同 Path B 自动入账）
        if not already:
            try:
                confirm = PaymentConfirmPayload(
                    txn_id=txn_id,
                    amount_fen=row["amount_fen"],
                    payment_channel=row["payment_channel"],
                    voucher_number=row["voucher_number"],
                    confirmed_at=datetime.now(timezone.utc).isoformat(),
                    remark=f"人工确认到账，流水号: {txn_id}",
                )
                from .finance_agent import _agent_id
                await hmessaging.send_message(
                    from_agent=_agent_id or "agent-finance-001",
                    to_agent=row["payer_agent_id"],
                    message_type="payment_confirm",
                    payload=confirm.model_dump(),
                    priority="high",
                )
            except Exception:
                logger.exception("IM approve 后发送 payment_confirm 失败: %s", message_id)

        await _maybe_notify(
            title="充值已入账（人工确认）",
            content=(f"{row['company_name']} {row['amount_fen'] / 100.0:.2f} 元已入账，"
                     f"流水号 {txn_id}。操作人：{user_id}。"),
            severity="critical",
            amount_fen=row["amount_fen"],
            metadata={"message_id": message_id, "txn_id": str(txn_id),
                      "decided_by": user_id},
        )
        return {"status": "ok", "txn_id": txn_id,
                "already_processed": already, "note": "已入账"}


async def _im_reject_pending_recharge(user_id: str, message_id: str) -> dict:
    """IM 人审拒绝待审充值单（P0 接线，9-1 修复日）。"""
    from .finance_agent import _maybe_notify

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.pending_recharges WHERE message_id = $1",
            message_id,
        )
        if not row:
            return {"status": "not_found", "note": f"无此待审充值单: {message_id}"}
        if row["status"] != "pending":
            return {"status": f"already_{row['status']}",
                    "note": f"该充值单已处理（{row['status']}，操作人 {row['decided_by']}）"}

        await conn.execute(
            f"UPDATE {SCHEMA}.pending_recharges "
            f"SET status='rejected', decided_at=NOW(), decided_by=$2 "
            f"WHERE message_id=$1 AND status='pending'",
            message_id, user_id,
        )
        await write_finance_audit(conn, {
            "agent_id": user_id, "action": "im_reject_recharge",
            "event_type": "im_human_action", "target_id": row["payer_agent_id"],
            "amount_fen": row["amount_fen"], "severity": "high",
            "detail": {"company_name": row["company_name"], "message_id": message_id},
        })
        await _maybe_notify(
            title="充值已拒绝（人工确认）",
            content=(f"{row['company_name']} {row['amount_fen'] / 100.0:.2f} 元充值单已拒绝，"
                     f"未入账。操作人：{user_id}。"),
            severity="warning",
            metadata={"message_id": message_id, "decided_by": user_id},
        )
        return {"status": "ok", "note": "已拒绝，未入账"}


async def _handle_im_action(channel: str, action: str, user_id: str,
                            message_id: str, query: str) -> dict:
    """执行人类财务通过 IM 发来的操作指令。所有操作写入审计日志。"""
    logger.info("IM 指令: channel=%s, action=%s, user=%s, query=%s",
                channel, action, user_id, query)

    # 写入审计日志 — 人类操作留痕
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            from .audit import write_finance_audit
            await write_finance_audit(conn, {
                "agent_id": user_id,
                "action": f"im_{action}",
                "event_type": "im_human_action",
                "target_id": query,
                "severity": "high" if action in ("approve", "reject") else "info",
                "detail": {
                    "channel": channel, "action": action,
                    "user_id": user_id, "query": query,
                    "message_id": message_id,
                },
            })
    except Exception:
        pass

    if action == "balance":
        return {"action": "balance", "query": query, "note": "余额查询需接入 account_service"}

    if action == "transactions":
        return {"action": "transactions", "query": query, "note": "流水查询需接入 account_service"}

    if action == "approve":
        # P0 接线（9-1 修复日）：query = 待审单 message_id（action_hint 引导
        # 财务回复 "通过 {message_id}"）。此前仅 acknowledged，人审闭环不存在。
        result = await _im_approve_pending_recharge(user_id, query)
        return {"action": "approve", "message_id": query, **result}

    if action == "reject":
        result = await _im_reject_pending_recharge(user_id, query)
        return {"action": "reject", "message_id": query, **result}

    if action == "invoice":
        return {"action": "invoice", "query": query, "note": "发票请求需接入 invoice 流程"}

    return {"action": "unknown", "query": query}


# ── 健康检查 ──────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok", "module": "siku", "version": "1.0.0"}
