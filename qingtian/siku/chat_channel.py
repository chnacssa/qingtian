"""
司库 — IM 通道：飞书 / 企业微信 / 普通微信
统一通知路由层，finance_agent 通过本模块向现实财务人员发消息。
"""

import hashlib
import hmac
import json
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from . import config as cfg
from .models import ChatPayload

logger = logging.getLogger("siku.chat_channel")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════
# 飞书卡片构建
# ══════════════════════════════════════════════════════════

def _build_feishu_card(payload: ChatPayload) -> dict:
    """构建飞书通知卡片（纯文字，无按钮——财务留痕要求）。

    不渲染 action_buttons。改为在正文末尾附操作指引文字，
    引导财务人员通过 IM 回复文字指令（如"通过 txn_001"），
    所有回复经 IM callback → siku API → finance_audit 表持久化，
    确保每笔操作都有文字痕迹、不可抵赖。
    """
    severity = payload.severity
    color_map = {"critical": "red", "warning": "orange", "info": "blue"}
    header_color = color_map.get(severity, "blue")

    content_lines = [payload.content]

    if payload.metadata:
        meta_lines = []
        for k, v in payload.metadata.items():
            if v and k not in ("action_hint",):
                meta_lines.append(f"**{k}**: {v}")
        if meta_lines:
            content_lines.append("")
            content_lines.extend(meta_lines)

    hint = payload.metadata.get("action_hint", "")
    if hint:
        content_lines.append("")
        content_lines.append(f"---")
        content_lines.append(hint)

    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(content_lines)},
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"司库会计 | {_now_iso()} | 请通过 IM 回复指令操作"}],
        },
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": payload.title},
                "template": header_color,
            },
            "elements": elements,
        },
    }


# ══════════════════════════════════════════════════════════
# 企业微信 markdown 构建
# ══════════════════════════════════════════════════════════

def _build_wecom_markdown(payload: ChatPayload) -> str:
    severity_icon = {"critical": "🔴", "warning": "🟠", "info": "📋"}
    icon = severity_icon.get(payload.severity, "📋")

    lines = [f"{icon} **{payload.title}**", "", payload.content, ""]

    if payload.metadata:
        for k, v in payload.metadata.items():
            if v:
                lines.append(f"> {k}: {v}")
        lines.append("")

    lines.append(f"---")
    lines.append(f"司库会计 | {_now_iso()}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════
# 通道基类
# ══════════════════════════════════════════════════════════

class BaseChannel(ABC):
    @abstractmethod
    async def send(self, payload: ChatPayload) -> bool:
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        ...


# ══════════════════════════════════════════════════════════
# 飞书通道
# ══════════════════════════════════════════════════════════

class FeishuChannel(BaseChannel):
    """飞书 incoming webhook — 支持交互卡片 + 纯文本。"""

    def __init__(self, config: dict):
        self.webhook_url = config.get("webhook_url", "")
        self.enabled = bool(self.webhook_url)

    async def send(self, payload: ChatPayload) -> bool:
        if not self.enabled:
            return False
        card = _build_feishu_card(payload)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json=card)
                if resp.status_code >= 400:
                    logger.error("飞书发送失败: %s %s", resp.status_code, resp.text[:200])
                    return False
                logger.info("飞书消息已发送: %s", payload.title)
                return True
        except Exception as e:
            logger.error("飞书发送异常: %s", e)
            return False

    async def health_check(self) -> bool:
        if not self.webhook_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json={"msg_type": "text", "content": {"text": "司库 IM 通道连通性测试"}})
                return resp.status_code < 400
        except Exception:
            return False


# ══════════════════════════════════════════════════════════
# 企业微信通道
# ══════════════════════════════════════════════════════════

