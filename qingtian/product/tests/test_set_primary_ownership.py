"""P2 (R11): set_primary 图片归属校验回归测试

原实现第二句 UPDATE 仅按 image_id 更新，不校验 image 归属 product——
可把其他产品的图片误设为本产品主图（跨产品污染），且会误清本产品现有主图。

修复后：先校验 image 确属本 product（AND product_id 双条件），
归属不符直接拒绝（不误清主图、不改别产品图片），命中才执行双条件 UPDATE。
"""

from unittest.mock import AsyncMock, patch

import pytest

from product.repository import ProductImageRepo


class _FakeConn:
    """模拟连接：fetchrow 返回归属校验结果，execute 记录调用。"""

    def __init__(self, owned: bool):
        self.owned = owned
        self.fetched: list[tuple] = []
        self.executed: list[tuple] = []

    async def fetchrow(self, q, *params):
        self.fetched.append((q, params))
        return {"image_id": params[0]} if self.owned else None

    async def execute(self, q, *params):
        self.executed.append((q, params))
        return "UPDATE 1"


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def _repo_with(conn):
    pool = _FakePool(conn)
    patcher = patch("product.repository.get_pool", AsyncMock(return_value=pool))
    return patcher


class TestSetPrimaryOwnership:
    @pytest.mark.asyncio
    async def test_set_primary_owned_image_updates_with_product_condition(self):
        """image 属于本 product → 执行双条件 UPDATE（AND product_id）"""
        conn = _FakeConn(owned=True)
        with _repo_with(conn):
            await ProductImageRepo().set_primary("pid-1", "img-1")

        # 归属校验：fetchrow 用 (image_id, product_id)
        assert len(conn.fetched) == 1
        assert conn.fetched[0][1] == ("img-1", "pid-1")

        # 两句 UPDATE 都在，第二句必须带 product_id 条件
        assert len(conn.executed) == 2
        clear_sql, set_sql = conn.executed[0][0], conn.executed[1][0]
        assert "is_primary = FALSE" in clear_sql
        assert "WHERE product_id = $1" in clear_sql
        assert "is_primary = TRUE" in set_sql
        assert "image_id = $1" in set_sql
        assert "product_id = $2" in set_sql
        assert conn.executed[1][1] == ("img-1", "pid-1")

    @pytest.mark.asyncio
    async def test_set_primary_foreign_image_rejected_no_update(self):
        """image 不属于本 product → 拒绝，且不执行任何 UPDATE（不误清主图）"""
        conn = _FakeConn(owned=False)
        with _repo_with(conn):
            await ProductImageRepo().set_primary("pid-1", "img-x")

        assert len(conn.fetched) == 1
        assert conn.fetched[0][1] == ("img-x", "pid-1")
        assert conn.executed == [], "归属不符时不得执行任何 UPDATE"
