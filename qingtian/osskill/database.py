"""
技能库 — 数据库 Schema 初始化 + CRUD 操作
在 skills schema 下创建 5 张核心表及索引
"""

import json
import logging
from pathlib import Path

from common.db import get_pool

SCHEMA = "skills"

TABLES_SQL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

-- 1. 技能定义
CREATE TABLE IF NOT EXISTS {SCHEMA}.skill_definitions (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'proposed',
    source          TEXT NOT NULL DEFAULT 'evolved',
    version         TEXT NOT NULL DEFAULT '0.0.0',
    input_schema    JSONB NOT NULL DEFAULT '{{}}',
    output_schema   JSONB NOT NULL DEFAULT '{{}}',
    schema_format   TEXT NOT NULL DEFAULT 'json_schema_draft07',
    knowledge_deps  TEXT[] DEFAULT '{{}}',
    tool_deps       TEXT[] DEFAULT '{{}}',
    model_deps      TEXT DEFAULT '',
    reason          TEXT DEFAULT '',
    evidence        JSONB DEFAULT '{{}}',
    proposed_at     TIMESTAMPTZ,
    activated_at    TIMESTAMPTZ,
    deprecated_at   TIMESTAMPTZ,
    replacement_id  BIGINT REFERENCES {SCHEMA}.skill_definitions(id),
    rejection_reason TEXT DEFAULT '',
    applicable_agents TEXT[] DEFAULT '{{}}',
    permissions     TEXT[] DEFAULT '{{}}',
    /* 声明权限列表 */
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sk_status ON {SCHEMA}.skill_definitions(status);
CREATE INDEX IF NOT EXISTS idx_sk_category ON {SCHEMA}.skill_definitions(category);
CREATE INDEX IF NOT EXISTS idx_sk_source ON {SCHEMA}.skill_definitions(source);

-- 2. 技能版本
CREATE TABLE IF NOT EXISTS {SCHEMA}.skill_versions (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES {SCHEMA}.skill_definitions(id),
    version         TEXT NOT NULL,
    changelog       TEXT DEFAULT '',
    breaking_changes TEXT[] DEFAULT '{{}}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(skill_id, version)
);

-- 3. Agent 绑定
CREATE TABLE IF NOT EXISTS {SCHEMA}.agent_skills (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    skill_id        BIGINT NOT NULL REFERENCES {SCHEMA}.skill_definitions(id) ON DELETE CASCADE,
    is_active       BOOLEAN DEFAULT TRUE,
    config          JSONB DEFAULT '{{}}',
    pinned_version  TEXT DEFAULT '',
    license_cert_id TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_id, skill_id)
);
CREATE INDEX IF NOT EXISTS idx_as_agent ON {SCHEMA}.agent_skills(agent_id);

