"""销售智能体 — 月度/周度运营报表生成。

数据来源：product schema（产品目录/价目表/文档）
自包含，不依赖ACSSA 底座。
"""

import logging
from datetime import date, timedelta

from .db import get_pool
from . import config as pcfg

logger = logging.getLogger("product.reports")

SCHEMA = pcfg.get_schema_name()


def _calc_period(period_type: str) -> tuple[date, date]:
    today = date.today()
    if period_type == "daily":
        return today, today
    elif period_type == "weekly":
        monday = today - timedelta(days=today.weekday())
        return monday, today
    else:
        month_start = today.replace(day=1)
        return month_start, today


async def generate_report(enterprise_id: str, period_type: str = "monthly") -> dict:
    """生成销售智能体运营报表。"""
    period_start, period_end = _calc_period(period_type)
    pool = await get_pool()

    async with pool.acquire() as conn:
        # ── 产品目录统计 ──
        prod_stats = await conn.fetchrow(
            f"""SELECT COUNT(*)::int AS total,
                       COUNT(*) FILTER (WHERE status='active')::int AS active,
                       COUNT(*) FILTER (WHERE status='discontinued')::int AS discontinued,
                       COUNT(DISTINCT category)::int AS categories
                FROM {SCHEMA}.product_catalog
                WHERE enterprise_id=$1""",
            enterprise_id,
        )

        # ── 分类分布 ──
        cat_dist = []
        if prod_stats["total"] > 0:
            rows = await conn.fetch(
                f"""SELECT category, COUNT(*)::int AS cnt
                    FROM {SCHEMA}.product_catalog
                    WHERE enterprise_id=$1 AND status='active'
                    GROUP BY category ORDER BY cnt DESC LIMIT 10""",
                enterprise_id,
            )
            cat_dist = [dict(r) for r in rows]

        # ── 本期新增产品 ──
        new_products = await conn.fetchval(
            f"""SELECT COUNT(*)::int FROM {SCHEMA}.product_catalog
                WHERE enterprise_id=$1
                  AND created_at >= $2 AND created_at < $3""",
            enterprise_id, period_start, period_end + timedelta(days=1),
        ) or 0

        # ── 价目表统计 ──
        price_stats = await conn.fetchrow(
            f"""SELECT
                    COUNT(*)::int AS total,
                    COUNT(*) FILTER (WHERE status='active')::int AS active,
                    COUNT(*) FILTER (WHERE status='draft')::int AS draft,
                    COUNT(*) FILTER (WHERE status='superseded')::int AS superseded,
                    COUNT(*) FILTER (WHERE daily_update=true)::int AS daily_update_count
                FROM {SCHEMA}.price_lists
                WHERE enterprise_id=$1""",
            enterprise_id,
        )

        # ── 即将过期价目表 ──
        expiring = []
        rows = await conn.fetch(
            f"""SELECT price_list_id::text, name, version, valid_until
                FROM {SCHEMA}.price_lists
                WHERE enterprise_id=$1 AND status='active'
                  AND valid_until IS NOT NULL
                  AND valid_until BETWEEN $2 AND $3
                ORDER BY valid_until LIMIT 5""",
            enterprise_id, period_start, period_end + timedelta(days=30),
        )
        expiring = [dict(r) for r in rows]

        # ── 文档统计 ──
        doc_stats = await conn.fetchrow(
            f"""SELECT COUNT(*)::int AS total,
                       COUNT(*) FILTER (WHERE document_type='brochure')::int AS brochures,
                       COUNT(*) FILTER (WHERE document_type='qualification')::int AS quals,
                       COUNT(*) FILTER (WHERE document_type='contract_template')::int AS contracts,
                       COUNT(*) FILTER (WHERE document_type='certificate')::int AS certs
                FROM {SCHEMA}.enterprise_documents
                WHERE enterprise_id=$1 AND status='active'""",
            enterprise_id,
        )

    return {
        "module": "sales",
        "period": f"{period_start}~{period_end}",
        "summary": (f"产品目录{prod_stats['active']}个活跃产品，{prod_stats['categories']}个类别；"
                     f"价目表{price_stats['active']}个活跃；文档{doc_stats['total']}份"),
        "kpi": {
            "产品目录": [
                {"label": "产品总数", "value": prod_stats["total"], "unit": "个"},
                {"label": "活跃产品", "value": prod_stats["active"], "unit": "个"},
                {"label": "产品类别", "value": prod_stats["categories"], "unit": "类"},
                {"label": "本期新增", "value": new_products, "unit": "个",
                 "trend": "up" if new_products > 0 else "flat"},
            ],
            "价目表": [
                {"label": "价目表总数", "value": price_stats["total"], "unit": "份"},
                {"label": "活跃中", "value": price_stats["active"], "unit": "份"},
                {"label": "草稿", "value": price_stats["draft"], "unit": "份"},
                {"label": "每日更新", "value": price_stats["daily_update_count"], "unit": "份"},
            ],
            "企业文档": [
                {"label": "文档总数", "value": doc_stats["total"], "unit": "份"},
                {"label": "产品手册", "value": doc_stats["brochures"], "unit": "份"},
                {"label": "资质文件", "value": doc_stats["quals"], "unit": "份"},
                {"label": "合同模板", "value": doc_stats["contracts"], "unit": "份"},
            ],
        },
        "tables": {
            "产品分类分布": cat_dist,
            "30天内将过期价目表": [dict(r) for r in expiring],
        },
    }
