-- 擎天底座 init.sql — auto-generated
-- 模块: huanyu/yongheng/huichuan/zhice/zhenyue/xixing/siku/osskill/license


CREATE SCHEMA IF NOT EXISTS huanyu;
CREATE SCHEMA IF NOT EXISTS billing;

-- 1. agents
-- agent_id: TEXT (接受 UUID 字符串或可读标识，如 'agent-buyer-001')
CREATE TABLE IF NOT EXISTS huanyu.agents (
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
    metadata          JSONB DEFAULT '{}',
    last_heartbeat    TIMESTAMPTZ DEFAULT NOW(),
    heartbeat_interval INTERVAL DEFAULT '300 seconds',
    deleted_at        TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMPTZ,
    UNIQUE (name, server_host)
);
CREATE INDEX IF NOT EXISTS idx_agents_category ON huanyu.agents (category);
CREATE INDEX IF NOT EXISTS idx_agents_status ON huanyu.agents (status);
CREATE INDEX IF NOT EXISTS idx_agents_host ON huanyu.agents (server_host);
CREATE INDEX IF NOT EXISTS idx_agents_name ON huanyu.agents (name);
CREATE INDEX IF NOT EXISTS idx_agents_industry ON huanyu.agents (industry);
CREATE INDEX IF NOT EXISTS idx_agents_c_level ON huanyu.agents (c_level);

-- Migration: add category column for existing tables
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'biz:buyer'
    CHECK (category IN ('biz:buyer','biz:seller','biz:broker','biz:inspector','infra:scheduler','infra:monitor','infra:resolver','infra:notifier','infra:gateway','infra:archive','infra:finance','sys:admin','sys:root','sys:observer','sys:bridge'));

-- Migration: add agent_id column for existing tables (旧表主键是 ain，缺 agent_id)
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS agent_id TEXT;
UPDATE huanyu.agents SET agent_id = gen_random_uuid()::text WHERE agent_id IS NULL;
ALTER TABLE huanyu.agents ALTER COLUMN agent_id SET NOT NULL;

-- Migration: add remaining columns for existing tables
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS public_key TEXT DEFAULT '';
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS cert_fingerprint TEXT DEFAULT '';
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS subcategory TEXT DEFAULT '';
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS contact_info TEXT DEFAULT '';
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS uscc TEXT DEFAULT '';
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS company_name TEXT DEFAULT '';
ALTER TABLE huanyu.agents ADD COLUMN IF NOT EXISTS scale TEXT DEFAULT '';

-- Migration: add UNIQUE (name, server_host) constraint
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'huanyu.agents'::regclass
        AND conname = 'agents_name_server_host_key'
    ) THEN
        ALTER TABLE huanyu.agents ADD CONSTRAINT agents_name_server_host_key UNIQUE (name, server_host);
    END IF;
END $$;