-- 4. 审核记录
CREATE TABLE IF NOT EXISTS {SCHEMA}.skill_reviews (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES {SCHEMA}.skill_definitions(id) ON DELETE CASCADE,
    action          TEXT NOT NULL,
    reviewer        TEXT NOT NULL,
    reason          TEXT DEFAULT '',
    from_status     TEXT NOT NULL,
    to_status       TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_review_skill ON {SCHEMA}.skill_reviews(skill_id);

-- 5. 使用统计（第二期预留）
CREATE TABLE IF NOT EXISTS {SCHEMA}.skill_usage_stats (
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
CREATE TABLE IF NOT EXISTS {SCHEMA}.skill_packages (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES {SCHEMA}.skill_definitions(id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS idx_pkg_skill ON {SCHEMA}.skill_packages(skill_id);

-- 7. 回滚快照（R3 版本回滚用）
CREATE TABLE IF NOT EXISTS {SCHEMA}.rollback_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    skill_id        BIGINT NOT NULL REFERENCES {SCHEMA}.skill_definitions(id) ON DELETE CASCADE,
    version_from    TEXT NOT NULL,
    version_to      TEXT NOT NULL,
    snapshot_path   TEXT NOT NULL DEFAULT '',
    snapshot_sha256 TEXT NOT NULL DEFAULT '',
    reason          TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rb_skill ON {SCHEMA}.rollback_snapshots(skill_id);

-- 8. 管理员消息（MessageBus 持久化）
CREATE TABLE IF NOT EXISTS {SCHEMA}.admin_messages (
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
CREATE INDEX IF NOT EXISTS idx_admin_msg_level ON {SCHEMA}.admin_messages(level);
CREATE INDEX IF NOT EXISTS idx_admin_msg_created ON {SCHEMA}.admin_messages(created_at);
"""


# ── 迁移 SQL（现有数据库的列追加，兼容首次部署） ─────

_MIGRATIONS_SQL = f"""
ALTER TABLE {SCHEMA}.skill_definitions
    ADD COLUMN IF NOT EXISTS permissions TEXT[] DEFAULT '{{}}';
ALTER TABLE {SCHEMA}.skill_definitions
    ADD COLUMN IF NOT EXISTS sast_result JSONB DEFAULT NULL;
ALTER TABLE {SCHEMA}.skill_definitions
    ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{{}}';
ALTER TABLE {SCHEMA}.skill_definitions
    ADD COLUMN IF NOT EXISTS commands JSONB NOT NULL DEFAULT '[]';
-- workflow 证书绑定到 agent_skills（delivery/cert_issue/cert_sweep 均 UPDATE 该列）
ALTER TABLE {SCHEMA}.agent_skills
    ADD COLUMN IF NOT EXISTS license_cert_id TEXT DEFAULT '';
-- 自动升级开关（底座启用后定时检查市场拉取新版）
ALTER TABLE {SCHEMA}.skill_definitions
    ADD COLUMN IF NOT EXISTS auto_update BOOLEAN NOT NULL DEFAULT FALSE;

-- G1 评估与监控：执行轨迹表（失败必录 + 成功抽样 10%，业务侧 hook 写入）
CREATE TABLE IF NOT EXISTS {SCHEMA}.exec_trajectories (
    traj_id      BIGSERIAL PRIMARY KEY,
    agent_id     TEXT NOT NULL DEFAULT '',
    skill_name   TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL DEFAULT '',
    goal         TEXT,
    steps        JSONB NOT NULL DEFAULT '[]',
    result       TEXT,
    ok           BOOLEAN NOT NULL DEFAULT TRUE,
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    elapsed_ms   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_exec_traj_created ON {SCHEMA}.exec_trajectories (created_at);
"""

# 反迁移 SQL（回滚 commands 列）
_REVERSE_MIGRATIONS_SQL = f"""
ALTER TABLE {SCHEMA}.skill_definitions
    DROP COLUMN IF EXISTS commands;
"""


async def ensure_schema():
    """确保所有 skills 表和索引存在"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(TABLES_SQL)
        await conn.execute(_MIGRATIONS_SQL)


# ── CRUD 帮助函数 ──────────────────────────────────


async def get_skill_by_id(skill_id: int) -> dict | None:
    """获取 Skill 详情（含版本、审核、绑定）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.skill_definitions WHERE id = $1", skill_id
        )
        if not row:
            return None
        skill = dict(row)

        # 版本列表
        versions = await conn.fetch(
            f"SELECT id, version, changelog, breaking_changes, created_at "
            f"FROM {SCHEMA}.skill_versions WHERE skill_id = $1 ORDER BY created_at DESC",
            skill_id,
        )
        skill["versions"] = [dict(v) for v in versions]

        # 审核记录
        reviews = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.skill_reviews WHERE skill_id = $1 ORDER BY created_at DESC",
            skill_id,
        )
        skill["reviews"] = [dict(r) for r in reviews]

        # 绑定列表
        bindings = await conn.fetch(
            f"SELECT agent_id, is_active, pinned_version, config FROM {SCHEMA}.agent_skills "
            f"WHERE skill_id = $1 ORDER BY created_at DESC",
            skill_id,
        )
        skill["bound_agents"] = [dict(b) for b in bindings]

        return skill


async def get_skill_by_name(name: str) -> dict | None:
    """按名称查找 Skill。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT id, name, status FROM {SCHEMA}.skill_definitions WHERE name = $1", name
        )
        return dict(row) if row else None


async def register_bundled_skill(skill_json_path: str) -> dict | None:
    """注册 bundled Skill（读取 skill.json → 写入 skill_definitions → 设 active）。

    首次部署时注册，后续重启时检测已有则跳过。
    返回 {"id": ..., "name": ..., "status": "active"} 或 None。
    """
    try:
        with open(skill_json_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.getLogger(__name__).warning("读取 skill.json 失败: %s", e)
        return None

    name = manifest.get("name", "")
    if not name:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        # UPSERT: 存在则更新，不存在则插入
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.skill_definitions
                (name, display_name, description, category, status, source, version,
                 input_schema, output_schema, knowledge_deps, permissions, tags, commands,
                 activated_at)
                VALUES ($1, $2, $3, $4, 'active', 'bundled', $5,
                        $6::jsonb, $7::jsonb, $8, $9, $10, $11::jsonb,
                        NOW())
                ON CONFLICT (name) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    category = EXCLUDED.category,
                    status = CASE
                        WHEN {SCHEMA}.skill_definitions.status IN ('active', 'deprecated') THEN {SCHEMA}.skill_definitions.status
                        ELSE 'active'
                    END,
                    version = EXCLUDED.version,
                    tags = EXCLUDED.tags,
                    commands = EXCLUDED.commands,
                    updated_at = NOW()
                RETURNING id, name, status""",
            name,
            manifest.get("display_name", name),
            manifest.get("description", ""),
            manifest.get("category", "tool"),
            manifest.get("version", "1.0.0"),
            json.dumps(manifest.get("input_schema", {})),
            json.dumps(manifest.get("output_schema", {})),
            manifest.get("knowledge_deps", []),
            manifest.get("permissions", []),
            manifest.get("tags", []),
            json.dumps(manifest.get("commands", [])),
        )
        skill = dict(row)

        # 同步写入 skill_versions（首次）
        await conn.execute(
            f"""INSERT INTO {SCHEMA}.skill_versions (skill_id, version, changelog)
                VALUES ($1, $2, $3)
                ON CONFLICT (skill_id, version) DO NOTHING""",
            skill["id"], manifest.get("version", "1.0.0"),
            manifest.get("description", "bundled Skill")[:200],
        )

        return skill


async def register_market_skill(install_dir: str) -> int | None:
    """注册市场安装的 Skill（读安装目录 skill.json → skill_definitions）。

    幂等：同名已存在则更新元数据并返回现有 id；status 强制 active，
    source='market'（区别于 bundled/evolved）。返回 skill_definitions.id 或 None。
    """
    try:
        with open(Path(install_dir) / "skill.json", "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logging.getLogger(__name__).warning("读取市场 Skill skill.json 失败: %s", e)
        return None

    name = manifest.get("name", "")
    if not name:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.skill_definitions
                (name, display_name, description, category, status, source, version,
                 input_schema, output_schema, knowledge_deps, permissions, tags, commands,
                 activated_at)
                VALUES ($1, $2, $3, $4, 'active', 'market', $5,
                        $6::jsonb, $7::jsonb, $8, $9, $10, $11::jsonb,
                        NOW())
                ON CONFLICT (name) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    category = EXCLUDED.category,
                    status = 'active',
                    source = EXCLUDED.source,
                    version = EXCLUDED.version,
                    tags = EXCLUDED.tags,
                    commands = EXCLUDED.commands,
                    updated_at = NOW()
                RETURNING id""",
            name,
            manifest.get("display_name", name),
            manifest.get("description", ""),
            manifest.get("category", "tool"),
            manifest.get("version", "1.0.0"),
            json.dumps(manifest.get("input_schema", {})),
            json.dumps(manifest.get("output_schema", {})),
            manifest.get("knowledge_deps", []),
            manifest.get("permissions", []),
            manifest.get("tags", []),
            json.dumps(manifest.get("commands", [])),
        )
        skill_id = row["id"]

        await conn.execute(
            f"""INSERT INTO {SCHEMA}.skill_versions (skill_id, version, changelog)
                VALUES ($1, $2, $3)
                ON CONFLICT (skill_id, version) DO NOTHING""",
            skill_id, manifest.get("version", "1.0.0"),
            "market skill",
        )
        return skill_id


async def insert_proposal(proposal: dict) -> dict:
    """插入吸星提交的轻量提案，返回完整记录"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.skill_definitions
                (name, display_name, description, category, status, source,
                 reason, evidence, proposed_at)
                VALUES ($1, $2, $3, $4, 'proposed', 'evolved',
                        $5, $6, NOW())
                ON CONFLICT (name) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    description = EXCLUDED.description,
                    reason = EXCLUDED.reason,
                    evidence = EXCLUDED.evidence,
                    updated_at = NOW()
                RETURNING id, name, status""",
            proposal["name"],
            proposal["display_name"],
            proposal["description"],
            proposal.get("category", "cost"),
            proposal.get("reason", ""),
            proposal.get("evidence", {}),
        )
        return dict(row)


async def list_skills(
    status: str = "",
    category: str = "",
    source: str = "",
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """查询技能列表，返回 (skills, total)"""
    conditions = ["1=1"]
    params = []
    idx = 1

    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if statuses:
            placeholders = ", ".join(f"${i+idx}" for i in range(len(statuses)))
            conditions.append(f"status IN ({placeholders})")
            params.extend(statuses)
            idx += len(statuses)

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    if source:
        conditions.append(f"source = ${idx}")
        params.append(source)
        idx += 1

    if q:
        conditions.append(
            f"(name ILIKE ${idx} OR display_name ILIKE ${idx} OR description ILIKE ${idx})"
        )
        params.append(f"%{q}%")
        idx += 1

    where = " AND ".join(conditions)
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.skill_definitions WHERE {where}",
            *params,
        )
        offset = (page - 1) * page_size
        rows = await conn.fetch(
            f"""SELECT id, name, display_name, description, category, status, source,
                       version, proposed_at, activated_at, created_at
                FROM {SCHEMA}.skill_definitions
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx+1}""",
            *params,
            page_size,
            offset,
        )
        return [dict(r) for r in rows], total


async def add_review(
    skill_id: int, action: str, reviewer: str,
    reason: str = "", from_status: str = "", to_status: str = "",
) -> None:
    """写入审核记录"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO {SCHEMA}.skill_reviews
                (skill_id, action, reviewer, reason, from_status, to_status)
                VALUES ($1, $2, $3, $4, $5, $6)""",
            skill_id, action, reviewer, reason, from_status, to_status,
        )


async def update_status(
    skill_id: int, new_status: str, **extra_fields,
) -> None:
    """更新 Skill 状态 + 可选额外字段"""
    sets = ["status = $2", "updated_at = NOW()"]
    params = [skill_id, new_status]
    idx = 3
    for field, value in extra_fields.items():
        sets.append(f"{field} = ${idx}")
        params.append(value)
        idx += 1

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.skill_definitions SET {', '.join(sets)} WHERE id = $1",
            *params,
        )


async def get_agent_skills(agent_id: str) -> list[dict]:
    """查询 Agent 绑定的所有技能"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT sd.id, sd.name, sd.display_name, sd.status, sd.version,
                       ask.is_active, ask.pinned_version, ask.config
                FROM {SCHEMA}.agent_skills ask
                JOIN {SCHEMA}.skill_definitions sd ON sd.id = ask.skill_id
                WHERE ask.agent_id = $1
                ORDER BY sd.created_at DESC""",
            agent_id,
        )
        return [dict(r) for r in rows]


async def get_skill_bindings(skill_id: int) -> list[dict]:
    """查询某个 Skill 的所有绑定"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT agent_id, is_active, pinned_version, config, created_at
                FROM {SCHEMA}.agent_skills
                WHERE skill_id = $1
                ORDER BY created_at DESC""",
            skill_id,
        )
        return [dict(r) for r in rows]


