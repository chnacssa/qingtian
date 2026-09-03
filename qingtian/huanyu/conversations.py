"""
智能体交互会话管理 — 对标 GB/Z 185.6

追踪多轮对话的上下文，支持:
  - 会话创建/关闭
  - 消息上下文关联
  - 会话状态流转
  - 会话历史查询
"""
from __future__ import annotations
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from pydantic import BaseModel, Field

from . import config as hcfg

logger = logging.getLogger("huanyu.conversations")

SCHEMA = hcfg.get_schema_name()

# ═══════════════════════════════════════════
# Models
# ═══════════════════════════════════════════

class ConversationContext(BaseModel):
    """会话上下文 — 一轮交互的完整快照"""
    agent_a: str = Field(..., description="发起方 Agent ID")
    agent_b: str = Field(..., description="接收方 Agent ID")
    topic: str = Field(default="", description="会话主题/意图")
    status: str = Field(default="active", description="active/paused/completed/aborted")
    message_count: int = Field(default=0, description="消息数")
    first_message_at: str = Field(default="", description="首条消息时间")
    last_message_at: str = Field(default="", description="最后消息时间")
    context: dict[str, Any] = Field(default_factory=dict, description="上下文快照(如谈判状态/订单ID)")
    conversation_id: str = Field(default="", description="会话唯一标识")


# ═══════════════════════════════════════════
# DDL (追加到 huanyu/database.py)
# ═══════════════════════════════════════════

CONVERSATION_DDL = f"""
CREATE TABLE IF NOT EXISTS {SCHEMA}.conversations (
    conversation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_a        TEXT NOT NULL,
    agent_b        TEXT NOT NULL,
    topic          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    message_count  INT NOT NULL DEFAULT 0,
    context        JSONB NOT NULL DEFAULT '{{}}',
    first_message_at TIMESTAMPTZ,
    last_message_at  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_conv_agents ON {SCHEMA}.conversations(agent_a, agent_b);
CREATE INDEX IF NOT EXISTS idx_conv_status ON {SCHEMA}.conversations(status);
"""

CONVERSATION_MESSAGE_DDL = f"""
ALTER TABLE {SCHEMA}.messages ADD COLUMN IF NOT EXISTS conversation_id UUID;
CREATE INDEX IF NOT EXISTS idx_msg_conv ON {SCHEMA}.messages(conversation_id);
"""


# ═══════════════════════════════════════════
# Service
# ═══════════════════════════════════════════

async def ensure_schema():
    """幂等创建 conversations 表"""
    from common.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CONVERSATION_DDL)
        await conn.execute(CONVERSATION_MESSAGE_DDL)


async def create_conversation(agent_a: str, agent_b: str, topic: str = "",
                              initial_context: dict | None = None) -> ConversationContext:
    """创建新会话"""
    from common.db import get_pool
    pool = await get_pool()
    conv_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO {SCHEMA}.conversations
               (conversation_id, agent_a, agent_b, topic, context, first_message_at, last_message_at)
               VALUES ($1,$2,$3,$4,$5,$6,$6)""",
            conv_id, agent_a, agent_b, topic,
            json.dumps(initial_context or {}, ensure_ascii=False), now,
        )

    logger.info("会话创建: %s (%s ↔ %s)", conv_id, agent_a, agent_b)
    return ConversationContext(
        conversation_id=conv_id, agent_a=agent_a, agent_b=agent_b,
        topic=topic, context=initial_context or {},
        first_message_at=now.isoformat(), last_message_at=now.isoformat(),
    )


async def get_active_conversation(agent_a: str, agent_b: str) -> ConversationContext | None:
    """查找两 Agent 间活跃会话"""
    from common.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT * FROM {SCHEMA}.conversations
               WHERE agent_a=$1 AND agent_b=$2 AND status='active'
               ORDER BY created_at DESC LIMIT 1""",
            agent_a, agent_b,
        )
        if row:
            return _row_to_context(row)
        # 反向查找
        row = await conn.fetchrow(
            f"""SELECT * FROM {SCHEMA}.conversations
               WHERE agent_a=$2 AND agent_b=$1 AND status='active'
               ORDER BY created_at DESC LIMIT 1""",
            agent_a, agent_b,
        )
        return _row_to_context(row) if row else None


async def attach_message(conversation_id: str, message_id: str):
    """将消息关联到会话，更新计数和时间（message_id 为 messages.message_id UUID）"""
    from common.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.messages SET conversation_id=$1 WHERE message_id=$2::uuid",
            conversation_id, message_id,
        )
        await conn.execute(
            f"""UPDATE {SCHEMA}.conversations
               SET message_count=message_count+1, last_message_at=NOW()
               WHERE conversation_id=$1""",
            conversation_id,
        )


async def close_conversation(conversation_id: str, reason: str = ""):
    """关闭会话"""
    from common.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""UPDATE {SCHEMA}.conversations
               SET status='completed', closed_at=NOW(),
               context = CASE WHEN jsonb_typeof(context) = 'object'
                              THEN context || $2::jsonb
                              ELSE $2::jsonb END
               WHERE conversation_id=$1""",
            # review(2026-08-16): 原实现 json.dumps 预序列化 → jsonb 双重编码存成 string；
            # string || object 直接报错。改为传 dict + CASE 兼容历史 string 行（直接替换）。
            conversation_id, {"close_reason": reason},
        )
    logger.info("会话关闭: %s (%s)", conversation_id, reason)


async def list_conversations(agent_id: str, status: str = "",
                             limit: int = 20) -> list[ConversationContext]:
    """查询某 Agent 的会话列表"""
    from common.db import get_pool
    pool = await get_pool()
    q = f"SELECT * FROM {SCHEMA}.conversations WHERE (agent_a=$1 OR agent_b=$1)"
    params: list = [agent_id]
    if status:
        q += " AND status=$2 ORDER BY last_message_at DESC LIMIT $3"
        params += [status, limit]
    else:
        q += " ORDER BY last_message_at DESC LIMIT $2"
        params += [limit]

    async with pool.acquire() as conn:
        rows = await conn.fetch(q, *params)
        return [_row_to_context(r) for r in rows]


def _row_to_context(row) -> ConversationContext:
    return ConversationContext(
        conversation_id=str(row["conversation_id"]),
        agent_a=row["agent_a"], agent_b=row["agent_b"],
        topic=row.get("topic", ""), status=row["status"],
        message_count=row.get("message_count", 0),
        first_message_at=row["first_message_at"].isoformat() if row.get("first_message_at") else "",
        last_message_at=row["last_message_at"].isoformat() if row.get("last_message_at") else "",
        context=row.get("context", {}) or {},
    )
