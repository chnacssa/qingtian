"""告警服务 —— 去重、限流、静默时段管理、飞书/微信卡片通知。"""

import asyncio
import json
import os
import time
import logging
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

import httpx
import yaml

from . import config as cfg

logger = logging.getLogger("zhenyue.alert")

# 审批接收人映射表路径
_APPROVAL_RECIPIENTS_PATH = Path(
    os.environ.get(
        "ZHENYUE_RECIPIENTS_CONFIG",
        "/opt/qingtian/zhenyue/configs/approval_recipients.yaml",
    )
)


def _load_recipients() -> dict:
    """加载审批接收人映射表。文件不存在时返回空，不报错。"""
    if not _APPROVAL_RECIPIENTS_PATH.exists():
        logger.info("approval_recipients.yaml 未找到，使用默认 feishu 通道")
        return {}
    try:
        raw = _APPROVAL_RECIPIENTS_PATH.read_text(encoding="utf-8")
        return yaml.safe_load(raw) or {}
    except Exception as e:
        logger.warning("加载 approval_recipients.yaml 失败: %s", e)
        return {}


# ── 飞书卡片构建 ──────────────────────────────────

def _build_approval_card(approval_data: dict) -> dict:
    """构建飞书审批通知卡片 + 文字审批指引。

    管理员在 IM 中直接回复文字即可审批：
      允许 <request_id前8位>   → 批准
      拒绝 <request_id前8位>   → 拒绝
    破军/执策插件自动检测回复并调镇岳API执行。
    """
    request_id = approval_data.get("request_id", "")
    agent_id = approval_data.get("agent_id", "")
    action = approval_data.get("action", "")
    severity = approval_data.get("severity", "high")
    target = approval_data.get("target", "")

    severity_label = {"critical": "🔴 紧急", "high": "🟠 高危", "medium": "🟡 中危", "low": "🟢 低"}.get(severity, severity)

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🛡️ 镇岳审批请求"},
                "template": "red" if severity in ("critical", "high") else "orange",
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**Agent**\n{agent_id}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**操作**\n{action}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**严重度**\n{severity_label}"}},
                        {"is_short": True, "text": {"tag": "lark_md", "content": f"**请求ID**\n{request_id[:8]}..."}},
                    ],
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**审批方式**：直接回复文字\n✅ 允许 {request_id[:8]}\n❌ 拒绝 {request_id[:8]}",
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"目标: {target or '无'}",
                        }
                    ],
                },
            ],
        },
    }


# ── 告警通道 ─────────────────────────────────────

