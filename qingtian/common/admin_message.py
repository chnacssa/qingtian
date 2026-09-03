"""
管理员消息系统 — 收件箱 + 去重聚合 + 推送

消息分级：
  critical  → 立即推送 + 收件箱 + 邮件必达
  warning   → 收件箱 + 每小时聚合推送
  info      → 仅收件箱

触发场景：存储配额告警、Skill 崩溃、安全事件、License 到期、审核通知等。
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger("common.admin_message")

DEDUP_WINDOW = timedelta(hours=1)
"""去重时间窗口：相同 dedup_key 的消息在此窗口内折叠"""

FALLBACK_LOG = "/opt/qingtian/logs/failed_messages.log"
"""DB 不可用时的消息落盘路径"""


@dataclass
class AdminMessage:
    """管理员消息数据模型"""
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    level: str = "info"         # critical | warning | info
    source: str = "system"      # storage | skill | security | license | system
    title: str = ""
    body: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    read: bool = False
    archived: bool = False
    dedup_key: str = ""
    count: int = 1


ADMIN_MESSAGES_DDL = """\
CREATE TABLE IF NOT EXISTS admin_messages (
    id              BIGSERIAL PRIMARY KEY,
    msg_id          TEXT NOT NULL,
    level           TEXT NOT NULL DEFAULT 'info',
    source          TEXT NOT NULL DEFAULT 'system',
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT DEFAULT '',
    dedup_key       TEXT DEFAULT '',
    count           INT DEFAULT 1,
    read            BOOLEAN DEFAULT FALSE,
    archived        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
"""


class AdminMessageBus:
    """管理员消息总线

    用法:
        bus = AdminMessageBus()
        bus.register(my_handler)              # 注册处理器
        await bus.enable_persistence(pool)    # 启用 DB 持久化（自动建表）

        await bus.send(AdminMessage(
            level="warning",
            source="storage",
            title="存储配额 95%",
            body="Skill 'bidding' 已达配额的 95%",
            dedup_key="bidding:quota:95",
        ))
    """

    def __init__(self):
        self._handlers: list = []
        self._pool = None
        # 去重追踪: {dedup_key: (first_seen, count)}
        self._dedup: dict[str, tuple[datetime, int]] = {}
        self._send_count = 0

    def register(self, handler):
        """注册消息处理器（callable, 接收 AdminMessage 参数）"""
        self._handlers.append(handler)

    async def enable_persistence(self, pool):
        """启用数据库持久化（自动建表）

        Args:
            pool: asyncpg 连接池
        """
        self._pool = pool
        # 自动建表（幂等），避免首次启动时因表不存在丢消息
        try:
            async with pool.acquire() as conn:
                await conn.execute(ADMIN_MESSAGES_DDL)
        except Exception:
            logger.warning("Failed to ensure admin_messages table, will fallback to log file")
        self._handlers.append(self._persist_handler)

    async def _persist_handler(self, msg: AdminMessage):
        """将消息写入数据库（失败时 fallback 到日志文件）"""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO admin_messages
                       (msg_id, level, source, title, body, dedup_key, count,
                        created_at, read, archived)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
                    msg.msg_id, msg.level, msg.source, msg.title, msg.body,
                    msg.dedup_key, msg.count, msg.created_at, msg.read, msg.archived,
                )
        except Exception:
            logger.exception("DB persist failed for message '%s', falling back to log", msg.msg_id)
            # Fallback: 写入本地日志文件
            try:
                log_dir = os.path.dirname(FALLBACK_LOG)
                os.makedirs(log_dir, exist_ok=True)
                with open(FALLBACK_LOG, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "msg_id": msg.msg_id,
                        "level": msg.level,
                        "source": msg.source,
                        "title": msg.title,
                        "body": msg.body,
                        "dedup_key": msg.dedup_key,
                        "count": msg.count,
                        "created_at": msg.created_at.isoformat(),
                    }, ensure_ascii=False) + "\n")
            except Exception:
                logger.exception("Fallback log write also failed for '%s'", msg.msg_id)

    def _cleanup_dedup(self, now: datetime):
        """清理过期的去重记录（超过窗口期的 key 移除）"""
        expired = [
            k for k, (first_seen, _) in self._dedup.items()
            if now - first_seen >= DEDUP_WINDOW
        ]
        for k in expired:
            del self._dedup[k]
        if expired:
            logger.debug("Cleaned up %d expired dedup entries", len(expired))
        self._send_count = 0

    async def send(self, msg: AdminMessage):
        """发送消息（自动去重聚合 + 持久化 + 广播）"""
        now = datetime.utcnow()

        # 定期清理过期 dedup 记录（每 100 次发送清理一次）
        self._send_count += 1
        if self._send_count >= 100:
            self._cleanup_dedup(now)

        # 去重检查
        if msg.dedup_key:
            existing = self._dedup.get(msg.dedup_key)
            if existing is not None:
                first_seen, count = existing
                if now - first_seen < DEDUP_WINDOW:
                    # 窗口内重复 → 折叠，不广播
                    self._dedup[msg.dedup_key] = (first_seen, count + 1)
                    logger.debug(
                        "Dedup: '%s' folded (count=%d)", msg.dedup_key, count + 1,
                    )
                    # 仅更新已持久化消息的 count
                    if self._pool:
                        await self._persist_update_count(msg.dedup_key, count + 1)
                    return
            # 新消息或窗口已过
            self._dedup[msg.dedup_key] = (now, 1)

        # 首次消息：先持久化到 DB
        if self._pool:
            await self._persist_handler(msg)

        # 广播给其他处理器（跳过 _persist_handler，已在上方处理）
        for handler in self._handlers:
            if handler is self._persist_handler:
                continue
            try:
                await handler(msg)
            except Exception:
                logger.exception("Admin message handler failed")

    async def _persist_update_count(self, dedup_key: str, count: int):
        """更新已持久化的重复消息计数"""
        if self._pool is None:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """UPDATE admin_messages SET count=$1
                       WHERE dedup_key=$2 AND created_at > NOW() - $3::interval""",
                    count, dedup_key, DEDUP_WINDOW,
                )
        except Exception:
            logger.exception("Failed to update dedup count for '%s'", dedup_key)


# ── IM Webhook 推送 ─────────────────────────────────


async def push_dingtalk(webhook_url: str, msg: AdminMessage) -> bool:
    """推送钉钉群机器人消息

    Args:
        webhook_url: 钉钉机器人 webhook URL
        msg: AdminMessage

    Returns:
        是否推送成功
    """
    level_tag = {"critical": "[🔴紧急]", "warning": "[🟡警告]", "info": "[ℹ️通知]"}.get(
        msg.level, "[通知]"
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json={
                "msgtype": "markdown",
                "markdown": {
                    "title": f"{level_tag} {msg.title}",
                    "text": (
                        f"## {level_tag} {msg.title}\n\n"
                        f"**来源**: {msg.source}\n\n"
                        f"**内容**: {msg.body}\n\n"
                        f"**时间**: {msg.created_at.isoformat()}\n"
                        f"---\n"
                        f"*ACSSA 智能体操作系统管理员消息*"
                    ),
                },
            })
            return resp.is_success
    except Exception:
        logger.exception("Failed to push dingtalk message: %s", msg.msg_id)
        return False


async def push_wecom(webhook_url: str, msg: AdminMessage) -> bool:
    """推送企微群机器人消息

    Args:
        webhook_url: 企微机器人 webhook URL
        msg: AdminMessage

    Returns:
        是否推送成功
    """
    level_tag = {"critical": "[🔴紧急]", "warning": "[🟡警告]", "info": "[ℹ️通知]"}.get(
        msg.level, "[通知]"
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json={
                "msgtype": "markdown",
                "markdown": {
                    "content": (
                        f"## {level_tag} {msg.title}\n"
                        f"> 来源：{msg.source}\n"
                        f"> 内容：{msg.body}\n"
                        f"> 时间：{msg.created_at.isoformat()}\n"
                    ),
                },
            })
            return resp.is_success
    except Exception:
        logger.exception("Failed to push wecom message: %s", msg.msg_id)
        return False


def create_im_push_handler(admin_config: dict):
    """根据管理员配置创建 IM 推送处理器

    Args:
        admin_config: config.yaml 中的 admin.accounts[] 配置项

    Returns:
        消息处理器（接收 AdminMessage，推送 IM）
    """
    im_config = admin_config.get("im", {})
    im_type = im_config.get("type", "")
    webhook = im_config.get("webhook", "")

    if not im_type or not webhook:
        return None

    async def _handler(msg: AdminMessage):
        """IM 推送处理器"""
        # 只有 critical 和 warning 级推送 IM
        if msg.level not in ("critical", "warning"):
            return

        if im_type == "dingtalk":
            await push_dingtalk(webhook, msg)
        elif im_type == "wecom":
            await push_wecom(webhook, msg)
        else:
            logger.warning("Unknown IM type: %s", im_type)

    return _handler


# 全局单例
admin_bus: AdminMessageBus | None = None
"""延迟初始化的 AdminMessageBus 单例（由 enable_persistence 时创建）"""


def create_admin_bus() -> AdminMessageBus:
    """创建并返回全局 AdminMessageBus 单例"""
    global admin_bus
    if admin_bus is None:
        admin_bus = AdminMessageBus()
    return admin_bus
