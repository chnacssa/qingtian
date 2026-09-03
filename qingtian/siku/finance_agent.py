"""
司库 — 财务 Agent (infra:finance)
多底座通用：同一份代码跑在所有服务器上。

处理路径：
  Path A — 我是付款方会计 (outgoing):
    agent → payment_notify → 我 → 寻址目标会计 → Ed25519 验证 → 转发

  Path B — 我是收款方会计 (incoming):
    对方会计 → payment_notify → 我 → 银联查账 → 充值 → payment_confirm
"""

import asyncio
import httpx
import json
import logging
import os
import secrets
from datetime import datetime, timezone

from common.config import get as root_get
from common.db import get_pool
from huanyu import ain as ain_mod
from huanyu import certificate as cert
from huanyu import ed25519_utils as ed
from huanyu import messaging as hmessaging
from huanyu.config import get_schema_name as _huanyu_schema
from huanyu.directory import SCHEMA as hschema
from . import account_service as acct
from . import config as cfg
from .audit import write_finance_audit
from .chat_channel import ChatPayload, chat_notifier
from .models import PaymentNotifyPayload, PaymentConfirmPayload

logger = logging.getLogger("siku.finance_agent")

SCHEMA = cfg.get_schema_name()
HUANYU_SCHEMA = _huanyu_schema()

DEFAULT_FINANCE_AGENT_NAME = "司库Agent"
DEFAULT_POLL_INTERVAL = 30

_running = False
_task: asyncio.Task | None = None
_agent_id: str | None = None
_agent_ain: str | None = None
_agent_private_key: bytes | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════
# 注册与身份
# ══════════════════════════════════════════════════════════

def _load_persisted_private_key() -> bytes | None:
    """从磁盘回读财务 Agent 私钥（C7/R11: 重启后私钥恒 None → 验证 500）。"""
    key_path = cfg.get_finance_key_path()
    try:
        if os.path.isfile(key_path):
            with open(key_path, "r", encoding="utf-8") as f:
                return ed.private_key_from_pem(f.read())
    except Exception as e:
        logger.warning("Failed to load finance agent private key from %s: %s", key_path, e)
    return None


def _persist_private_key(private_key_bytes: bytes) -> None:
    """持久化财务 Agent 私钥到受保护文件（权限 600，仅属主可读）。"""
    key_path = cfg.get_finance_key_path()
    try:
        pem = ed.private_key_to_pem(private_key_bytes)
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(pem)
        except Exception:
            os.close(fd)
            raise
        logger.info("Persisted finance agent private key to %s", key_path)
    except Exception as e:
        logger.error("Failed to persist finance agent private key to %s: %s", key_path, e)


async def _register_finance_agent(conn) -> str:
    """注册 infra:finance agent，生成 Ed25519 密钥对并持久化"""
    global _agent_private_key, _agent_ain

    existing = await conn.fetchrow(
        f"SELECT agent_id, ain FROM {HUANYU_SCHEMA}.agents "
        f"WHERE category = 'infra:finance' LIMIT 1"
    )
    if existing:
        _agent_ain = existing.get("ain", "")
        agent_id = existing["agent_id"]
        # C7 (R11): 已存在时回读私钥（新建分支才生成），否则重启后 verify-challenge 恒 500。
        if _agent_private_key is None:
            _agent_private_key = _load_persisted_private_key()
        await _write_audit(conn, agent_id, "agent_register", "lifecycle", "info",
                           detail={"ain": _agent_ain or "",
                                   "status": "existing",
                                   "key_loaded": _agent_private_key is not None})
        return agent_id

    private_key, public_key = ed.generate_keypair()
    _agent_private_key = private_key
    _persist_private_key(private_key)

    org = root_get("organization", "acssa")
    country = root_get("country", "cn")
    city = root_get("city", "hf")
    base_name = root_get("host", "localhost")
    instance = await ain_mod.next_instance(org, country, city, base_name, "infra:finance")
    agent_ain = ain_mod.generate_ain(org, country, city, base_name, "infra:finance", instance)
    _agent_ain = agent_ain

    public_key_pem = ed.public_key_to_pem(public_key)
    cert_body = cert.create_self_signed_cert(agent_ain, private_key, "alliance")

    row = await conn.fetchrow(
        f"INSERT INTO {hschema}.agents (ain, public_key, cert_fingerprint, name, category, "
        f"server_host, status, trust_level) "
        f"VALUES ($1, $2, $3, $4, 'infra:finance', $5, 'active', 'admin') "
        f"ON CONFLICT (name, server_host) DO UPDATE SET category = 'infra:finance' "
        f"RETURNING agent_id",
        agent_ain, public_key_pem, cert_body["fingerprint"],
        DEFAULT_FINANCE_AGENT_NAME, base_name,
    )
    agent_id = row["agent_id"]

    await _write_audit(conn, agent_id, "agent_register", "lifecycle", "info",
                       detail={"ain": agent_ain, "host": base_name, "status": "new"})
    return agent_id


