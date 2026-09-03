"""
群组消息分发 — 对标 GB/Z 185.6 §6.3 群组模式

群组交互模式中，请求智能体通过"消息分发功能模块"与多服务智能体交互。
支持创建群组、邀请成员、退出群组、群组内消息分发。
"""

from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("huanyu.group")


# ── 数据模型 ──────────────────────────────

class GroupMember(BaseModel):
    agent_id: str
    role: str = "member"  # owner / member
    joined_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Group(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    description: str = ""
    owner_agent_id: str
    members: list[GroupMember] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── DDL（追加到 huanyu/database.py）────────────────

GROUP_DDL = """
-- groups — GB/Z 185.6 群组
CREATE TABLE IF NOT EXISTS huanyu.groups (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    owner_agent_id  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- group_members
CREATE TABLE IF NOT EXISTS huanyu.group_members (
    group_id    UUID NOT NULL REFERENCES huanyu.groups(id) ON DELETE CASCADE,
    agent_id    TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner','member')),
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_group_members_agent ON huanyu.group_members(agent_id);
"""


# ── 群组管理 ──────────────────────────────

class GroupManager:
    """群组管理器 — 创建/邀请/退出/分发"""

    def __init__(self, db_pool):
        self.pool = db_pool

    async def create(self, name: str, owner_agent_id: str,
                     description: str = "") -> Group:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO huanyu.groups (name, description, owner_agent_id)
                   VALUES ($1,$2,$3) RETURNING *""",
                name, description, owner_agent_id,
            )
            await conn.execute(
                """INSERT INTO huanyu.group_members (group_id, agent_id, role)
                   VALUES ($1,$2,'owner')""",
                row["id"], owner_agent_id,
            )
            logger.info("Group %s created by %s", name, owner_agent_id[:20])
            return Group(
                id=str(row["id"]),
                name=row["name"],
                description=row["description"] or "",
                owner_agent_id=row["owner_agent_id"],
                members=[GroupMember(agent_id=owner_agent_id, role="owner")],
                created_at=row["created_at"].isoformat(),
            )

    async def invite(self, group_id: str, agent_id: str) -> bool:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO huanyu.group_members (group_id, agent_id, role)
                   VALUES ($1,$2,'member') ON CONFLICT DO NOTHING""",
                group_id, agent_id,
            )
            logger.info("Agent %s invited to group %s", agent_id[:20], group_id[:8])
            return True

    async def leave(self, group_id: str, agent_id: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM huanyu.group_members WHERE group_id=$1 AND agent_id=$2",
                group_id, agent_id,
            )
            return "DELETE 1" in result

    async def get_members(self, group_id: str) -> list[GroupMember]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT agent_id, role, joined_at FROM huanyu.group_members WHERE group_id=$1",
                group_id,
            )
            return [GroupMember(
                agent_id=r["agent_id"],
                role=r["role"],
                joined_at=r["joined_at"].isoformat(),
            ) for r in rows]

    async def list_groups(self, agent_id: str) -> list[str]:
        """返回 Agent 所属的所有群组 ID"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id FROM huanyu.group_members WHERE agent_id=$1",
                agent_id,
            )
            return [str(r["group_id"]) for r in rows]
