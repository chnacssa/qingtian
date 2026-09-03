"""
产品目录/价目表/图片/文档 — REST API

前缀 /v1/product

解耦说明：
  - 不直接导入 common.db（数据访问委托给 repository.py）
  - 不直接导入 common.config（配置委托给 config.py）

端点组：
  /catalog                     — 产品目录 CRUD + 搜索
  /catalog/import              — XLSX 批量导入
  /{product_id}/images         — 产品图片管理
  /price-lists                 — 价目表头 CRUD + 版本管理
  /price-lists/{id}/items      — 价目表明细
  /price-lists/{id}/import     — 价目表 XLSX 导入
  /documents                   — 企业文档管理
"""

import json
import logging
from datetime import date, datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from zhenyue.auth import auth_dependency

from . import config as pcfg
from . import models as pmod
from .excel_processor import parse_catalog_xlsx, parse_price_list_xlsx
from .reports import generate_report
from .repository import (
    DocumentRepo,
    PriceListRepo,
    ProductCatalogRepo,
    ProductImageRepo,
)

logger = logging.getLogger("product.api")
# A4 (R11): 全量端点挂鉴权——无有效 Bearer token 一律 401。
# 调用方（Skill 子进程/前端）均带登录 token；企业归属校验待第三梯队深化。
router = APIRouter(
    prefix="/v1/product",
    tags=["产品目录"],
    dependencies=[Depends(auth_dependency)],
)


def _serialize(d: dict) -> dict:
    """将 datetime/date 转为 isoformat 字符串"""
    for k, v in d.items():
        if isinstance(v, (datetime, date)):
            d[k] = v.isoformat()
    return d


async def _require_product_owner(product_id: str, enterprise_id: str) -> None:
    """P1 (R11): 产品 by-id 归属校验——资源不存在或归属其他企业一律 404，不泄露存在性。

    A4 首轮只挂了 Bearer 鉴权门；企业归属比对原列入第三梯队，此处收口。
    调用方（Skill 子进程/前端）经 _base.py 恒携带 enterprise_id query。
    """
    ent = await ProductCatalogRepo().get_enterprise_id(product_id)
    if not ent or ent != enterprise_id:
        raise HTTPException(404, "产品不存在")


async def _require_price_list_owner(price_list_id: str, enterprise_id: str) -> None:
    """P1 (R11): 价目表 by-id 归属校验。"""
    ent = await PriceListRepo().get_enterprise_id(price_list_id)
    if not ent or ent != enterprise_id:
        raise HTTPException(404, "价目表不存在")


async def _require_document_owner(document_id: str, enterprise_id: str) -> None:
    """P1 (R11): 文档 by-id 归属校验。"""
    doc = await DocumentRepo().get(document_id)
    if not doc or doc.get("enterprise_id") != enterprise_id:
        raise HTTPException(404, "文档不存在")


async def _download_file(file_id: str, enterprise_id: str) -> bytes:
    """通过 file_service 下载文件内容。"""
    url = pcfg.get_internal_file_url(file_id)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url, headers={"X-Enterprise-ID": enterprise_id},
            )
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPError as e:
        raise HTTPException(400, f"无法下载文件: {e}")


# ═══════════════════════════════════════════════════
# 运营报表
# ═══════════════════════════════════════════════════


@router.get("/report")
async def api_product_report(
    enterprise_id: str = Query(...),
    period_type: str = Query("monthly"),
):
    """产品/销售运营报表 — 结构化 KPI + 分类分布。

    period_type: daily / weekly / monthly
    供 ExecutiveReportSkill 调用。
    """
    result = await generate_report(enterprise_id, period_type)
    return {"status": "ok", "data": result}


# ═══════════════════════════════════════════════════
# 产品目录 CRUD


@router.get("/catalog")
async def list_products(
    enterprise_id: str = Query(...),
    category: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """列出产品，支持按类别/搜索词/状态过滤。"""
    repo = ProductCatalogRepo()
    items, total = await repo.list_products(
        enterprise_id, category=category, q=q, status=status,
        page=page, page_size=page_size,
    )
    return {
        "status": "ok",
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(i) for i in items],
    }


