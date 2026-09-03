"""
吸星 — 数据库 Schema 初始化
在 xixing schema 下创建 6 张核心表及索引
"""

import logging

from common.db import get_pool
from . import config as xcfg

logger = logging.getLogger("xixing.database")

SCHEMA = xcfg.get_schema_name()

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

-- 1. 知识源管理（替代 sources.json）
CREATE TABLE IF NOT EXISTS {SCHEMA}.sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'custom',
    schedule        TEXT NOT NULL DEFAULT 'daily',
    day_of_week     INTEGER,
    categories      TEXT[] DEFAULT '{{}}',
    notes           TEXT DEFAULT '',
    enabled         BOOLEAN DEFAULT TRUE,
    reputation      REAL DEFAULT 0.5,
    last_fetched_at TIMESTAMPTZ,
    last_status     TEXT DEFAULT 'pending',
    consecutive_errors INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. 采集运行日志
CREATE TABLE IF NOT EXISTS {SCHEMA}.collection_runs (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES {SCHEMA}.sources(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT DEFAULT 'running',
    http_status     INTEGER,
    content_hash    TEXT,
    content_size    INTEGER,
    raw_path        TEXT,
    error_message   TEXT,
    metadata        JSONB DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_cr_source ON {SCHEMA}.collection_runs (source_id);
CREATE INDEX IF NOT EXISTS idx_cr_status ON {SCHEMA}.collection_runs (status);

-- 3. 知识条目（替代 knowledge/*.md）
CREATE TABLE IF NOT EXISTS {SCHEMA}.knowledge_items (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES {SCHEMA}.sources(id),
    run_id          BIGINT REFERENCES {SCHEMA}.collection_runs(id),
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    quality_score   REAL DEFAULT 0,
    gate_results    JSONB DEFAULT '{{}}',
    tags            TEXT[] DEFAULT '{{}}',
    injected_to_yongheng BOOLEAN DEFAULT FALSE,
    injected_memory_id  BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ki_source ON {SCHEMA}.knowledge_items (source_id);
CREATE INDEX IF NOT EXISTS idx_ki_category ON {SCHEMA}.knowledge_items (category);
CREATE INDEX IF NOT EXISTS idx_ki_injected ON {SCHEMA}.knowledge_items (injected_to_yongheng) WHERE injected_to_yongheng = FALSE;

-- 4. 踩坑记录
CREATE TABLE IF NOT EXISTS {SCHEMA}.xizhenji (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    root_cause      TEXT DEFAULT '',
    solution        TEXT DEFAULT '',
    severity        TEXT DEFAULT 'medium',
    source          TEXT DEFAULT 'manual',
    related_agent   TEXT,
    tags            TEXT[] DEFAULT '{{}}',
    category        TEXT DEFAULT '',
    learned_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved        BOOLEAN DEFAULT FALSE,
    injected_to_yongheng BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_xz_severity ON {SCHEMA}.xizhenji (severity);
CREATE INDEX IF NOT EXISTS idx_xz_resolved ON {SCHEMA}.xizhenji (resolved) WHERE resolved = FALSE;
CREATE INDEX IF NOT EXISTS idx_xz_category ON {SCHEMA}.xizhenji (category);

-- 5. 经验反馈追踪（学习闭环）
CREATE TABLE IF NOT EXISTS {SCHEMA}.experience_feedback (
    id              BIGSERIAL PRIMARY KEY,
    experience_id   TEXT NOT NULL,
    experience_type TEXT NOT NULL DEFAULT 'personal',
    source_agent    TEXT NOT NULL,
    feedback_agent  TEXT NOT NULL,
    feedback_type   TEXT NOT NULL CHECK (feedback_type IN ('useful', 'useless', 'incorrect')),
    feedback_detail TEXT DEFAULT '',
    task_id         TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ef_experience ON {SCHEMA}.experience_feedback (experience_id);
CREATE INDEX IF NOT EXISTS idx_ef_source ON {SCHEMA}.experience_feedback (source_agent);
CREATE INDEX IF NOT EXISTS idx_ef_feedback ON {SCHEMA}.experience_feedback (feedback_agent);

-- 6. 竞品扫描结果
CREATE TABLE IF NOT EXISTS {SCHEMA}.scan_results (
    id              BIGSERIAL PRIMARY KEY,
    scan_date       DATE NOT NULL,
    skill_name      TEXT NOT NULL,
    function_cluster TEXT,
    score           REAL DEFAULT 0,
    difference      TEXT,
    description     TEXT,
    url             TEXT,
    actionable      BOOLEAN DEFAULT FALSE,
    action_taken    TEXT DEFAULT '',
    injected_to_yongheng BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_scan_date ON {SCHEMA}.scan_results (scan_date DESC);

-- 7. 蒸馏日志
CREATE TABLE IF NOT EXISTS {SCHEMA}.distillation_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    namespace       TEXT NOT NULL DEFAULT 'global',
    source_count    INTEGER DEFAULT 0,
    produced_count  INTEGER DEFAULT 0,
    llm_model       TEXT,
    token_used      INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'running'
);
"""


async def ensure_schema():
    """确保所有 xixing 表和索引存在 + 存量迁移"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
        # 存量迁移：旧 xizhenji 表缺少 category 列
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.xizhenji ADD COLUMN IF NOT EXISTS "
                f"category TEXT DEFAULT ''"
            )
        except Exception:
            logger.exception("xixing schema: xizhenji category migration failed")