# ══════════════════════════════════════════════════════════
# 会计寻址
# ══════════════════════════════════════════════════════════

async def _resolve_target_finance_agent(conn, company_name: str) -> dict | None:
    """按企业名找目标底座的 infra:finance agent。

    返回:
        {"local": true, "target_host": "..."}  — 目标底座就是本底座 (Path B)
        {"local": false, "agent_id": ..., "ain": ..., "server_host": ..., "public_key": ...} (Path A)
        None — 公司未找到或目标底座无会计
    """
    my_host = root_get("host", "localhost")

    target_row = await conn.fetchrow(
        f"SELECT agent_id, server_host FROM {HUANYU_SCHEMA}.agents "
        f"WHERE company_name = $1 AND status != 'deleted' "
        f"ORDER BY created_at LIMIT 1",
        company_name,
    )
    if not target_row:
        return None

    target_host = target_row["server_host"]

    if target_host == my_host:
        return {"local": True, "target_host": target_host}

    finance_row = await conn.fetchrow(
        f"SELECT agent_id, ain, name, server_host, public_key FROM {HUANYU_SCHEMA}.agents "
        f"WHERE category = 'infra:finance' AND server_host = $1 AND status = 'active' "
        f"ORDER BY created_at LIMIT 1",
        target_host,
    )
    if not finance_row:
        return None

    return {
        "local": False,
        "agent_id": finance_row["agent_id"],
        "ain": finance_row["ain"] or "",
        "name": finance_row["name"],
        "server_host": finance_row["server_host"],
        "public_key": finance_row["public_key"] or "",
    }


# ══════════════════════════════════════════════════════════
# Ed25519 身份挑战
# ══════════════════════════════════════════════════════════

async def _challenge_verify_remote(target_server_host: str, target_ain: str, public_key_pem: str) -> bool:
    """Ed25519 challenge-response 验证远程会计身份。

    1. 生成随机 nonce
    2. POST 到目标服务器的 /v1/siku/finance/verify-challenge
    3. 用对方公钥验证签名
    """
    if not public_key_pem:
        logger.warning("目标会计 %s 无 public_key，无法验证", target_ain)
        return False

    challenge_nonce = secrets.token_hex(32)
    target_url = f"http://{target_server_host}:1996/v1/siku/finance/verify-challenge"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                target_url,
                json={"ain": target_ain, "nonce": challenge_nonce},
            )
            if resp.status_code != 200:
                logger.warning("Challenge HTTP %s from %s", resp.status_code, target_server_host)
                return False
            data = resp.json()
            signature = data.get("signature", "")
    except Exception as e:
        logger.warning("Challenge request failed to %s: %s", target_server_host, e)
        return False

    try:
        pk_bytes = ed.public_key_from_pem(public_key_pem)
        # P1 (R18): 与 api.py 签名侧保持一致 —— 同样绑定 finance-challenge 领域前缀，
        # 避免把对端签名重放到其他签名用途上。
        challenge = f"finance-challenge:{target_ain}:{challenge_nonce}"
        return ed.verify_signature(pk_bytes, challenge, signature)
    except Exception as e:
        logger.warning("Challenge signature verify failed: %s", e)
        return False