-- 1b. skills — GB/Z 185.4 表2
CREATE TABLE IF NOT EXISTS huanyu.skills (
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
CREATE INDEX IF NOT EXISTS idx_skills_agent ON huanyu.skills (agent_id);

-- 1c. gbz185_mappings — AIN ↔ GB/Z 185.2 身份码映射（一对多）
CREATE TABLE IF NOT EXISTS huanyu.gbz185_mappings (
    ain             TEXT NOT NULL,
    gbz185_id       TEXT NOT NULL,
    issuer          TEXT NOT NULL DEFAULT '',
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    PRIMARY KEY (ain, gbz185_id)
);

-- 2. messages
-- from_agent_id / to_agent_id: TEXT (接受 UUID 或可读标识)，无 FK（跨底座互信）
CREATE TABLE IF NOT EXISTS huanyu.messages (
    message_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent_id    TEXT NOT NULL,
    to_agent_id      TEXT,
    message_type     TEXT NOT NULL CHECK (message_type IN ('inquiry','quote','counter','accept','reject','clarify','cancel','info','payment_notify','payment_confirm','counter_response','file','image','structured_data','overload_l4','overload_l5','deal_closed','fulfillment_ask','notification')),
    payload          JSONB DEFAULT '{}',
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
    delivery_status  TEXT NOT NULL DEFAULT 'local' CHECK (delivery_status IN ('local','pending','delivered','failed')),
    idempotency_key  TEXT,
    signature        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_at          TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_idempotency ON huanyu.messages (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_from ON huanyu.messages (from_agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_to ON huanyu.messages (to_agent_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_negotiation ON huanyu.messages (negotiation_id);
CREATE INDEX IF NOT EXISTS idx_messages_delivery ON huanyu.messages (delivery_status) WHERE delivery_status = 'pending';

-- 3. negotiations
-- buyer_id / supplier_id: TEXT，无 FK（跨底座互信）
CREATE TABLE IF NOT EXISTS huanyu.negotiations (
    negotiation_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buyer_id         TEXT NOT NULL,
    supplier_id      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','accepted','rejected','cancelled','expired','counter_proposed')),
    product_category TEXT DEFAULT '',
    initial_inquiry  JSONB DEFAULT '{}',
    latest_offer     JSONB DEFAULT '{}',
    counter_count    INTEGER DEFAULT 0,
    max_counters     INTEGER DEFAULT 5,
    last_activity_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at       TIMESTAMPTZ DEFAULT (NOW() + INTERVAL '7 days'),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_negotiations_buyer ON huanyu.negotiations (buyer_id, status);
CREATE INDEX IF NOT EXISTS idx_negotiations_supplier ON huanyu.negotiations (supplier_id, status);
CREATE INDEX IF NOT EXISTS idx_negotiations_expires ON huanyu.negotiations (expires_at) WHERE status = 'active';

-- 4. agreements
-- buyer_id / supplier_id: TEXT，无 FK（跨底座互信）
CREATE TABLE IF NOT EXISTS huanyu.agreements (
    agreement_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    negotiation_id UUID NOT NULL REFERENCES huanyu.negotiations(negotiation_id),
    buyer_id       TEXT NOT NULL,
    supplier_id    TEXT NOT NULL,
    product        TEXT NOT NULL,
    quantity       TEXT NOT NULL,
    unit_price     TEXT NOT NULL,
    total_price    TEXT NOT NULL,
    terms          JSONB DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','signed','completed','cancelled','disputed')),
    buyer_finance_ain TEXT DEFAULT '',
    seller_finance_ain TEXT DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Migration: add finance_ain columns for multi-server accounting
ALTER TABLE huanyu.agreements ADD COLUMN IF NOT EXISTS buyer_finance_ain TEXT DEFAULT '';
ALTER TABLE huanyu.agreements ADD COLUMN IF NOT EXISTS seller_finance_ain TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_agreements_buyer ON huanyu.agreements (buyer_id);
CREATE INDEX IF NOT EXISTS idx_agreements_supplier ON huanyu.agreements (supplier_id);

-- 5. ratings
-- from_agent / to_agent: TEXT，无 FK（跨底座互信）
CREATE TABLE IF NOT EXISTS huanyu.ratings (
    rating_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_agent   TEXT NOT NULL,
    to_agent     TEXT NOT NULL,
    agreement_id UUID REFERENCES huanyu.agreements(agreement_id),
    score        NUMERIC(3,1) NOT NULL CHECK (score BETWEEN 1.0 AND 5.0),
    dimensions   JSONB DEFAULT '{}',
    comment      TEXT DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ratings_agent ON huanyu.ratings (to_agent);

-- 6. purchase_orders (PO)
CREATE TABLE IF NOT EXISTS huanyu.purchase_orders (
    po_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agreement_id   UUID NOT NULL REFERENCES huanyu.agreements(agreement_id),
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
CREATE INDEX IF NOT EXISTS idx_po_buyer ON huanyu.purchase_orders (buyer_id);
CREATE INDEX IF NOT EXISTS idx_po_supplier ON huanyu.purchase_orders (supplier_id);

-- Migration: ratings score from INTEGER to NUMERIC for float support
-- Drop dependent view before altering column type
DROP VIEW IF EXISTS huanyu.agent_rating_summary CASCADE;
ALTER TABLE huanyu.ratings ALTER COLUMN score TYPE NUMERIC(3,1);

-- 8. topic_subscriptions
CREATE TABLE IF NOT EXISTS huanyu.topic_subscriptions (
    id        BIGSERIAL PRIMARY KEY,
    agent_id  TEXT NOT NULL,
    topic     TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_topic_sub_topic ON huanyu.topic_subscriptions (topic);

-- 9. peers (跨底座注册)
CREATE TABLE IF NOT EXISTS huanyu.peers (
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
CREATE INDEX IF NOT EXISTS idx_peers_status ON huanyu.peers (status);

-- 9. cert_revocations (QACP 证书吊销列表)
CREATE TABLE IF NOT EXISTS huanyu.cert_revocations (
    cert_fingerprint TEXT PRIMARY KEY,
    revoked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reason           TEXT DEFAULT ''
);

-- 10. agent_processes (Agent Runtime Manager 进程表)
CREATE TABLE IF NOT EXISTS huanyu.agent_processes (
    agent_id      TEXT PRIMARY KEY REFERENCES huanyu.agents(agent_id),
    pid           INTEGER,
    status        TEXT NOT NULL DEFAULT 'stopped'
                  CHECK (status IN ('running','stopped','crashed','restarting','starting','unhealthy','fatal','paused')),
    started_at    TIMESTAMPTZ,
    stopped_at    TIMESTAMPTZ,
    restart_count INTEGER DEFAULT 0,
    last_error    TEXT DEFAULT '',
    config_json   JSONB DEFAULT '{}'
);

-- 11. bus_states (总线状态表 — BusScheduler 核心)
CREATE TABLE IF NOT EXISTS huanyu.bus_states (
    agent_id       TEXT PRIMARY KEY,
    state          TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (state IN ('unknown','registered','adopted','ready','paused','stopped')),
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata       JSONB DEFAULT '{}'
);

-- 8. audit_log (镇岳审计 — 蓝图 + 镇岳对齐)
CREATE TABLE IF NOT EXISTS huanyu.audit_log (
    log_id       BIGSERIAL PRIMARY KEY,
    actor_id     TEXT NOT NULL,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL DEFAULT '',
    target_id    TEXT NOT NULL DEFAULT '',
    detail       JSONB DEFAULT '{}',
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
CREATE INDEX IF NOT EXISTS idx_audit_actor ON huanyu.audit_log (actor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON huanyu.audit_log (action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON huanyu.audit_log (target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_audit_hash ON huanyu.audit_log (row_hash);
-- 防篡改验证：每行的 row_hash = SHA256(prev_hash || actor_id || action || target_type || target_id || result || created_at)
-- 镇岳附加签名：signature = HMAC(sign_key_id, row_hash)，用于跨底座审计校验

-- 综合评分视图
-- 先 DROP 再 CREATE，避免 CREATE OR REPLACE 列名变更时报错
-- （PostgreSQL 不允许 CREATE OR REPLACE VIEW 改变列名/列序）
DROP VIEW IF EXISTS huanyu.agent_rating_summary CASCADE;
CREATE VIEW huanyu.agent_rating_summary AS
SELECT
    to_agent AS agent_id,
    ROUND(AVG(score)::numeric, 1) AS avg_score,
    COUNT(*) AS total_ratings,
    COUNT(DISTINCT from_agent) AS unique_raters
FROM huanyu.ratings
GROUP BY to_agent;



CREATE SCHEMA IF NOT EXISTS yongheng;
CREATE EXTENSION IF NOT EXISTS vector;

-- 1. memories
CREATE TABLE IF NOT EXISTS yongheng.memories (
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
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON yongheng.memories (namespace);
CREATE INDEX IF NOT EXISTS idx_memories_type ON yongheng.memories (memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON yongheng.memories (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_memories_protected ON yongheng.memories (protected) WHERE protected = TRUE;
CREATE INDEX IF NOT EXISTS idx_memories_consolidated ON yongheng.memories (consolidated) WHERE consolidated = FALSE;
CREATE INDEX IF NOT EXISTS idx_memories_fts ON yongheng.memories USING GIN (to_tsvector('simple', content));
CREATE INDEX IF NOT EXISTS idx_memories_embedding ON yongheng.memories
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
    WHERE embedding_status = 'done';

-- 2. trajectories
CREATE TABLE IF NOT EXISTS yongheng.trajectories (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,
    date            DATE NOT NULL,
    actions         JSONB NOT NULL DEFAULT '[]',
    summary         TEXT DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (namespace, date)
);
CREATE INDEX IF NOT EXISTS idx_trajectories_namespace_date ON yongheng.trajectories (namespace, date);

-- 3. profiles
CREATE TABLE IF NOT EXISTS yongheng.profiles (
    namespace       TEXT PRIMARY KEY,
    traits          JSONB DEFAULT '{}',
    learned         JSONB DEFAULT '[]',
    state           JSONB DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. digests
CREATE TABLE IF NOT EXISTS yongheng.digests (
    id              BIGSERIAL PRIMARY KEY,
    namespace       TEXT NOT NULL,
    target_date     DATE NOT NULL,
    type            TEXT NOT NULL DEFAULT 'daily',
    digest          TEXT NOT NULL,
    source_records  BIGINT[],
    record_count    INTEGER DEFAULT 0,
    timeline_entry  TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (namespace, target_date, type)
);
CREATE INDEX IF NOT EXISTS idx_digests_namespace_date ON yongheng.digests (namespace, target_date DESC);

-- 5. tokens
CREATE TABLE IF NOT EXISTS yongheng.tokens (
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
CREATE INDEX IF NOT EXISTS idx_yh_tokens_namespace ON yongheng.tokens (namespace);
CREATE INDEX IF NOT EXISTS idx_yh_tokens_hash ON yongheng.tokens (token_hash);



CREATE SCHEMA IF NOT EXISTS huichuan;

-- ── 1. knowledge_entries ─────────────────────────────

CREATE TABLE IF NOT EXISTS huichuan.knowledge_entries (
    knowledge_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title            TEXT NOT NULL,
    domain           TEXT NOT NULL,
    tags             TEXT[] DEFAULT '{}',
    visibility       TEXT NOT NULL DEFAULT 'public'
                         CHECK (visibility IN ('public', 'enterprise', 'private')),
    owner_agent      TEXT,
    authorized_agents TEXT[] DEFAULT '{}',
    content          TEXT NOT NULL,
    source           TEXT DEFAULT 'manual',
    version          INT DEFAULT 1,
    valid_from       DATE,
    valid_until      DATE,
    metadata         JSONB DEFAULT '{}',
    entry_type       TEXT DEFAULT 'entity'
                         CHECK (entry_type IN ('entity','concept','comparison','query','source')),
    original_filename      TEXT,
    original_storage_path  TEXT,
    original_file_sha256   TEXT,
    quality          INT DEFAULT 3 CHECK (quality BETWEEN 1 AND 5),
    status           TEXT DEFAULT 'active'
                         CHECK (status IN ('draft', 'active', 'archived', 'revoked')),
    refined_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ke_domain
    ON huichuan.knowledge_entries (domain);
CREATE INDEX IF NOT EXISTS idx_ke_tags
    ON huichuan.knowledge_entries USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_ke_visibility
    ON huichuan.knowledge_entries (visibility);
CREATE INDEX IF NOT EXISTS idx_ke_owner
    ON huichuan.knowledge_entries (owner_agent)
    WHERE owner_agent IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ke_updated
    ON huichuan.knowledge_entries (updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ke_fts
    ON huichuan.knowledge_entries
    USING GIN (to_tsvector('simple', title || ' ' || content));

-- ── 2. knowledge_versions ────────────────────────────

CREATE TABLE IF NOT EXISTS huichuan.knowledge_versions (
    version_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    knowledge_id    UUID NOT NULL REFERENCES huichuan.knowledge_entries(knowledge_id)
                        ON DELETE CASCADE,
    version         INT NOT NULL,
    content         TEXT NOT NULL,
    changed_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kv_knowledge
    ON huichuan.knowledge_versions (knowledge_id, version DESC);

-- ── 3. subscriptions ─────────────────────────────────

CREATE TABLE IF NOT EXISTS huichuan.subscriptions (
    subscription_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id          TEXT NOT NULL,
    subscription_name TEXT NOT NULL DEFAULT 'default',
    domains           TEXT[] DEFAULT '{}',
    tags              TEXT[] DEFAULT '{}',
    active            BOOLEAN DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, subscription_name)
);

CREATE INDEX IF NOT EXISTS idx_sub_agent
    ON huichuan.subscriptions (agent_id)
    WHERE active = TRUE;

-- ── 4. refinement_queue ──────────────────────────────

CREATE TABLE IF NOT EXISTS huichuan.refinement_queue (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    submitter       TEXT NOT NULL,
    domain           TEXT,
    raw_experience  TEXT NOT NULL,
    confidence      INT DEFAULT 3 CHECK (confidence BETWEEN 1 AND 5),
    status          TEXT DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'approved', 'rejected')),
    refined_content TEXT,
    knowledge_id    UUID REFERENCES huichuan.knowledge_entries(knowledge_id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_rq_status
    ON huichuan.refinement_queue (status, created_at);

-- ── 5. knowledge_links (Phase 5) ─────────────────────────

CREATE TABLE IF NOT EXISTS huichuan.knowledge_links (
    link_id      BIGSERIAL PRIMARY KEY,
    source_id    UUID NOT NULL REFERENCES huichuan.knowledge_entries(knowledge_id)
                     ON DELETE CASCADE,
    target_id    UUID NOT NULL REFERENCES huichuan.knowledge_entries(knowledge_id)
                     ON DELETE CASCADE,
    link_type    TEXT NOT NULL CHECK (link_type IN ('related','contradicts','extends','depends','cites')),
    confidence   FLOAT DEFAULT 1.0,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_id, target_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_kl_source
    ON huichuan.knowledge_links(source_id);
CREATE INDEX IF NOT EXISTS idx_kl_target
    ON huichuan.knowledge_links(target_id);

-- ── 6. file_registry (Phase 2+ Layer 1 文件生命周期管理) ──

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

-- ── 7. file_images (Phase 1+ 图片提取索引) ─────────────────

CREATE TABLE IF NOT EXISTS huichuan.file_images (
    image_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_id        UUID NOT NULL REFERENCES huichuan.file_registry(file_id) ON DELETE CASCADE,
    source_type    TEXT NOT NULL,          -- pdf|docx|xlsx
    source_sheet   TEXT DEFAULT '',        -- Excel sheet 名
    page_num       INT DEFAULT 0,         -- PDF 页码
    image_index    INT NOT NULL,          -- 文件内图片序号
    image_format   TEXT NOT NULL,         -- png|jpg|svg|webp
    image_size     INT NOT NULL,          -- bytes
    image_sha256   TEXT NOT NULL,         -- 去重
    storage_path   TEXT NOT NULL,         -- Layer 1 路径
    width          INT DEFAULT 0,
    height         INT DEFAULT 0,
    context_before TEXT DEFAULT '',       -- 前文 200 字
    context_after  TEXT DEFAULT '',       -- 后文 200 字
    alt_text       TEXT DEFAULT '',       -- 多模态预留
    metadata       JSONB DEFAULT '{}',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fi_file
    ON huichuan.file_images (file_id);
CREATE INDEX IF NOT EXISTS idx_fi_sha256
    ON huichuan.file_images (image_sha256);



CREATE SCHEMA IF NOT EXISTS zhice;

CREATE TABLE IF NOT EXISTS zhice.workflows (
    workflow_id    BIGSERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT,
    version        INT NOT NULL DEFAULT 1,
    definition     JSONB NOT NULL,
    source_task_id BIGINT,
    created_by     TEXT NOT NULL,
    last_used_at   TIMESTAMPTZ,
    use_count      INT NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_zhice_wf_last_used ON zhice.workflows (last_used_at);

CREATE TABLE IF NOT EXISTS zhice.tasks (
    task_id            BIGSERIAL PRIMARY KEY,
    title              TEXT NOT NULL,
    description        TEXT NOT NULL,
    priority           TEXT NOT NULL DEFAULT 'P2',
    status             TEXT NOT NULL DEFAULT 'pending',
    workflow_id        BIGINT,
    workflow_version   INT,
    created_by         TEXT NOT NULL,
    participants       TEXT[] NOT NULL DEFAULT '{}',
    acceptance_criteria JSONB,
    expected_outputs   JSONB,
    timeout_minutes    INT,
    progress           INT NOT NULL DEFAULT 0,
    result             TEXT,
    started_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS zhice.steps (
    step_id            BIGSERIAL PRIMARY KEY,
    task_id            BIGINT NOT NULL REFERENCES zhice.tasks(task_id) ON DELETE CASCADE,
    step_index         INT NOT NULL,
    title              TEXT NOT NULL,
    instruction        TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    status_reason      TEXT,
    assigned_agent     TEXT,
    assigned_at        TIMESTAMPTZ,
    depends_on         INT[],
    acceptance_criteria JSONB,
    expected_outputs   JSONB,
    outputs            JSONB,
    summary            TEXT,
    auto_retry         INT NOT NULL DEFAULT 0,
    timeout_minutes    INT,
    idempotency_key    TEXT,
    last_heartbeat_at  TIMESTAMPTZ,
    started_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(task_id, step_index)
);

CREATE TABLE IF NOT EXISTS zhice.verifications (
    verification_id    BIGSERIAL PRIMARY KEY,
    task_id            BIGINT NOT NULL REFERENCES zhice.tasks(task_id) ON DELETE CASCADE,
    step_id            BIGINT REFERENCES zhice.steps(step_id),
    rule_type          TEXT NOT NULL,
    check_mode         TEXT NOT NULL DEFAULT 'engine',
    rule_details       JSONB,
    result             TEXT NOT NULL,
    actual_value       TEXT,
    signature          TEXT,     -- Ed25519 签名（Agent 对 check_results 签名，Phase 2）
    verified_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_by        TEXT,
    notes              TEXT
);

CREATE INDEX IF NOT EXISTS idx_zhice_tasks_status ON zhice.tasks(status);
CREATE INDEX IF NOT EXISTS idx_zhice_tasks_created_by ON zhice.tasks(created_by);
CREATE INDEX IF NOT EXISTS idx_zhice_tasks_participants ON zhice.tasks USING GIN(participants);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_task ON zhice.steps(task_id);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_status ON zhice.steps(status);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_agent ON zhice.steps(assigned_agent);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_heartbeat ON zhice.steps(last_heartbeat_at)
    WHERE status = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_zhice_steps_assigned_at ON zhice.steps(assigned_at)
    WHERE status = 'assigned';
CREATE INDEX IF NOT EXISTS idx_zhice_steps_started_at ON zhice.steps(started_at)
    WHERE status = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_zhice_verifications_task ON zhice.verifications(task_id);

-- 5. behavior_policies（v1.10 行为规范系统）
CREATE TABLE IF NOT EXISTS zhice.behavior_policies (
    policy_id      SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    agent_id       TEXT,
    category       TEXT,
    policy_type    TEXT NOT NULL CHECK (policy_type IN ('scope','keyword','pattern')),
    rule           JSONB NOT NULL,
    action         TEXT NOT NULL DEFAULT 'block' CHECK (action IN ('block','warn','log_only')),
    reject_message TEXT,
    priority       INT NOT NULL DEFAULT 0,
    enabled        BOOLEAN NOT NULL DEFAULT true,
    created_by     TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zhice_policies_agent ON zhice.behavior_policies (agent_id);
CREATE INDEX IF NOT EXISTS idx_zhice_policies_category ON zhice.behavior_policies (category);
CREATE INDEX IF NOT EXISTS idx_zhice_policies_enabled ON zhice.behavior_policies (enabled) WHERE enabled = true;

-- Phase C: task_quality_stats（质量可观测性）
CREATE TABLE IF NOT EXISTS zhice.task_quality_stats (
    stat_id               BIGSERIAL PRIMARY KEY,
    task_id               BIGINT NOT NULL REFERENCES zhice.tasks(task_id) ON DELETE CASCADE,
    workflow_id           BIGINT,
    has_quality_criteria  BOOLEAN NOT NULL DEFAULT false,
    iteration_count       INT NOT NULL DEFAULT 0,
    max_iterations        INT,
    self_check_passed     BOOLEAN,
    engine_recheck_fails  INT NOT NULL DEFAULT 0,
    qc_categories         JSONB,
    failure_patterns      JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zhice_qstats_task ON zhice.task_quality_stats (task_id);
CREATE INDEX IF NOT EXISTS idx_zhice_qstats_workflow ON zhice.task_quality_stats (workflow_id);
CREATE INDEX IF NOT EXISTS idx_zhice_qstats_created ON zhice.task_quality_stats (created_at);



CREATE SCHEMA IF NOT EXISTS zhenyue;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA zhenyue;

-- 1. sign_keys（审计签名用）
CREATE TABLE IF NOT EXISTS zhenyue.sign_keys (
    id         INTEGER PRIMARY KEY,
    public_key TEXT NOT NULL,
    algorithm  TEXT NOT NULL DEFAULT 'ed25519',
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_zt_sign_keys_active
    ON zhenyue.sign_keys (status) WHERE status = 'active';

-- 预置 seed key
INSERT INTO zhenyue.sign_keys (id, public_key, algorithm, status)
VALUES (1, '34cf6cdf8adea8f368097dff19ddd51031a659152ed84e132e14b1d1749b8ded', 'ed25519', 'active')
ON CONFLICT (id) DO NOTHING;

-- 2. audit_log（哈希链审计日志）
CREATE TABLE IF NOT EXISTS zhenyue.audit_log (
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
    sign_key_id     INTEGER NOT NULL REFERENCES zhenyue.sign_keys(id)
);
CREATE INDEX IF NOT EXISTS idx_zt_audit_agent_time ON zhenyue.audit_log (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_zt_audit_severity_time ON zhenyue.audit_log (severity, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_zt_audit_action_time ON zhenyue.audit_log (action, created_at DESC);

-- 3. tokens（Bearer Token 存储）
CREATE TABLE IF NOT EXISTS zhenyue.tokens (
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
CREATE INDEX IF NOT EXISTS idx_zt_tokens_agent ON zhenyue.tokens (agent_id);
CREATE INDEX IF NOT EXISTS idx_zt_tokens_hash ON zhenyue.tokens (token_hash);

-- 4. agents（Agent 注册信息）
CREATE TABLE IF NOT EXISTS zhenyue.agents (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT,
    trust_level     TEXT NOT NULL DEFAULT 'basic',
    status          TEXT NOT NULL DEFAULT 'inactive',
    capabilities    JSONB DEFAULT '{}',
    password_hash   TEXT,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by     TEXT,
    reviewed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_zt_agents_status ON zhenyue.agents (status);

-- 5. agent_keys（Agent Ed25519 密钥对）
CREATE TABLE IF NOT EXISTS zhenyue.agent_keys (
    key_id      SERIAL PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    private_key TEXT,
    algorithm   TEXT NOT NULL DEFAULT 'ed25519',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_zt_agent_keys_agent ON zhenyue.agent_keys (agent_id);
CREATE INDEX IF NOT EXISTS idx_zt_agent_keys_active ON zhenyue.agent_keys (agent_id)
    WHERE status = 'active';

-- 6. danger_rules（危险操作注册表）
CREATE TABLE IF NOT EXISTS zhenyue.danger_rules (
    id                    SERIAL PRIMARY KEY,
    method                TEXT NOT NULL,
    path_pattern          TEXT NOT NULL,
    action_name           TEXT NOT NULL UNIQUE,
    severity              TEXT NOT NULL DEFAULT 'high',
    capabilities_required TEXT[] DEFAULT '{}',
    enabled               BOOLEAN DEFAULT true,
    description           TEXT DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_danger_rules_enabled ON zhenyue.danger_rules (enabled);

-- 默认种子数据
INSERT INTO zhenyue.danger_rules (method, path_pattern, action_name, severity, capabilities_required, description) VALUES
('DELETE', '/v1/huanyu/agents/{agent_id}', 'delete_agent', 'critical', '{"admin"}', '删除 Agent'),
('POST',   '/v1/huanyu/agents/{agent_id}/archive', 'archive_agent', 'high', '{"admin","ops_admin"}', '归档 Agent'),
('POST',   '/v1/zhenyue/agents/review', 'review_agent', 'high', '{"admin","ops_admin"}', '审核 Agent'),
('POST',   '/v1/huanyu/messages/batch-read', 'batch_mark_read', 'low', '{}', '批量标记已读'),
('DELETE', '/v1/huanyu/messages/*', 'delete_messages', 'high', '{"admin"}', '删除消息'),
('POST',   '/v1/zhenyue/emergency/break_glass', 'break_glass', 'critical', '{"admin"}', '应急破窗'),
('DELETE', '/v1/xixing/**', 'xixing_delete', 'critical', '{"admin"}', '删除吸星数据'),
('DELETE', '/v1/yongheng/**', 'yongheng_delete', 'critical', '{"admin"}', '删除永恒数据')
ON CONFLICT (action_name) DO NOTHING;

-- 7. approvals（审批门控）
CREATE TABLE IF NOT EXISTS zhenyue.approvals (
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
CREATE INDEX IF NOT EXISTS idx_zt_approvals_status ON zhenyue.approvals (status);
CREATE INDEX IF NOT EXISTS idx_zt_approvals_agent ON zhenyue.approvals (agent_id);

-- 8. approval_requests（应用层审批工作流）
CREATE TABLE IF NOT EXISTS zhenyue.approval_requests (
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
    metadata      JSONB DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_approval_req_status ON zhenyue.approval_requests (status);
CREATE INDEX IF NOT EXISTS idx_zt_approval_req_requester ON zhenyue.approval_requests (requester_id);

-- 9. quarantine（删除隔离区）
CREATE TABLE IF NOT EXISTS zhenyue.quarantine (
    quarantine_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        TEXT NOT NULL,
    source          TEXT NOT NULL,
    original_path   TEXT NOT NULL,
    quarantine_path TEXT NOT NULL,
    original_size   BIGINT DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'quarantined' CHECK (status IN ('quarantined','restored','purged')),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '30 days'),
    restored_at     TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_quarantine_agent ON zhenyue.quarantine (agent_id);
CREATE INDEX IF NOT EXISTS idx_zt_quarantine_status ON zhenyue.quarantine (status);
CREATE INDEX IF NOT EXISTS idx_zt_quarantine_expires ON zhenyue.quarantine (expires_at)
    WHERE status = 'quarantined';

-- 10. guard_rules（守卫规则引擎）
CREATE TABLE IF NOT EXISTS zhenyue.guard_rules (
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
CREATE INDEX IF NOT EXISTS idx_zt_guard_rules_enabled ON zhenyue.guard_rules (enabled);
CREATE INDEX IF NOT EXISTS idx_zt_guard_rules_priority ON zhenyue.guard_rules (priority DESC);

-- 10. agent_reminders — 工作秘书提醒
CREATE TABLE IF NOT EXISTS zhenyue.agent_reminders (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT DEFAULT '',
    remind_at   TEXT DEFAULT '',
    priority    TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high')),
    type        TEXT NOT NULL DEFAULT 'task' CHECK (type IN ('task','deadline','followup','custom')),
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','snoozed','cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_zt_reminders_agent ON zhenyue.agent_reminders (agent_id, status, remind_at);



CREATE SCHEMA IF NOT EXISTS xixing;

-- 1. 知识源管理（替代 sources.json）
CREATE TABLE IF NOT EXISTS xixing.sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'custom',
    schedule        TEXT NOT NULL DEFAULT 'daily',
    day_of_week     INTEGER,
    categories      TEXT[] DEFAULT '{}',
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
CREATE TABLE IF NOT EXISTS xixing.collection_runs (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES xixing.sources(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT DEFAULT 'running',
    http_status     INTEGER,
    content_hash    TEXT,
    content_size    INTEGER,
    raw_path        TEXT,
    error_message   TEXT,
    metadata        JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_cr_source ON xixing.collection_runs (source_id);
CREATE INDEX IF NOT EXISTS idx_cr_status ON xixing.collection_runs (status);

-- 3. 知识条目（替代 knowledge/*.md）
CREATE TABLE IF NOT EXISTS xixing.knowledge_items (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES xixing.sources(id),
    run_id          BIGINT REFERENCES xixing.collection_runs(id),
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL UNIQUE,
    category        TEXT DEFAULT 'general',
    quality_score   REAL DEFAULT 0,
    gate_results    JSONB DEFAULT '{}',
    tags            TEXT[] DEFAULT '{}',
    injected_to_yongheng BOOLEAN DEFAULT FALSE,
    injected_memory_id  BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ki_source ON xixing.knowledge_items (source_id);
CREATE INDEX IF NOT EXISTS idx_ki_category ON xixing.knowledge_items (category);
CREATE INDEX IF NOT EXISTS idx_ki_injected ON xixing.knowledge_items (injected_to_yongheng) WHERE injected_to_yongheng = FALSE;

-- 4. 踩坑记录
CREATE TABLE IF NOT EXISTS xixing.xizhenji (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    root_cause      TEXT DEFAULT '',
    solution        TEXT DEFAULT '',
    severity        TEXT DEFAULT 'medium',
    source          TEXT DEFAULT 'manual',
    related_agent   TEXT,
    tags            TEXT[] DEFAULT '{}',
    category        TEXT DEFAULT '',
    learned_at      TIMESTAMPTZ DEFAULT NOW(),
    resolved        BOOLEAN DEFAULT FALSE,
    injected_to_yongheng BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_xz_severity ON xixing.xizhenji (severity);
CREATE INDEX IF NOT EXISTS idx_xz_resolved ON xixing.xizhenji (resolved) WHERE resolved = FALSE;
CREATE INDEX IF NOT EXISTS idx_xz_category ON xixing.xizhenji (category);

-- 5. 经验反馈追踪（学习闭环）
CREATE TABLE IF NOT EXISTS xixing.experience_feedback (
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
CREATE INDEX IF NOT EXISTS idx_ef_experience ON xixing.experience_feedback (experience_id);
CREATE INDEX IF NOT EXISTS idx_ef_source ON xixing.experience_feedback (source_agent);
CREATE INDEX IF NOT EXISTS idx_ef_feedback ON xixing.experience_feedback (feedback_agent);

-- 6. 竞品扫描结果
CREATE TABLE IF NOT EXISTS xixing.scan_results (
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
CREATE INDEX IF NOT EXISTS idx_scan_date ON xixing.scan_results (scan_date DESC);

-- 6. 蒸馏日志
CREATE TABLE IF NOT EXISTS xixing.distillation_runs (
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



CREATE SCHEMA IF NOT EXISTS siku;

-- 1. accounts — 账户余额
CREATE TABLE IF NOT EXISTS siku.accounts (
    account_id      BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL UNIQUE,
    balance_fen     BIGINT NOT NULL DEFAULT 0 CHECK (balance_fen >= 0),
    frozen_fen      BIGINT NOT NULL DEFAULT 0 CHECK (frozen_fen >= 0),
    total_recharged BIGINT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_accounts_agent ON siku.accounts (agent_id);

-- 2. transactions — 交易流水（只追加）
CREATE TABLE IF NOT EXISTS siku.transactions (
    txn_id          BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    txn_type        TEXT NOT NULL CHECK (txn_type IN ('recharge','deduct','admin_adjust')),
    amount_fen      BIGINT NOT NULL,
    balance_after   BIGINT NOT NULL,
    fee_type        TEXT NOT NULL DEFAULT '' CHECK (fee_type IN ('','cert_upgrade','annual_fee')),
    reference_id    TEXT DEFAULT '',
    idempotency_key TEXT,
    detail          JSONB DEFAULT '{}',
    prev_hash       TEXT NOT NULL DEFAULT '',
    row_hash        TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cs_txn_idempotency
    ON siku.transactions (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cs_txn_agent ON siku.transactions (agent_id, created_at DESC);

-- 只追加保护：禁止 UPDATE / DELETE
CREATE OR REPLACE FUNCTION siku.block_txn_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'siku.transactions 只追加，禁止 UPDATE/DELETE';
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cs_txn_no_mutate'
    ) THEN
        CREATE TRIGGER trg_cs_txn_no_mutate
            BEFORE UPDATE OR DELETE ON siku.transactions
            FOR EACH ROW EXECUTE FUNCTION siku.block_txn_mutation();
    END IF;
END $$;

-- 3. annual_fee_status — 年费状态
CREATE TABLE IF NOT EXISTS siku.annual_fee_status (
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
    ON siku.annual_fee_status (expires_at) WHERE is_expired = false;

-- 4. invoices — 发票管理
CREATE TABLE IF NOT EXISTS siku.invoices (
    invoice_id      BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    invoice_type    TEXT NOT NULL DEFAULT 'electronic' CHECK (invoice_type IN ('electronic', 'paper')),
    title           TEXT NOT NULL,
    tax_number      TEXT NOT NULL DEFAULT '',
    amount_fen      BIGINT NOT NULL,
    related_txn_ids TEXT[] DEFAULT '{}',
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
CREATE INDEX IF NOT EXISTS idx_cs_invoice_agent ON siku.invoices (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_invoice_status ON siku.invoices (status, created_at);

-- 5. admin_operations — 管理员操作记录
CREATE TABLE IF NOT EXISTS siku.admin_operations (
    op_id           BIGSERIAL PRIMARY KEY,
    operator_id     TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    amount_fen      BIGINT DEFAULT 0,
    txn_id          BIGINT,
    detail          JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_admin_ops_time ON siku.admin_operations (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_admin_ops_target ON siku.admin_operations (target_agent_id, created_at DESC);

-- 6. finance_audit — 财务审计日志（只追加，哈希链，不可篡改）
CREATE TABLE IF NOT EXISTS siku.finance_audit (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    action          TEXT NOT NULL,
    event_type      TEXT NOT NULL DEFAULT '',
    target_id       TEXT DEFAULT '',
    amount_fen      BIGINT DEFAULT 0,
    severity        TEXT NOT NULL DEFAULT 'info',
    detail          TEXT DEFAULT '{}',
    prev_hash       TEXT NOT NULL,
    row_hash        TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_fa_agent_time ON siku.finance_audit (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_fa_action ON siku.finance_audit (action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_fa_event ON siku.finance_audit (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cs_fa_severity ON siku.finance_audit (severity, created_at DESC);



CREATE SCHEMA IF NOT EXISTS skills;

-- 1. 技能定义
CREATE TABLE IF NOT EXISTS skills.skill_definitions (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'proposed',
    source          TEXT NOT NULL DEFAULT 'evolved',
    version         TEXT NOT NULL DEFAULT '0.0.0',
    input_schema    JSONB NOT NULL DEFAULT '{}',
    output_schema   JSONB NOT NULL DEFAULT '{}',
    schema_format   TEXT NOT NULL DEFAULT 'json_schema_draft07',
    knowledge_deps  TEXT[] DEFAULT '{}',
    tool_deps       TEXT[] DEFAULT '{}',
    model_deps      TEXT DEFAULT '',
    reason          TEXT DEFAULT '',
    evidence        JSONB DEFAULT '{}',
    proposed_at     TIMESTAMPTZ,
    activated_at    TIMESTAMPTZ,
    deprecated_at   TIMESTAMPTZ,
    replacement_id  BIGINT REFERENCES skills.skill_definitions(id),
    rejection_reason TEXT DEFAULT '',
    applicable_agents TEXT[] DEFAULT '{}',
    permissions     TEXT[] DEFAULT '{}',
    /* 声明权限列表 */
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sk_status ON skills.skill_definitions(status);
CREATE INDEX IF NOT EXISTS idx_sk_category ON skills.skill_definitions(category);
CREATE INDEX IF NOT EXISTS idx_sk_source ON skills.skill_definitions(source);

-- 2. 技能版本
CREATE TABLE IF NOT EXISTS skills.skill_versions (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES skills.skill_definitions(id),
    version         TEXT NOT NULL,
    changelog       TEXT DEFAULT '',
    breaking_changes TEXT[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(skill_id, version)
);

-- 3. Agent 绑定
CREATE TABLE IF NOT EXISTS skills.agent_skills (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    skill_id        BIGINT NOT NULL REFERENCES skills.skill_definitions(id) ON DELETE CASCADE,
    is_active       BOOLEAN DEFAULT TRUE,
    config          JSONB DEFAULT '{}',
    pinned_version  TEXT DEFAULT '',
    license_cert_id TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_id, skill_id)
);
CREATE INDEX IF NOT EXISTS idx_as_agent ON skills.agent_skills(agent_id);

-- 4. 审核记录
CREATE TABLE IF NOT EXISTS skills.skill_reviews (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES skills.skill_definitions(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    reviewer        TEXT NOT NULL,
    reason          TEXT DEFAULT '',
    from_status     TEXT NOT NULL,
    to_status       TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_review_skill ON skills.skill_reviews(skill_id);

-- 5. 使用统计（第二期预留）
CREATE TABLE IF NOT EXISTS skills.skill_usage_stats (
    id              BIGSERIAL PRIMARY KEY,
    skill_name      TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    invoke_count    INT DEFAULT 0,
    success_count   INT DEFAULT 0,
    avg_confidence  REAL DEFAULT 0.0,
    avg_latency_ms  INT DEFAULT 0,
    stat_date       DATE NOT NULL,
    UNIQUE(skill_name, agent_id, stat_date)
);

-- 6. 技能包（R3 市场下载的 .skill 包文件）
CREATE TABLE IF NOT EXISTS skills.skill_packages (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES skills.skill_definitions(id) ON DELETE CASCADE,
    version         TEXT NOT NULL,
    filename        TEXT NOT NULL DEFAULT '',
    file_size       BIGINT NOT NULL DEFAULT 0,
    sha256          TEXT NOT NULL DEFAULT '',
    storage_path    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    installed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_pkg_skill ON skills.skill_packages(skill_id);

-- 7. 回滚快照（R3 版本回滚用）
CREATE TABLE IF NOT EXISTS skills.rollback_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES skills.skill_definitions(id) ON DELETE CASCADE,
    version_from    TEXT NOT NULL,
    version_to      TEXT NOT NULL,
    snapshot_path   TEXT NOT NULL DEFAULT '',
    snapshot_sha256 TEXT NOT NULL DEFAULT '',
    reason          TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rb_skill ON skills.rollback_snapshots(skill_id);

-- 8. 管理员消息（MessageBus 持久化）
CREATE TABLE IF NOT EXISTS skills.admin_messages (
    id              BIGSERIAL PRIMARY KEY,
    msg_id          TEXT NOT NULL,
    level           TEXT NOT NULL DEFAULT 'info',
    source          TEXT NOT NULL DEFAULT 'system',
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT DEFAULT '',
    dedup_key       TEXT DEFAULT '',
    count           INT DEFAULT 1,
    read            BOOLEAN DEFAULT FALSE,
    archived        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_admin_msg_level ON skills.admin_messages(level);
CREATE INDEX IF NOT EXISTS idx_admin_msg_created ON skills.admin_messages(created_at);


-- License/订阅管理 DDL — 在管理服 database.py 中引用
-- 部署时自动执行（幂等）

-- billing.subscriptions 表 + 扩展字段
CREATE TABLE IF NOT EXISTS billing.subscriptions (
    id              TEXT PRIMARY KEY,
    enterprise_id   TEXT NOT NULL DEFAULT '',
    enterprise_name TEXT DEFAULT '',
    module          TEXT DEFAULT 'bidding',
    amount          NUMERIC(10,2) DEFAULT 0,
    start_date      DATE,
    end_date        DATE,
    invoice_needed  BOOLEAN DEFAULT false,
    remark          TEXT DEFAULT '',
    status          TEXT DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- billing.subscriptions 扩展字段（幂等补丁）
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