class WeComChannel(BaseChannel):
    """企业微信群机器人 webhook — markdown 文本通知。"""

    def __init__(self, config: dict):
        self.webhook_url = config.get("webhook_url", "")
        self.enabled = bool(self.webhook_url)

    async def send(self, payload: ChatPayload) -> bool:
        if not self.enabled:
            return False
        markdown = _build_wecom_markdown(payload)
        msg = {"msgtype": "markdown", "markdown": {"content": markdown}}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json=msg)
                if resp.status_code >= 400:
                    logger.error("企微发送失败: %s %s", resp.status_code, resp.text[:200])
                    return False
                logger.info("企微消息已发送: %s", payload.title)
                return True
        except Exception as e:
            logger.error("企微发送异常: %s", e)
            return False

    async def health_check(self) -> bool:
        if not self.webhook_url:
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self.webhook_url, json={
                    "msgtype": "text", "text": {"content": "司库 IM 通道连通性测试"}
                })
                return resp.status_code < 400
        except Exception:
            return False


# ══════════════════════════════════════════════════════════
# 微信通道（企微互通 / 公众号模板消息）
# ══════════════════════════════════════════════════════════

class WeChatChannel(BaseChannel):
    """微信通道 — 优先走企业微信「客户联系」互通到个人微信。

    wecom_bridge 模式: 企业微信 API 发消息给外部联系人 (微信用户)
    official_account 模式: 公众号模板消息 (需认证服务号)
    """

    def __init__(self, config: dict):
        self.mode = config.get("mode", "wecom_bridge")
        self.corp_id = config.get("wecom_corp_id", "")
        self.secret = config.get("wecom_secret", "")
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.enabled = bool(self.corp_id and self.secret) if self.mode == "wecom_bridge" else bool(self.app_id and self.app_secret)
        self._access_token: str | None = None
        self._token_expiry: float = 0.0

    async def _get_access_token(self) -> str | None:
        now = time.time()
        if self._access_token and now < self._token_expiry - 60:
            return self._access_token

        if self.mode == "wecom_bridge":
            url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={self.corp_id}&corpsecret={self.secret}"
        else:
            url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={self.app_id}&secret={self.app_secret}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                data = resp.json()
                if data.get("errcode", -1) != 0:
                    logger.error("微信 access_token 获取失败: %s", data)
                    return None
                self._access_token = data["access_token"]
                self._token_expiry = now + data.get("expires_in", 7200)
                return self._access_token
        except Exception as e:
            logger.error("微信 access_token 请求异常: %s", e)
            return None

    async def send(self, payload: ChatPayload) -> bool:
        if not self.enabled:
            return False

        token = await self._get_access_token()
        if not token:
            return False

        if self.mode == "wecom_bridge":
            return await self._send_via_wecom(token, payload)
        else:
            return await self._send_template(token, payload)

    async def _send_via_wecom(self, token: str, payload: ChatPayload) -> bool:
        """企业微信客户联系 → 微信用户。需要外部联系人 external_user_id。"""
        external_user_id = payload.metadata.get("wechat_user_id", "")
        if not external_user_id:
            logger.warning("微信通道: 缺少 wechat_user_id, 跳过发送")
            return False

        text = f"{payload.title}\n\n{payload.content}"
        url = f"https://qyapi.weixin.qq.com/cgi-bin/kf/send_msg?access_token={token}"
        msg = {
            "touser": external_user_id,
            "open_kfid": payload.metadata.get("wecom_kf_id", ""),
            "msgtype": "text",
            "text": {"content": text},
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=msg)
                data = resp.json()
                if data.get("errcode", -1) != 0:
                    logger.error("企微互通发送失败: %s", data)
                    return False
                logger.info("微信消息已发送 (wecom_bridge): %s", payload.title)
                return True
        except Exception as e:
            logger.error("企微互通发送异常: %s", e)
            return False

    async def _send_template(self, token: str, payload: ChatPayload) -> bool:
        """公众号模板消息。需要模板 ID 和接收者 openid。"""
        openid = payload.metadata.get("wechat_openid", "")
        template_id = payload.metadata.get("wechat_template_id", "")
        if not openid or not template_id:
            logger.warning("微信通道: 缺少 openid/template_id, 跳过发送")
            return False

        url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={token}"
        msg = {
            "touser": openid,
            "template_id": template_id,
            "data": {
                "first": {"value": payload.title, "color": "#173177"},
                "keyword1": {"value": payload.content, "color": "#173177"},
                "keyword2": {"value": _now_iso(), "color": "#999"},
            },
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=msg)
                data = resp.json()
                if data.get("errcode", -1) != 0:
                    logger.error("公众号模板消息发送失败: %s", data)
                    return False
                logger.info("微信模板消息已发送: %s", payload.title)
                return True
        except Exception as e:
            logger.error("公众号模板消息发送异常: %s", e)
            return False

    async def health_check(self) -> bool:
        token = await self._get_access_token()
        return token is not None