async def bind_skill(agent_id: str, skill_id: int, config: dict | None = None,
                     pinned_version: str = "") -> bool:
    """绑定 Skill 到 Agent。返回 True 表示新建，False 表示已存在。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            f"SELECT id FROM {SCHEMA}.agent_skills WHERE agent_id = $1 AND skill_id = $2",
            agent_id, skill_id,
        )
        if existing:
            return False
        await conn.execute(
            f"""INSERT INTO {SCHEMA}.agent_skills
                (agent_id, skill_id, config, pinned_version)
                VALUES ($1, $2, $3, $4)""",
            agent_id, skill_id, config or {}, pinned_version or "",
        )
        return True


async def unbind_skill(agent_id: str, skill_id: int) -> bool:
    """解绑 Skill。返回 True 表示删除成功，False 表示不存在。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"DELETE FROM {SCHEMA}.agent_skills WHERE agent_id = $1 AND skill_id = $2",
            agent_id, skill_id,
        )
        return "DELETE 1" in result


# ── 回滚快照操作 ──


async def save_rollback_snapshot(
    skill_id: int, version_from: str, version_to: str,
    snapshot_path: str, sha256_hash: str = "", reason: str = "",
) -> int:
    """保存回滚快照记录"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 清理旧快照（保留最近 3 个）
        await conn.execute(
            f"""DELETE FROM {SCHEMA}.rollback_snapshots
                WHERE skill_id = $1 AND id NOT IN (
                    SELECT id FROM {SCHEMA}.rollback_snapshots
                    WHERE skill_id = $1
                    ORDER BY created_at DESC LIMIT 2
                )""",
            skill_id,
        )
        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.rollback_snapshots
                (skill_id, version_from, version_to, snapshot_path, snapshot_sha256, reason)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id""",
            skill_id, version_from, version_to,
            snapshot_path, sha256_hash, reason,
        )
        return row["id"]