# ══════════════════════════════════════════════════════════
# 银行查账
# ══════════════════════════════════════════════════════════

async def _verify_bank_transfer(
    company_name: str, amount_fen: int, payment_channel: str, voucher_number: str,
) -> dict:
    """银联查账 — 按 siku.finance.bank_verify 模式分派（P0，9-1 修复日）。

    此前桩实现无条件 matched=True：消息总线上任何 agent 发一条 payment_notify
    即可为任意本地企业无中生有充值（自铸余额原语）。修复后：
      - manual（默认）：恒不自动通过 → 入待确认队列 + IM 人审（pending_manual）
      - off           ：Path B 自动充值禁用（直接 skip）
      - stub          ：仅开发/测试，显式配置才恒过
    真实银联查账 API 接入后在此按 voucher_number 查账替换 stub 分支。
    """
    mode = cfg.get_bank_verify_mode()
    logger.info(
        "银联查账: mode=%s, company=%s, amount=%s分, channel=%s, voucher=%s",
        mode, company_name, amount_fen, payment_channel, voucher_number,
    )
    if mode == "off":
        return {"matched": False, "mode": "off",
                "reason": "bank_verify=off：自动充值禁用"}
    if mode == "manual":
        return {"matched": False, "mode": "manual", "pending_manual": True,
                "reason": "bank_verify=manual：待人工确认"}
    return {"matched": True, "mode": "stub", "verified_by": "unionpay_stub"}


async def _find_agent_by_company(conn, company_name: str) -> str | None:
    """按企业全称查找 agent_id（精确匹配）"""
    return await conn.fetchval(
        f"SELECT agent_id FROM {HUANYU_SCHEMA}.agents "
        f"WHERE company_name = $1 AND status != 'deleted' "
        f"ORDER BY created_at LIMIT 1",
        company_name,
    )


# ══════════════════════════════════════════════════════════
# 核心处理 — 两路径分发
# ══════════════════════════════════════════════════════════

async def process_payment_notify(msg: dict) -> dict:
    """处理单条 payment_notify，自动判断 Path A 或 B。

    Path A (outgoing): receipt_server != my_server → 寻址 → 验证 → 转发
    Path B (incoming): receipt_server == my_server → 查账 → 充值 → 回复
    """
    message_id = str(msg["message_id"])
    from_agent = msg["from_agent"]
    payload = msg.get("payload", {})
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {"status": "skip", "reason": "invalid_payload_json", "message_id": message_id}

    try:
        notify = PaymentNotifyPayload(**payload)
    except Exception as e:
        logger.warning("payment_notify payload 校验失败: %s", e)
        return {"status": "skip", "reason": f"invalid_payload: {e}", "message_id": message_id}

    pool = await get_pool()
    async with pool.acquire() as conn:
        target = await _resolve_target_finance_agent(conn, notify.company_name)

        if target is None:
            logger.warning("未找到企业对应的会计: %s", notify.company_name)
            return {
                "status": "skip",
                "reason": f"company not found: {notify.company_name}",
                "message_id": message_id,
            }

        if target.get("local"):
            return await _process_incoming(conn, notify, message_id, from_agent)
        else:
            return await _process_outgoing(conn, notify, message_id, from_agent, target)


