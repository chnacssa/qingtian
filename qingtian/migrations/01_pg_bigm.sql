-- 汇川 v2.4 Phase 0: 中文全文搜索
-- pg_bigm (2-gram) 扩展 + GIN 索引
-- 回滚: 见文件末尾注释

-- 1. 安装 pg_bigm 扩展
CREATE EXTENSION IF NOT EXISTS pg_bigm;

-- 2. 创建 2-gram 索引（大小写不敏感）
-- content 字段 bigm 索引
CREATE INDEX IF NOT EXISTS idx_ke_content_bigm
  ON huichuan.knowledge_entries USING gin (content gin_bigm_ops);

-- title 字段 bigm 索引
CREATE INDEX IF NOT EXISTS idx_ke_title_bigm
  ON huichuan.knowledge_entries USING gin (title gin_bigm_ops);

-- 回滚:
-- DROP INDEX IF EXISTS huichuan.idx_ke_content_bigm;
-- DROP INDEX IF EXISTS huichuan.idx_ke_title_bigm;
-- DROP EXTENSION IF EXISTS pg_bigm;
