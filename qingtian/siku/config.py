"""
司库配置 — 从 qingtian/config.yaml 读取 siku 相关配置
"""

import os

from common.config import get


def get_schema_name() -> str:
    return get("siku.schema_name", "siku")


def get_cert_price_fen(level: str) -> int:
    key = f"siku.pricing.cert.{level.lower()}_fen"
    return get(key, 0)


def get_annual_fee_fen() -> int:
    return get("siku.pricing.annual_fee_fen", 99600)


def get_annual_free_months() -> int:
    return get("siku.annual_free_months", 12)


def get_bid_min_fen() -> int:
    return get("siku.pricing.bid.min_fen", 5000)


def get_bid_max_fen() -> int:
    return get("siku.pricing.bid.max_fen", 20000)


def get_invoice_output_dir() -> str:
    return get("siku.invoice.output_dir", "/opt/qingtian/invoices")


def get_invoice_issuer_default() -> str:
    return get("siku.invoice.issuer_default", "ACSSA财务")


def get_payment_info() -> dict:
    return get("siku.payment_account", {})


def get_im_channel_config(channel: str) -> dict:
    """获取指定 IM 通道配置 (feishu / wecom / wechat)"""
    return get(f"siku.im_channels.{channel}", {})


def im_channel_enabled(channel: str) -> bool:
    """检查指定 IM 通道是否启用"""
    return get(f"siku.im_channels.{channel}.enabled", False)


def get_im_notify_rules() -> dict:
    """获取 IM 通知规则"""
    return get("siku.im_channels.notify_rules", {})


def get_admin_token() -> str:
    return os.getenv("ZHENYUE_ADMIN_TOKEN", get("zhenyue.auth.bootstrap_admin_token", ""))


def get_finance_key_path() -> str:
    """infra:finance 财务 Agent 的 Ed25519 私钥持久化路径（C7/R11）。"""
    return get("siku.finance.key_path", "/opt/qingtian/keys/finance_agent_ed25519.pem")


# 银联查账模式（P0，9-1 修复日）：
#   stub  — 桩恒过（仅开发/测试；生产显式配置才生效）
#   manual— 默认。查账恒不自动通过：payment_notify 入待确认队列，IM 人审
#           "通过 {message_id}" 才入账（防消息总线上自铸余额）
#   off   — Path B 自动充值完全禁用
_BANK_VERIFY_MODES = ("stub", "manual", "off")


def get_bank_verify_mode() -> str:
    """银联查账模式。env SIKU_BANK_VERIFY > yaml siku.finance.bank_verify > 默认 manual。

    非法值一律回落 manual（fail-safe：宁可全部人审，不可自动入账）。
    """
    mode = (os.getenv("SIKU_BANK_VERIFY", "").strip()
            or get("siku.finance.bank_verify", "manual")).strip().lower()
    return mode if mode in _BANK_VERIFY_MODES else "manual"


GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"
