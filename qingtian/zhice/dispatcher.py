"""执策分派器 — 多 Agent 协作 + WebSocket 推送通知 + IM 通道兜底

Phase 2 核心模块：
  - assign_step: Step 分派给其他 Agent（含镇岳鉴权）
  - ws_notify: 统一 WebSocket 推送 → 失败回退 IM（飞书/微信/企微）
  - get_recovery_state: 中断恢复查询
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx

from common.db import get_pool
from huanyu.config import get_schema_name as _huanyu_schema
from . import config as cfg
from .ws import ws_notify as zhice_ws_notify

logger = logging.getLogger("zhice.dispatcher")
SCHEMA = cfg.get_schema_name()

# IM 通道配置（从环境变量读取，未设则跳过）
_FEISHU_WEBHOOK = os.getenv("ZHICE_FEISHU_WEBHOOK", "")
_WECOM_WEBHOOK = os.getenv("ZHICE_WECOM_WEBHOOK", "")
_WECHAT_WEBHOOK = os.getenv("ZHICE_WECHAT_WEBHOOK", "")
# 通用 webhook（不区分通道时使用）
_IM_WEBHOOK = os.getenv("ZHICE_IM_WEBHOOK", "")


def _now():
    return datetime.now(timezone.utc)


async def _get_agent_channels(agent_id: str) -> list[str]:
    """读取 agent 的 IM 通道偏好（从 huanyu.agents.metadata 或 approval_recipients）。"""
    channels = []
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT metadata FROM {_huanyu_schema()}.agents WHERE agent_id = $1",
                agent_id,
            )
            if row:
                meta = row["metadata"] or {}
                if isinstance(meta, str):
                    meta = json.loads(meta)
                agent_channels = meta.get("im_channels") or meta.get("channels") or []
                if agent_channels:
                    channels = [c.get("type", c) if isinstance(c, dict) else c
                                for c in agent_channels]
    except Exception:
        pass
    # 未配置 → 尝试所有可用通道
    return channels or ["feishu", "wecom", "wechat"]


async def _send_im_card(channel: str, title: str, content: str) -> bool:
    """通过 IM 通道发送通知卡片。"""
    webhook = {
        "feishu": _FEISHU_WEBHOOK or _IM_WEBHOOK,
        "wecom": _WECOM_WEBHOOK or _IM_WEBHOOK,
        "wechat": _WECHAT_WEBHOOK or _IM_WEBHOOK,
    }.get(channel, "")

    if not webhook:
        return False

    # 按通道类型构建消息体
    if channel == "feishu":
        body = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": f"📋 {title}"},
                           "template": "blue"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                                     "content": content}}],
            },
        }
    elif channel == "wecom":
        body = {
            "msgtype": "markdown",
            "markdown": {"content": f"## 📋 {title}\n{content}"},
        }
    elif channel == "wechat":
        body = {
            "msgtype": "text",
            "text": {"content": f"【{title}】\n{content}"},
        }
    else:
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook, json=body)
            ok = resp.status_code < 400
            if ok:
                logger.info(f"[im] {channel} 推送成功: {title}")
            else:
                logger.warning(f"[im] {channel} 推送失败: {resp.status_code}")
            return ok
    except Exception as e:
        logger.warning(f"[im] {channel} 推送异常: {e}")
        return False


async def ws_notify(agent_id: str, event_type: str, data: dict) -> bool:
    """统一通知推送：WebSocket → IM 兜底（飞书/微信/企微）。

    优先级：zhice WS → huanyu WS → IM 通道
    """
    # 首选：zhice 自有 WebSocket
    try:
        sent = await zhice_ws_notify(agent_id, event_type, data)
        if sent:
            return True
    except Exception:
        pass

    # fallback 1：寰宇 WebSocket
    try:
        from huanyu.api_ws import manager as ws_manager
    except ImportError:
        logger.debug(f"[ws] huanyu.api_ws 不可用")
    else:
        payload = {"type": f"zhice:{event_type}", "timestamp": _now().isoformat(), **data}
        sent = await ws_manager.send_to(agent_id, payload)
        if sent:
            logger.info(f"[ws] {event_type} → {agent_id}")
            return True
        logger.debug(f"[ws] {agent_id} 不在线，回退 IM")

    # fallback 2：IM 通道（飞书/微信/企微）
    event_labels = {
        "assigned": "新任务分派", "task_completed": "任务完成", "task_failed": "任务失败",
        "timed_out": "步骤超时", "retry_exhausted": "重试耗尽", "rejected": "步骤被驳回",
        "reclaimed": "任务回收", "cancelled": "任务取消", "issue_reported": "问题上报",
        "multisig_failed": "多签失败", "reverify_requested": "要求复验",
    }
    title = event_labels.get(event_type, event_type)
    instruction = data.get("instruction", data.get("title", ""))
    task_id = data.get("task_id", "")
    step_index = data.get("step_index", "")
    reason = data.get("reason", data.get("error", ""))

    content = f"**Agent**: {agent_id}\n"
    if task_id:
        content += f"**Task**: #{task_id}"
        if step_index:
            content += f" Step {step_index}"
        content += "\n"
    if instruction:
        content += f"**内容**: {instruction[:120]}\n"
    if reason:
        content += f"**原因**: {reason[:120]}\n"

    channels = await _get_agent_channels(agent_id)
    for ch in channels:
        sent = await _send_im_card(ch, title, content)
        if sent:
            return True

    logger.debug(f"[im] {agent_id} 所有通道推送失败")
    return False


async def ws_broadcast(agent_ids: list[str], event_type: str, data: dict):
    """向多个 Agent 广播 WebSocket 通知"""
    for aid in agent_ids:
        await ws_notify(aid, event_type, data)


# ── 分派 ──────────────────────────────────────────────────

async def assign_step(
    task_id: int,
    step_index: int,
    assigned_agent: str,
    requested_by: str,
) -> dict:
    """将 Step 分派给指定 Agent

    鉴权说明：
      镇岳网关中间件（gateway/middleware.py）在 HTTP 层对 /v1/zhice/tasks/{id}/assign
      路径做能力检查。本函数做额外业务层校验：目标 Agent 存在且在线。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # 1. 校验目标 Agent 存在且 active
            target = await conn.fetchrow(
                f"SELECT agent_id, name, status FROM {_huanyu_schema()}.agents "
                "WHERE agent_id = $1 OR name = $1",
                assigned_agent,
            )
            if not target:
                return {"success": False, "error": f"Agent '{assigned_agent}' 不存在"}

            if target["status"] != "active":
                return {"success": False, "error": f"Agent '{assigned_agent}' 状态为 {target['status']}，无法接任务"}

            target_id = target["agent_id"]

            # 2. 找到指定 step_index 的 pending Step
            step = await conn.fetchrow(
                f"SELECT * FROM {SCHEMA}.steps "
                f"WHERE task_id = $1 AND step_index = $2 "
                f"FOR UPDATE",
                task_id, step_index,
            )
            if not step:
                return {"success": False, "error": f"Task {task_id} 中 step_index={step_index} 不存在"}

            if step["status"] != "pending":
                return {"success": False, "error": f"Step {step_index} 状态为 '{step['status']}'，不是 pending，无法分派"}

            # 3. 原子分配
            row = await conn.fetchrow(
                f"UPDATE {SCHEMA}.steps SET status = 'assigned', "
                f"assigned_agent = $2, assigned_at = NOW(), updated_at = NOW() "
                f"WHERE step_id = $1 AND status = 'pending' "
                f"RETURNING *",
                step["step_id"], target_id,
            )
            if not row:
                return {"success": False, "error": "分配失败（Step 状态已被并发修改）"}

            # 4. 更新 participants
            await conn.execute(
                f"UPDATE {SCHEMA}.tasks SET participants = "
                f"ARRAY(SELECT DISTINCT UNNEST(participants || $2::text)) "
                f"WHERE task_id = $1",
                task_id, target_id,
            )

            step_dict = dict(row)
            logger.info(
                f"Step {step_dict['step_id']} (task={task_id}, idx={step_index}) "
                f"assigned to {target_id} by {requested_by}"
            )

            # 5. hook: trajectory + audit_log
            from . import runner
            asyncio.create_task(runner.step_hooks(dict(row), requested_by, "assigned"))

            # 6. WebSocket 通知目标 Agent
            await ws_notify(target_id, "assigned", {
                "task_id": task_id,
                "step_id": step_dict["step_id"],
                "step_index": step_index,
                "title": step_dict["title"],
                "instruction": step_dict["instruction"],
                "assigned_by": requested_by,
            })

            return {
                "success": True,
                "step_id": step_dict["step_id"],
                "step_index": step_index,
                "title": step_dict["title"],
                "assigned_agent": target_id,
                "assigned_by": requested_by,
            }