async def _process_outgoing(
    conn, notify: PaymentNotifyPayload, message_id: str,
    from_agent: str, target: dict,
) -> dict:
    """Path A: 转发 payment_notify 到目标会计"""
    target_host = target.get("server_host", "")
    target_ain = target.get("ain", "")
    public_key = target.get("public_key", "")

    # Step 1: Ed25519 challenge 验证对方身份
    verified = await _challenge_verify_remote(target_host, target_ain, public_key)
    if not verified:
        logger.warning("Ed25519 challenge 失败 for %s", target_ain)
        await _write_audit(conn, _agent_id or "unknown", "challenge_fail", "challenge",
                           "warning", target_id=target_ain,
                           detail={"target_host": target_host, "message_id": message_id})
        await _maybe_notify(
            title="转款失败 — 身份验证未通过",
            content=f"目标会计 {target_ain} Ed25519 challenge 验证失败，转款已拦截。",
            severity="warning",
            metadata={"target_ain": target_ain, "message_id": message_id},
        )
        return {
            "status": "error",
            "reason": "remote finance agent identity verification failed",
            "message_id": message_id,
        }

    await _write_audit(conn, _agent_id or "unknown", "challenge_ok", "challenge",
                       "info", target_id=target_ain,
                       detail={"target_host": target_host, "message_id": message_id})

    # Step 2: 转发 payment_notify
    try:
        await hmessaging.send_message(
            from_agent=_agent_id or "unknown",
            to_agent=target["agent_id"],
            message_type="payment_notify",
            payload={
                **notify.model_dump(),
                "from_finance_ain": _agent_ain or "",
                "to_finance_ain": target_ain,
            },
            priority="high",
        )
    except Exception as e:
        logger.exception("转发 payment_notify 失败 to %s", target_ain)
        await _write_audit(conn, _agent_id or "unknown", "outgoing_forward_fail", "payment_notify",
                           "high", target_id=target_ain, amount_fen=notify.amount_fen,
                           detail={"error": str(e), "target_host": target_host, "message_id": message_id})
        await _maybe_notify(
            title="转款转发失败",
            content=f"payment_notify 转发到 {target_ain} 失败: {e}",
            severity="warning",
            metadata={"target_ain": target_ain, "message_id": message_id},
        )
        return {"status": "error", "reason": str(e), "message_id": message_id}

    await _write_audit(conn, _agent_id or "unknown", "outgoing_forward_ok", "payment_notify",
                       "high", target_id=target_ain, amount_fen=notify.amount_fen,
                       detail={
                           "company_name": notify.company_name, "channel": notify.payment_channel,
                           "target_host": target_host, "message_id": message_id,
                           "from_finance_ain": _agent_ain or "", "to_finance_ain": target_ain,
                       })

    # 标记原消息已读
    try:
        await hmessaging.mark_read(message_id)
    except Exception:
        pass

    logger.info("Path A: 转发 payment_notify → %s (%s)", target_ain, target_host)

    amount_yuan = notify.amount_fen / 100.0
    out_metadata: dict = {
        "company_name": notify.company_name, "amount_fen": str(notify.amount_fen),
        "target_ain": target_ain, "target_server": target_host,
        "channel": notify.payment_channel, "message_id": message_id,
    }
    threshold = _get_notify_threshold()
    if notify.amount_fen >= threshold:
        out_metadata["action_hint"] = (
            f"请确认转款无误后回复：通过 {message_id}  或  拒绝 {message_id}"
        )

    await _maybe_notify(
        title="转款已转发",
        content=f"付款 {amount_yuan:.2f} 元已转发给 {notify.company_name} 的会计 ({target_ain})。",
        severity="info",
        amount_fen=notify.amount_fen,
        metadata=out_metadata,
    )

    return {
        "status": "forwarded",
        "message_id": message_id,
        "target_finance_ain": target_ain,
        "target_server": target_host,
    }


