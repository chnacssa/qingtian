"""执策数据库 — Schema 初始化"""
import logging
from common.db import get_pool
from . import config as cfg

SCHEMA = cfg.get_schema_name()
logger = logging.getLogger("zhice.database")

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

CREATE TABLE IF NOT EXISTS {SCHEMA}.workflows (
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
CREATE INDEX IF NOT EXISTS idx_zhice_wf_last_used ON {SCHEMA}.workflows (last_used_at);

CREATE TABLE IF NOT EXISTS {SCHEMA}.tasks (
    task_id            BIGSERIAL PRIMARY KEY,
    title              TEXT NOT NULL,
    description        TEXT NOT NULL,
    priority           TEXT NOT NULL DEFAULT 'P2',
    status             TEXT NOT NULL DEFAULT 'pending',
    workflow_id        BIGINT,
    workflow_version   INT,
    created_by         TEXT NOT NULL,
    participants       TEXT[] NOT NULL DEFAULT '{{}}',
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

CREATE TABLE IF NOT EXISTS {SCHEMA}.steps (
    step_id            BIGSERIAL PRIMARY KEY,
    task_id            BIGINT NOT NULL REFERENCES {SCHEMA}.tasks(task_id) ON DELETE CASCADE,
    step_index         INT NOT NULL,
    title              TEXT NOT NULL,
    instruction        TEXT NOT NULL,
    params             JSONB,
    exec_type          TEXT NOT NULL DEFAULT 'shell',
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

CREATE TABLE IF NOT EXISTS {SCHEMA}.verifications (
    verification_id    BIGSERIAL PRIMARY KEY,
    task_id            BIGINT NOT NULL REFERENCES {SCHEMA}.tasks(task_id) ON DELETE CASCADE,
    step_id            BIGINT REFERENCES {SCHEMA}.steps(step_id),
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

CREATE INDEX IF NOT EXISTS idx_zhice_tasks_status ON {SCHEMA}.tasks(status);
CREATE INDEX IF NOT EXISTS idx_zhice_tasks_created_by ON {SCHEMA}.tasks(created_by);
CREATE INDEX IF NOT EXISTS idx_zhice_tasks_participants ON {SCHEMA}.tasks USING GIN(participants);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_task ON {SCHEMA}.steps(task_id);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_status ON {SCHEMA}.steps(status);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_agent ON {SCHEMA}.steps(assigned_agent);
CREATE INDEX IF NOT EXISTS idx_zhice_steps_heartbeat ON {SCHEMA}.steps(last_heartbeat_at)
    WHERE status = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_zhice_steps_assigned_at ON {SCHEMA}.steps(assigned_at)
    WHERE status = 'assigned';
CREATE INDEX IF NOT EXISTS idx_zhice_steps_started_at ON {SCHEMA}.steps(started_at)
    WHERE status = 'in_progress';
CREATE INDEX IF NOT EXISTS idx_zhice_verifications_task ON {SCHEMA}.verifications(task_id);

-- 5. behavior_policies（v1.10 行为规范系统）
CREATE TABLE IF NOT EXISTS {SCHEMA}.behavior_policies (
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
CREATE INDEX IF NOT EXISTS idx_zhice_policies_agent ON {SCHEMA}.behavior_policies (agent_id);
CREATE INDEX IF NOT EXISTS idx_zhice_policies_category ON {SCHEMA}.behavior_policies (category);
CREATE INDEX IF NOT EXISTS idx_zhice_policies_enabled ON {SCHEMA}.behavior_policies (enabled) WHERE enabled = true;

-- Phase C: task_quality_stats（质量可观测性）
CREATE TABLE IF NOT EXISTS {SCHEMA}.task_quality_stats (
    stat_id               BIGSERIAL PRIMARY KEY,
    task_id               BIGINT NOT NULL REFERENCES {SCHEMA}.tasks(task_id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS idx_zhice_qstats_task ON {SCHEMA}.task_quality_stats (task_id);
CREATE INDEX IF NOT EXISTS idx_zhice_qstats_workflow ON {SCHEMA}.task_quality_stats (workflow_id);
CREATE INDEX IF NOT EXISTS idx_zhice_qstats_created ON {SCHEMA}.task_quality_stats (created_at);
"""


async def ensure_schema():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
        # 存量迁移：旧 workflows 表缺少 use_count 列
        try:
            await conn.execute(
                f"ALTER TABLE {SCHEMA}.workflows ADD COLUMN IF NOT EXISTS "
                f"use_count INT NOT NULL DEFAULT 0"
            )
        except Exception:
            pass

        # ── v2 迁移（全部 ADD COLUMN IF NOT EXISTS，向后兼容）──
        v2_alter_statements = [
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS quality_criteria JSONB",
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS max_iterations INT NOT NULL DEFAULT 3",
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS iteration_log JSONB",
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS risk_level TEXT NOT NULL DEFAULT 'low'",
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS confirmation_required BOOLEAN NOT NULL DEFAULT false",
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS confirmed_by TEXT",
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ",
            f"ALTER TABLE {SCHEMA}.tasks ADD COLUMN IF NOT EXISTS high_risk_steps INT NOT NULL DEFAULT 0",
            # v3: 执行类型列
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS exec_type TEXT NOT NULL DEFAULT 'shell'",
            # v4: 步骤结构化参数（投标文件内容等透传）
            f"ALTER TABLE {SCHEMA}.steps ADD COLUMN IF NOT EXISTS params JSONB",
        ]
        for sql in v2_alter_statements:
            try:
                await conn.execute(sql)
            except Exception as exc:
                logger.warning("v2 migration (non-fatal): %s", exc)
