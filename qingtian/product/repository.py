"""
产品目录/价目表/图片/文档 — 数据访问层 (Repository)

封装所有 product schema 的数据库操作，API 层禁止直接使用 common.db。
每个方法独立管理数据库连接。跨表操作在同一连接内完成。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from . import config as pcfg
from .db import get_pool

logger = logging.getLogger("product.repository")
SCHEMA = pcfg.get_schema_name()


class ProductCatalogRepo:
    """产品目录数据访问"""

    async def list_products(
        self,
        enterprise_id: str,
        category: Optional[str] = None,
        q: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """分页列出产品，返回 (items, total)。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            conditions = ["enterprise_id = $1"]
            params = [enterprise_id]
            idx = 2

            if category:
                conditions.append(f"category = ${idx}")
                params.append(category)
                idx += 1
            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1
            if q:
                conditions.append(
                    f"to_tsvector('simple', name || ' ' || model) "
                    f"@@ plainto_tsquery('simple', ${idx})"
                )
                params.append(q)
                idx += 1

            where = " AND ".join(conditions)
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM {SCHEMA}.product_catalog WHERE {where}",
                *params,
            ) or 0

            offset = (page - 1) * page_size
            rows = await conn.fetch(
                f"""SELECT product_id::text, enterprise_id, category, name, model,
                           voltage_level, power_rating, standards, technical_params,
                           accessories, certification_required, unit, status,
                           created_by, created_at, updated_at
                    FROM {SCHEMA}.product_catalog
                    WHERE {where}
                    ORDER BY updated_at DESC
                    LIMIT ${idx} OFFSET ${idx + 1}""",
                *params, page_size, offset,
            )
        return [dict(r) for r in rows], total

    async def get(self, product_id: str) -> Optional[dict]:
        """获取单个产品。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT product_id::text, enterprise_id, category, name, model,
                           voltage_level, power_rating, standards, technical_params,
                           accessories, certification_required, unit, status,
                           created_by, created_at, updated_at
                    FROM {SCHEMA}.product_catalog WHERE product_id = $1""",
                product_id,
            )
        return dict(row) if row else None

    async def get_enterprise_id(self, product_id: str) -> Optional[str]:
        """反查产品归属企业（含 archived，供 API 归属校验用）。

        P1 (R11): by-id 端点此前不校验企业归属——任意登录者用 id 可直接读/写/删
        其他企业产品。API 层反查比对，不符返回 404（不泄露存在性）。
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT enterprise_id FROM {SCHEMA}.product_catalog "
                f"WHERE product_id = $1",
                product_id,
            )
        return row["enterprise_id"] if row else None

    async def get_with_images(self, product_id: str) -> tuple[Optional[dict], list[dict]]:
        """获取产品及关联图片（同一连接）。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT product_id::text, enterprise_id, category, name, model,
                           voltage_level, power_rating, standards, technical_params,
                           accessories, certification_required, unit, status,
                           created_by, created_at, updated_at
                    FROM {SCHEMA}.product_catalog WHERE product_id = $1""",
                product_id,
            )
            if not row:
                return None, []

            images = await conn.fetch(
                f"""SELECT image_id::text, file_id, filename, is_primary, sort_order,
                           file_size, mime_type, created_at
                    FROM {SCHEMA}.product_images WHERE product_id = $1
                    ORDER BY sort_order, created_at""",
                product_id,
            )
        return dict(row), [dict(i) for i in images]

    async def create(self, data) -> str:
        """创建产品，返回 product_id。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.product_catalog
                    (enterprise_id, category, name, model, voltage_level, power_rating,
                     standards, technical_params, accessories, certification_required,
                     unit, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    RETURNING product_id::text""",
                data.enterprise_id, data.category, data.name, data.model,
                data.voltage_level, data.power_rating,
                data.standards, json.dumps(data.technical_params),
                data.accessories, data.certification_required,
                data.unit, data.created_by,
            )
        return row["product_id"]

    async def update(self, product_id: str, data) -> bool:
        """更新产品，返回是否存在该记录。"""
        fields = []
        params = []
        idx = 1

        for field in ("category", "name", "model", "voltage_level", "power_rating",
                      "unit", "status"):
            val = getattr(data, field, None)
            if val is not None:
                fields.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1

        for field in ("standards", "accessories", "certification_required"):
            val = getattr(data, field, None)
            if val is not None:
                fields.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1

        if data.technical_params is not None:
            fields.append(f"technical_params = ${idx}")
            params.append(json.dumps(data.technical_params))
            idx += 1

        if data.updated_by:
            fields.append(f"updated_by = ${idx}")
            params.append(data.updated_by)
            idx += 1

        if not fields:
            return False

        fields.append("updated_at = NOW()")
        params.append(product_id)

        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"UPDATE {SCHEMA}.product_catalog SET {', '.join(fields)} "
                f"WHERE product_id = ${idx}",
                *params,
            )
        return "0" not in str(result).split()[-1]

    async def soft_delete(self, product_id: str) -> None:
        """软删除产品。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.product_catalog SET status = 'archived', "
                f"updated_at = NOW() WHERE product_id = $1",
                product_id,
            )

    async def import_batch(self, products: list[dict]) -> tuple[int, list[str]]:
        """批量导入产品，返回 (成功数, 错误列表)。"""
        pool = await get_pool()
        created = 0
        errors = []
        async with pool.acquire() as conn:
            for prod in products:
                try:
                    await conn.execute(
                        f"""INSERT INTO {SCHEMA}.product_catalog
                            (enterprise_id, category, name, model, voltage_level,
                             power_rating, standards, unit, created_by)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                        prod["enterprise_id"], prod["category"], prod["name"],
                        prod["model"], prod["voltage_level"], prod["power_rating"],
                        prod["standards"], prod["unit"], prod["created_by"],
                    )
                    created += 1
                except Exception as e:
                    errors.append(f"导入 '{prod['name']}' 失败: {e}")
        return created, errors


class ProductImageRepo:
    """产品图片数据访问"""

    async def list_by_product(self, product_id: str) -> list[dict]:
        """列出产品图片。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT image_id::text, file_id, filename, is_primary, sort_order,
                           file_size, mime_type, created_by, created_at
                    FROM {SCHEMA}.product_images WHERE product_id = $1
                    ORDER BY is_primary DESC, sort_order, created_at""",
                product_id,
            )
        return [dict(r) for r in rows]

    async def get_product_enterprise_id(self, product_id: str) -> Optional[str]:
        """通过产品查 enterprise_id。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT enterprise_id FROM {SCHEMA}.product_catalog "
                f"WHERE product_id = $1 AND status = 'active'",
                product_id,
            )
        return row["enterprise_id"] if row else None

    async def add(self, product_id: str, enterprise_id: str, data) -> str:
        """添加图片（同一连接完成主图清除+插入），返回 image_id。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            if data.is_primary:
                await conn.execute(
                    f"UPDATE {SCHEMA}.product_images SET is_primary = FALSE "
                    f"WHERE product_id = $1",
                    product_id,
                )
            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.product_images
                    (product_id, enterprise_id, file_id, filename, is_primary,
                     sort_order, file_size, image_sha256, mime_type, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                    RETURNING image_id::text""",
                product_id, enterprise_id, data.file_id, data.filename,
                data.is_primary, data.sort_order, data.file_size,
                data.image_sha256, data.mime_type, data.created_by,
            )
        return row["image_id"]

    async def set_primary(self, product_id: str, image_id: str) -> None:
        """设置主图。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            # P2 (R11): 先校验 image 确属本 product —— 原实现第二句仅按 image_id 更新，
            # 未校验归属，可把其他产品的图片误设为本产品主图（跨产品污染）。
            # 归属不符直接拒绝，且不误清本产品现有主图。
            row = await conn.fetchrow(
                f"SELECT image_id FROM {SCHEMA}.product_images "
                f"WHERE image_id = $1 AND product_id = $2",
                image_id, product_id,
            )
            if row is None:
                logger.warning(
                    "set_primary rejected: image %s not owned by product %s",
                    image_id, product_id,
                )
                return
            await conn.execute(
                f"UPDATE {SCHEMA}.product_images SET is_primary = FALSE "
                f"WHERE product_id = $1",
                product_id,
            )
            # P2 (R11): 双条件更新——只允许本产品的图片成为主图
            await conn.execute(
                f"UPDATE {SCHEMA}.product_images SET is_primary = TRUE "
                f"WHERE image_id = $1 AND product_id = $2",
                image_id, product_id,
            )

    async def delete(self, product_id: str, image_id: str) -> None:
        """删除图片。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {SCHEMA}.product_images "
                f"WHERE image_id = $1 AND product_id = $2",
                image_id, product_id,
            )


