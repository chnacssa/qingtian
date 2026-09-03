"""
寰宇 — Agent 目录服务
注册、发现、心跳、软删除、topic 订阅
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from common.config import get as root_get

logger = logging.getLogger("huanyu.directory")
from . import config as hcfg
from . import ain as ain_mod
from . import ed25519_utils as ed
from . import certificate as cert

SCHEMA = hcfg.get_schema_name()


def _now():
    return datetime.now(timezone.utc)


async def _sync_to_zhenyue(conn, agent_id: str, name: str, category: str) -> None:
    """同步写入 zhenyue.agents（密码登录 + 信任等级）

    所有 agent 注册路径（显式 register_agent / 无感 register_agent_silent）
    统一走此函数写入镇岳本地登记表，避免多路径重复写表逻辑。
    """
    try:
        await conn.execute(
            """INSERT INTO zhenyue.agents (agent_id, name, category, trust_level, status)
                VALUES ($1, $2, $3, 'basic', 'active')
                ON CONFLICT (agent_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    status = 'active'""",
            agent_id, name, category,
        )
    except Exception:
        pass  # zhenyue schema 未初始化时静默跳过


# ── Schema ────────────────────────────────────────────

async def ensure_schema():
    from .database import ensure_schema as _ensure
    await _ensure()


# ── 注册 ──────────────────────────────────────────────

async def register_agent(
    name: str,
    category: str,
    subcategory: str = "",
    capabilities: Optional[list[str]] = None,
    contact_info: str = "",
    server_host: str = "",
    metadata: Optional[dict] = None,
    instance: Optional[str] = None,
    uscc: str = "",
    agent_id: Optional[str] = None,
    company_name: str = "",
) -> dict:
    org = hcfg.get_organization()
    country = hcfg.get_country()
    city = hcfg.get_city()
    base_name = server_host or root_get("host", "localhost")
    # 🧭 跨底座精准回发地址：host_ip（config.yaml 配置的 WG 内网 IP），主机名仅作故障定位
    host_ip = root_get("host_ip", "")
    role_code = category  # category 直接映射为角色码

    if instance is None:
        instance = await ain_mod.next_instance(org, country, city, base_name, role_code)

    agent_ain = ain_mod.generate_ain(org, country, city, base_name, role_code, instance)

    # trust_level → tier 映射
    tier_map = {"basic": "free", "verified": "pro", "trusted": "enterprise", "admin": "alliance"}

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 检查是否已存在（同名 + 同主机 = 更新而非新建）
        existing = await conn.fetchrow(
            f"SELECT agent_id, ain, public_key, cert_fingerprint FROM {SCHEMA}.agents "
            f"WHERE name = $1 AND server_host = $2",
            name, base_name,
        )

        is_new = existing is None

        if is_new:
            # 新注册：生成 Ed25519 密钥对 + 自签名证书
            private_key, public_key = ed.generate_keypair()
            tier = tier_map.get("basic", "free")
            cert_body = cert.create_self_signed_cert(agent_ain, private_key, tier)
            public_key_pem = cert_body["public_key"]
            cert_fp = cert_body["fingerprint"]
        else:
            private_key = None
            public_key_pem = existing.get("public_key", "")
            cert_fp = existing.get("cert_fingerprint", "")

        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.agents (agent_id, ain, public_key, cert_fingerprint, name, category,
               subcategory, capabilities, contact_info, server_host, server_ip, metadata,
               uscc, company_name, c_level, last_heartbeat)
               VALUES (COALESCE($1, gen_random_uuid()::text), $2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,'C0', NOW())
               ON CONFLICT (name, server_host) DO UPDATE
               SET category = EXCLUDED.category,
                   capabilities = EXCLUDED.capabilities,
                   uscc = EXCLUDED.uscc,
                   company_name = EXCLUDED.company_name,
                   server_ip = EXCLUDED.server_ip,
                   status = 'active',
                   ain = COALESCE({SCHEMA}.agents.ain, EXCLUDED.ain),
                   public_key = COALESCE({SCHEMA}.agents.public_key, EXCLUDED.public_key),
                   cert_fingerprint = COALESCE({SCHEMA}.agents.cert_fingerprint, EXCLUDED.cert_fingerprint),
                   updated_at = NOW()
               RETURNING agent_id, ain, public_key, cert_fingerprint, name, category, status, server_host, server_ip, c_level, industry, scale, created_at""",
            agent_id, agent_ain, public_key_pem, cert_fp, name, category, subcategory,
            json.dumps(capabilities or []),
            contact_info,
            base_name,
            host_ip,
            json.dumps(metadata or {}),
            uscc,
            company_name,
        )
        result = dict(row)
        # 私钥仅在新注册时返回（更新时调用方已持有）
        if is_new and private_key is not None:
            result["private_key"] = ed.private_key_to_pem(private_key)

        # 司库 — 自动开户 + biz:seller 年费初始化
        try:
            from siku.account_service import ensure_account
            from siku.config import get_annual_free_months, get_schema_name as get_siku_schema
            await ensure_account(conn, result["agent_id"])
            if category == "biz:seller":
                free_months = get_annual_free_months()
                siku_schema = get_siku_schema()
                await conn.execute(
                    f"INSERT INTO {siku_schema}.annual_fee_status (agent_id, free_months, expires_at) "
                    f"VALUES ($1, $2, NOW() + INTERVAL '{free_months} months') "
                    f"ON CONFLICT (agent_id) DO NOTHING",
                    result["agent_id"], free_months,
                )
        except Exception:
            logger.exception("siku开户/年费初始化失败 for %s", result["agent_id"])

        # ── 镇岳本地 agent 登记（统一入口）──
        await _sync_to_zhenyue(conn, result["agent_id"], name, category)

        # 跨底座广播：通知其他底座同步此 agent（事务提交后发送）
        agent_data = {
            "type": "agent_register",
            "agent": {
                "agent_id": result["agent_id"],
                "ain": result.get("ain", ""),
                "name": name,
                "category": category,
                "subcategory": subcategory,
                "public_key": result.get("public_key", ""),
                "cert_fingerprint": result.get("cert_fingerprint", ""),
                "capabilities": capabilities or [],
                "server_host": base_name,
                "server_ip": host_ip,
                "metadata": metadata or {},
            },
        }
        # 存入局部变量，事务外使用
        _broadcast_data = agent_data

    # 事务已提交，广播 agent 注册事件
    try:
        from .peers import get_engine
        engine = get_engine()
        if engine._redis is not None:
            await engine._redis.publish(
                "huanyu:broadcast",
                json.dumps(_broadcast_data, ensure_ascii=False),
            )
    except Exception:
        logger.exception("failed to broadcast agent_register for %s", result["agent_id"])

    # HTTP 直推管理服（弥补 Redis 广播不可达）
    try:
        from .peers import notify_hub_agent_registered
        await notify_hub_agent_registered(_broadcast_data["agent"])
    except Exception:
        logger.exception("failed to notify hub for %s", result["agent_id"])

    return result


