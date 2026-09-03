-- License/订阅管理 DDL — 在管理服 database.py 中引用
-- 部署时自动执行（幂等）

-- billing.subscriptions 扩展字段
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS enterprise_name TEXT DEFAULT '';
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS module TEXT DEFAULT 'bidding';
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS amount NUMERIC(10,2) DEFAULT 0;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS start_date DATE;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS end_date DATE;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS invoice_needed BOOLEAN DEFAULT false;
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS remark TEXT DEFAULT '';
ALTER TABLE billing.subscriptions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- 唯一约束：同一企业 + 同一模块只保留一条订阅记录
ALTER TABLE billing.subscriptions DROP CONSTRAINT IF EXISTS uq_sub_ent_module;
ALTER TABLE billing.subscriptions ADD CONSTRAINT uq_sub_ent_module UNIQUE (enterprise_id, module);

-- 发票表
CREATE TABLE IF NOT EXISTS billing.invoices (
    id          SERIAL PRIMARY KEY,
    enterprise_id TEXT NOT NULL,
    invoice_no  TEXT NOT NULL UNIQUE,
    amount      NUMERIC(10,2) NOT NULL DEFAULT 0,
    module      TEXT NOT NULL DEFAULT 'bidding',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / issued / cancelled
    issued_at   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
