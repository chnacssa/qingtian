"""
rate_limit.py 单元测试
内存滑动窗口速率限制
"""

import pytest

from zhenyue.rate_limit import RateLimiter


class TestRateLimiter:
    def setup_method(self):
        self.limiter = RateLimiter(limit=5, window_seconds=60)

    @pytest.mark.asyncio
    async def test_within_limit(self):
        """在限制内 → True"""
        for _ in range(5):
            result = await self.limiter.check("key-1")
            assert result is True

    @pytest.mark.asyncio
    async def test_exceeds_limit(self):
        """超出限制 → False"""
        for _ in range(5):
            await self.limiter.check("key-1")

        result = await self.limiter.check("key-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_different_keys_independent(self):
        """不同 key 独立计数"""
        for _ in range(5):
            await self.limiter.check("key-a")

        # key-a 应被限流
        assert await self.limiter.check("key-a") is False

        # key-b 仍可用
        assert await self.limiter.check("key-b") is True

    @pytest.mark.asyncio
    async def test_remaining(self):
        """remaining 返回剩余次数"""
        await self.limiter.check("key-r")
        rem = await self.limiter.remaining("key-r")
        assert rem == 4  # 5 - 1

    @pytest.mark.asyncio
    async def test_remaining_after_exhausted(self):
        """超出后 remaining 返回 0"""
        for _ in range(5):
            await self.limiter.check("key-e")

        rem = await self.limiter.remaining("key-e")
        assert rem == 0

    @pytest.mark.asyncio
    async def test_reset(self):
        """reset 清空计数"""
        for _ in range(5):
            await self.limiter.check("key-rst")

        await self.limiter.reset("key-rst")
        rem = await self.limiter.remaining("key-rst")
        assert rem == 5

    @pytest.mark.asyncio
    async def test_window_reset(self):
        """窗口过期后计数重置（使用 1 秒窗口）"""
        import asyncio

        fast_limiter = RateLimiter(limit=2, window_seconds=1)

        assert await fast_limiter.check("key-w") is True
        assert await fast_limiter.check("key-w") is True
        assert await fast_limiter.check("key-w") is False

        # 等待窗口过期
        await asyncio.sleep(1.1)

        # 窗口过期后应重置
        assert await fast_limiter.check("key-w") is True
        assert await fast_limiter.check("key-w") is True

    @pytest.mark.asyncio
    async def test_different_limit_values(self):
        """不同 limit 配置"""
        tiny = RateLimiter(limit=1, window_seconds=60)
        assert await tiny.check("tiny") is True
        assert await tiny.check("tiny") is False

        big = RateLimiter(limit=1000, window_seconds=60)
        for _ in range(100):
            assert await big.check("big") is True