@router.get("/catalog/{product_id}")
async def get_product(product_id: str, enterprise_id: str = Query(...)):
    """获取单个产品详情（含图片）。"""
    await _require_product_owner(product_id, enterprise_id)
    repo = ProductCatalogRepo()
    product, images = await repo.get_with_images(product_id)
    if not product:
        raise HTTPException(404, "产品不存在")

    result = _serialize(product)
    result["images"] = [_serialize(i) for i in images]
    return {"status": "ok", "product": result}


@router.post("/catalog")
async def create_product(body: pmod.ProductCreate):
    """创建产品。"""
    repo = ProductCatalogRepo()
    product_id = await repo.create(body)
    return {"status": "ok", "product_id": product_id}


@router.put("/catalog/{product_id}")
async def update_product(product_id: str, body: pmod.ProductUpdate, enterprise_id: str = Query(...)):
    """更新产品（仅传入非空字段）。"""
    await _require_product_owner(product_id, enterprise_id)
    repo = ProductCatalogRepo()
    updated = await repo.update(product_id, body)
    if not updated:
        raise HTTPException(400, "没有需要更新的字段" if not body.model_dump(exclude_defaults=True)
                            else "产品不存在")
    return {"status": "ok", "product_id": product_id}


@router.delete("/catalog/{product_id}")
async def delete_product(product_id: str, enterprise_id: str = Query(...)):
    """软删除产品（设为 archived）。"""
    await _require_product_owner(product_id, enterprise_id)
    repo = ProductCatalogRepo()
    await repo.soft_delete(product_id)
    return {"status": "ok", "product_id": product_id}


# ═══════════════════════════════════════════════════
# 产品图片
# ═══════════════════════════════════════════════════


@router.get("/{product_id}/images")
async def list_product_images(product_id: str, enterprise_id: str = Query(...)):
    """列出产品关联图片。"""
    await _require_product_owner(product_id, enterprise_id)
    repo = ProductImageRepo()
    images = await repo.list_by_product(product_id)
    return {"status": "ok", "images": [_serialize(i) for i in images]}


@router.post("/{product_id}/images")
async def add_product_image(product_id: str, body: pmod.ProductImageCreate):
    """添加产品图片（文件已通过 file_service 上传）。"""
    image_repo = ProductImageRepo()

    # 从产品目录反查 enterprise_id（不依赖客户端传入）
    enterprise_id = await image_repo.get_product_enterprise_id(product_id)
    if not enterprise_id:
        return {"status": "error", "error": f"产品不存在或已下架: {product_id}"}

    image_id = await image_repo.add(product_id, enterprise_id, body)
    return {"status": "ok", "image_id": image_id}


@router.put("/{product_id}/images/{image_id}/primary")
async def set_primary_image(product_id: str, image_id: str, enterprise_id: str = Query(...)):
    """设置主图。"""
    await _require_product_owner(product_id, enterprise_id)
    repo = ProductImageRepo()
    await repo.set_primary(product_id, image_id)
    return {"status": "ok", "image_id": image_id}


@router.delete("/{product_id}/images/{image_id}")
async def delete_product_image(product_id: str, image_id: str, enterprise_id: str = Query(...)):
    """删除产品图片。"""
    await _require_product_owner(product_id, enterprise_id)
    repo = ProductImageRepo()
    await repo.delete(product_id, image_id)
    return {"status": "ok", "image_id": image_id}


# ═══════════════════════════════════════════════════
# 价目表头 CRUD
# ═══════════════════════════════════════════════════


@router.get("/price-lists")
async def list_price_lists(
    enterprise_id: str = Query(...),
    status: Optional[str] = Query(None),
    valid_on: Optional[date] = Query(None),
    daily_update: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """列出价目表。"""
    repo = PriceListRepo()
    items, total = await repo.list_prices(
        enterprise_id, status=status, valid_on=valid_on,
        daily_update=daily_update, page=page, page_size=page_size,
    )
    return {
        "status": "ok",
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(i) for i in items],
    }


