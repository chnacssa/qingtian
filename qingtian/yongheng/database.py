"""
永恒 — 数据库 Schema 初始化
在 yongheng schema 下创建 5 张核心表及索引
"""

from common.db import get_pool
from . import config as ycfg

SCHEMA = ycfg.get_schema_name()

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA {SCHEMA};

-- 1. memories
CREATE TABLE IF NOT EXISTS {SCHEMA}.memories (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,
    memory_type     TEXT NOT NULL DEFAULT 'episodic',
    content         TEXT NOT NULL,
    embedding       vector(512),
    embedding_status TEXT DEFAULT 'pending',
    search_hit_count INTEGER DEFAULT 0,
    keywords        TEXT[],
    source          TEXT DEFAULT 'openclaw',
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    protected       BOOLEAN NOT NULL DEFAULT FALSE,
    consolidated    BOOLEAN NOT NULL DEFAULT FALSE,
    consolidated_to_id BIGINT DEFAULT NULL,
    review_status   TEXT DEFAULT 'pending',
    metadata        JSONB DEFAULT '{{}}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON {SCHEMA}.memories (namespace);
CREATE INDEX IF NOT EXISTS idx_memories_type ON {SCHEMA}.memories (memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON {SCHEMA}.memories (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_memories_protected ON {SCHEMA}.memories (protected) WHERE protected = TRUE;
CREATE INDEX IF NOT EXISTS idx_memories_consolidated ON {SCHEMA}.memories (consolidated) WHERE consolidated = FALSE;
CREATE INDEX IF NOT EXISTS idx_memories_fts ON {SCHEMA}.memories USING GIN (to_tsvector('simple', content));
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON {SCHEMA}.memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    WHERE embedding_status = 'done';

-- 2. trajectories
CREATE TABLE IF NOT EXISTS {SCHEMA}.trajectories (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,
    date            DATE NOT NULL,
    actions         JSONB NOT NULL DEFAULT '[]',
    summary         TEXT DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (namespace, date)
);
CREATE INDEX IF NOT EXISTS idx_trajectories_namespace_date ON {SCHEMA}.trajectories (namespace, date);

-- 3. profiles
CREATE TABLE IF NOT EXISTS {SCHEMA}.profiles (
    namespace       TEXT PRIMARY KEY,
    traits          JSONB DEFAULT '{{}}',
    learned         JSONB DEFAULT '[]',
    state           JSONB DEFAULT '{{}}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. digests
CREATE TABLE IF NOT EXISTS {SCHEMA}.digests (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,
    target_date     DATE NOT NULL,
    type            TEXT NOT NULL DEFAULT 'daily',
    digest          TEXT NOT NULL,
    source_records  BIGINT[],
    record_count    INTEGER DEFAULT 0,
    timeline_entry  TEXT DEFAULT '',
    review_status   TEXT DEFAULT 'pending',
    reviewed_at     TIMESTAMPTZ DEFAULT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (namespace, target_date, type)
);
CREATE INDEX IF NOT EXISTS idx_digests_namespace_date ON {SCHEMA}.digests (namespace, target_date DESC);

-- 5. tokens
CREATE TABLE IF NOT EXISTS {SCHEMA}.tokens (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL,
    level           TEXT NOT NULL DEFAULT 'namespace',
    created_by      TEXT DEFAULT '',
    expires_at      TIMESTAMPTZ DEFAULT NULL,
    revoked         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_yh_tokens_namespace ON {SCHEMA}.tokens (namespace);
CREATE INDEX IF NOT EXISTS idx_yh_tokens_hash ON {SCHEMA}.tokens (token_hash);
"""


async def ensure_schema():
    """确保所有 yongheng 表和索引存在"""
    import os, hashlib

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)

        # 存量表迁移：digests 补审核列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）
        await conn.execute(
            f"ALTER TABLE {SCHEMA}.digests "
            "ADD COLUMN IF NOT EXISTS review_status TEXT DEFAULT 'pending', "
            "ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ"
        )

        # 启动时从 YONGHENG_ADMIN_TOKEN 注入持久化 token（siku 重建表后自动恢复）
        admin_token = os.environ.get("YONGHENG_ADMIN_TOKEN", "")
        if admin_token:
            existing = await conn.fetchval(
                f"SELECT COUNT(*) FROM {SCHEMA}.tokens WHERE level = 'admin' AND revoked = FALSE"
            )
            if not existing:
                token_hash = hashlib.sha256(admin_token.encode()).hexdigest()
                prefix = admin_token[:8]
                await conn.execute(
                    f"INSERT INTO {SCHEMA}.tokens (namespace, token_hash, token_prefix, level, created_by) "
                    f"VALUES ($1, $2, $3, 'admin', 'system:ensure_schema') "
                    f"ON CONFLICT (token_hash) DO NOTHING",
                    "system:admin", token_hash, prefix,
                )
