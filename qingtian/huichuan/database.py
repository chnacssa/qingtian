"""汇川 — PostgreSQL DDL"""

from common.db import get_pool

from . import config as kcfg

SCHEMA = kcfg.get_schema_name()

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

-- ── 1. knowledge_entries ─────────────────────────────

CREATE TABLE IF NOT EXISTS {SCHEMA}.knowledge_entries (
    knowledge_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title            TEXT NOT NULL,
    domain           TEXT NOT NULL,
    tags             TEXT[] DEFAULT '{{}}',
    visibility       TEXT NOT NULL DEFAULT 'public'
                         CHECK (visibility IN ('public', 'enterprise', 'private')),
    owner_agent      TEXT,
    authorized_agents TEXT[] DEFAULT '{{}}',
    content          TEXT NOT NULL,
    source           TEXT DEFAULT 'manual',
    version          INT DEFAULT 1,
    valid_from       DATE,
    valid_until      DATE,
    metadata         JSONB DEFAULT '{{}}',
    entry_type       TEXT DEFAULT 'entity'
                         CHECK (entry_type IN ('entity','concept','comparison','query','source')),
    original_filename      TEXT,
    original_storage_path  TEXT,
    original_file_sha256   TEXT,
    quality          INT DEFAULT 3 CHECK (quality BETWEEN 1 AND 5),
    status           TEXT DEFAULT 'active'
                         CHECK (status IN ('draft', 'active', 'archived', 'revoked')),
    refined_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ke_domain
    ON {SCHEMA}.knowledge_entries (domain);
CREATE INDEX IF NOT EXISTS idx_ke_tags
    ON {SCHEMA}.knowledge_entries USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_ke_visibility
    ON {SCHEMA}.knowledge_entries (visibility);
CREATE INDEX IF NOT EXISTS idx_ke_owner
    ON {SCHEMA}.knowledge_entries (owner_agent)
    WHERE owner_agent IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ke_updated
    ON {SCHEMA}.knowledge_entries (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ke_fts
    ON {SCHEMA}.knowledge_entries
    USING GIN (to_tsvector('simple', title || ' ' || content));

-- ── 2. knowledge_versions ────────────────────────────

CREATE TABLE IF NOT EXISTS {SCHEMA}.knowledge_versions (
    version_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_id    UUID NOT NULL REFERENCES {SCHEMA}.knowledge_entries(knowledge_id)
                        ON DELETE CASCADE,
    version         INT NOT NULL,
    content         TEXT NOT NULL,
    changed_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kv_knowledge
    ON {SCHEMA}.knowledge_versions (knowledge_id, version DESC);

-- ── 3. subscriptions ─────────────────────────────────

CREATE TABLE IF NOT EXISTS {SCHEMA}.subscriptions (
    subscription_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id          TEXT NOT NULL,
    subscription_name TEXT NOT NULL DEFAULT 'default',
    domains           TEXT[] DEFAULT '{{}}',
    tags              TEXT[] DEFAULT '{{}}',
    active            BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, subscription_name)
);

CREATE INDEX IF NOT EXISTS idx_sub_agent
    ON {SCHEMA}.subscriptions (agent_id)
    WHERE active = TRUE;

-- ── 4. refinement_queue ──────────────────────────────

CREATE TABLE IF NOT EXISTS {SCHEMA}.refinement_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submitter       TEXT NOT NULL,
    domain           TEXT,
    raw_experience  TEXT NOT NULL,
    confidence      INT DEFAULT 3 CHECK (confidence BETWEEN 1 AND 5),
    status          TEXT DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'approved', 'rejected')),
    refined_content TEXT,
    knowledge_id    UUID REFERENCES {SCHEMA}.knowledge_entries(knowledge_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_rq_status
    ON {SCHEMA}.refinement_queue (status, created_at);

-- ── 5. knowledge_links (Phase 5) ─────────────────────────

CREATE TABLE IF NOT EXISTS {SCHEMA}.knowledge_links (
    link_id      BIGSERIAL PRIMARY KEY,
    source_id    UUID NOT NULL REFERENCES {SCHEMA}.knowledge_entries(knowledge_id)
                     ON DELETE CASCADE,
    target_id    UUID NOT NULL REFERENCES {SCHEMA}.knowledge_entries(knowledge_id)
                     ON DELETE CASCADE,
    link_type    TEXT NOT NULL CHECK (link_type IN ('related','contradicts','extends','depends','cites')),
    confidence   FLOAT DEFAULT 1.0,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, target_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_kl_source
    ON {SCHEMA}.knowledge_links(source_id);
CREATE INDEX IF NOT EXISTS idx_kl_target
    ON {SCHEMA}.knowledge_links(target_id);

-- ── 6. file_registry (Phase 2+ Layer 1 文件生命周期管理) ──

CREATE TABLE IF NOT EXISTS {SCHEMA}.file_registry (
    file_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    storage_path   TEXT NOT NULL UNIQUE,
    original_filename TEXT,
    file_sha256    TEXT,
    file_size      BIGINT DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN (
                           'active',       -- 正常：关联 entry 在库中
                           'corrupted',    -- 损坏：解析/提取失败
                           'low_quality',  -- 低质：所有关联 entry quality < 3
                           'revoked',      -- 已撤销：所有关联 entry 已软删除
                           'expired',      -- 过期：关联 entry 全部过期/归档
                           'deleted'       -- 冷静期：已删进回收区，30 天后 purge
                       )),
    deleted_at     TIMESTAMPTZ,            -- 软删时间（进入冷静期）
    purge_at       TIMESTAMPTZ,            -- 计划真删时间（默认 +30 天）
    entries_total    INT DEFAULT 0,
    entries_revoked  INT DEFAULT 0,
    metadata         JSONB DEFAULT '{{}}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 冷静期软删（2026-08-13）：已有部署补列 + status CHECK 放宽到含 'deleted'
-- （CREATE IF NOT EXISTS 不修改已存在表，需幂等 ALTER；约束先 DROP 再 ADD 保证最新）
ALTER TABLE {SCHEMA}.file_registry ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE {SCHEMA}.file_registry ADD COLUMN IF NOT EXISTS purge_at TIMESTAMPTZ;
ALTER TABLE {SCHEMA}.file_registry DROP CONSTRAINT IF EXISTS file_registry_status_check;
ALTER TABLE {SCHEMA}.file_registry ADD CONSTRAINT file_registry_status_check
    CHECK (status IN ('active','corrupted','low_quality','revoked','expired','deleted'));
CREATE INDEX IF NOT EXISTS idx_fr_purge
    ON {SCHEMA}.file_registry (purge_at) WHERE status = 'deleted';

CREATE INDEX IF NOT EXISTS idx_fr_status
    ON {SCHEMA}.file_registry (status);
CREATE INDEX IF NOT EXISTS idx_fr_storage_path
    ON {SCHEMA}.file_registry (storage_path);
CREATE INDEX IF NOT EXISTS idx_fr_updated
    ON {SCHEMA}.file_registry (updated_at DESC);

-- R11 P2: refinement_queue 增加 metadata（LLM 失败计数/指数退避）+ status 放宽含 'failed'
-- （CREATE IF NOT EXISTS 不修改已存在表，需幂等 ALTER；约束先 DROP 再 ADD 保证最新）
ALTER TABLE {SCHEMA}.refinement_queue ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{{}}';
ALTER TABLE {SCHEMA}.refinement_queue DROP CONSTRAINT IF EXISTS refinement_queue_status_check;
ALTER TABLE {SCHEMA}.refinement_queue ADD CONSTRAINT refinement_queue_status_check
    CHECK (status IN ('pending','processing','approved','rejected','failed'));

-- ── 7. file_images (Phase 1+ 图片提取索引) ─────────────────

CREATE TABLE IF NOT EXISTS {SCHEMA}.file_images (
    image_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id        UUID NOT NULL REFERENCES {SCHEMA}.file_registry(file_id) ON DELETE CASCADE,
    source_type    TEXT NOT NULL,          -- pdf|docx|xlsx
    source_sheet   TEXT DEFAULT '',        -- Excel sheet 名
    page_num       INT DEFAULT 0,         -- PDF 页码
    image_index    INT NOT NULL,          -- 文件内图片序号
    image_format   TEXT NOT NULL,         -- png|jpg|svg|webp
    image_size     INT NOT NULL,          -- bytes
    image_sha256   TEXT NOT NULL,         -- 去重
    storage_path   TEXT NOT NULL,         -- Layer 1 路径
    width          INT DEFAULT 0,
    height         INT DEFAULT 0,
    context_before TEXT DEFAULT '',       -- 前文 200 字
    context_after  TEXT DEFAULT '',       -- 后文 200 字
    alt_text       TEXT DEFAULT '',       -- 多模态预留
    metadata       JSONB DEFAULT '{{}}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fi_file
    ON {SCHEMA}.file_images (file_id);
CREATE INDEX IF NOT EXISTS idx_fi_sha256
    ON {SCHEMA}.file_images (image_sha256);
"""


async def ensure_schema():
    """确保所有 huichuan 表和索引存在"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
