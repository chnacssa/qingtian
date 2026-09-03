"""
寰宇 — 数据库 Schema 初始化
在 huanyu schema 下创建 5 张核心表及索引
"""

from common.db import get_pool
from . import config as hcfg
import logging

logger = logging.getLogger("huanyu.database")

SCHEMA = hcfg.get_schema_name()

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

-- 1. agents
-- agent_id: TEXT (接受 UUID 字符串或可读标识，如 'agent-buyer-001')
CREATE TABLE IF NOT EXISTS {SCHEMA}.agents (
    agent_id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    ain               TEXT UNIQUE,
    public_key        TEXT DEFAULT '',
    cert_fingerprint  TEXT DEFAULT '',
    name              TEXT NOT NULL,
    category          TEXT NOT NULL CHECK (category IN ('biz:buyer','biz:seller','biz:broker','biz:inspector','infra:scheduler','infra:monitor','infra:resolver','infra:notifier','infra:gateway','infra:archive','infra:finance','sys:admin','sys:root','sys:observer','sys:bridge')),
    subcategory       TEXT DEFAULT '',
    capabilities      JSONB DEFAULT '[]',
    password_hash     TEXT,
    contact_info      TEXT DEFAULT '',
    server_host       TEXT NOT NULL DEFAULT '',
    server_ip         TEXT DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','suspended','deleted')),
    trust_level       TEXT NOT NULL DEFAULT 'basic',
    industry          TEXT DEFAULT '',
    c_level           TEXT NOT NULL DEFAULT 'C0' CHECK (c_level IN ('C0','C1','C2','C3')),
    scale             TEXT DEFAULT '',
    uscc              TEXT DEFAULT '',
    company_name      TEXT DEFAULT '',
    -- GB/Z 185.4 新增字段
    provider          TEXT DEFAULT '',
    default_input_types  JSONB DEFAULT '["text"]',
    default_output_types JSONB DEFAULT '["text"]',
    alias             TEXT DEFAULT '',
    icon_address      TEXT DEFAULT '',
    serving_area      TEXT DEFAULT '',
    access_method     TEXT DEFAULT 'api',
    metadata          JSONB DEFAULT '{{}}',
    last_heartbeat    TIMESTAMPTZ DEFAULT NOW(),
    heartbeat_interval INTERVAL DEFAULT '300 seconds',
    deleted_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMPTZ,
    UNIQUE (name, server_host)
);
CREATE INDEX IF NOT EXISTS idx_agents_category ON {SCHEMA}.agents (category);
CREATE INDEX IF NOT EXISTS idx_agents_status ON {SCHEMA}.agents (status);
CREATE INDEX IF NOT EXISTS idx_agents_host ON {SCHEMA}.agents (server_host);
CREATE INDEX IF NOT EXISTS idx_agents_name ON {SCHEMA}.agents (name);
CREATE INDEX IF NOT EXISTS idx_agents_industry ON {SCHEMA}.agents (industry);
CREATE INDEX IF NOT EXISTS idx_agents_c_level ON {SCHEMA}.agents (c_level);

-- Migration: add category column for existing tables
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'biz:buyer'
    CHECK (category IN ('biz:buyer','biz:seller','biz:broker','biz:inspector','infra:scheduler','infra:monitor','infra:resolver','infra:notifier','infra:gateway','infra:archive','infra:finance','sys:admin','sys:root','sys:observer','sys:bridge'));

-- Migration: add agent_id column for existing tables (旧表主键是 ain，缺 agent_id)
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS agent_id TEXT;
UPDATE {SCHEMA}.agents SET agent_id = gen_random_uuid()::text WHERE agent_id IS NULL;
ALTER TABLE {SCHEMA}.agents ALTER COLUMN agent_id SET NOT NULL;

-- Migration: add remaining columns for existing tables
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS public_key TEXT DEFAULT '';
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS cert_fingerprint TEXT DEFAULT '';
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS subcategory TEXT DEFAULT '';
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS contact_info TEXT DEFAULT '';
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS uscc TEXT DEFAULT '';
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS company_name TEXT DEFAULT '';
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS scale TEXT DEFAULT '';

-- Migration: add server_ip column (跨底座精准回发地址, 方案甲显式配置; server_host 保留主机名供故障定位)
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS server_ip TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_agents_ip ON {SCHEMA}.agents (server_ip);