async def _process_incoming(
    conn, notify: PaymentNotifyPayload, message_id: str, from_agent: str,
) -> dict:
    """Path B: 收款方会计 — 银联查账 → 充值 → 回复"""
    payer_agent_id = await _find_agent_by_company(conn, notify.company_name)
    if not payer_agent_id:
        logger.warning("本地未找到企业对应的 agent: %s", notify.company_name)
        await _write_audit(conn, _agent_id or "unknown", "incoming_skip", "payment_notify",
                           "warning", target_id=notify.company_name, amount_fen=notify.amount_fen,
                           detail={"reason": "company_not_found", "message_id": message_id})
        await _maybe_notify(
            title="入账失败 — 企业未找到",
            content=f"收款方企业 '{notify.company_name}' 在本地 agents 表中未找到。",
            severity="warning",
            metadata={"company_name": notify.company_name, "message_id": message_id},
        )
        return {
            "status": "skip",
            "reason": f"company not found locally: {notify.company_name}",
            "message_id": message_id,
        }

    bank_result = await _verify_bank_transfer(
        notify.company_name, notify.amount_fen,
        notify.payment_channel, notify.voucher_number,
    )

    # P0（9-1 修复日）：manual 模式待审入队 —— 不自动充值，IM 人审
    # "通过 {message_id}" 后才入账（_handle_im_action 接线）。
    if not bank_result.get("matched") and bank_result.get("pending_manual"):
        try:
            await conn.execute(
                f"INSERT INTO {SCHEMA}.pending_recharges "
                f"(message_id, company_name, payer_agent_id, amount_fen, "
                f" payment_channel, voucher_number) "
                f"VALUES ($1,$2,$3,$4,$5,$6) "
                f"ON CONFLICT (message_id) DO NOTHING",
                message_id, notify.company_name, payer_agent_id,
                notify.amount_fen, notify.payment_channel, notify.voucher_number,
            )
        except Exception as e:
            logger.exception("待审充值单入库失败: %s", message_id)
            await _write_audit(conn, _agent_id or "unknown", "incoming_pending_fail",
                               "payment_notify", "critical",
                               target_id=payer_agent_id, amount_fen=notify.amount_fen,
                               detail={"error": str(e), "message_id": message_id})
            return {"status": "error", "reason": str(e), "message_id": message_id}

        # 原消息标已读：待审状态由 pending_recharges 承载，防轮询重复处理
        try:
            await hmessaging.mark_read(message_id)
        except Exception:
            pass

        await _write_audit(conn, _agent_id or "unknown", "incoming_pending_manual",
                           "payment_notify", "high",
                           target_id=payer_agent_id, amount_fen=notify.amount_fen,
                           detail={
                               "company_name": notify.company_name,
                               "channel": notify.payment_channel,
                               "voucher": notify.voucher_number,
                               "message_id": message_id,
                           })
        await _maybe_notify(
            title="到账待人工确认",
            content=(
                f"{notify.company_name} 声明入账 {notify.amount_fen / 100.0:.2f} 元"
                f"（渠道 {notify.payment_channel} / 凭证 {notify.voucher_number}）。"
                f"bank_verify=manual：请核实银行流水后回复确认。"
            ),
            severity="critical",
            amount_fen=notify.amount_fen,
            metadata={
                "company_name": notify.company_name,
                "amount_fen": str(notify.amount_fen),
                "channel": notify.payment_channel, "voucher": notify.voucher_number,
                "message_id": message_id,
                "action_hint": f"核实无误回复：通过 {message_id}  或  拒绝 {message_id}",
            },
        )
        return {"status": "pending_manual", "message_id": message_id,
                "agent_id": payer_agent_id, "path": "B_incoming"}

    if not bank_result.get("matched"):
        await _write_audit(conn, _agent_id or "unknown", "incoming_bank_fail", "payment_notify",
                           "high", target_id=payer_agent_id, amount_fen=notify.amount_fen,
                           detail={
                               "company_name": notify.company_name, "channel": notify.payment_channel,
                               "voucher": notify.voucher_number, "message_id": message_id,
                               "bank_result": bank_result,
                           })
        await _maybe_notify(
            title="入账失败 — 银行查账未匹配",
            content=f"银联查账未找到匹配记录: {notify.company_name} {notify.amount_fen / 100.0:.2f}元",
            severity="warning",
            metadata={
                "company_name": notify.company_name, "amount_fen": str(notify.amount_fen),
                "channel": notify.payment_channel, "voucher": notify.voucher_number,
                "message_id": message_id,
            },
        )
        return {
            "status": "skip",
            "reason": f"bank verification failed: {bank_result.get('reason', 'unknown')}",
            "message_id": message_id,
        }

    await _write_audit(conn, _agent_id or "unknown", "incoming_bank_ok", "payment_notify",
                       "info", target_id=payer_agent_id, amount_fen=notify.amount_fen,
                       detail={
                           "company_name": notify.company_name, "channel": notify.payment_channel,
                           "voucher": notify.voucher_number, "verified_by": bank_result.get("verified_by", ""),
                           "message_id": message_id,
                       })

    idem_key = f"finance_agent:recharge:{message_id}"
    try:
        async with conn.transaction():
            recharge_result = await acct.recharge(
                conn, payer_agent_id, notify.amount_fen,
                idempotency_key=idem_key,
                remark=f"{notify.payment_channel}/{notify.voucher_number}",
            )
    except ValueError as e:
        logger.exception("充值失败: %s", e)
        await _write_audit(conn, _agent_id or "unknown", "incoming_recharge_fail", "payment_notify",
                           "critical", target_id=payer_agent_id, amount_fen=notify.amount_fen,
                           detail={"error": str(e), "message_id": message_id})
        return {"status": "error", "reason": str(e), "message_id": message_id}

    txn_id = recharge_result.get("txn_id")
    already = recharge_result.get("already_processed", False)

    if not already:
        confirm = PaymentConfirmPayload(
            txn_id=txn_id,
            amount_fen=notify.amount_fen,
            payment_channel=notify.payment_channel,
            voucher_number=notify.voucher_number,
            confirmed_at=_now_iso(),
            remark=f"已到账，流水号: {txn_id}",
        )
        try:
            await hmessaging.send_message(
                from_agent=_agent_id or "agent-finance-001",
                to_agent=payer_agent_id,
                message_type="payment_confirm",
                payload=confirm.model_dump(),
                priority="high",
            )
        except Exception:
            logger.exception("发送 payment_confirm 失败")

    try:
        await hmessaging.mark_read(message_id)
    except Exception:
        pass

    logger.info(
        "Path B: 入账处理完成. company=%s, txn_id=%s, amount=%s",
        notify.company_name, txn_id, notify.amount_fen,
    )

    await _write_audit(conn, _agent_id or "unknown", "incoming_recharge_ok", "payment_notify",
                       "high", target_id=payer_agent_id, amount_fen=notify.amount_fen,
                       detail={
                           "company_name": notify.company_name, "txn_id": str(txn_id),
                           "channel": notify.payment_channel, "voucher": notify.voucher_number,
                           "already_processed": already, "message_id": message_id,
                       })

    amount_yuan = notify.amount_fen / 100.0
    in_metadata: dict = {
        "company_name": notify.company_name, "amount_fen": str(notify.amount_fen),
        "txn_id": str(txn_id), "channel": notify.payment_channel,
        "already_processed": str(already),
    }
    threshold = _get_notify_threshold()
    if notify.amount_fen >= threshold:
        in_metadata["action_hint"] = (
            f"请确认到账无误后回复：通过 {message_id}  或  拒绝 {message_id}"
        )

    await _maybe_notify(
        title="到账确认",
        content=f"{notify.company_name} 入账 {amount_yuan:.2f} 元已确认，流水号: {txn_id}。",
        severity="info",
        amount_fen=notify.amount_fen,
        metadata=in_metadata,
    )

    return {
        "status": "ok",
        "txn_id": txn_id,
        "agent_id": payer_agent_id,
        "already_processed": already,
        "path": "B_incoming",
    }


