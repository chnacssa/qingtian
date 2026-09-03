"""EventBus — 事件总线

职责:
  - 事件订阅/取消订阅管理
  - 事件广播（emit）到所有订阅者
  - 关键事件持久化（at-least-once）：先 INSERT event_logs → broadcast → UPDATE delivered

用法:
    bus = EventBus()
    bus.subscribe("workflow:order_created", my_handler)

    await bus.emit("workflow:order_created", {"order_id": "...", ...})
    await bus.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("event_bus")

# 关键事件：必须先 persist 再 broadcast
CRITICAL_EVENTS = {
    "workflow:order_created",
    "workflow:order_completed",
    "payment:completed",
}

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class EventBus:
    """事件总线

    提供进程内事件发布/订阅机制。
    关键事件（CRITICAL_EVENTS）自动写入 event_logs 表确保 at-least-once 语义。
    """

    def __init__(self):
        self._subscribers: dict[str, list[EventHandler]] = {}
        self._pool = None  # 由 init_db 注入
        self._lock = asyncio.Lock()
        self._table_ready = False

    async def init_db(self, pool) -> None:
        """注入数据库连接池（事件日志持久化用）"""
        self._pool = pool

    # ── 订阅管理 ──────────────────────────────────────────

    def subscribe(self, event: str, handler: EventHandler) -> None:
        """订阅事件"""
        if event not in self._subscribers:
            self._subscribers[event] = []
        self._subscribers[event].append(handler)
        logger.debug("Subscribed handler for event: %s", event)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        """取消订阅"""
        handlers = self._subscribers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)
            logger.debug("Unsubscribed handler for event: %s", event)

    # ── 发布 ──────────────────────────────────────────────

    async def emit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """发布事件

        at-least-once 保证：
          1. 关键事件 → INSERT event_logs（pending）
          2. 广播到所有订阅者
          3. 关键事件 → UPDATE event_logs（delivered）
        """
        payload = payload or {}
        log_id: int | None = None

        # Step 1: 关键事件持久化（pending）
        if event in CRITICAL_EVENTS and self._pool:
            try:
                if not self._table_ready:
                    # review 修复（2026-08-15）：全仓此前无 qingtian.event_logs 建表语句，
                    # INSERT 恒失败被 except 吞掉 → at-least-once 持久化形同虚设。
                    # 首次落库前惰性建表，兼容既有部署（无需重跑 init.sql）。
                    async with self._lock:
                        if not self._table_ready:
                            await self._pool.execute(
                                """CREATE TABLE IF NOT EXISTS qingtian.event_logs (
                                     id         BIGSERIAL PRIMARY KEY,
                                     event      TEXT NOT NULL,
                                     payload    JSONB DEFAULT '{}',
                                     status     TEXT DEFAULT 'pending',
                                     created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                                 )"""
                            )
                            self._table_ready = True
                # review 修复（2026-08-15）：jsonb codec 对 str 会二次 json.dumps →
                # 双重编码入库为 JSON 字符串。直接传 dict，codec 负责序列化。
                log_id = await self._pool.fetchval(
                    """INSERT INTO qingtian.event_logs (event, payload, status)
                       VALUES ($1, $2::jsonb, 'pending')
                       RETURNING id""",
                    event,
                    payload or {},
                )
            except Exception as e:
                logger.error("Failed to persist critical event %s: %s", event, e)

        # Step 2: 广播
        handlers = self._subscribers.get(event, [])
        if handlers:
            results = await asyncio.gather(
                *(h(event, payload) for h in handlers),
                return_exceptions=True,
            )
            for i, r in enumerate(results):
                if isinstance(r, Exception):
                    logger.error(
                        "Event handler %s failed for %s: %s",
                        handlers[i].__name__,
                        event,
                        r,
                    )

        # Step 3: 关键事件标记 delivered
        if log_id is not None and self._pool:
            try:
                await self._pool.execute(
                    "UPDATE qingtian.event_logs SET status = 'delivered' WHERE id = $1",
                    log_id,
                )
            except Exception as e:
                logger.error("Failed to mark event %s delivered: %s", log_id, e)

    # ── 生命周期 ──────────────────────────────────────────

    async def close(self) -> None:
        """关闭事件总线，清理订阅者"""
        self._subscribers.clear()
        self._pool = None
        logger.info("EventBus closed")
