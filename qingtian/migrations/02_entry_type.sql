-- 汇川 v2.4 Phase 1: 知识结构化 — 类型系统
-- entry_type + metadata + 文件溯源字段
-- 回滚: 见文件末尾注释

-- 1. entry_type: 知识条目类型
ALTER TABLE huichuan.knowledge_entries
  ADD COLUMN IF NOT EXISTS entry_type TEXT DEFAULT 'entity'
    CHECK (entry_type IN ('entity','concept','comparison','query','source'));

-- 2. metadata: 扩展元数据（已有 JSONB DEFAULT '{}'，此步幂等）
--    如果表中已有该列，此语句跳过（IF NOT EXISTS 不适用于列，用 DO 块兜底）
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'huichuan' AND table_name = 'knowledge_entries'
    AND column_name = 'metadata'
  ) THEN
    ALTER TABLE huichuan.knowledge_entries ADD COLUMN metadata JSONB DEFAULT '{}';
  END IF;
END $$;

-- 3. original_filename: 原始文件名（溯源）
ALTER TABLE huichuan.knowledge_entries
  ADD COLUMN IF NOT EXISTS original_filename TEXT;

-- 4. original_storage_path: Layer 1 存储路径（溯源）
ALTER TABLE huichuan.knowledge_entries
  ADD COLUMN IF NOT EXISTS original_storage_path TEXT;

-- 5. original_file_sha256: 文件完整性校验
ALTER TABLE huichuan.knowledge_entries
  ADD COLUMN IF NOT EXISTS original_file_sha256 TEXT;

-- 回滚:
-- ALTER TABLE huichuan.knowledge_entries DROP COLUMN IF EXISTS entry_type;
-- ALTER TABLE huichuan.knowledge_entries DROP COLUMN IF EXISTS original_filename;
-- ALTER TABLE huichuan.knowledge_entries DROP COLUMN IF EXISTS original_storage_path;
-- ALTER TABLE huichuan.knowledge_entries DROP COLUMN IF EXISTS original_file_sha256;
-- 注意: metadata 列不回滚（可能已有数据）