@router.get("/price-lists/{price_list_id}")
async def get_price_list(price_list_id: str, enterprise_id: str = Query(...)):
    """获取价目表详情（含明细行）。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    price_list, items = await repo.get_with_items(price_list_id)
    if not price_list:
        raise HTTPException(404, "价目表不存在")

    result = _serialize(price_list)
    result["items"] = items
    return {"status": "ok", "price_list": result}


@router.post("/price-lists")
async def create_price_list(body: pmod.PriceListCreate):
    """创建价目表。"""
    repo = PriceListRepo()
    price_list_id = await repo.create(body)
    return {"status": "ok", "price_list_id": price_list_id}


@router.put("/price-lists/{price_list_id}")
async def update_price_list(price_list_id: str, body: pmod.PriceListUpdate, enterprise_id: str = Query(...)):
    """更新价目表头信息。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    updated = await repo.update(price_list_id, body)
    if not updated:
        raise HTTPException(400, "没有需要更新的字段")
    return {"status": "ok", "price_list_id": price_list_id}


@router.post("/price-lists/{price_list_id}/activate")
async def activate_price_list(price_list_id: str, enterprise_id: str = Query(...)):
    """激活价目表（draft → active）。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    await repo.activate(price_list_id)
    return {"status": "ok", "price_list_id": price_list_id}


@router.post("/price-lists/{price_list_id}/supersede")
async def supersede_price_list(
    price_list_id: str,
    enterprise_id: str = Query(...),
    new_valid_from: Optional[date] = Query(None),
):
    """版本升级：当前价目表标记 superseded，创建新版本。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    try:
        new_id, new_version = await repo.supersede(price_list_id, new_valid_from)
    except ValueError:
        raise HTTPException(404, "价目表不存在")

    return {
        "status": "ok",
        "old_id": price_list_id,
        "new_id": new_id,
        "version": new_version,
    }


@router.delete("/price-lists/{price_list_id}")
async def delete_price_list(price_list_id: str, enterprise_id: str = Query(...)):
    """删除价目表（设为 archived）。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    await repo.soft_delete(price_list_id)
    return {"status": "ok", "price_list_id": price_list_id}


# ═══════════════════════════════════════════════════
# 价目表明细
# ═══════════════════════════════════════════════════


@router.get("/price-lists/{price_list_id}/items")
async def list_price_list_items(price_list_id: str, enterprise_id: str = Query(...)):
    """列出价目表明细。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    items = await repo.list_items(price_list_id)
    return {"status": "ok", "items": items}


@router.post("/price-lists/{price_list_id}/items")
async def add_price_list_item(
    price_list_id: str, body: pmod.PriceListItemCreate, enterprise_id: str = Query(...),
):
    """添加价目表明细行。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    item_id = await repo.add_item(price_list_id, body)
    return {"status": "ok", "item_id": item_id}


@router.put("/price-lists/{price_list_id}/items/{item_id}")
async def update_price_list_item(
    price_list_id: str, item_id: int, body: pmod.PriceListItemUpdate,
    enterprise_id: str = Query(...),
):
    """更新价目表明细行。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    await repo.update_item(price_list_id, item_id, body)
    return {"status": "ok", "item_id": item_id}


