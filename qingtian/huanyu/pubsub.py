"""
寰宇 — 轻量 Pub/Sub 包装器

用于 Skill 动态加载等跨底座实时通知。
基于 Redis Pub/Sub，独立于 peers.py 的引擎通道。
"""

import asyncio
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_redis = None
_subscriber_task: asyncio.Task | None = None
_running = False
_handlers: dict[str, list[Callable]] = {}

EVENT_SKILL_BIND_CHANGED = "huanyu:skill_bind_changed"


async def _get_redis():
    global _redis
    if _redis is None:
        from huanyu.config import get_redis_url, get_redis_password

        import redis.asyncio as redis

        url = get_redis_url()
        password = get_redis_password()
        _redis = redis.from_url(url, password=password or None, decode_responses=True)
        await _redis.ping()
    return _redis


async def publish(channel: str, data: dict[str, Any]):
    """向指定 channel 广播消息"""
    r = await _get_redis()
    payload = json.dumps(data, ensure_ascii=False, default=str)
    await r.publish(channel, payload)
    logger.info("Published to channel %s: %s", channel, data)


async def subscribe(channel: str, handler: Callable):
    """注册 channel 订阅处理器（启动 listener 如未运行）。

    listener 已运行时无需重启：_listener_loop 每轮同步 _handlers 差异，
    新 channel 会自动订阅（Redis pubsub 支持运行时 subscribe）。
    """
    if channel not in _handlers:
        _handlers[channel] = []
    _handlers[channel].append(handler)

    global _subscriber_task, _running
    if _subscriber_task is None or _subscriber_task.done():
        _running = True
        _subscriber_task = asyncio.create_task(_listener_loop())


async def _listener_loop():
    """后台 listener：订阅所有已注册 channel，收到消息后分发给对应 handler

    每轮同步 _handlers 的 channel 集合，支持运行时新增订阅。
    """
    global _running
    r = await _get_redis()
    pubsub = r.pubsub()

    _subscribed: set[str] = set()

    async def _sync_subscriptions():
        """将 _handlers 的 channel 差异同步到 pubsub（subscribe/unsubscribe）"""
        desired = set(_handlers.keys())
        to_add = desired - _subscribed
        to_remove = _subscribed - desired
        if to_add:
            await pubsub.subscribe(*to_add)
            _subscribed.update(to_add)
        if to_remove:
            await pubsub.unsubscribe(*to_remove)
            _subscribed.difference_update(to_remove)
        return to_add or to_remove

    await _sync_subscriptions()
    logger.info("Pub/Sub listener started on channels: %s", sorted(_subscribed))

    try:
        while _running:
            try:
                changed = await _sync_subscriptions()
                if changed:
                    logger.info("Pub/Sub listener channels updated: %s", sorted(_subscribed))
                message = await pubsub.get_message(timeout=1.0)
                if message is None:
                    continue
                if message["type"] != "message":
                    continue
                channel = message["channel"]
                data = json.loads(message["data"])
                for handler in _handlers.get(channel, []):
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            await handler(data)
                        else:
                            handler(data)
                    except Exception:
                        logger.exception("Pub/Sub handler error on channel %s", channel)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Pub/Sub listener error")
                await asyncio.sleep(1)
    finally:
        if _subscribed:
            await pubsub.unsubscribe(*_subscribed)
        logger.info("Pub/Sub listener stopped")


async def unsubscribe_all():
    """停止所有订阅（清理用）"""
    global _running
    _running = False
    if _subscriber_task:
        _subscriber_task.cancel()
        try:
            await _subscriber_task
        except asyncio.CancelledError:
            pass
    _handlers.clear()
