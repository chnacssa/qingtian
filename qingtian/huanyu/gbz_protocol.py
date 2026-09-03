"""
寰宇 — GB/Z 185 协议网关

国标 = 传输层，寰宇 = 应用层（类比 TCP/IP vs HTTP）。
协议网关在收/发边界完成国标格式 ↔ 寰宇内部格式的双向转换。

设计原则：
  - 内部路由全用 AIN + 自有格式（高效）
  - 外部通信（跨底座 / API 返回）封装为国标格式（合规）
  - gbz185_id 为空时不阻塞通信
  - extensions 为开放式 dict[str, Any]，不影响国标合规性
"""

import logging
from typing import Any
from datetime import datetime, timezone

from common.db import get_pool
from . import config as hcfg

logger = logging.getLogger("huanyu.gbz_protocol")

SCHEMA = hcfg.get_schema_name()

# ── 国标 ↔ 寰宇 双身份 ──────────────────────────────────


async def get_gbz185_id(ain: str) -> str:
    """查询 AIN 对应的国标身份码，不存在返回空字符串"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            gbz_id = await conn.fetchval(
                f"SELECT gbz185_id FROM {SCHEMA}.gbz185_mappings "
                f"WHERE ain = $1 ORDER BY created_at DESC LIMIT 1",
                ain,
            )
            return gbz_id or ""
    except Exception:
        logger.debug("gbz185_mappings 查询失败: %s", ain)
        return ""


async def get_dual_identity(ain: str) -> dict[str, str]:
    """获取 AIN + 国标 双身份"""
    return {
        "ain": ain,
        "gbz185_id": await get_gbz185_id(ain),
    }


# ── GBZ Encode / Decode ──────────────────────────────────


class GBZEnvelope:
    """GB/Z 185.6 消息信封 — 编码/解码"""

    # ── Encode: 寰宇内部 → 国标格式 ──────────────

    @staticmethod
    def encode(message: dict) -> dict:
        """将寰宇内部消息封装为国标格式

        输入：寰宇自有格式（from_agent_id / to_agent_id / payload ...）
        输出：国标 185.6 信封（senderRole / taskId / artifact / from / to ...）
        """
        from_gbz = message.get("from_gbz185_id", "")
        to_gbz = message.get("to_gbz185_id", "")

        envelope = {
            # ── GB/Z 185.6 必填字段 ──
            "senderRole": message.get("sender_role", "requester"),
            "taskId": str(message.get("task_id") or ""),
            "artifact": message.get("artifact", "work_communication"),
            "final": message.get("final_flag", False),
            "chunkIndex": message.get("chunk_index"),
            "lastChunk": message.get("last_chunk", False),

            # ── 身份（185.2 双格式）──
            "from": {
                "ain": message.get("from_agent_id", ""),
                "gbz185_id": from_gbz,
            },
            "to": {
                "ain": message.get("to_agent_id", ""),
                "gbz185_id": to_gbz,
            },

            # ── 载荷 ──
            "payload": message.get("payload", {}),
            "messageType": message.get("message_type", "info"),
            "priority": message.get("priority", "normal"),
            "signature": message.get("signature", ""),
            "idempotencyKey": message.get("idempotency_key", ""),
            "replyTo": str(message["reply_to"]) if message.get("reply_to") else None,

            # ── 时间戳 ──
            "timestamp": message.get(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            ),

            # ── 寰宇扩展（不影响国标合规性）──
            "extensions": {
                "namespace": "huanyu:v1",
                "negotiationId": message.get("negotiation_id"),
                "priority": message.get("priority", "normal"),
            },
        }
        return envelope

    # ── Decode: 国标格式 → 寰宇内部 ──────────────

    @staticmethod
    def decode(envelope: dict) -> dict:
        """将国标格式解包为寰宇内部格式

        输入：国标 185.6 信封
        输出：寰宇自有格式
        """
        from_info = envelope.get("from", {})
        to_info = envelope.get("to", {})
        extensions = envelope.get("extensions", {})

        message = {
            "from_agent_id": from_info.get("ain", ""),
            "from_gbz185_id": from_info.get("gbz185_id", ""),
            "to_agent_id": to_info.get("ain", ""),
            "to_gbz185_id": to_info.get("gbz185_id", ""),
            "sender_role": envelope.get("senderRole", "requester"),
            "task_id": envelope.get("taskId"),
            "artifact": envelope.get("artifact", "work_communication"),
            "final_flag": envelope.get("final", False),
            "chunk_index": envelope.get("chunkIndex"),
            "last_chunk": envelope.get("lastChunk", False),
            "message_type": envelope.get("messageType", "info"),
            "priority": envelope.get("priority") or extensions.get("priority", "normal"),
            "signature": envelope.get("signature", ""),
            "idempotency_key": envelope.get("idempotencyKey", ""),
            "reply_to": envelope.get("replyTo"),
            "payload": envelope.get("payload", {}),
            "negotiation_id": extensions.get("negotiationId"),
            "timestamp": envelope.get("timestamp", ""),
        }
        return message

    # ── 格式检测 ──────────────────────────────────

    @staticmethod
    def is_gbz_format(data: dict) -> bool:
        """检测是否为 GB/Z 185.6 国标格式

        通过检查国标必填字段判断（senderRole / from / to）。
        """
        return (
            isinstance(data, dict)
            and "senderRole" in data
            and isinstance(data.get("from"), dict)
            and isinstance(data.get("to"), dict)
            and "ain" in data.get("from", {})
        )


# ── GBZ Agent 描述格式化 ──────────────────────────────────


async def format_agent_for_gbz(agent: dict) -> dict:
    """将 agent 记录格式化为 GB/Z 185.4/185.5 兼容的智能体描述

    内部用 AIN 为主键，对外同时返回 AIN + 国标码。
    注意：agent 中优先取 ain 字段（结构化身份码），回退到 agent_id。
    """
    ain = agent.get("ain") or agent.get("agent_id", "")
    dual = await get_dual_identity(ain)

    return {
        "ain": dual["ain"],
        "gbz185Id": dual["gbz185_id"],
        "name": agent.get("name", ""),
        "provider": agent.get("provider", ""),
        "description": agent.get("description", ""),
        "category": agent.get("category", ""),
        "industry": agent.get("industry", ""),
        "defaultInputTypes": agent.get("default_input_types", ["text"]),
        "defaultOutputTypes": agent.get("default_output_types", ["text"]),
        "capabilities": agent.get("capabilities", []),
        "skills": agent.get("skills", []),
        "trustLevel": agent.get("trust_level", "basic"),
        "status": agent.get("status", "active"),
        "serverHost": agent.get("server_host", ""),
    }