@router.delete("/price-lists/{price_list_id}/items/{item_id}")
async def delete_price_list_item(price_list_id: str, item_id: int, enterprise_id: str = Query(...)):
    """删除价目表明细行。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    await repo.delete_item(price_list_id, item_id)
    return {"status": "ok", "item_id": item_id}


@router.put("/price-lists/{price_list_id}/items/batch")
async def batch_update_items(
    price_list_id: str, items: list[pmod.PriceListItemCreate],
    enterprise_id: str = Query(...),
):
    """批量替换价目表明细（先删后插）。"""
    await _require_price_list_owner(price_list_id, enterprise_id)
    repo = PriceListRepo()
    count = await repo.batch_replace_items(price_list_id, items)
    return {"status": "ok", "count": count}


# ═══════════════════════════════════════════════════
# 企业文档
# ═══════════════════════════════════════════════════


@router.get("/documents")
async def list_documents(
    enterprise_id: str = Query(...),
    document_type: Optional[str] = Query(None),
    owner_agent: Optional[str] = Query(None),
    status: str = Query("active"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """列出企业文档。"""
    repo = DocumentRepo()
    items, total = await repo.list_docs(
        enterprise_id, document_type=document_type,
        owner_agent=owner_agent, status=status,
        page=page, page_size=page_size,
    )
    return {
        "status": "ok",
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_serialize(i) for i in items],
    }


@router.get("/documents/{document_id}")
async def get_document(document_id: str, enterprise_id: str = Query(...)):
    """获取文档元数据。"""
    await _require_document_owner(document_id, enterprise_id)
    repo = DocumentRepo()
    doc = await repo.get(document_id)
    if not doc:
        raise HTTPException(404, "文档不存在")
    return {"status": "ok", "document": _serialize(doc)}


@router.post("/documents")
async def create_document(body: pmod.DocumentCreate):
    """登记企业文档（文件已通过 file_service 上传）。"""
    repo = DocumentRepo()
    document_id = await repo.create(body)
    return {"status": "ok", "document_id": document_id}


@router.put("/documents/{document_id}")
async def update_document(document_id: str, body: pmod.DocumentUpdate, enterprise_id: str = Query(...)):
    """更新文档元数据。"""
    await _require_document_owner(document_id, enterprise_id)
    repo = DocumentRepo()
    updated = await repo.update(document_id, body)
    if not updated:
        raise HTTPException(400, "没有需要更新的字段")
    return {"status": "ok", "document_id": document_id}


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str, enterprise_id: str = Query(...)):
    """删除文档（设为 archived）。"""
    await _require_document_owner(document_id, enterprise_id)
    repo = DocumentRepo()
    await repo.soft_delete(document_id)
    return {"status": "ok", "document_id": document_id}


# ═══════════════════════════════════════════════════
# XLSX 批量导入
# ═══════════════════════════════════════════════════


@router.post("/catalog/import")
async def import_catalog(
    enterprise_id: str = Query(...),
    file_id: str = Body(..., embed=True),
    created_by: str = Body(default=""),
):
    """XLSX 批量导入产品目录。

    先通过 file_service 上传 XLSX，再调用此端点：
      POST /v1/product/catalog/import?enterprise_id=xxx
      {"file_id": "abc123...", "created_by": "optional"}
    """
    if not file_id:
        raise HTTPException(400, "请提供 file_id")

    # 通过 file_service 下载文件
    file_bytes = await _download_file(file_id, enterprise_id)

    result = parse_catalog_xlsx(file_bytes, enterprise_id, created_by)
    if not result["products"]:
        raise HTTPException(400, f"未解析到任何产品: {result['errors']}")

    # 批量写入数据库
    repo = ProductCatalogRepo()
    created, errors = await repo.import_batch(result["products"])

    return {
        "status": "ok",
        "total": result["total"],
        "created": created,
        "errors": errors,
    }


@router.post("/price-lists/{price_list_id}/import")
async def import_price_list_items(
    price_list_id: str,
    file_id: str = Body(..., embed=True),
):
    """XLSX 导入价目表明细。

      POST /v1/product/price-lists/{id}/import
      {"file_id": "abc123..."}
    """
    if not file_id:
        raise HTTPException(400, "请提供 file_id")

    # 获取价目表 enterprise_id
    pl_repo = PriceListRepo()
    enterprise_id = await pl_repo.get_enterprise_id(price_list_id)
    if not enterprise_id:
        raise HTTPException(404, "价目表不存在")

    # 下载文件
    file_bytes = await _download_file(file_id, enterprise_id)

    result = parse_price_list_xlsx(file_bytes, price_list_id)
    if not result["items"]:
        raise HTTPException(400, f"未解析到任何价目项: {result['errors']}")

    # 先清空旧明细，再批量插入
    created, errors = await pl_repo.import_items(price_list_id, result["items"])

    return {
        "status": "ok",
        "total": result["total"],
        "created": created,
        "errors": errors,
    }
