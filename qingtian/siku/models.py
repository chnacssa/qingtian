"""
司库 — Pydantic v2 数据模型
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── 账户 ──

class RechargeRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    amount_fen: int = Field(..., gt=0)
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    remark: str = Field(default="")


class DeductRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    amount_fen: int = Field(..., gt=0)
    fee_type: str = Field(default="")
    reference_id: str = Field(default="")
    idempotency_key: str = Field(default="")


class CheckBalanceRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    required_fen: int = Field(..., gt=0)


# ── 年费 ──

class AnnualPayRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    request_id: str = Field(default="", max_length=128,
                            description="客户端幂等请求号，重试/双击时携带相同值以去重，防止重复扣费")


# ── 发票 ──

class InvoiceRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1, max_length=256)
    tax_number: str = Field(default="", max_length=32)
    amount_fen: int = Field(..., gt=0)
    related_txn_ids: list[str] = Field(default_factory=list)
    remark: str = Field(default="")


class InvoiceIssueRequest(BaseModel):
    invoice_id: int = Field(..., gt=0)
    file_url: str = Field(..., min_length=1)
    file_hash: str = Field(..., min_length=1)
    issuer: str = Field(default="")


class InvoiceRejectRequest(BaseModel):
    invoice_id: int = Field(..., gt=0)
    reject_reason: str = Field(..., min_length=1)


class InvoiceVoidRequest(BaseModel):
    invoice_id: int = Field(..., gt=0)
    reason: str = Field(..., min_length=1)


# ── 财务 Agent 消息 ──

class PaymentNotifyPayload(BaseModel):
    """Agent → finance agent 的 payment_notify 消息载荷"""
    company_name: str = Field(..., min_length=1, max_length=256, description="付款企业全称")
    amount_fen: int = Field(..., gt=0, description="转账金额（分）")
    payment_channel: str = Field(default="corporate", description="渠道: corporate / wechat / alipay")
    transfer_time: Optional[str] = Field(default=None, description="转账时间 ISO8601")
    voucher_number: str = Field(default="", description="银行流水号/转账凭证号")
    remark: str = Field(default="", max_length=500)
    from_finance_ain: str = Field(default="", description="发送方财务Agent的AIN")
    to_finance_ain: str = Field(default="", description="接收方财务Agent的AIN")


class PaymentConfirmPayload(BaseModel):
    """Finance agent → paying agent 的 payment_confirm 消息载荷"""
    txn_id: int = Field(..., description="充值流水号")
    amount_fen: int = Field(..., description="确认到账金额（分）")
    payment_channel: str = Field(default="corporate")
    voucher_number: str = Field(default="")
    confirmed_at: Optional[str] = Field(default=None, description="确认时间 ISO8601")
    remark: str = Field(default="")


# ── IM 通道 ──

class ChatPayload(BaseModel):
    """统一 IM 消息载荷，finance_agent → chat_channel"""
    title: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., max_length=2000)
    severity: str = Field(default="info")
    action_buttons: list[dict] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class IMCallbackPayload(BaseModel):
    """IM 平台回调（飞书卡片按钮 / 企微回复）"""
    channel: str = Field(...)
    action: str = Field(...)
    user_id: str = Field(...)
    original_message_id: str = Field(default="")
    value: str = Field(default="")