class AlertChannel:
    def __init__(self):
        self._last_sent: dict[str, float] = {}
        self._counts: dict[str, list[float]] = defaultdict(list)
        self._silent_queue: list[dict] = []
        self._lock = asyncio.Lock()

    def _is_silent_hours(self) -> bool:
        now = datetime.now()
        current_minutes = now.hour * 60 + now.minute
        start_minutes = 23 * 60
        end_minutes = 7 * 60
        if start_minutes <= end_minutes:
            return start_minutes <= current_minutes <= end_minutes
        else:
            return current_minutes >= start_minutes or current_minutes <= end_minutes

    def _dedup(self, alert_type: str, window_seconds: int | None = None) -> bool:
        if window_seconds is None:
            window_seconds = cfg.get_alert_dedup_window_seconds()
        now = time.time()
        self._counts[alert_type] = [t for t in self._counts[alert_type] if now - t < window_seconds]
        self._counts[alert_type].append(now)
        return len(self._counts[alert_type]) > 1

    def _throttle(self, max_per_hour: int | None = None) -> bool:
        if max_per_hour is None:
            max_per_hour = cfg.get_alert_throttle_max_per_hour()
        now = time.time()
        recent = [t for t in self._counts.get("__all__", []) if now - t < 3600]
        if len(recent) >= max_per_hour:
            return True
        self._counts["__all__"].append(now)
        return False

    async def _send_feishu_card(self, card: dict, open_id: str = "") -> bool:
        """通过飞书 Webhook 发送卡片（无独立 API 依赖）。

        审批通知走 Gateway 现有通道——插件拦截时通过 event.channel
        确定通道类型，调此函数时传入对应 webhook URL。
        """
        feishu_webhook = cfg.get("zhenyue.alert.feishu_webhook", "")
        if not feishu_webhook:
            logger.info("飞书 webhook 未配置，审批通知将走 Gateway 通道发送")
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(feishu_webhook, json=card)
                if resp.status_code >= 400:
                    logger.warning(f"飞书卡片发送失败: {resp.status_code}")
                    return False
                return True
        except Exception as e:
            logger.warning(f"飞书卡片发送异常: {e}")
            return False

    async def send_feishu_text(self, open_id: str, text: str) -> bool:
        """通过 openclaw CLI 发送飞书 DM（利用 Gateway 现有通道）。

        不走飞书 API / webhook，直接用 Gateway 已配好的飞书 Bot 发消息。
        openclaw message send 通过 Gateway 内部通道发送，不依赖额外配置。
        """
        try:
            # 转义消息中的特殊字符
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            env = {
                **os.environ,
                "OPENCLAW_CONFIG_PATH": "/root/.openclaw/openclaw.json",
            }
            proc = await asyncio.create_subprocess_exec(
                "/usr/local/bin/openclaw", "message", "send",
                "--channel", "feishu",
                "--target", open_id,
                "--message", escaped,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=15.0
            )
            if proc.returncode != 0:
                err_msg = stderr.decode()[:300] if stderr else "unknown"
                logger.warning(f"openclaw message send 失败 (exit={proc.returncode}): {err_msg}")
                return False
            logger.info("飞书 DM 已通过 Gateway 发送: open_id=%s", open_id)
            return True
        except asyncio.TimeoutError:
            logger.warning("openclaw message send 超时")
            return False
        except FileNotFoundError:
            logger.warning("openclaw CLI 未找到，尝试 /usr/local/bin/openclaw")
            return False
        except Exception as e:
            logger.warning(f"飞书 DM 发送异常: {e}")
            return False

    async def _send_wechat_card(self, card: dict, target: str) -> bool:
        """微信通知（待实现）。"""
        raise NotImplementedError("微信审批通知通道尚未实现")

    async def send(self, alert_type: str, content: dict, severity: str = "high") -> bool:
        async with self._lock:
            if self._dedup(alert_type):
                return False
            if severity != "critical" and self._is_silent_hours():
                if len(self._silent_queue) < 10:
                    self._silent_queue.append({"type": alert_type, "content": content, "severity": severity})
                return False
            return await self._do_send(alert_type, content, severity)

    async def send_critical(self, alert_type: str, content: dict) -> bool:
        async with self._lock:
            return await self._do_send(alert_type, content, "critical")

    async def _do_send(self, alert_type: str, content: dict, severity: str) -> bool:
        self._last_sent[alert_type] = time.time()
        if alert_type == "approval":
            card = _build_approval_card(content)
            # 从审批数据中取 caller_id 做路由
            caller_id = content.get("caller_id", content.get("agent_id", ""))
            return await self._route_approval(caller_id, card)
        return True

    async def send_approval(self, approval_data: dict) -> bool:
        """发送审批通知，根据 caller_id 路由到对应通道。

        approval_data 应包含:
          - request_id, agent_id, action, severity, target (卡片展示)
          - caller_id: 指令发起者标识，用于查 approval_recipients.yaml
        """
        return await self._do_send("approval", approval_data, approval_data.get("severity", "high"))

    async def _route_approval(self, caller_id: str, card: dict) -> bool:
        """根据用户映射表路由审批通知到对应通道。

        优先使用 caller_channel（从 event.channel 传入，如 feishu/wechat），
        走对应 webhook 直接发送，不依赖独立 API 配置。
        """
        recipients = _load_recipients()
        users = recipients.get("users", {})
        user_cfg = users.get(caller_id, {})
        channels = user_cfg.get("channels", [])

        if not channels:
            # 无配置 → 走默认飞书 webhook
            logger.info("审批路由: caller=%s 无映射配置，走默认 feishu webhook", caller_id)
            return await self._send_feishu_card(card)

        sent_any = False
        for ch in channels:
            if not ch.get("enabled", True):
                continue
            ch_type = ch.get("type", "feishu")

            if ch_type == "feishu":
                ok = await self._send_feishu_card(card)
            elif ch_type == "wechat":
                try:
                    ok = await self._send_wechat_card(card, ch.get("target", ""))
                except NotImplementedError:
                    logger.warning("审批路由: wechat 通道未实现, 回退 feishu")
                    ok = await self._send_feishu_card(card)
            else:
                continue

            if ok:
                sent_any = True
                logger.info("审批已推送: caller=%s channel=%s", caller_id, ch_type)

        if not sent_any:
            logger.warning("审批路由: caller=%s 所有通道失败, 回退 feishu", caller_id)
            return await self._send_feishu_card(card)

        return True

    async def flush_silent_queue(self):
        for item in self._silent_queue:
            await self._do_send(item["type"], item["content"], item["severity"])
        self._silent_queue.clear()


alert_channel = AlertChannel()