# ── 查询 ──────────────────────────────────────────────

async def discover_agents(
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    capability: Optional[str] = None,
    server_host: Optional[str] = None,
    industry: Optional[str] = None,
    c_level_min: Optional[str] = None,
    scale: Optional[str] = None,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = [f"status = 'active'"]
        params = []
        idx = 1

        if category:
            conditions.append(f"category = ${idx}")
            params.append(category)
            idx += 1
        if subcategory:
            conditions.append(f"subcategory = ${idx}")
            params.append(subcategory)
            idx += 1
        if capability:
            # review(2026-08-16): 兼容历史双重编码行（jsonb 存成了 string），string 时
            # 先 #>> 取出字符串内容再转 jsonb 数组，避免 jsonb_array_elements_text 报错
            conditions.append(
                f"""${idx}::text = ANY(ARRAY(SELECT jsonb_array_elements_text(
                    CASE WHEN jsonb_typeof(capabilities) = 'string'
                         THEN (capabilities #>> '{{}}')::jsonb
                         ELSE capabilities END)))"""
            )
            params.append(capability)
            idx += 1
        if server_host:
            conditions.append(f"server_host = ${idx}")
            params.append(server_host)
            idx += 1
        if industry:
            conditions.append(f"industry = ${idx}")
            params.append(industry)
            idx += 1
        if c_level_min:
            conditions.append(f"c_level >= ${idx}")
            params.append(c_level_min)
            idx += 1
        if scale:
            conditions.append(f"scale = ${idx}")
            params.append(scale)
            idx += 1

        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"SELECT agent_id::text, ain, name, category, subcategory, capabilities, server_host, "
            f"status, trust_level, industry, c_level, scale, last_heartbeat, created_at "
            f"FROM {SCHEMA}.agents WHERE {where} ORDER BY name",
            *params,
        )
        return [dict(r) for r in rows]