async def get_rollback_snapshots(skill_id: int) -> list[dict]:
    """获取回滚快照列表（按时间降序）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT id, version_from, version_to, snapshot_path,
                       snapshot_sha256, reason, created_at
                FROM {SCHEMA}.rollback_snapshots
                WHERE skill_id = $1
                ORDER BY created_at DESC""",
            skill_id,
        )
        return [dict(r) for r in rows]


# ── SAST 结果操作 ──────────────────────────────


async def update_sast_result(skill_id: int, sast_summary: dict) -> None:
    """更新 Skill 的 SAST 扫描结果（存入 sast_result JSONB 列）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.skill_definitions "
            f"SET sast_result = $1::jsonb, updated_at = NOW() "
            f"WHERE id = $2",
            json.dumps(sast_summary),
            skill_id,
        )


async def list_active_skill_routes() -> list[dict]:
    """列出所有活跃 Skill 的路由元数据。

    返回每个 active Skill 的:
      - name, display_name, description, category, tags
      - actions: 从 input_schema 提取的 action 枚举值列表
    供秘书/探针做跨 Skill 路由匹配。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT name, display_name, description, category, tags, input_schema
                FROM {SCHEMA}.skill_definitions
                WHERE status = 'active'
                ORDER BY name"""
        )
        result = []
        for r in rows:
            d = dict(r)
            # 从 input_schema 提取 action 枚举
            actions = []
            try:
                schema = d.get("input_schema") or {}
                if isinstance(schema, str):
                    schema = json.loads(schema)
                props = schema.get("properties", {})
                action_schema = props.get("action", {})
                enum_vals = action_schema.get("enum", [])
                if enum_vals:
                    actions = list(enum_vals)
            except (json.JSONDecodeError, TypeError):
                pass
            d["actions"] = actions
            d["tags"] = d.get("tags") or []
            result.append(d)
        return result


async def list_active_skills_with_tags() -> list[dict]:
    """列出所有活跃 Skill 的基础元数据（含 tags）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT id, name, display_name, description, category, tags
                FROM {SCHEMA}.skill_definitions
                WHERE status = 'active'
                ORDER BY name"""
        )
        return [dict(r) for r in rows]


async def update_permissions(skill_id: int, permissions: list[str]) -> None:
    """更新 Skill 的声明权限列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.skill_definitions "
            f"SET permissions = $1, updated_at = NOW() "
            f"WHERE id = $2",
            permissions,
            skill_id,
        )
