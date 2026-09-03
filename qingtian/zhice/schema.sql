-- ============================================
-- zhice: 任务管理器（执策）
-- ============================================

CREATE SCHEMA IF NOT EXISTS zhice;

-- Workflow 模板
CREATE TABLE IF NOT EXISTS zhice.workflows (
    workflow_id    BIGSERIAL PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT,
    version        INT NOT NULL DEFAULT 1,
    definition     JSONB NOT NULL,
    source_task_id BIGINT,
    created_by     TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, version)
);

-- 任务
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

-- 步骤
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

-- 验收记录
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

-- 索引
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
CREATE INDEX IF NOT EXISTS idx_zhice_verifications_task ON zhice.verifications(task_id);
