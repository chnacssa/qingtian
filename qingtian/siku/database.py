"""
司库 — 数据库 Schema 初始化
在 siku schema 下创建 5 张核心表 + 触发器 + 索引
"""

from common.db import get_pool
from . import config as cfg

SCHEMA = cfg.get_schema_name()

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

-- 1. accounts — 账户余额
CREATE TABLE IF NOT EXISTS {SCHEMA}.accounts (
    account_id      BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL UNIQUE,
    balance_fen     BIGINT NOT NULL DEFAULT 0 CHECK (balance_fen >= 0),
    frozen_fen      BIGINT NOT NULL DEFAULT 0 CHECK (frozen_fen >= 0),
    total_recharged BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_accounts_agent ON {SCHEMA}.accounts (agent_id);

-- 2. transactions — 交易流水（只追加）
CREATE TABLE IF NOT EXISTS {SCHEMA}.transactions (
    txn_id          BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    txn_type        TEXT NOT NULL CHECK (txn_type IN ('recharge','deduct','admin_adjust')),
    amount_fen      BIGINT NOT NULL,
    balance_after   BIGINT NOT NULL,
    fee_type        TEXT NOT NULL DEFAULT '' CHECK (fee_type IN ('','cert_upgrade','annual_fee')),
    reference_id    TEXT DEFAULT '',
    idempotency_key TEXT,
    detail          JSONB DEFAULT '{{}}',
    prev_hash       TEXT NOT NULL DEFAULT '',
    row_hash        TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cs_txn_idempotency
    ON {SCHEMA}.transactions (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cs_txn_agent ON {SCHEMA}.transactions (agent_id, created_at DESC);

-- 只追加保护：禁止 UPDATE / DELETE
CREATE OR REPLACE FUNCTION {SCHEMA}.block_txn_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '{SCHEMA}.transactions 只追加，禁止 UPDATE/DELETE';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cs_txn_no_mutate'
    ) THEN
        CREATE TRIGGER trg_cs_txn_no_mutate
            BEFORE UPDATE OR DELETE ON {SCHEMA}.transactions
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.block_txn_mutation();
    END IF;
END $$;

-- 3. annual_fee_status — 年费状态
CREATE TABLE IF NOT EXISTS {SCHEMA}.annual_fee_status (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL UNIQUE,
    free_months     INTEGER NOT NULL DEFAULT 12 CHECK (free_months IN (6, 12)),
    first_paid_at   TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ NOT NULL,
    is_expired      BOOLEAN NOT NULL DEFAULT false,
    expired_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- 注意：部分索引 WHERE 子句中 NOW() 是 STABLE 非 IMMUTABLE，PostgreSQL 拒绝建表。
-- 改为普通索引，去掉了时间谓词。
CREATE INDEX IF NOT EXISTS idx_cs_annual_expires
    ON {SCHEMA}.annual_fee_status (expires_at) WHERE is_expired = false;

-- 4. invoices — 发票管理
CREATE TABLE IF NOT EXISTS {SCHEMA}.invoices (
    invoice_id      BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    invoice_type    TEXT NOT NULL DEFAULT 'electronic' CHECK (invoice_type IN ('electronic', 'paper')),
    title           TEXT NOT NULL,
    tax_number      TEXT NOT NULL DEFAULT '',
    amount_fen      BIGINT NOT NULL,
    related_txn_ids TEXT[] DEFAULT '{{}}',
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','issued','rejected','voided')),
    file_url        TEXT DEFAULT '',
    file_hash       TEXT DEFAULT '',
    issuer          TEXT DEFAULT '',
    issued_at       TIMESTAMPTZ,
    reject_reason   TEXT DEFAULT '',
    remark          TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_invoice_agent ON {SCHEMA}.invoices (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_invoice_status ON {SCHEMA}.invoices (status, created_at);

-- 5. admin_operations — 管理员操作记录
CREATE TABLE IF NOT EXISTS {SCHEMA}.admin_operations (
    op_id           BIGSERIAL PRIMARY KEY,
    operator_id     TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    amount_fen      BIGINT DEFAULT 0,
    txn_id          BIGINT,
    detail          JSONB DEFAULT '{{}}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_admin_ops_time ON {SCHEMA}.admin_operations (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_admin_ops_target ON {SCHEMA}.admin_operations (target_agent_id, created_at DESC);

-- 6. finance_audit — 财务审计日志（只追加，哈希链，不可篡改）
CREATE TABLE IF NOT EXISTS {SCHEMA}.finance_audit (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    action          TEXT NOT NULL,
    event_type      TEXT NOT NULL DEFAULT '',
    target_id       TEXT DEFAULT '',
    amount_fen      BIGINT DEFAULT 0,
    severity        TEXT NOT NULL DEFAULT 'info',
    detail          TEXT DEFAULT '{{}}',
    prev_hash       TEXT NOT NULL,
    row_hash        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_fa_agent_time ON {SCHEMA}.finance_audit (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_fa_action ON {SCHEMA}.finance_audit (action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_fa_event ON {SCHEMA}.finance_audit (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_fa_severity ON {SCHEMA}.finance_audit (severity, created_at DESC);

-- 7. pending_recharges — 待人审充值单（P0，9-1 修复日）
--    bank_verify=manual（默认）时 payment_notify 不自动入账，改入本队列，
--    IM 人审 "通过 {{message_id}}" 后才执行 recharge（幂等键同 Path B）。
CREATE TABLE IF NOT EXISTS {SCHEMA}.pending_recharges (
    pending_id      BIGSERIAL PRIMARY KEY,
    message_id      TEXT NOT NULL UNIQUE,          -- 幂等：一条 notify 至多一张待审单
    company_name    TEXT NOT NULL,
    payer_agent_id  TEXT NOT NULL,
    amount_fen      BIGINT NOT NULL CHECK (amount_fen > 0),
    payment_channel TEXT NOT NULL DEFAULT '',
    voucher_number  TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','approved','rejected')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT NOT NULL DEFAULT ''       -- IM 人审 user_id
);
CREATE INDEX IF NOT EXISTS idx_cs_pend_status ON {SCHEMA}.pending_recharges (status, created_at);
"""

AUDIT_TRIGGERS_SQL = f"""
-- finance_audit 哈希链验证触发器：插入时强制 prev_hash 链接 + row_hash 一致性
CREATE OR REPLACE FUNCTION {SCHEMA}.enforce_finance_audit_hash()
RETURNS TRIGGER AS $$
DECLARE
    expected TEXT;
    detail_json TEXT;
BEGIN
    detail_json := COALESCE(NEW.detail, '{{}}');
    expected := encode(sha256(
        (NEW.prev_hash || ':' || NEW.agent_id || ':' || NEW.action || ':'
         || NEW.event_type || ':'
         || to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
         || ':' || detail_json)::bytea
    ), 'hex');

    IF NEW.row_hash != expected THEN
        RAISE EXCEPTION 'finance_audit hash mismatch: expected=%, got=%', expected, NEW.row_hash;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_finance_audit_hash'
    ) THEN
        CREATE TRIGGER trg_finance_audit_hash
            BEFORE INSERT ON {SCHEMA}.finance_audit
            FOR EACH ROW
            EXECUTE FUNCTION {SCHEMA}.enforce_finance_audit_hash();
    END IF;
END $$;

-- finance_audit 禁止 UPDATE/DELETE
CREATE OR REPLACE FUNCTION {SCHEMA}.block_finance_audit_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '{SCHEMA}.finance_audit 只追加，禁止 UPDATE/DELETE';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_finance_audit_no_mutate'
    ) THEN
        CREATE TRIGGER trg_finance_audit_no_mutate
            BEFORE UPDATE OR DELETE ON {SCHEMA}.finance_audit
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.block_finance_audit_mutation();
    END IF;
END $$;
"""


async def ensure_schema():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
        await conn.execute(AUDIT_TRIGGERS_SQL)
