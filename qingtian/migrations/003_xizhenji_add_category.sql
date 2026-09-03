-- Migration 003: xizhenji 表新增 category 字段 + 存量数据迁移
-- Issue 9: xizhenji 表缺少分类字段，无法按问题类型统计和过滤
-- 值域: agent_report / zhenyue_block / auto_capture / performance / security / config / manual

-- Step 1: 新增列（幂等）
ALTER TABLE xixing.xizhenji ADD COLUMN IF NOT EXISTS category TEXT DEFAULT '';

-- Step 2: 存量数据按 source 映射 category
-- source='agent-report'  → agent_report (Agent 主动汇报)
-- source='audit-log'     → zhenyue_block (镇岳 tool:exec 拦截产生)
-- source='auto-capture'  → auto_capture (异常自动捕获)
-- source='manual'        → manual (手动录入)
UPDATE xixing.xizhenji SET category = 'agent_report'  WHERE source = 'agent-report' AND category = '';
UPDATE xixing.xizhenji SET category = 'zhenyue_block' WHERE source = 'audit-log'     AND category = '';
UPDATE xixing.xizhenji SET category = 'auto_capture'  WHERE source = 'auto-capture'  AND category = '';
UPDATE xixing.xizhenji SET category = 'manual'        WHERE source = 'manual'        AND category = '';

-- Step 3: 索引（幂等）
CREATE INDEX IF NOT EXISTS idx_xz_category ON xixing.xizhenji (category);

-- 验证
-- SELECT category, count(*) FROM xixing.xizhenji GROUP BY category ORDER BY count(*) DESC;
