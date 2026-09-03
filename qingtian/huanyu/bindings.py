"""通道身份绑定 — 通道 open_id ↔ 规范 agent 名 归一（X 模型落地）。

同一实体在两个命名空间：飞书消息 from.open_id（通道身份）vs OpenClaw agent 名
（文件 owner / execute 携带）。下载校验 owner == agent_id 精确比较，通道身份不归一 →
403。绑定存独立表 agent_channel_bindings，由账号绑定流程动态维护（禁止硬编码进仓库）。

查询高频路径（插件每消息 resolve）走索引 (channel, channel_id) 精确匹配；
channel 未给定（插件只有裸 channel_id）时按 channel_id 去重后单 agent 判定。
"""

import logging

from common.db import get_pool
from . import config as hcfg

logger = logging.getLogger("huanyu.bindings")

# 已知通道前缀（与插件 _CHANNEL_ID_PREFIXES 对齐）
_CHANNELS = ("feishu", "dingtalk", "wechat", "slack", "discord")


async def bind_agent(agent_id: str, channel: str, channel_id: str) -> dict:
    """绑定通道身份 → 规范 agent 名。channel_id 已被其他 agent 占用 → error（防静默夺占）。"""
    agent_id = (agent_id or "").strip()
    channel = (channel or "").strip().lower()
    channel_id = (channel_id or "").strip()
    if not agent_id or not channel or not channel_id:
        return {"status": "error", "error": "agent_id/channel/channel_id 不能为空"}
    pool = await get_pool()
    schema = hcfg.get_schema_name()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            f"SELECT 1 FROM {schema}.agents WHERE agent_id = $1", agent_id
        )
        if not exists:
            return {"status": "error", "error": f"agent {agent_id} 未注册"}
        current = await conn.fetchrow(
            f"SELECT agent_id FROM {schema}.agent_channel_bindings "
            f"WHERE channel = $1 AND channel_id = $2",
            channel, channel_id,
        )
        if current and current["agent_id"] != agent_id:
            return {"status": "error",
                    "error": f"{channel}:{channel_id} 已绑定到 {current['agent_id']}"}
        await conn.execute(
            f"""INSERT INTO {schema}.agent_channel_bindings (agent_id, channel, channel_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (channel, channel_id)
                DO UPDATE SET agent_id = EXCLUDED.agent_id, updated_at = NOW()""",
            agent_id, channel, channel_id,
        )
    return {"status": "ok", "agent_id": agent_id}


async def unbind_agent(agent_id: str, channel: str, channel_id: str) -> dict:
    """解绑通道身份。"""
    channel = (channel or "").strip().lower()
    channel_id = (channel_id or "").strip()
    pool = await get_pool()
    schema = hcfg.get_schema_name()
    async with pool.acquire() as conn:
        res = await conn.execute(
            f"DELETE FROM {schema}.agent_channel_bindings "
            f"WHERE agent_id = $1 AND channel = $2 AND channel_id = $3",
            agent_id, channel, channel_id,
        )
    removed = int(res.split()[-1]) if res else 0
    return {"status": "ok", "removed": removed}


async def list_bindings(agent_id: str) -> list[dict]:
    """列出某 agent 的全部通道绑定。"""
    pool = await get_pool()
    schema = hcfg.get_schema_name()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT agent_id, channel, channel_id, created_at "
            f"FROM {schema}.agent_channel_bindings WHERE agent_id = $1 ORDER BY channel",
            agent_id,
        )
    return [dict(r) for r in rows]


async def resolve_agent(channel: str, channel_id: str) -> str | None:
    """通道身份 → 规范 agent 名；未绑定/二义 → None。

    channel 给定：精确 (channel, channel_id) 匹配。
    channel 未给定：按 channel_id 取全部 distinct agent，唯一才返回。
    """
    channel_id = (channel_id or "").strip()
    if not channel_id:
        return None
    channel = (channel or "").strip().lower()
    pool = await get_pool()
    schema = hcfg.get_schema_name()
    async with pool.acquire() as conn:
        if channel:
            return await conn.fetchval(
                f"SELECT agent_id FROM {schema}.agent_channel_bindings "
                f"WHERE channel = $1 AND channel_id = $2",
                channel, channel_id,
            )
        rows = await conn.fetch(
            f"SELECT DISTINCT agent_id FROM {schema}.agent_channel_bindings "
            f"WHERE channel_id = $1",
            channel_id,
        )
        if len(rows) == 1:
            return rows[0]["agent_id"]
    return None