-- Migration: 存量迁移 organization_id 列（跨企业通讯：agent 归属企业码）
-- review(2026-08-25): peers.py push local agents SELECT organization_id，开源版此前
-- 只在 manager 副本有此迁移——P0 移植漏合，采购服上报方向报缺列。与 manager 版对齐。
ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS organization_id TEXT;

-- Migration: add UNIQUE (name, server_host) constraint
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = '{SCHEMA}.agents'::regclass
        AND conname = 'agents_name_server_host_key'
    ) THEN
        ALTER TABLE {SCHEMA}.agents ADD CONSTRAINT agents_name_server_host_key UNIQUE (name, server_host);
    END IF;
END $$;

-- 1a2. agent_channel_bindings — 通道身份 ↔ 规范 agent 名 归一映射（X 模型落地）
-- 飞书消息 from.open_id 是通道身份，文件 owner 存 OpenClaw agent 名；同一实体两个命名空间。
-- 由账号绑定流程动态维护（禁止硬编码 open_id 进仓库），插件 resolve 未命中 alias 时查本表。
CREATE TABLE IF NOT EXISTS {SCHEMA}.agent_channel_bindings (
    agent_id   TEXT NOT NULL,
    channel    TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (channel, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_bindings_agent ON {SCHEMA}.agent_channel_bindings (agent_id);

-- 1b. skills — GB/Z 185.4 表2
CREATE TABLE IF NOT EXISTS {SCHEMA}.skills (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    skill_id        TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    tags            JSONB DEFAULT '[]',
    examples        JSONB DEFAULT '[]',
    input_types     JSONB DEFAULT '["text"]',
    output_types    JSONB DEFAULT '["text"]',
    dependencies    JSONB DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, skill_id)
);
CREATE INDEX IF NOT EXISTS idx_skills_agent ON {SCHEMA}.skills (agent_id);

-- 1c. gbz185_mappings — AIN ↔ GB/Z 185.2 身份码映射（一对多）
CREATE TABLE IF NOT EXISTS {SCHEMA}.gbz185_mappings (
    ain             TEXT NOT NULL,
    gbz185_id       TEXT NOT NULL,
    issuer          TEXT NOT NULL DEFAULT '',
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    PRIMARY KEY (ain, gbz185_id)
);

-- 2. messages
-- from_agent_id / to_agent_id: TEXT (接受 UUID 或可读标识)，无 FK（跨底座互信）
CREATE TABLE IF NOT EXISTS {SCHEMA}.messages (
    message_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent_id    TEXT NOT NULL,
    to_agent_id      TEXT,
    message_type     TEXT NOT NULL CHECK (message_type IN ('inquiry','quote','counter','accept','reject','clarify','cancel','info','payment_notify','payment_confirm','counter_response','file','image','structured_data','overload_l4','overload_l5','deal_closed','fulfillment_ask','notification')),
    payload          JSONB DEFAULT '{{}}',
    negotiation_id   VARCHAR(64),
    reply_to         UUID,
    -- GB/Z 185.6 表3 新增字段
    sender_role      TEXT DEFAULT 'requester' CHECK (sender_role IN ('requester','service')),
    task_id          UUID,
    artifact         TEXT DEFAULT 'work_communication' CHECK (artifact IN ('work_communication','work_product')),
    final_flag       BOOLEAN DEFAULT FALSE,
    chunk_index      INT,
    last_chunk       BOOLEAN DEFAULT FALSE,
    priority         TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high','urgent')),
    status           TEXT NOT NULL DEFAULT 'unread' CHECK (status IN ('unread','read','archived')),
    delivery_status  TEXT NOT NULL DEFAULT 'local' CHECK (delivery_status IN ('local','pending','sent','hub_acked','delivered','failed','dead','cross_org')),
    idempotency_key  TEXT,
    signature        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_at          TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency ON {SCHEMA}.messages (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_from ON {SCHEMA}.messages (from_agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_to ON {SCHEMA}.messages (to_agent_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_negotiation ON {SCHEMA}.messages (negotiation_id);
CREATE INDEX IF NOT EXISTS idx_messages_delivery ON {SCHEMA}.messages (delivery_status) WHERE delivery_status = 'pending';

-- 3. negotiations
-- buyer_id / supplier_id: TEXT，无 FK（跨底座互信）
CREATE TABLE IF NOT EXISTS {SCHEMA}.negotiations (
    negotiation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id         TEXT NOT NULL,
    supplier_id      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','accepted','rejected','cancelled','expired','counter_proposed')),
    product_category TEXT DEFAULT '',
    initial_inquiry  JSONB DEFAULT '{{}}',
    latest_offer     JSONB DEFAULT '{{}}',
    counter_count    INTEGER DEFAULT 0,
    max_counters     INTEGER DEFAULT 5,
    last_activity_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at       TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '7 days'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_negotiations_buyer ON {SCHEMA}.negotiations (buyer_id, status);
CREATE INDEX IF NOT EXISTS idx_negotiations_supplier ON {SCHEMA}.negotiations (supplier_id, status);
CREATE INDEX IF NOT EXISTS idx_negotiations_expires ON {SCHEMA}.negotiations (expires_at) WHERE status = 'active';

-- 4. agreements
-- buyer_id / supplier_id: TEXT，无 FK（跨底座互信）
CREATE TABLE IF NOT EXISTS {SCHEMA}.agreements (
    agreement_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    negotiation_id UUID NOT NULL REFERENCES {SCHEMA}.negotiations(negotiation_id),
    buyer_id       TEXT NOT NULL,
    supplier_id    TEXT NOT NULL,
    product        TEXT NOT NULL,
    quantity       TEXT NOT NULL,
    unit_price     TEXT NOT NULL,
    total_price    TEXT NOT NULL,
    terms          JSONB DEFAULT '{{}}',
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','signed','completed','cancelled','disputed')),
    buyer_finance_ain TEXT DEFAULT '',
    seller_finance_ain TEXT DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Migration: add finance_ain columns for multi-server accounting
ALTER TABLE {SCHEMA}.agreements ADD COLUMN IF NOT EXISTS buyer_finance_ain TEXT DEFAULT '';
ALTER TABLE {SCHEMA}.agreements ADD COLUMN IF NOT EXISTS seller_finance_ain TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_agreements_buyer ON {SCHEMA}.agreements (buyer_id);
CREATE INDEX IF NOT EXISTS idx_agreements_supplier ON {SCHEMA}.agreements (supplier_id);

-- 5. ratings
-- from_agent / to_agent: TEXT，无 FK（跨底座互信）
-- agreement_id: TEXT（2026-08-09 由 UUID 改为 TEXT）——协议评分传 huanyu.agreements 的 uuid 文本，
--   履约链评分传 e3_xxx 文本（无对应 agreements 行，原 uuid 列 + FK 导致写入失败）
CREATE TABLE IF NOT EXISTS {SCHEMA}.ratings (
    rating_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent   TEXT NOT NULL,
    to_agent     TEXT NOT NULL,
    agreement_id TEXT,
    score        NUMERIC(3,1) NOT NULL CHECK (score BETWEEN 1.0 AND 5.0),
    dimensions   JSONB DEFAULT '{{}}',
    comment      TEXT DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ratings_agent ON {SCHEMA}.ratings (to_agent);

-- 6. purchase_orders (PO)
CREATE TABLE IF NOT EXISTS {SCHEMA}.purchase_orders (
    po_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agreement_id   UUID NOT NULL REFERENCES {SCHEMA}.agreements(agreement_id),
    buyer_id       TEXT NOT NULL,
    supplier_id    TEXT NOT NULL,
    product        TEXT NOT NULL,
    quantity       TEXT NOT NULL,
    unit_price     TEXT NOT NULL,
    total_price    TEXT NOT NULL,
    delivery_date  TEXT DEFAULT '',
    payment_terms  TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','issued','confirmed','fulfilled','cancelled')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_po_buyer ON {SCHEMA}.purchase_orders (buyer_id);
CREATE INDEX IF NOT EXISTS idx_po_supplier ON {SCHEMA}.purchase_orders (supplier_id);

-- Migration: ratings score from INTEGER to NUMERIC for float support
-- Drop dependent view before altering column type
DROP VIEW IF EXISTS {SCHEMA}.agent_rating_summary CASCADE;
ALTER TABLE {SCHEMA}.ratings ALTER COLUMN score TYPE NUMERIC(3,1);

-- 8. topic_subscriptions
CREATE TABLE IF NOT EXISTS {SCHEMA}.topic_subscriptions (
    id        BIGSERIAL PRIMARY KEY,
    agent_id  TEXT NOT NULL,
    topic     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_topic_sub_topic ON {SCHEMA}.topic_subscriptions (topic);

-- 9. peers (跨底座注册)
CREATE TABLE IF NOT EXISTS {SCHEMA}.peers (
    peer_id        TEXT PRIMARY KEY,
    host           TEXT NOT NULL,
    port           INTEGER NOT NULL DEFAULT 1996,
    name           TEXT NOT NULL,
    public_key     TEXT DEFAULT '',
    endpoint       TEXT DEFAULT '',
    agents_count   INTEGER DEFAULT 0,
    last_heartbeat TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','offline')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (host, port)
);
CREATE INDEX IF NOT EXISTS idx_peers_status ON {SCHEMA}.peers (status);

-- 9. cert_revocations (QACP 证书吊销列表)
CREATE TABLE IF NOT EXISTS {SCHEMA}.cert_revocations (
    cert_fingerprint TEXT PRIMARY KEY,
    revoked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason           TEXT DEFAULT ''
);

-- 10. agent_processes (Agent Runtime Manager 进程表)
CREATE TABLE IF NOT EXISTS {SCHEMA}.agent_processes (
    agent_id      TEXT PRIMARY KEY REFERENCES {SCHEMA}.agents(agent_id),
    pid           INTEGER,
    status        TEXT NOT NULL DEFAULT 'stopped'
                  CHECK (status IN ('running','stopped','crashed','restarting','starting','unhealthy','fatal','paused')),
    started_at    TIMESTAMPTZ,
    stopped_at    TIMESTAMPTZ,
    restart_count INTEGER DEFAULT 0,
    last_error    TEXT DEFAULT '',
    config_json   JSONB DEFAULT '{{}}'
);

-- 11. bus_states (总线状态表 — BusScheduler 核心)
CREATE TABLE IF NOT EXISTS {SCHEMA}.bus_states (
    agent_id       TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (state IN ('unknown','registered','adopted','ready','paused','stopped')),
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata       JSONB DEFAULT '{{}}'
);

-- 8. audit_log (镇岳审计 — 蓝图 + 镇岳对齐)
CREATE TABLE IF NOT EXISTS {SCHEMA}.audit_log (
    log_id       BIGSERIAL PRIMARY KEY,
    actor_id     TEXT NOT NULL,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL DEFAULT '',
    target_id    TEXT NOT NULL DEFAULT '',
    detail       JSONB DEFAULT '{{}}',
    result       TEXT NOT NULL DEFAULT 'success' CHECK (result IN ('success','failure','pending','denied')),
    source_ip    TEXT DEFAULT '',              -- 请求来源 IP（镇岳填充）
    source_host  TEXT DEFAULT '',              -- 发起底座 hostname（蓝图字段）
    signature    TEXT DEFAULT '',              -- 镇岳对该行的 HMAC 签名
    sign_key_id  TEXT DEFAULT '',              -- 签名使用的密钥 ID
    prev_hash    TEXT DEFAULT '',              -- 哈希链：上一条日志的 SHA256
    row_hash     TEXT NOT NULL DEFAULT '',     -- 本行哈希：SHA256(prev_hash + 本行内容)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- 审计日志不允许 DELETE/UPDATE，仅 INSERT + SELECT
CREATE INDEX IF NOT EXISTS idx_audit_actor ON {SCHEMA}.audit_log (actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON {SCHEMA}.audit_log (action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON {SCHEMA}.audit_log (target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_audit_hash ON {SCHEMA}.audit_log (row_hash);
-- 防篡改验证：每行的 row_hash = SHA256(prev_hash || actor_id || action || target_type || target_id || result || created_at)
-- 镇岳附加签名：signature = HMAC(sign_key_id, row_hash)，用于跨底座审计校验

-- 综合评分视图
-- 先 DROP 再 CREATE，避免 CREATE OR REPLACE 列名变更时报错
-- （PostgreSQL 不允许 CREATE OR REPLACE VIEW 改变列名/列序）
DROP VIEW IF EXISTS {SCHEMA}.agent_rating_summary CASCADE;
CREATE VIEW {SCHEMA}.agent_rating_summary AS
SELECT
    to_agent AS agent_id,
    ROUND(AVG(score)::numeric, 1) AS avg_score,
    COUNT(*) AS total_ratings,
    COUNT(DISTINCT from_agent) AS unique_raters
FROM {SCHEMA}.ratings
GROUP BY to_agent;
"""


async def ensure_schema():
    """确保所有 huanyu 表和索引存在 + 存量迁移"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
        # 存量迁移：补镇岳需要的审核字段
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS reviewed_by TEXT"
            )
        except Exception:
            pass
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ"
            )
        except Exception:
            pass
        # 存量迁移：password_hash 列（Web 登录用）
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS password_hash TEXT"
            )
        except Exception:
            pass
        # Agent 心跳时间戳（供 full_snapshot 比对时区分"刚注册/有心跳"与"真离线"）
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.agents ADD COLUMN IF NOT EXISTS last_heartbeat TIMESTAMPTZ"
            )
        except Exception:
            pass
        # 存量迁移：messages 表 message_type CHECK 支持 file/image/structured_data
        # + C10/C11 (R11): overload_l4/overload_l5（L4/L5 过载告警）、deal_closed/fulfillment_ask（成交/履约）
        # + C14 (R11): notification（notifier 推送）——bus._notify_emergency 与 notifier_agent 消费端使用
        # 模型层已定义这些类型，DB 约束滞后修复
        # review(2026-08-24 P0-7): 补 fulfillment_ask/deal_closed——orders_cron 发、
        # inbox_scanner/_ROUTES 消费这两种类型，缺了新库 INSERT 直接 23514
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.messages DROP CONSTRAINT IF EXISTS messages_message_type_check"
            )
            await conn.execute(
                f"""ALTER TABLE {SCHEMA}.messages ADD CONSTRAINT messages_message_type_check
                    CHECK (message_type IN ('inquiry','quote','counter','accept','reject','clarify',
                           'cancel','info','payment_notify','payment_confirm','file','image',
                           'structured_data','counter_response','overload_l4','overload_l5',
                           'deal_closed','fulfillment_ask','notification'))"""
            )
            logger.info("huanyu schema: messages.message_type CHECK extended (file/image/structured_data + overload/deal/fulfillment + notification)")
        except Exception:
            pass
        # 存量迁移：messages.delivery_status 支持 cross_org（跨企业通讯接线，P0-1）
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.messages DROP CONSTRAINT IF EXISTS messages_delivery_status_check"
            )
            await conn.execute(
                f"""ALTER TABLE {SCHEMA}.messages ADD CONSTRAINT messages_delivery_status_check
                    CHECK (delivery_status IN ('local','pending','sent','hub_acked','delivered','failed','dead','cross_org'))"""
            )
            logger.info("huanyu schema: messages.delivery_status CHECK extended with cross_org")
        except Exception:
            pass
        # 存量迁移：messages.retry_count（可靠投递失败重试计数，P0）
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.messages ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0"
            )
        except Exception:
            pass
        # 存量迁移：ratings.agreement_id uuid→TEXT（2026-08-09）
        # 履约链 agreement_id 为 e3_xxx 文本，原 uuid 列 + FK(agreements) 导致 submit_rating 写入失败；
        # 改 TEXT 后协议评分（uuid 文本）与履约评分（e3_xxx）同时兼容。
        # 注意：PG 不会在 ALTER COLUMN TYPE 时自动删 FK（text vs uuid 冲突报错），须显式先 DROP CONSTRAINT（大师 2026-08-09 指正）。
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.ratings DROP CONSTRAINT IF EXISTS ratings_agreement_id_fkey"
            )
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.ratings ALTER COLUMN agreement_id TYPE TEXT USING agreement_id::text"
            )
            logger.info("huanyu schema: ratings.agreement_id converted uuid→TEXT")
        except Exception:
            logger.exception("huanyu schema: ratings.agreement_id migration failed")
        # ARM 进程表存量迁移
        try:
            await conn.execute(f"""
                ALTER TABLE {SCHEMA}.agent_processes
                ALTER COLUMN status SET DEFAULT 'stopped'
            """)
        except Exception:
            pass

        # ARM 进程表 — status CHECK 约束扩容
        try:
            await conn.execute(f"""
                ALTER TABLE {SCHEMA}.agent_processes
                DROP CONSTRAINT IF EXISTS agent_processes_status_check
            """)
            await conn.execute(f"""
                ALTER TABLE {SCHEMA}.agent_processes
                ADD CONSTRAINT agent_processes_status_check
                CHECK (status IN ('running','stopped','crashed','restarting',
                                  'starting','unhealthy','fatal','paused'))
            """)
            logger.info("huanyu schema: agent_processes.status CHECK expanded")
        except Exception:
            pass

        # GB/Z 185 会话管理 — conversations 表
        try:
            from .conversations import ensure_schema as conv_schema
            await conv_schema()
            logger.info("huanyu schema: conversations table ready")
        except Exception:
            logger.exception("huanyu schema: conversations ensure_schema failed")