# ══════════════════════════════════════════════════════════
# 审计 + IM 通知辅助
# ══════════════════════════════════════════════════════════

def _get_notify_threshold() -> int:
    try:
        rules = cfg.get_im_notify_rules()
        return rules.get("large_payment_threshold_fen", 0)
    except Exception:
        return 0


async def _write_audit(conn, agent_id: str, action: str, event_type: str,
                       severity: str, target_id: str = "", amount_fen: int = 0,
                       detail: dict | None = None):
    """写入 finance_audit 哈希链审计记录。失败不抛异常。"""
    try:
        await write_finance_audit(conn, {
            "agent_id": agent_id,
            "action": action,
            "event_type": event_type,
            "target_id": target_id,
            "amount_fen": amount_fen,
            "severity": severity,
            "detail": detail or {},
        })
    except Exception:
        logger.exception("审计写入异常: action=%s", action)


async def _maybe_notify(
    title: str, content: str, severity: str = "info",
    amount_fen: int = 0, metadata: dict | None = None,
):
    """根据配置规则决定是否发送 IM 通知。"""
    try:
        rules = cfg.get_im_notify_rules()
        if not rules:
            return

        threshold = rules.get("large_payment_threshold_fen", 0)
        if severity == "info" and amount_fen > threshold:
            severity = "critical"

        if severity == "critical":
            await chat_notifier.notify_critical(ChatPayload(
                title=title, content=content, severity=severity,
                metadata=metadata or {},
            ))
        else:
            await chat_notifier.notify(ChatPayload(
                title=title, content=content, severity=severity,
                metadata=metadata or {},
            ))
    except Exception:
        logger.exception("IM 通知发送异常")