# ══════════════════════════════════════════════════════════
# 通知路由器
# ══════════════════════════════════════════════════════════

class ChatNotifier:
    """通知路由 — 去重/限流/静默 → 遍历 enabled 通道 → 全部发送。"""

    def __init__(self):
        self._last_sent: dict[str, float] = {}
        self._counts: dict[str, list[float]] = defaultdict(list)
        self._silent_queue: list[tuple[ChatPayload, float]] = []

    def _is_silent_hours(self) -> bool:
        now = datetime.now()
        minutes = now.hour * 60 + now.minute
        return minutes >= 23 * 60 or minutes <= 7 * 60

    def _dedup(self, dedup_key: str, window_seconds: int = 300) -> bool:
        now = time.time()
        self._counts[dedup_key] = [t for t in self._counts[dedup_key] if now - t < window_seconds]
        self._counts[dedup_key].append(now)
        return len(self._counts[dedup_key]) > 1

    def _throttle(self, max_per_hour: int = 60) -> bool:
        now = time.time()
        recent = [t for t in self._counts.get("__all__", []) if now - t < 3600]
        if len(recent) >= max_per_hour:
            return True
        self._counts["__all__"].append(now)
        return False

    def _get_channels(self) -> list[tuple[str, BaseChannel]]:
        channels = []
        if cfg.im_channel_enabled("feishu"):
            channels.append(("feishu", FeishuChannel(cfg.get_im_channel_config("feishu"))))
        if cfg.im_channel_enabled("wecom"):
            channels.append(("wecom", WeComChannel(cfg.get_im_channel_config("wecom"))))
        if cfg.im_channel_enabled("wechat"):
            channels.append(("wechat", WeChatChannel(cfg.get_im_channel_config("wechat"))))
        return channels

    async def notify(self, payload: ChatPayload) -> dict[str, bool]:
        dedup_key = f"{payload.severity}:{payload.title}"
        if payload.severity != "critical" and self._dedup(dedup_key):
            return {}

        if payload.severity != "critical" and self._throttle():
            return {}

        if payload.severity != "critical" and self._is_silent_hours():
            rules = cfg.get_im_notify_rules()
            max_queue = 10
            if len(self._silent_queue) < max_queue:
                self._silent_queue.append((payload, time.time()))
            return {}

        # P2 (R11): 静默队列此前无任何生产调用点 → 静默期通知永久丢失。
        # 现接入事件驱动触发：每次在非静默时段收到通知时，先尽力补发静默期
        # 积压队列（非静默时段通知已可发送），再发当前消息，避免引入常驻定时器。
        await self.flush_silent_queue()

        return await self._do_notify(payload)

    async def _do_notify(self, payload: ChatPayload) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for name, channel in self._get_channels():
            try:
                ok = await channel.send(payload)
                results[name] = ok
            except Exception:
                logger.exception("%s 通道发送异常", name)
                results[name] = False
        return results

    async def notify_critical(self, payload: ChatPayload) -> dict[str, bool]:
        self._last_sent[payload.title] = time.time()
        return await self._do_notify(payload)

    async def flush_silent_queue(self) -> int:
        # P2 (R11): 先取出并清空队列，再逐条发送——单条失败（_do_notify 内部
        # 已按通道兜底，但极端异常仍可能外抛）不阻塞其余条目，也不导致死循环重发。
        queued = list(self._silent_queue)
        self._silent_queue.clear()
        sent = 0
        for payload, _ in queued:
            try:
                await self._do_notify(payload)
                sent += 1
            except Exception:
                logger.exception("静默队列发送失败: %s", payload.title)
        if sent:
            logger.info("静默队列已刷新: %s 条通知", sent)
        return sent


chat_notifier = ChatNotifier()
