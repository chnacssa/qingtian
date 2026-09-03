"""P1 (R11): product by-id 端点企业归属校验回归测试

R11 A4 首轮修复只挂了 Bearer 鉴权门；企业归属深度校验列第三梯队，本轮收口：
by-id 读/写/删端点统一反查资源 enterprise_id 与调用方 query enterprise_id 比对，
不匹配或资源不存在一律 404（不泄露存在性）。调用方（Skill 子进程/前端）经
_osskill/implementations/_base.py 恒携带 enterprise_id query。

本文件验证全部 19 个 by-id 端点均已接入归属校验——归属不匹配 → 404，
而非放行进业务逻辑（若某端点漏接 helper，此处会断言失败）。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from product.api import (
    activate_price_list,
    add_price_list_item,
    batch_update_items,
    delete_document,
    delete_price_list,
    delete_price_list_item,
    delete_product,
    delete_product_image,
    get_document,
    get_price_list,
    get_product,
    list_price_list_items,
    list_product_images,
    set_primary_image,
    supersede_price_list,
    update_document,
    update_price_list,
    update_price_list_item,
    update_product,
)
from product.models import (
    DocumentUpdate,
    PriceListItemCreate,
    PriceListItemUpdate,
    PriceListUpdate,
    ProductUpdate,
)

# 资源归属企业（模拟库存资源属于 ent-a）
ENT = "ent-a"
FOREIGN = "ent-b"


def _assert_404(exc):
    assert exc.value.status_code == 404


class TestProductOwnership:
    """产品 + 图片 by-id 端点：归属不匹配/资源不存在 → 404"""

    @pytest.mark.asyncio
    async def test_get_product_foreign_404(self):
        with patch("product.api.ProductCatalogRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await get_product("pid-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_get_product_missing_404(self):
        with patch("product.api.ProductCatalogRepo.get_enterprise_id",
                   AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await get_product("pid-1", ENT)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_get_product_owned_ok(self):
        with (
            patch("product.api.ProductCatalogRepo.get_enterprise_id",
                  AsyncMock(return_value=ENT)),
            patch("product.api.ProductCatalogRepo.get_with_images",
                  AsyncMock(return_value=({"product_id": "pid-1", "enterprise_id": ENT}, []))),
        ):
            result = await get_product("pid-1", ENT)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_product_foreign_404(self):
        with patch("product.api.ProductCatalogRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await update_product("pid-1", ProductUpdate(name="x"), FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_delete_product_foreign_404(self):
        with patch("product.api.ProductCatalogRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await delete_product("pid-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_list_product_images_foreign_404(self):
        with patch("product.api.ProductCatalogRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await list_product_images("pid-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_set_primary_image_foreign_404(self):
        with patch("product.api.ProductCatalogRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await set_primary_image("pid-1", "img-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_delete_product_image_foreign_404(self):
        with patch("product.api.ProductCatalogRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await delete_product_image("pid-1", "img-1", FOREIGN)
        _assert_404(exc)


class TestPriceListOwnership:
    """价目表头 + 明细 by-id 端点：归属不匹配 → 404"""

    @pytest.mark.asyncio
    async def test_get_price_list_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await get_price_list("pl-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_get_price_list_owned_ok(self):
        with (
            patch("product.api.PriceListRepo.get_enterprise_id",
                  AsyncMock(return_value=ENT)),
            patch("product.api.PriceListRepo.get_with_items",
                  AsyncMock(return_value=({"price_list_id": "pl-1", "enterprise_id": ENT}, []))),
        ):
            result = await get_price_list("pl-1", ENT)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_price_list_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await update_price_list("pl-1", PriceListUpdate(name="x"), FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_activate_price_list_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await activate_price_list("pl-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_supersede_price_list_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await supersede_price_list("pl-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_delete_price_list_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await delete_price_list("pl-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_list_price_list_items_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await list_price_list_items("pl-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_add_price_list_item_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await add_price_list_item("pl-1", PriceListItemCreate(unit_price=1.0), FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_update_price_list_item_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await update_price_list_item(
                    "pl-1", 1, PriceListItemUpdate(unit_price=2.0), FOREIGN,
                )
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_delete_price_list_item_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await delete_price_list_item("pl-1", 1, FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_batch_update_items_foreign_404(self):
        with patch("product.api.PriceListRepo.get_enterprise_id",
                   AsyncMock(return_value=ENT)):
            with pytest.raises(HTTPException) as exc:
                await batch_update_items("pl-1", [], FOREIGN)
        _assert_404(exc)


class TestDocumentOwnership:
    """文档 by-id 端点：归属不匹配/资源不存在 → 404"""

    @pytest.mark.asyncio
    async def test_get_document_foreign_404(self):
        with patch("product.api.DocumentRepo.get",
                   AsyncMock(return_value={"document_id": "doc-1", "enterprise_id": ENT})):
            with pytest.raises(HTTPException) as exc:
                await get_document("doc-1", FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_get_document_missing_404(self):
        with patch("product.api.DocumentRepo.get", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await get_document("doc-1", ENT)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_get_document_owned_ok(self):
        doc = {"document_id": "doc-1", "enterprise_id": ENT, "title": "t"}
        with patch("product.api.DocumentRepo.get", AsyncMock(return_value=doc)):
            result = await get_document("doc-1", ENT)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_update_document_foreign_404(self):
        with patch("product.api.DocumentRepo.get",
                   AsyncMock(return_value={"enterprise_id": ENT})):
            with pytest.raises(HTTPException) as exc:
                await update_document("doc-1", DocumentUpdate(title="x"), FOREIGN)
        _assert_404(exc)

    @pytest.mark.asyncio
    async def test_delete_document_foreign_404(self):
        with patch("product.api.DocumentRepo.get",
                   AsyncMock(return_value={"enterprise_id": ENT})):
            with pytest.raises(HTTPException) as exc:
                await delete_document("doc-1", FOREIGN)
        _assert_404(exc)