async def get_agent(agent_id: str) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT a.*, m.gbz185_id "
            f"FROM {SCHEMA}.agents a "
            f"LEFT JOIN {SCHEMA}.gbz185_mappings m ON a.ain = m.ain "
            f"WHERE a.agent_id = $1 "
            f"ORDER BY a.created_at DESC LIMIT 1",
            agent_id,
        )
        return dict(row) if row else None


async def resolve_agent(ain_or_id: str) -> Optional[dict]:
    """解析 AIN 或 agent_id → Agent 完整信息（含 server_host / public_key）。

    供 /agents/resolve 端点与 GB/Z 185 合规客户端使用（身份即地址）。
    仅返回 active 状态的 agent，与 discover_agents 语义一致。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT a.*, m.gbz185_id "
            f"FROM {SCHEMA}.agents a "
            f"LEFT JOIN {SCHEMA}.gbz185_mappings m ON a.ain = m.ain "
            f"WHERE (a.ain = $1 OR a.agent_id::text = $1) AND a.status = 'active' "
            f"ORDER BY a.created_at DESC LIMIT 1",
            ain_or_id,
        )
        return dict(row) if row else None


async def search_agents(query: str) -> list[dict]:
    """全文搜索 Agent（名称/子分类/联系信息）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT agent_id::text, name, category, subcategory, capabilities, server_host,
                       status, trust_level, last_heartbeat
                FROM {SCHEMA}.agents
                WHERE status = 'active'
                  AND (name ILIKE $1 OR subcategory ILIKE $1 OR contact_info ILIKE $1)
                ORDER BY name""",
            f"%{query}%",
        )
        return [dict(r) for r in rows]


async def search_by_capability(capability: str, tag: str = "") -> list[dict]:
    """按能力/标签搜索 Agent — 对标 GB/Z 185.5 基于发现服务的语义查询"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if tag:
            rows = await conn.fetch(
                f"""SELECT agent_id::text, name, category, capabilities, status, trust_level
                    FROM {SCHEMA}.agents
                    WHERE status = 'active'
                      AND (capabilities::text ILIKE $1)
                    ORDER BY name""",
                f"%{tag}%",
            )
        elif capability:
            rows = await conn.fetch(
                f"""SELECT agent_id::text, name, category, capabilities, status, trust_level
                    FROM {SCHEMA}.agents
                    WHERE status = 'active'
                      AND (category ILIKE $1 OR capabilities::text ILIKE $1
                           OR name ILIKE $1)
                    ORDER BY name""",
                f"%{capability}%",
            )
        else:
            rows = await conn.fetch(
                f"""SELECT agent_id::text, name, category, capabilities, status, trust_level
                    FROM {SCHEMA}.agents WHERE status='active' ORDER BY name"""
            )
        return [dict(r) for r in rows]


async def get_categories() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT category, COUNT(*)::int AS cnt FROM {SCHEMA}.agents WHERE status = 'active' GROUP BY category")
        return [dict(r) for r in rows]


# ── 心跳 ──────────────────────────────────────────────

