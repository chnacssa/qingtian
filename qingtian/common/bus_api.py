"""
总线 — Buffer 查询 API

提供 per-agent 当日缓冲区的只读查询接口。
Agent 恢复时可通过此接口读取当日原始对话（弥补 Yongheng 粗过滤摘要丢失的细节）。

注意：buffer 是内存组件，底座崩溃后丢失。查询为空时调用方应降级到 Yongheng。
"""

import logging
from fastapi import APIRouter, HTTPException

# C2 (R11): 缓冲区 API 用的是 MessageBus 实例（含 buffer_snapshot），
# 而非 BusScheduler 中间件——此前误引入 bus_scheduler 导致端点必 500。
from .bus import bus

logger = logging.getLogger("common.bus_api")

router = APIRouter(prefix="/v1/bus", tags=["bus"])


@router.get("/buffer/{agent_id}")
async def get_agent_buffer(agent_id: str):
    """查询 Agent 当日缓冲区（只读快照，不删除数据）

    返回当日起到查询点为止的原始全量事件。
    底座崩溃后 buffer 为空，请降级到 Yongheng 恢复历史记忆。
    """
    snapshot = bus.buffer_snapshot(agent_id)
    date_str = ""
    if snapshot:
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "agent_id": agent_id,
        "date": date_str,
        "events_count": len(snapshot),
        "events": snapshot,
    }


@router.get("/buffer/{agent_id}/summary")
async def get_agent_buffer_summary(agent_id: str):
    """查询 Agent 缓冲区摘要（仅数量，不含事件内容）"""
    snapshot = bus.buffer_snapshot(agent_id)
    types = {}
    for ev in snapshot:
        t = ev.get("type", "unknown")
        types[t] = types.get(t, 0) + 1

    return {
        "agent_id": agent_id,
        "events_count": len(snapshot),
        "types": types,
    }
