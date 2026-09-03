-- 汇川 v2.4 Phase 2+: Layer 1 文件生命周期注册表
-- 追踪每个原始文件的解析状态，用于安全清理
-- 回滚: DROP TABLE IF EXISTS huichuan.file_registry;

CREATE TABLE IF NOT EXISTS huichuan.file_registry (
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
                           'expired'       -- 过期：关联 entry 全部过期/归档
                       )),
    entries_total    INT DEFAULT 0,
    entries_revoked  INT DEFAULT 0,
    metadata         JSONB DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fr_status
    ON huichuan.file_registry (status);
CREATE INDEX IF NOT EXISTS idx_fr_storage_path
    ON huichuan.file_registry (storage_path);
CREATE INDEX IF NOT EXISTS idx_fr_updated
    ON huichuan.file_registry (updated_at DESC);