async def heartbeat(agent_id: str) -> dict:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 仅 active/inactive 可刷新心跳；deleted/suspended 不允许复活
            row = await conn.fetchrow(
                f"UPDATE {SCHEMA}.agents SET last_heartbeat = NOW(), status = 'active' "
                f"WHERE agent_id = $1 AND status IN ('active', 'inactive') "
                f"RETURNING agent_id::text, status",
                agent_id,
            )
            if row:
                return {"status": "ok", "agent_id": row["agent_id"]}
            # 区分：未注册 vs 已删除/挂起
            status_row = await conn.fetchrow(
                f"SELECT status FROM {SCHEMA}.agents WHERE agent_id = $1",
                agent_id,
            )
            if status_row is None:
                return {"status": "error", "error": "Agent 未注册"}
            return {"status": "error", "error": f"Agent 已{status_row['status']}，心跳被拒绝"}
    except Exception as e:
        return {"status": "error", "error": f"心跳更新失败: {e}"}


async def start_heartbeat_monitor(interval_seconds: int = 604800):
    """启动后台活跃度监控任务。

    每 interval_seconds 秒（默认 7 天）检查一次，标记 7 天无交流记录的 active → inactive，
    再标记 inactive 超 30 天 → suspended。
    在 main.py startup 中通过 asyncio.create_task 启动。
    """
    import asyncio
    hb_logger = logging.getLogger("huanyu.heartbeat-monitor")
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            stale = await check_stale_agents()
            if stale:
                hb_logger.info("标记 inactive: %s", stale)
            suspended = await check_suspended_agents()
            if suspended:
                hb_logger.info("标记 suspended: %s", suspended)
        except Exception as e:
            hb_logger.error("活跃度监控异常: %s", e)


async def check_stale_agents() -> list[str]:
    """标记 7 天无交流记录的 Agent 为 inactive（替代原心跳超时机制）。

    仅判定本底座本地注册的 agent（server_host = 本底座 host）：跨底座 agent 的
    活跃状态由所属底座通过 /peers/sync 整表上报维护（_handle_agent_register 采纳
    上报 status），本底座消息活跃度对它们不适用。管理服/hub 作为目录汇总方，本地
    messages 表没有跨底座 agent 的交流记录，若无 server_host 过滤会每 5 分钟把
    同步来的跨底座 agent 全误杀成 inactive（2026-08-14 线上：管理服 agent 全 inactive、
    销售服全 active）。
    """
    host_name = root_get("host", "")
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""UPDATE {SCHEMA}.agents SET status = 'inactive'
                WHERE status = 'active'
                  AND server_host = $1
                  AND NOT EXISTS (
                    SELECT 1 FROM huanyu.messages
                    WHERE (from_agent_id = agents.agent_id
                        OR to_agent_id = agents.agent_id)
                      AND created_at > NOW() - INTERVAL '7 days'
                  )
                RETURNING agent_id::text""",
            host_name,
        )
        return [r["agent_id"] for r in rows]


async def check_suspended_agents() -> list[str]:
    """标记 inactive 超 30 天 → suspended"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""UPDATE {SCHEMA}.agents SET status = 'suspended'
                WHERE status = 'inactive'
                  AND updated_at < NOW() - INTERVAL '30 days'
                RETURNING agent_id::text""",
        )
        return [r["agent_id"] for r in rows]


# ── 软删除 ────────────────────────────────────────────

async def soft_delete_agent(agent_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE {SCHEMA}.agents SET status = 'deleted', deleted_at = NOW() "
            f"WHERE agent_id = $1 RETURNING agent_id::text",
            agent_id,
        )
        # P2 (R11): 显式 bool 标记存在/不存在——此前仅返回 dict（恒为真值），
        # 调用方按 `if ok:` 判定时删除不存在的 agent 也误报 deleted。
        if not row:
            return {"deleted": False, "status": "error", "agent_id": agent_id, "error": "Agent 未找到"}
        return {"deleted": True, "status": "deleted", "agent_id": agent_id, "deleted_at": _now().isoformat()}


# ── Topic 订阅 ─────────────────────────────────────────

async def subscribe_topics(agent_id: str, topics: list[str]) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        for topic in topics:
            await conn.execute(
                f"""INSERT INTO {SCHEMA}.topic_subscriptions (agent_id, topic)
                    VALUES ($1, $2) ON CONFLICT (agent_id, topic) DO NOTHING""",
                agent_id, topic,
            )
    return {"status": "ok", "agent_id": agent_id, "topics": topics}


async def unsubscribe_topics(agent_id: str, topics: list[str]) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"DELETE FROM {SCHEMA}.topic_subscriptions WHERE agent_id = $1 AND topic = ANY($2)",
            agent_id, topics,
        )
    return {"status": "ok", "agent_id": agent_id, "topics": topics}


async def get_topic_subscribers(topic: str) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT a.agent_id::text, a.name, a.server_host
                FROM {SCHEMA}.topic_subscriptions ts
                JOIN {SCHEMA}.agents a ON ts.agent_id = a.agent_id
                WHERE ts.topic = $1 AND a.status = 'active'""",
            topic,
        )
        return [dict(r) for r in rows]


