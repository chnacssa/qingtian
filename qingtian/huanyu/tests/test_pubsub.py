"""
寰宇 Pub/Sub 包装器单元测试
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from huanyu.pubsub import (
    EVENT_SKILL_BIND_CHANGED,
    publish,
    subscribe,
    unsubscribe_all,
)

# Reset global state before each test
@pytest.fixture(autouse=True)
def reset_pubsub():
    import huanyu.pubsub as ps
    ps._redis = None
    ps._subscriber_task = None
    ps._running = False
    ps._handlers.clear()


class TestConstants:
    def test_event_skill_bind_changed(self):
        assert EVENT_SKILL_BIND_CHANGED == "huanyu:skill_bind_changed"


@pytest.mark.asyncio
async def test_publish_uses_redis():
    """publish 调用 Redis publish"""
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()

    with patch("huanyu.pubsub._get_redis", AsyncMock(return_value=mock_redis)):
        await publish("test:channel", {"key": "value"})
        mock_redis.publish.assert_called_once()
        # Verify channel
        args, _ = mock_redis.publish.call_args
        assert args[0] == "test:channel"


@pytest.mark.asyncio
async def test_subscribe_starts_listener():
    """subscribe 注册 handler 并启动 listener"""
    handler = MagicMock()
    await subscribe("test:chan", handler)

    import huanyu.pubsub as ps
    assert "test:chan" in ps._handlers
    assert handler in ps._handlers["test:chan"]


@pytest.mark.asyncio
async def test_subscribe_multiple_handlers():
    """同一 channel 可注册多个 handler"""
    h1 = MagicMock()
    h2 = MagicMock()

    await subscribe("chan1", h1)
    await subscribe("chan1", h2)

    import huanyu.pubsub as ps
    assert len(ps._handlers["chan1"]) == 2


@pytest.mark.asyncio
async def test_unsubscribe_all_stops_listener():
    """unsubscribe_all 清空 handler 并停止 listener"""
    handler = MagicMock()
    await subscribe("ch", handler)
    await unsubscribe_all()

    import huanyu.pubsub as ps
    assert ps._handlers == {}
    assert ps._running is False


@pytest.mark.asyncio
async def test_publish_handles_unicode():
    """publish 正确处理中文字符"""
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()

    with patch("huanyu.pubsub._get_redis", AsyncMock(return_value=mock_redis)):
        await publish("ch", {"text": "吸星引擎Skill提案"})
        args, _ = mock_redis.publish.call_args
        payload = args[1]
        assert "吸星引擎" in payload


# 注：_get_redis 内部依赖 redis.asyncio 模块，此模块在测试环境可能不可用。
# 该函数的集成测试在部署环境中通过 huanyu 的集成测试覆盖。
