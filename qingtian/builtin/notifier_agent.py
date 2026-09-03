"""
infra:notifier — 通知推送 Agent

接收其他模块的通知请求，通过 IM/WebSocket/邮件通道推送。
充当 bus 的 IM 降级通道。
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from common.bus import bus

logger = logging.getLogger("builtin.notifier")

# IM 通道配置
_FEISHU_WEBHOOK = os.getenv("NOTIFIER_FEISHU_WEBHOOK", "")
_WECOM_WEBHOOK = os.getenv("NOTIFIER_WECOM_WEBHOOK", "")
_WECHAT_WEBHOOK = os.getenv("NOTIFIER_WECHAT_WEBHOOK", "")

# ── 通道支持 ──────────────────────────────────────────

SUPPORTED_CHANNELS = {"feishu", "wecom", "wechat"}

async def send_notification(
    agent_id: str,
    title: str,
    content: str,
    channels: Optional[list[str]] = None,
    priority: str = "normal",
) -> dict:
    """多通道推送通知

    Args:
        agent_id: 目标 Agent
        title: 通知标题
        content: 通知内容
        channels: 优先使用的通道列表（默认按配置尝试所有）
        priority: high / normal / low

    Returns:
        {"delivered": True/False, "channels_used": [...], "failed_channels": [...]}
    """
    if not channels:
        channels = _get_agent_channels(agent_id) or ["feishu", "wecom", "wechat"]

    delivered = False
    channels_used = []
    failed_channels = []

    for ch in channels:
        if ch not in SUPPORTED_CHANNELS:
            continue
        try:
            ok = await _send_via_channel(ch, title, content, priority)
            if ok:
                delivered = True
                channels_used.append(ch)
                break  # 第一个送达的通道即成功
            else:
                failed_channels.append(ch)
        except Exception as e:
            logger.warning("Channel %s failed for %s: %s", ch, agent_id, e)
            failed_channels.append(ch)

    return {
        "delivered": delivered,
        "channels_used": channels_used,
        "failed_channels": failed_channels,
    }


def _get_agent_channels(agent_id: str) -> list[str]:
    """从 Agent 配置获取通道偏好"""
    # 简单实现：按角色判断
    if agent_id.startswith("infra:"):
        return ["feishu"]
    return ["feishu", "wecom"]


async def _send_via_channel(channel: str, title: str, content: str, priority: str) -> bool:
    """通过指定通道发送"""
    webhook = {
        "feishu": _FEISHU_WEBHOOK,
        "wecom": _WECOM_WEBHOOK,
        "wechat": _WECHAT_WEBHOOK,
    }.get(channel, "")

    if not webhook:
        return False

    timeout = 5 if priority == "high" else 10
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if channel == "feishu":
                body = {
                    "msg_type": "interactive",
                    "card": {
                        "header": {"title": {"tag": "plain_text", "content": title},
                                   "template": "red" if priority == "high" else "blue"},
                        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content[:2000]}}],
                    },
                }
            elif channel == "wecom":
                body = {
                    "msgtype": "markdown",
                    "markdown": {"content": f"## {title}\n{content[:2000]}"},
                }
            else:
                body = {
                    "msgtype": "text",
                    "text": {"content": f"{title}\n{content[:2000]}"},
                }

            resp = await client.post(webhook, json=body)
            return resp.status_code == 200
    except Exception:
        return False


# ── 总线事件处理 ──────────────────────────────────────

async def handle_notification_request(event: dict):
    """处理来自 bus 的通知请求"""
    payload = event.get("payload", {})
    agent_id = payload.get("agent_id", "")
    title = payload.get("title", "通知")
    content = payload.get("content", "")
    channels = payload.get("channels")
    priority = payload.get("priority", "normal")

    result = await send_notification(agent_id, title, content, channels, priority)
    logger.info(
        "Notification for %s: delivered=%s channels=%s",
        agent_id, result["delivered"], result["channels_used"],
    )
    return result


# ── 独立运行入口 ──────────────────────────────────────

AGENT_ID = "infra:notifier-01"
NOTIFY_POLL_INTERVAL = 10  # 秒

async def _drain_notifications():
    """轮询 notifier 收件箱（huanyu.messages 未读 notification）→ 推送 → 标记已读。

    C14 (R11): 原 run() 只 sleep 不消费，bus._notify_emergency 写入的通知
    永远无人处理。此处接管消费端：to_agent_id=AGENT_ID 且 message_type='notification'
    的未读消息逐条推送，成功才标记已读（失败留待下轮重试）。
    """
    from common.db import get_pool
    from huanyu import config as hcfg

    schema = hcfg.get_schema_name()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT message_id::text AS message_id, payload "
            f"FROM {schema}.messages "
            f"WHERE to_agent_id = $1 AND status = 'unread' AND message_type = 'notification' "
            f"ORDER BY created_at ASC LIMIT 50",
            AGENT_ID,
        )
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            try:
                result = await handle_notification_request({"payload": payload})
                logger.info(
                    "notifier %s delivered=%s channels=%s",
                    row["message_id"][:8],
                    result.get("delivered"),
                    result.get("channels_used"),
                )
                await conn.execute(
                    f"UPDATE {schema}.messages SET status = 'read', read_at = NOW() "
                    f"WHERE message_id = $1 AND status = 'unread'",
                    row["message_id"],
                )
            except Exception as e:
                logger.warning("notifier handle %s failed: %s", row["message_id"][:8], e)


async def run():
    """notifier Agent 主循环 — 轮询收件箱推送通知"""
    logger.info("Notifier agent %s started", AGENT_ID)
    while True:
        try:
            await _drain_notifications()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notifier drain failed")
        await asyncio.sleep(NOTIFY_POLL_INTERVAL)
