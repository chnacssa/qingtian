"""
镇岳 — 数据库 Schema 初始化
在 zhenyue schema 下创建所有核心表及索引
"""

import logging

from common.db import get_pool
from . import config as zcfg

logger = logging.getLogger("zhenyue.database")

SCHEMA = zcfg.get_schema_name()

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA {SCHEMA};

-- 1. sign_keys（审计签名用）
CREATE TABLE IF NOT EXISTS {SCHEMA}.sign_keys (
    id              INTEGER PRIMARY KEY,
    public_key      TEXT NOT NULL,
    algorithm       TEXT NOT NULL DEFAULT 'ed25519',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    -- review(2026-08-16): 审计签名恒为占位符 "0"*128，签名从未实现。补私钥列，
    -- 由 ensure_schema 生成真实 Ed25519 密钥对，write_audit 签名、verify 验签。
    private_key_enc TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_zt_sign_keys_active
    ON {SCHEMA}.sign_keys (status) WHERE status = 'active';

-- 预置 seed key（占位公钥；ensure_schema 检测无私钥时替换为真实密钥对）
INSERT INTO {SCHEMA}.sign_keys (id, public_key, algorithm, status)
VALUES (1, '34cf6cdf8adea8f368097dff19ddd51031a659152ed84e132e14b1d1749b8ded', 'ed25519', 'active')
ON CONFLICT (id) DO NOTHING;
-- 老库补列（建表语句不改存量表）
ALTER TABLE {SCHEMA}.sign_keys ADD COLUMN IF NOT EXISTS private_key_enc TEXT;

-- 2. audit_log（哈希链审计日志）
CREATE TABLE IF NOT EXISTS {SCHEMA}.audit_log (
    id              BIGSERIAL PRIMARY KEY,
    audit_uid       UUID NOT NULL DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent_id        VARCHAR(64) NOT NULL,
    agent_role      VARCHAR(16) NOT NULL,
    action          VARCHAR(64) NOT NULL,
    target_type     VARCHAR(32),
    target_id       VARCHAR(64),
    severity        VARCHAR(8) NOT NULL,
    detail_enc      TEXT,
    approval_status VARCHAR(16) DEFAULT 'auto',
    approval_chain  JSONB,
    prev_hash       VARCHAR(64) NOT NULL,
    hash            VARCHAR(64) NOT NULL,
    signature       VARCHAR(128) NOT NULL,
    sign_key_id     INTEGER NOT NULL REFERENCES {SCHEMA}.sign_keys(id)
);
CREATE INDEX IF NOT EXISTS idx_zt_audit_agent_time ON {SCHEMA}.audit_log (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_zt_audit_severity_time ON {SCHEMA}.audit_log (severity, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_zt_audit_action_time ON {SCHEMA}.audit_log (action, created_at DESC);

-- 3. tokens（Bearer Token 存储）
CREATE TABLE IF NOT EXISTS {SCHEMA}.tokens (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'agent',
    expires_at      TIMESTAMPTZ DEFAULT NULL,
    revoked         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_tokens_agent ON {SCHEMA}.tokens (agent_id);
CREATE INDEX IF NOT EXISTS idx_zt_tokens_hash ON {SCHEMA}.tokens (token_hash);

-- 4. agents（Agent 注册信息）
CREATE TABLE IF NOT EXISTS {SCHEMA}.agents (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT,
    trust_level     TEXT NOT NULL DEFAULT 'basic',
    status          TEXT NOT NULL DEFAULT 'inactive',
    capabilities    JSONB DEFAULT '{{}}',
    password_hash   TEXT,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by     TEXT,
    reviewed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_zt_agents_status ON {SCHEMA}.agents (status);

-- 5. agent_keys（Agent Ed25519 密钥对）
CREATE TABLE IF NOT EXISTS {SCHEMA}.agent_keys (
    key_id      SERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    private_key TEXT,
    algorithm   TEXT NOT NULL DEFAULT 'ed25519',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_zt_agent_keys_agent ON {SCHEMA}.agent_keys (agent_id);
CREATE INDEX IF NOT EXISTS idx_zt_agent_keys_active ON {SCHEMA}.agent_keys (agent_id)
    WHERE status = 'active';

-- 6. danger_rules（危险操作注册表）
CREATE TABLE IF NOT EXISTS {SCHEMA}.danger_rules (
    id                    SERIAL PRIMARY KEY,
    method                TEXT NOT NULL,
    path_pattern          TEXT NOT NULL,
    action_name           TEXT NOT NULL UNIQUE,
    severity              TEXT NOT NULL DEFAULT 'high',
    capabilities_required TEXT[] DEFAULT '{{}}',
    enabled               BOOLEAN DEFAULT true,
    description           TEXT DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_danger_rules_enabled ON {SCHEMA}.danger_rules (enabled);

-- 默认种子数据
INSERT INTO {SCHEMA}.danger_rules (method, path_pattern, action_name, severity, capabilities_required, description) VALUES
('DELETE', '/v1/huanyu/agents/{{agent_id}}', 'delete_agent', 'critical', '{{"admin"}}', '删除 Agent'),
('POST',   '/v1/huanyu/agents/{{agent_id}}/archive', 'archive_agent', 'high', '{{"admin","ops_admin"}}', '归档 Agent'),
('POST',   '/v1/zhenyue/agents/review', 'review_agent', 'high', '{{"admin","ops_admin"}}', '审核 Agent'),
('POST',   '/v1/huanyu/messages/batch-read', 'batch_mark_read', 'low', '{{}}', '批量标记已读'),
('DELETE', '/v1/huanyu/messages/*', 'delete_messages', 'high', '{{"admin"}}', '删除消息'),
('POST',   '/v1/zhenyue/emergency/break_glass', 'break_glass', 'critical', '{{"admin"}}', '应急破窗'),
('DELETE', '/v1/xixing/**', 'xixing_delete', 'critical', '{{"admin"}}', '删除吸星数据'),
('DELETE', '/v1/yongheng/**', 'yongheng_delete', 'critical', '{{"admin"}}', '删除永恒数据')
ON CONFLICT (action_name) DO NOTHING;

-- 7. approvals（审批门控）
CREATE TABLE IF NOT EXISTS {SCHEMA}.approvals (
    id              BIGSERIAL PRIMARY KEY,
    request_id      UUID NOT NULL DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    severity        TEXT NOT NULL DEFAULT 'high',
    status          TEXT NOT NULL DEFAULT 'pending',
    approver_chain  JSONB DEFAULT '[]',
    current_level   INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    executed_at     TIMESTAMPTZ,
    scheduled_execute_at TIMESTAMPTZ,
    resolution      TEXT,
    pending_request JSONB
);
CREATE INDEX IF NOT EXISTS idx_zt_approvals_status ON {SCHEMA}.approvals (status);
CREATE INDEX IF NOT EXISTS idx_zt_approvals_agent ON {SCHEMA}.approvals (agent_id);

-- 8. approval_requests（应用层审批工作流）
CREATE TABLE IF NOT EXISTS {SCHEMA}.approval_requests (
    approval_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_type  TEXT NOT NULL,
    requester_id  TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','cancelled')),
    reviewers     TEXT[] NOT NULL,
    decided_by    TEXT,
    decided_at    TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    metadata      JSONB DEFAULT '{{}}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_approval_req_status ON {SCHEMA}.approval_requests (status);
CREATE INDEX IF NOT EXISTS idx_zt_approval_req_requester ON {SCHEMA}.approval_requests (requester_id);

-- 9. quarantine（删除隔离区）
CREATE TABLE IF NOT EXISTS {SCHEMA}.quarantine (
    quarantine_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    source          TEXT NOT NULL,
    original_path   TEXT NOT NULL,
    quarantine_path TEXT NOT NULL,
    original_size   BIGINT DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'quarantined' CHECK (status IN ('quarantined','restored','purged')),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    restored_at     TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{{}}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_quarantine_agent ON {SCHEMA}.quarantine (agent_id);
CREATE INDEX IF NOT EXISTS idx_zt_quarantine_status ON {SCHEMA}.quarantine (status);
CREATE INDEX IF NOT EXISTS idx_zt_quarantine_expires ON {SCHEMA}.quarantine (expires_at)
    WHERE status = 'quarantined';

-- 10. guard_rules（守卫规则引擎）
CREATE TABLE IF NOT EXISTS {SCHEMA}.guard_rules (
    rule_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    description   TEXT,
    rule_type     TEXT NOT NULL CHECK (rule_type IN ('allow','deny','audit')),
    match_pattern TEXT NOT NULL,
    priority      INT NOT NULL DEFAULT 0,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_guard_rules_enabled ON {SCHEMA}.guard_rules (enabled);
CREATE INDEX IF NOT EXISTS idx_zt_guard_rules_priority ON {SCHEMA}.guard_rules (priority DESC);

-- 10. agent_reminders — 工作秘书提醒
CREATE TABLE IF NOT EXISTS {SCHEMA}.agent_reminders (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT DEFAULT '',
    remind_at   TEXT DEFAULT '',
    priority    TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent','critical')),
    type        TEXT NOT NULL DEFAULT 'task' CHECK (type IN ('task','deadline','followup','custom')),
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','delivered','done','snoozed','cancelled')),
    escalated   BOOLEAN NOT NULL DEFAULT FALSE,
    escalated_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_reminders_agent ON {SCHEMA}.agent_reminders (agent_id, status, remind_at);

-- Migration: 提醒升级链路（huanyu/api.py escalate：normal→urgent→critical→escalated）。
-- review(2026-08-25 大师实锤移植漏合): 代码引用 escalated/escalated_at 列与
-- urgent/critical/delivered 枚举，建表/迁移均未跟上——只补列不扩枚举的话，
-- UPDATE priority='urgent' 一旦命中行必违反 CHECK。存量约束删旧加新。
ALTER TABLE {SCHEMA}.agent_reminders ADD COLUMN IF NOT EXISTS escalated BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE {SCHEMA}.agent_reminders ADD COLUMN IF NOT EXISTS escalated_at TIMESTAMPTZ;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid = '{SCHEMA}.agent_reminders'::regclass
                 AND conname = 'agent_reminders_priority_check') THEN
        ALTER TABLE {SCHEMA}.agent_reminders DROP CONSTRAINT agent_reminders_priority_check;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid = '{SCHEMA}.agent_reminders'::regclass
                 AND conname = 'agent_reminders_status_check') THEN
        ALTER TABLE {SCHEMA}.agent_reminders DROP CONSTRAINT agent_reminders_status_check;
    END IF;
    ALTER TABLE {SCHEMA}.agent_reminders
        ADD CONSTRAINT agent_reminders_priority_check
        CHECK (priority IN ('low','normal','high','urgent','critical'));
    ALTER TABLE {SCHEMA}.agent_reminders
        ADD CONSTRAINT agent_reminders_status_check
        CHECK (status IN ('pending','delivered','done','snoozed','cancelled'));
END $$;
"""

TRIGGERS_SQL = f"""
-- 审计日志防篡改触发器
CREATE OR REPLACE FUNCTION {SCHEMA}.enforce_audit_integrity()
RETURNS TRIGGER AS $$
DECLARE
    active_count INTEGER;
    raw          TEXT;
    expected     TEXT;
    ts_iso       TEXT;
BEGIN
    SELECT COUNT(*) INTO active_count
    FROM {SCHEMA}.sign_keys WHERE id = NEW.sign_key_id AND status = 'active';

    IF active_count = 0 THEN
        RAISE EXCEPTION 'sign_key_id=% is not an active key', NEW.sign_key_id;
    END IF;

    ts_iso := to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
    raw := NEW.prev_hash || ':' || NEW.agent_id || ':' || NEW.action
        || ':' || ts_iso || ':' || COALESCE(NEW.detail_enc, '');
    expected := encode(sha256(raw::bytea), 'hex');

    IF NEW.hash != expected THEN
        RAISE EXCEPTION 'hash mismatch: expected=%, got=%', expected, NEW.hash;
    END IF;

    IF NEW.signature IS NULL OR length(NEW.signature) = 0 THEN
        RAISE EXCEPTION 'signature is required';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_integrity ON {SCHEMA}.audit_log;
CREATE TRIGGER trg_audit_integrity
    BEFORE INSERT ON {SCHEMA}.audit_log
    FOR EACH ROW
    EXECUTE FUNCTION {SCHEMA}.enforce_audit_integrity();

-- 触发器：禁止 audit_log UPDATE / DELETE
-- review(2026-08-16): 原实现无条件禁止，导致 cleanup_old_audit_logs 每日清理 DELETE
-- 恒抛异常（main.py 的 except:pass 吞掉）→ 保留期清理从不生效。改为仅当会话显式
-- 设置 app.audit_cleanup='true'（清理协程事务内 SET LOCAL）时放行，其余路径保持不可变。
CREATE OR REPLACE FUNCTION {SCHEMA}.block_audit_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('app.audit_cleanup', true) = 'true' THEN
        RETURN COALESCE(NEW, OLD);  -- UPDATE 返回 NEW，DELETE 返回 OLD → 放行
    END IF;
    RAISE EXCEPTION 'audit_log is immutable: updates and deletes are forbidden';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_no_delete ON {SCHEMA}.audit_log;
CREATE TRIGGER trg_audit_no_delete
    BEFORE DELETE ON {SCHEMA}.audit_log
    FOR EACH ROW
    EXECUTE FUNCTION {SCHEMA}.block_audit_mutation();

DROP TRIGGER IF EXISTS trg_audit_no_update ON {SCHEMA}.audit_log;
CREATE TRIGGER trg_audit_no_update
    BEFORE UPDATE ON {SCHEMA}.audit_log
    FOR EACH ROW
    EXECUTE FUNCTION {SCHEMA}.block_audit_mutation();
"""


async def ensure_schema():
    """确保所有 zhenyue 表、索引和触发器存在 + 存量迁移"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
        await conn.execute(TRIGGERS_SQL)
        # review(2026-08-16): 审计签名实现——seed key 无私钥时生成真实 Ed25519 密钥对，
        # 公钥 + 加密私钥落库，write_audit 据此签名（替代恒 "0"*128 占位符）。
        # 幂等：仅当 private_key_enc 为空时执行一次。
        from nacl.signing import SigningKey
        from .encryptor import encryptor
        existing_pk = await conn.fetchval(
            f"SELECT private_key_enc FROM {SCHEMA}.sign_keys WHERE id = 1"
        )
        if not existing_pk:
            sk = SigningKey.generate()
            pub = bytes(sk.verify_key).hex()
            enc_priv = encryptor.encrypt({"private_key": bytes(sk).hex()})
            await conn.execute(
                # fix(2026-09-03): SQL 只引用 $2/$3 却传 3 参 → "$1 无类型可推断"必抛错，
                # 干净库首启签名密钥对初始化静默失败（private_key_enc 恒空，审计签名链无密钥）
                f"UPDATE {SCHEMA}.sign_keys SET public_key = $2, private_key_enc = $3 "
                f"WHERE id = $1",
                1, pub, enc_priv,
            )
            logger.info("zhenyue audit sign keypair generated (id=1)")
        logger.info("zhenyue schema ensured: all tables and triggers created")