class PriceListRepo:
    """价目表数据访问"""

    async def list_prices(
        self,
        enterprise_id: str,
        status: Optional[str] = None,
        valid_on: Optional[str] = None,
        daily_update: Optional[bool] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """分页列出价目表，返回 (items, total)。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            conditions = ["enterprise_id = $1"]
            params = [enterprise_id]
            idx = 2

            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1
            if valid_on:
                conditions.append(
                    f"valid_from <= ${idx} AND "
                    f"(valid_until IS NULL OR valid_until >= ${idx})"
                )
                params.append(valid_on)
                idx += 1
            if daily_update is not None:
                conditions.append(f"daily_update = ${idx}")
                params.append(daily_update)
                idx += 1

            where = " AND ".join(conditions)
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM {SCHEMA}.price_lists WHERE {where}",
                *params,
            ) or 0

            offset = (page - 1) * page_size
            rows = await conn.fetch(
                f"""SELECT price_list_id::text, enterprise_id, name, version, valid_from,
                           valid_until, status, source, source_file_id, daily_update,
                           created_by, approved_by, approved_at, created_at, updated_at
                    FROM {SCHEMA}.price_lists
                    WHERE {where}
                    ORDER BY updated_at DESC
                    LIMIT ${idx} OFFSET ${idx + 1}""",
                *params, page_size, offset,
            )
        return [dict(r) for r in rows], total

    async def get(self, price_list_id: str) -> Optional[dict]:
        """获取价目表头。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT price_list_id::text, enterprise_id, name, version, valid_from,
                           valid_until, status, source, source_file_id, daily_update,
                           created_by, approved_by, approved_at, created_at, updated_at
                    FROM {SCHEMA}.price_lists WHERE price_list_id = $1""",
                price_list_id,
            )
        return dict(row) if row else None

    async def get_with_items(self, price_list_id: str) -> tuple[Optional[dict], list[dict]]:
        """获取价目表及明细（同一连接）。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT price_list_id::text, enterprise_id, name, version, valid_from,
                           valid_until, status, source, source_file_id, daily_update,
                           created_by, approved_by, approved_at, created_at, updated_at
                    FROM {SCHEMA}.price_lists WHERE price_list_id = $1""",
                price_list_id,
            )
            if not row:
                return None, []

            items = await conn.fetch(
                f"""SELECT item_id, price_list_id::text, product_id::text, product_spec,
                           unit_price, currency, quantity_discount, valid_from, valid_until,
                           sort_order, created_at
                    FROM {SCHEMA}.price_list_items WHERE price_list_id = $1
                    ORDER BY sort_order, item_id""",
                price_list_id,
            )
        return dict(row), [dict(i) for i in items]

    async def create(self, data) -> str:
        """创建价目表，返回 price_list_id。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.price_lists
                    (enterprise_id, name, valid_from, valid_until, daily_update,
                     source, source_file_id, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    RETURNING price_list_id::text""",
                data.enterprise_id, data.name, data.valid_from, data.valid_until,
                data.daily_update, data.source, data.source_file_id, data.created_by,
            )
        return row["price_list_id"]

    async def update(self, price_list_id: str, data) -> bool:
        """更新价目表头，返回是否有字段被更新。"""
        fields = []
        params = []
        idx = 1

        for field in ("name", "valid_from", "valid_until", "daily_update", "approved_by"):
            val = getattr(data, field, None)
            if val is not None:
                fields.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1

        if data.approved_by:
            fields.append("approved_at = NOW()")

        if not fields:
            return False

        fields.append("updated_at = NOW()")
        params.append(price_list_id)

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.price_lists SET {', '.join(fields)} "
                f"WHERE price_list_id = ${idx}",
                *params,
            )
        return True

    async def activate(self, price_list_id: str) -> None:
        """激活价目表。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.price_lists SET status = 'active', updated_at = NOW() "
                f"WHERE price_list_id = $1 AND status = 'draft'",
                price_list_id,
            )

    async def supersede(self, price_list_id: str, new_valid_from) -> tuple[str, int]:
        """版本升级，返回 (new_id, new_version)。

        P1 (R?): 原实现标记旧版→建新版→复制明细 三步非事务 —— 中途失败会留下
        旧版已 superseded 而新版缺失/明细不完整的「业务空窗」。现整体入事务，任一步
        失败全部回滚。
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"SELECT * FROM {SCHEMA}.price_lists WHERE price_list_id = $1",
                    price_list_id,
                )
                if not row:
                    raise ValueError("价目表不存在")

                effective_from = new_valid_from or datetime.now(timezone.utc).date()
                await conn.execute(
                    f"UPDATE {SCHEMA}.price_lists "
                    f"SET status = 'superseded', valid_until = $2, updated_at = NOW() "
                    f"WHERE price_list_id = $1",
                    price_list_id, effective_from,
                )

                new_row = await conn.fetchrow(
                    f"""INSERT INTO {SCHEMA}.price_lists
                        (enterprise_id, name, version, valid_from, valid_until, status,
                         source, source_file_id, created_by, daily_update)
                        VALUES ($1,$2,$3,$4,$5,'active',$6,$7,$8,$9)
                        RETURNING price_list_id::text""",
                    row["enterprise_id"], row["name"], row["version"] + 1,
                    effective_from, row["valid_until"],
                    row["source"], row["source_file_id"],
                    row["created_by"], row["daily_update"],
                )
                new_id = new_row["price_list_id"]

                items = await conn.fetch(
                    f"SELECT * FROM {SCHEMA}.price_list_items WHERE price_list_id = $1",
                    price_list_id,
                )
                for item in items:
                    await conn.execute(
                        f"""INSERT INTO {SCHEMA}.price_list_items
                            (price_list_id, product_id, product_spec, unit_price, currency,
                             quantity_discount, valid_from, valid_until, sort_order)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                        new_id, item["product_id"], item["product_spec"],
                        item["unit_price"], item["currency"],
                        item["quantity_discount"], item["valid_from"],
                        item["valid_until"], item["sort_order"],
                    )
        return new_id, row["version"] + 1

    async def soft_delete(self, price_list_id: str) -> None:
        """删除价目表。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.price_lists SET status = 'archived', "
                f"updated_at = NOW() WHERE price_list_id = $1",
                price_list_id,
            )

    async def list_items(self, price_list_id: str) -> list[dict]:
        """列出价目表明细。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT item_id, price_list_id::text, product_id::text, product_spec,
                           unit_price, currency, quantity_discount, valid_from, valid_until,
                           sort_order, created_at
                    FROM {SCHEMA}.price_list_items WHERE price_list_id = $1
                    ORDER BY sort_order, item_id""",
                price_list_id,
            )
        return [dict(r) for r in rows]

    async def add_item(self, price_list_id: str, data) -> int:
        """添加明细行，返回 item_id。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.price_list_items
                    (price_list_id, product_id, product_spec, unit_price, currency,
                     quantity_discount, valid_from, valid_until, sort_order)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    RETURNING item_id""",
                price_list_id, data.product_id, json.dumps(data.product_spec),
                data.unit_price, data.currency, json.dumps(data.quantity_discount),
                data.valid_from, data.valid_until, data.sort_order,
            )
        return row["item_id"]

    async def update_item(self, price_list_id: str, item_id: int, data) -> None:
        """更新明细行。"""
        fields = []
        params = []
        idx = 1

        for field in ("unit_price", "currency", "sort_order"):
            val = getattr(data, field, None)
            if val is not None:
                fields.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1

        if data.quantity_discount is not None:
            fields.append(f"quantity_discount = ${idx}")
            params.append(json.dumps(data.quantity_discount))
            idx += 1

        for field in ("valid_from", "valid_until"):
            val = getattr(data, field, None)
            if val is not None:
                fields.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1

        if not fields:
            return

        params.extend([price_list_id, item_id])
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.price_list_items SET {', '.join(fields)} "
                f"WHERE price_list_id = ${idx} AND item_id = ${idx + 1}",
                *params,
            )

    async def delete_item(self, price_list_id: str, item_id: int) -> None:
        """删除明细行。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {SCHEMA}.price_list_items "
                f"WHERE price_list_id = $1 AND item_id = $2",
                price_list_id, item_id,
            )

    async def batch_replace_items(self, price_list_id: str, items: list) -> int:
        """批量替换明细（先删后插），返回数量。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"DELETE FROM {SCHEMA}.price_list_items WHERE price_list_id = $1",
                price_list_id,
            )
            for i, item in enumerate(items):
                await conn.execute(
                    f"""INSERT INTO {SCHEMA}.price_list_items
                        (price_list_id, product_id, product_spec, unit_price, currency,
                         quantity_discount, valid_from, valid_until, sort_order)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                    price_list_id, item.product_id, json.dumps(item.product_spec),
                    item.unit_price, item.currency, json.dumps(item.quantity_discount),
                    item.valid_from, item.valid_until, i,
                )
        return len(items)

    async def get_enterprise_id(self, price_list_id: str) -> Optional[str]:
        """获取价目表企业 ID。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT enterprise_id FROM {SCHEMA}.price_lists WHERE price_list_id = $1",
                price_list_id,
            )
        return row["enterprise_id"] if row else None

    async def import_items(
        self, price_list_id: str, items: list[dict],
    ) -> tuple[int, list[str]]:
        """批量导入明细（先清空），返回 (成功数, 错误列表)。

        P1 (R?): 原实现「先清空再逐条插入 + 逐条吞错」—— 中间某条失败会留下
        旧明细被删、新明细只插了一半的脏状态（明细清空）。现整体入事务：任一条
        失败即整体回滚（清空与已插入全撤销，旧明细完整保留）。
        """
        pool = await get_pool()
        created = 0
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    await conn.execute(
                        f"DELETE FROM {SCHEMA}.price_list_items WHERE price_list_id = $1",
                        price_list_id,
                    )
                    for item in items:
                        await conn.execute(
                            f"""INSERT INTO {SCHEMA}.price_list_items
                                (price_list_id, product_spec, unit_price, currency,
                                 quantity_discount, sort_order)
                                VALUES ($1,$2,$3,$4,$5,$6)""",
                            item["price_list_id"], json.dumps(item["product_spec"]),
                            item["unit_price"], item["currency"],
                            json.dumps(item["quantity_discount"]), item["sort_order"],
                        )
                        created += 1
            except Exception as e:
                # 异常已使事务回滚（旧明细保留），整体失败，不再残留部分导入
                return 0, [f"导入失败，已整体回滚: {e}"]
        return created, []


class DocumentRepo:
    """企业文档数据访问"""

    async def list_docs(
        self,
        enterprise_id: str,
        document_type: Optional[str] = None,
        owner_agent: Optional[str] = None,
        status: str = "active",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        """分页列出文档，返回 (items, total)。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            conditions = ["enterprise_id = $1"]
            params = [enterprise_id]
            idx = 2

            if document_type:
                conditions.append(f"document_type = ${idx}")
                params.append(document_type)
                idx += 1
            if owner_agent:
                conditions.append(f"owner_agent = ${idx}")
                params.append(owner_agent)
                idx += 1
            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1

            where = " AND ".join(conditions)
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM {SCHEMA}.enterprise_documents WHERE {where}",
                *params,
            ) or 0

            offset = (page - 1) * page_size
            rows = await conn.fetch(
                f"""SELECT document_id::text, enterprise_id, title, document_type,
                           file_id, filename, file_size, tags, visibility,
                           owner_agent, valid_until, status, description,
                           created_at, updated_at
                    FROM {SCHEMA}.enterprise_documents
                    WHERE {where}
                    ORDER BY updated_at DESC
                    LIMIT ${idx} OFFSET ${idx + 1}""",
                *params, page_size, offset,
            )
        return [dict(r) for r in rows], total

    async def get(self, document_id: str) -> Optional[dict]:
        """获取文档。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""SELECT document_id::text, enterprise_id, title, document_type,
                           file_id, filename, file_size, tags, visibility,
                           owner_agent, valid_until, status, description,
                           created_at, updated_at
                    FROM {SCHEMA}.enterprise_documents WHERE document_id = $1""",
                document_id,
            )
        return dict(row) if row else None

    async def create(self, data) -> str:
        """登记文档，返回 document_id。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.enterprise_documents
                    (enterprise_id, title, document_type, file_id, filename, file_size,
                     file_sha256, tags, visibility, owner_agent, valid_until, description)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    RETURNING document_id::text""",
                data.enterprise_id, data.title, data.document_type, data.file_id,
                data.filename, data.file_size, data.file_sha256, data.tags,
                data.visibility, data.owner_agent, data.valid_until, data.description,
            )
        return row["document_id"]

    async def update(self, document_id: str, data) -> bool:
        """更新文档，返回是否有字段被更新。"""
        fields = []
        params = []
        idx = 1

        for field in ("title", "document_type", "visibility", "status", "description"):
            val = getattr(data, field, None)
            if val is not None:
                fields.append(f"{field} = ${idx}")
                params.append(val)
                idx += 1

        if data.tags is not None:
            fields.append(f"tags = ${idx}")
            params.append(data.tags)
            idx += 1

        if data.valid_until is not None:
            fields.append(f"valid_until = ${idx}")
            params.append(data.valid_until)
            idx += 1

        if not fields:
            return False

        fields.append("updated_at = NOW()")
        params.append(document_id)

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.enterprise_documents SET {', '.join(fields)} "
                f"WHERE document_id = ${idx}",
                *params,
            )
        return True

    async def soft_delete(self, document_id: str) -> None:
        """删除文档。"""
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA}.enterprise_documents SET status = 'archived', "
                f"updated_at = NOW() WHERE document_id = $1",
                document_id,
            )