# ── 中断恢复 ──────────────────────────────────────────────

async def get_recovery_state(agent_id: str) -> dict:
    """查询 Agent 所有未完成的 Steps，用于中断恢复"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 未完成的 Step（assigned + in_progress）
        unfinished_rows = await conn.fetch(
            f"SELECT s.step_id, s.task_id, s.step_index, s.title, s.instruction, "
            f"s.status, s.status_reason, s.acceptance_criteria, s.auto_retry, "
            f"s.timeout_minutes, s.started_at, s.last_heartbeat_at, "
            f"s.assigned_agent, "
            f"t.title AS task_title, t.status AS task_status "
            f"FROM {SCHEMA}.steps s "
            f"JOIN {SCHEMA}.tasks t ON s.task_id = t.task_id "
            f"WHERE s.assigned_agent = $1 "
            f"AND s.status IN ('assigned', 'in_progress') "
            f"ORDER BY s.task_id, s.step_index",
            agent_id,
        )

        # 这些 Task 中所有 pending 的 Step
        task_ids = list({r["task_id"] for r in unfinished_rows})
        pending = []
        if task_ids:
            pending_rows = await conn.fetch(
                f"SELECT s.task_id, s.step_index, s.title, s.status, s.depends_on, "
                f"t.title AS task_title, t.status AS task_status "
                f"FROM {SCHEMA}.steps s "
                f"JOIN {SCHEMA}.tasks t ON s.task_id = t.task_id "
                f"WHERE s.task_id = ANY($1) AND s.status = 'pending' "
                f"ORDER BY s.task_id, s.step_index",
                task_ids,
            )
            pending = [dict(r) for r in pending_rows]

        unfinished = []
        for r in unfinished_rows:
            d = dict(r)
            d["retries_left"] = d.get("auto_retry", 0)
            unfinished.append(d)

        return {
            "agent_id": agent_id,
            "timestamp": _now().isoformat(),
            "unfinished": unfinished,
            "pending": pending,
        }