# ══════════════════════════════════════════════════════════
# 轮询循环
# ══════════════════════════════════════════════════════════

async def process_inbox() -> int:
    """轮询 inbox 中未读的 payment_notify 消息并处理"""
    if not _agent_id:
        return 0

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT m.message_id, m.from_agent_id, m.to_agent_id, "
            f"m.message_type, m.payload, m.priority, m.status, m.created_at "
            f"FROM {HUANYU_SCHEMA}.messages m "
            f"WHERE m.to_agent_id = $1 AND m.message_type = 'payment_notify' "
            f"AND m.status = 'unread' "
            f"ORDER BY m.created_at LIMIT 50",
            _agent_id,
        )

    processed = 0
    for row in rows:
        msg = dict(row)
        msg["from_agent"] = msg.pop("from_agent_id")
        msg["message_id"] = str(msg.pop("message_id"))
        try:
            result = await process_payment_notify(msg)
            # pending_manual：消息已 mark_read + 待审单已入库，同样算已处理
            if result.get("status") in ("ok", "forwarded", "skip", "pending_manual"):
                processed += 1
            logger.info("payment_notify %s: %s", msg["message_id"], result.get("status"))
        except Exception:
            logger.exception("处理 payment_notify 异常: %s", msg.get("message_id"))

    return processed


async def _run_loop(poll_interval: int = DEFAULT_POLL_INTERVAL):
    global _running, _agent_id

    pool = await get_pool()
    async with pool.acquire() as conn:
        _agent_id = await _register_finance_agent(conn)

        my_host = root_get("host", "localhost")
        await _write_audit(conn, _agent_id or "unknown", "agent_start", "lifecycle",
                           "info", detail={"host": my_host, "ain": _agent_ain or ""})

    logger.info("司库 Agent 启动: agent_id=%s, ain=%s", _agent_id, _agent_ain)

    my_host = root_get("host", "localhost")
    await _maybe_notify(
        title="司库会计已上线",
        content=f"会计 Agent 已启动 @ {my_host}，AIN: {_agent_ain or 'N/A'}。",
        severity="info",
        metadata={"host": my_host, "agent_id": _agent_id or "", "ain": _agent_ain or ""},
    )

    _running = True
    while _running:
        try:
            count = await process_inbox()
            if count:
                logger.info("司库 Agent: 处理了 %s 条 payment_notify", count)
        except Exception:
            logger.exception("司库 Agent 轮询异常")
        await asyncio.sleep(poll_interval)


async def start(poll_interval: int = DEFAULT_POLL_INTERVAL):
    global _task
    if _task is not None:
        return
    _task = asyncio.create_task(_run_loop(poll_interval))
    logger.info("司库 Agent 已启动 (poll_interval=%ss)", poll_interval)


async def stop():
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