# ── 无感注册（总线自动注册用）────────────────────────

async def register_agent_silent(
    agent_id: str,
    name: str,
    category: str,
    server_host: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    """无感注册 — 总线 _auto_register 使用

    与 register_agent 的区别（Phase 2）：
    - 不生成 Ed25519 密钥对（总线自动注册时不需要）
    - 不广播（跨底座 Redis + HTTP 管理服都跳过）
    - 不创建司库账户（由羲和接管时 6 步集成完成）
    - 不返回 private_key

    P2 (R11): 传入的 agent_id 是调用方（总线自动注册）期望的权威身份，此前仅出现在
    日志、行数据 agent_id 全由 DB 默认 uuid 生成（参数被忽略）。现若传入则显式写入
    （已存在则按 agent_id 更新并复用原 AIN，保持唯一性）；缺省才走 next_instance。
    """
    org = hcfg.get_organization()
    country = hcfg.get_country()
    city = hcfg.get_city()
    base_name = server_host or root_get("host", "localhost")
    # 🧭 无感注册同样携带跨底座回发地址（方案甲），否则总线自动注册的 agent 永远 server_ip=''
    host_ip = root_get("host_ip", "")
    role_code = category

    pool = await get_pool()
    async with pool.acquire() as conn:
        if agent_id:
            # P2 (R11): agent_id 生效——先按 agent_id 查重
            existing = await conn.fetchrow(
                f"SELECT agent_id::text FROM {SCHEMA}.agents WHERE agent_id = $1",
                agent_id,
            )
            if existing:
                # 已存在：按 agent_id 更新（复用原 AIN，不重算实例，避免唯一性冲突）
                row = await conn.fetchrow(
                    f"""UPDATE {SCHEMA}.agents
                        SET name = $2, category = $3, server_host = $4,
                            server_ip = CASE WHEN $5 <> ''
                                             THEN $5
                                             ELSE {SCHEMA}.agents.server_ip END,
                            status = 'active', last_heartbeat = NOW(),
                            metadata = COALESCE(
                                {SCHEMA}.agents.metadata || $6::jsonb, $6::jsonb
                            ),
                            updated_at = NOW()
                        WHERE agent_id = $1
                        RETURNING agent_id, ain, name, category, status""",
                    agent_id, name, category, base_name, host_ip,
                    json.dumps(metadata or {}),
                )
            else:
                row = None
            if not row:
                # 不存在（或查重后并发删除致 UPDATE 未命中）→ 以传入 agent_id 新建
                # （仅此路径 next_instance 生成 AIN）
                instance = await ain_mod.next_instance(org, country, city, base_name, role_code)
                agent_ain = ain_mod.generate_ain(org, country, city, base_name, role_code, instance)
                row = await conn.fetchrow(
                    f"""INSERT INTO {SCHEMA}.agents
                        (agent_id, ain, public_key, cert_fingerprint, name, category,
                         server_host, server_ip, metadata, status, last_heartbeat)
                        VALUES ($1, $2, '', '', $3, $4, $5, $6, $7::jsonb, 'active', NOW())
                        ON CONFLICT (name, server_host) DO UPDATE
                        SET category = EXCLUDED.category,
                            server_ip = CASE WHEN EXCLUDED.server_ip <> ''
                                             THEN EXCLUDED.server_ip
                                             ELSE {SCHEMA}.agents.server_ip END,
                            status = 'active',
                            last_heartbeat = NOW(),
                            metadata = COALESCE(
                                {SCHEMA}.agents.metadata || EXCLUDED.metadata,
                                EXCLUDED.metadata
                            ),
                            updated_at = NOW()
                        RETURNING agent_id, ain, name, category, status""",
                    agent_id, agent_ain, name, category, base_name, host_ip,
                    json.dumps(metadata or {}),
                )
        else:
            # P2 (R11): 缺省 agent_id → 原行为：next_instance 生成实例 + DB 默认 uuid
            instance = await ain_mod.next_instance(org, country, city, base_name, role_code)
            agent_ain = ain_mod.generate_ain(org, country, city, base_name, role_code, instance)
            row = await conn.fetchrow(
                f"""INSERT INTO {SCHEMA}.agents
                    (ain, public_key, cert_fingerprint, name, category,
                     server_host, server_ip, metadata, status, last_heartbeat)
                    VALUES ($1, '', '', $2, $3, $4, $5, $6::jsonb, 'active', NOW())
                    ON CONFLICT (name, server_host) DO UPDATE
                    SET category = EXCLUDED.category,
                        server_ip = CASE WHEN EXCLUDED.server_ip <> ''
                                         THEN EXCLUDED.server_ip
                                         ELSE {SCHEMA}.agents.server_ip END,
                    status = 'active',
                    last_heartbeat = NOW(),
                    metadata = COALESCE(
                        {SCHEMA}.agents.metadata || EXCLUDED.metadata,
                        EXCLUDED.metadata
                    ),
                    updated_at = NOW()
                RETURNING agent_id, ain, name, category, status""",
                agent_ain, name, category, base_name, host_ip,
                json.dumps(metadata or {}),
            )
        result = dict(row)

        # ── 镇岳本地 agent 登记（统一入口）──
        await _sync_to_zhenyue(conn, result["agent_id"], name, category)

    logger.info("[目录] 无感注册 %s (name=%s, category=%s) → agent_id=%s, ain=%s",
                agent_id, name, category, result["agent_id"], result.get("ain", ""))

    return {
        "agent_id": result["agent_id"],
        "ain": result.get("ain", ""),
        "name": name,
        "category": category,
        "status": "active",
    }


# ── Inbox 写入（总线/模块推送降级用）───────────────────

async def write_inbox(agent_id: str, message: dict):
    """向 Agent 的 inbox 写入消息

    当目标 Agent WS 不在线时，总线自动降级写入 inbox。
    Agent 下次查 inbox 时可见。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO {SCHEMA}.messages "
            f"(from_agent_id, to_agent_id, message_type, payload, status) "
            f"VALUES ($1, $2, $3, $4::jsonb, 'unread')",
            message.get("source", "bus"),
            agent_id,
            message.get("type", "system"),
            json.dumps(message, ensure_ascii=False),
        )


# ── 统计 ──────────────────────────────────────────────

async def get_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        agents_total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.agents")
        agents_active = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.agents WHERE status = 'active'")
        msgs_total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.messages")
        negos_active = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.negotiations WHERE status = 'active'")
        agrs_total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.agreements")
        ratings_total = await conn.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.ratings")
    return {
        "total_agents": agents_total,
        "active_agents": agents_active,
        "total_messages": msgs_total,
        "active_negotiations": negos_active,
        "total_agreements": agrs_total,
        "total_ratings": ratings_total,
    }
