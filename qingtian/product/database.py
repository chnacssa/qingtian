"""
产品目录模块 — 数据库 Schema 初始化

在 product schema 下创建 5 张表：
  1. product_catalog         — 产品定义
  2. product_images          — 产品关联图片
  3. price_lists             — 价目表头（版本化）
  4. price_list_items        — 价目表明细
  5. enterprise_documents    — 企业文档登记
"""

from . import config as pcfg
from .db import get_pool

SCHEMA = pcfg.get_schema_name()

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

-- 1. product_catalog — 结构化产品定义
CREATE TABLE IF NOT EXISTS {SCHEMA}.product_catalog (
    product_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    enterprise_id       TEXT NOT NULL,
    category            TEXT NOT NULL,
    name                TEXT NOT NULL,
    model               TEXT DEFAULT '',
    voltage_level       TEXT DEFAULT '',
    power_rating        TEXT DEFAULT '',
    standards           TEXT[] DEFAULT '{{}}',
    technical_params    JSONB DEFAULT '{{}}',
    accessories         TEXT[] DEFAULT '{{}}',
    certification_required TEXT[] DEFAULT '{{}}',
    unit                TEXT DEFAULT '台',
    status              TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'discontinued', 'archived')),
    created_by          TEXT DEFAULT '',
    updated_by          TEXT DEFAULT '',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pc_enterprise ON {SCHEMA}.product_catalog (enterprise_id);
CREATE INDEX IF NOT EXISTS idx_pc_category ON {SCHEMA}.product_catalog (category);
CREATE INDEX IF NOT EXISTS idx_pc_status ON {SCHEMA}.product_catalog (status);
CREATE INDEX IF NOT EXISTS idx_pc_name ON {SCHEMA}.product_catalog USING gin (to_tsvector('simple', name || ' ' || model));

-- 2. product_images — 产品关联图片
CREATE TABLE IF NOT EXISTS {SCHEMA}.product_images (
    image_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id      UUID NOT NULL REFERENCES {SCHEMA}.product_catalog(product_id) ON DELETE CASCADE,
    enterprise_id   TEXT NOT NULL,
    file_id         TEXT NOT NULL,
    filename        TEXT NOT NULL,
    is_primary      BOOLEAN DEFAULT FALSE,
    sort_order      INT DEFAULT 0,
    file_size       BIGINT DEFAULT 0,
    image_sha256    TEXT DEFAULT '',
    mime_type       TEXT DEFAULT '',
    created_by      TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pim_product ON {SCHEMA}.product_images (product_id);
CREATE INDEX IF NOT EXISTS idx_pim_primary ON {SCHEMA}.product_images (product_id, is_primary) WHERE is_primary = TRUE;
CREATE INDEX IF NOT EXISTS idx_pim_enterprise ON {SCHEMA}.product_images (enterprise_id);

-- 3. price_lists — 价目表头（版本化、时效）
CREATE TABLE IF NOT EXISTS {SCHEMA}.price_lists (
    price_list_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    enterprise_id   TEXT NOT NULL,
    name            TEXT NOT NULL,
    version         INT DEFAULT 1,
    valid_from      DATE NOT NULL,
    valid_until     DATE,
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'active', 'superseded', 'archived')),
    source          TEXT NOT NULL DEFAULT 'manual'
                        CHECK (source IN ('manual', 'xlsx_import', 'api')),
    source_file_id  TEXT DEFAULT '',
    created_by      TEXT DEFAULT '',
    approved_by     TEXT DEFAULT '',
    approved_at     TIMESTAMPTZ,
    daily_update    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pl_enterprise ON {SCHEMA}.price_lists (enterprise_id, status);
CREATE INDEX IF NOT EXISTS idx_pl_valid ON {SCHEMA}.price_lists (valid_from, valid_until);
CREATE INDEX IF NOT EXISTS idx_pl_daily ON {SCHEMA}.price_lists (daily_update) WHERE daily_update = TRUE;

-- 4. price_list_items — 价目表明细
CREATE TABLE IF NOT EXISTS {SCHEMA}.price_list_items (
    item_id         BIGSERIAL PRIMARY KEY,
    price_list_id   UUID NOT NULL REFERENCES {SCHEMA}.price_lists(price_list_id) ON DELETE CASCADE,
    product_id      UUID REFERENCES {SCHEMA}.product_catalog(product_id),
    product_spec    JSONB DEFAULT '{{}}',
    unit_price      NUMERIC(14,2) NOT NULL CHECK (unit_price > 0),
    currency        TEXT DEFAULT 'CNY',
    quantity_discount JSONB DEFAULT '{{}}',
    valid_from      DATE,
    valid_until     DATE,
    sort_order      INT DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pli_list ON {SCHEMA}.price_list_items (price_list_id);
CREATE INDEX IF NOT EXISTS idx_pli_product ON {SCHEMA}.price_list_items (product_id);

-- 5. enterprise_documents — 企业文档登记
CREATE TABLE IF NOT EXISTS {SCHEMA}.enterprise_documents (
    document_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    enterprise_id   TEXT NOT NULL,
    title           TEXT NOT NULL,
    document_type   TEXT NOT NULL
                        CHECK (document_type IN ('contract_template', 'qualification',
                               'certificate', 'brochure', 'spec_sheet', 'other')),
    file_id         TEXT NOT NULL,
    filename        TEXT NOT NULL,
    file_size       BIGINT DEFAULT 0,
    file_sha256     TEXT DEFAULT '',
    tags            TEXT[] DEFAULT '{{}}',
    visibility      TEXT NOT NULL DEFAULT 'enterprise'
                        CHECK (visibility IN ('public', 'enterprise', 'private')),
    owner_agent     TEXT DEFAULT '',
    valid_until     DATE,
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'archived', 'revoked')),
    description     TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ed_enterprise ON {SCHEMA}.enterprise_documents (enterprise_id);
CREATE INDEX IF NOT EXISTS idx_ed_type ON {SCHEMA}.enterprise_documents (enterprise_id, document_type);
CREATE INDEX IF NOT EXISTS idx_ed_owner ON {SCHEMA}.enterprise_documents (owner_agent);
CREATE INDEX IF NOT EXISTS idx_ed_tags ON {SCHEMA}.enterprise_documents USING gin (tags);
"""


async def ensure_schema():
    """确保所有 product 表和索引存在"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
