"""
寰宇 — 跨底座通信（企业版：Redis Pub/Sub + OSPF DR 选举 + Gossip 注册表同步）

社区版见 peers.py（基础 HTTP 路由）。

企业版特性：
  - Redis Pub/Sub 联邦通知（新消息、升级、目录变更）
  - OSPF DR 模型：管理底座为 DR，普通底座仅向 Hub 报心跳
  - Gossip 注册表同步：非管理底座定期从 Hub 拉取全量 Agent 注册表

社区版 vs 企业版：
  社区版 | 基础 HTTP 直连路由（deliver_to_peer / check_peer_health）
  企业版 | Redis Pub/Sub + OSPF DR + Gossip 注册表同步

Channel 命名（v2.5 精确化）：
  - huanyu:notify:{role}  通知某底座有新消息（触发 WS 推送）
  - huanyu:broadcast      全底座广播
  - huanyu:sync           目录变更提示
  - huanyu:upgrade        升级通知
"""

import asyncio
import json
import logging
from typing import Optional

import redis.asyncio as redis

from . import config as hcfg

logger = logging.getLogger("huanyu.peers")

# ── 通道命名（v2.5 精确化）───────────────────────────

NOTIFY_PREFIX = "huanyu:notify"
BROADCAST_CHANNEL = "huanyu:broadcast"
SYNC_CHANNEL = "huanyu:sync"
UPGRADE_CHANNEL = "huanyu:upgrade"


def _notify_channel(role: str) -> str:
    """获取底座角色对应的通知 channel"""
    return f"{NOTIFY_PREFIX}:{role}"


# ── 引擎 ─────────────────────────────────────────────

class PeersEngine:
    """Redis Pub/Sub 引擎：仅承载实时通知，不承载消息体"""

    def __init__(self):
        self._redis: Optional[redis.Redis] = None
        self._pubsub: Optional[redis.client.PubSub] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._incoming_handler = None

    @property
    def peer_id(self) -> str:
        return hcfg.get_peer_id()

    @property
    def role(self) -> str:
        from common.config import get
        return get("role", "company")

    def set_incoming_handler(self, handler):
        """设置外来通知处理器 async fn(channel, data)"""
        self._incoming_handler = handler

    async def start(self):
        # 先尝试 Redis 连接（带重试），不阻塞启动
        connected = await self._connect_redis_with_retry()
        if not connected:
            logger.warning("Redis 连接失败，peers engine 以降级模式启动（仅 HTTP 直连）")
        else:
            try:
                # 订阅：本底座通知 channel + 全局 broadcast + sync + upgrade
                self._pubsub = self._redis.pubsub()
                channels = [
                    _notify_channel(self.role),
                    BROADCAST_CHANNEL,
                    SYNC_CHANNEL,
                    UPGRADE_CHANNEL,
                ]
                await self._pubsub.subscribe(*channels)
                # 启动发现：通过 Redis 广播宣告自己上线
                await self._announce_hello()
            except Exception as e:
                logger.warning("Redis 订阅/公告失败，降级模式继续: %s", e)
                self._redis = None
                self._pubsub = None

        self._running = True
        self._task = asyncio.create_task(self._listen())

        # 后台任务：定期底座间心跳（HTTP 直连，不依赖 Redis）
        asyncio.create_task(self._peer_heartbeat_loop())

        # 后台任务：Agent 注册表同步（非管理底座从 Hub 拉取总表）
        asyncio.create_task(_registry_sync_loop(self))

        logger.info(
            "peers engine started, peer_id=%s role=%s redis=%s",
            self.peer_id, self.role, "connected" if self._redis else "disconnected",
        )

    async def _connect_redis_with_retry(self, max_retries: int = 3, delay: float = 1.0) -> bool:
        """尝试 Redis 连接，失败后带指数退避重试。"""
        url = hcfg.get_redis_url()
        password = hcfg.get_redis_password()
        for attempt in range(1, max_retries + 1):
            try:
                self._redis = redis.from_url(url, password=password or None, decode_responses=True)
                await self._redis.ping()
                return True
            except Exception as e:
                logger.warning(
                    "Redis 连接失败 (attempt %d/%d): %s", attempt, max_retries, e,
                )
                if attempt < max_retries:
                    await asyncio.sleep(delay * (2 ** (attempt - 1)))  # 指数退避
                else:
                    self._redis = None
                    return False
        return False

    async def _reconnect_redis(self) -> bool:
        """运行时 Redis 重连（监听循环检测到连接断开后调用）。"""
        try:
            if self._redis:
                try:
                    await self._redis.aclose()
                except Exception:
                    pass
                self._redis = None
            if self._pubsub:
                try:
                    await self._pubsub.close()
                except Exception:
                    pass
                self._pubsub = None

            ok = await self._connect_redis_with_retry(max_retries=5, delay=2.0)
            if not ok:
                return False

            self._pubsub = self._redis.pubsub()
            channels = [
                _notify_channel(self.role),
                BROADCAST_CHANNEL,
                SYNC_CHANNEL,
                UPGRADE_CHANNEL,
            ]
            await self._pubsub.subscribe(*channels)
            logger.info("Redis 重连成功，已重新订阅 channels=%s", channels)
            return True
        except Exception as e:
            logger.warning("Redis 重连失败: %s", e)
            self._redis = None
            self._pubsub = None
            return False

    async def _announce_hello(self):
        """Redis 广播：宣告本底座上线（其他底座收到后更新 peers 表）"""
        # 查询本底座活跃 Agent 数量
        agents_count = 0
        try:
            from common.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    f"SELECT COUNT(*) FROM {hcfg.get_schema_name()}.agents WHERE status = 'active'"
                )
                agents_count = row[0] if row else 0
        except Exception:
            pass

        hello = {
            "type": "peer_hello",
            "peer_id": self.peer_id,
            "name": self.peer_id,
            "host": hcfg.get_peer_name(),
            "port": hcfg.get_peer_port(),
            "role": self.role,
            "agents_count": agents_count,
        }
        await self._redis.publish(BROADCAST_CHANNEL, json.dumps(hello, ensure_ascii=False))
        logger.info("peer hello broadcasted: %s agents=%d", self.peer_id, agents_count)

    async def _peer_heartbeat_loop(self):
        """后台任务：每 60s HTTP 向 Hub 报心跳；管理底座额外清理离线节点

        OSPF DR 模型 — 普通底座只跟 Hub 通信，不走 Redis 全员广播，消除 O(N²) 心跳风暴。
        """
        while self._running:
            try:
                await asyncio.sleep(60)

                # HTTP 直连 Hub 报心跳（替代 Redis 全员广播）
                hub_url = hcfg.get_hub_endpoint()
                if hub_url:
                    try:
                        # 查当前活跃 Agent 数
                        agents_count = 0
                        try:
                            from common.db import get_pool
                            pool = await get_pool()
                            async with pool.acquire() as conn:
                                row = await conn.fetchrow(
                                    f"SELECT COUNT(*) FROM {hcfg.get_schema_name()}.agents WHERE status = 'active'"
                                )
                                agents_count = row[0] if row else 0
                        except Exception:
                            pass

                        import httpx
                        from .signing import sign_peer_message
                        _peer_host = hcfg.get_peer_name()
                        _peer_port = hcfg.get_peer_port()
                        # P1 (R11): 心跳伪造会投毒 peers 目录（路由劫持），上报前签名。
                        hb_body = {"peer_id": self.peer_id, "status": "active",
                                   "host": _peer_host, "port": _peer_port,
                                   "agents_count": agents_count}
                        hb_body["peer_sig"] = sign_peer_message(
                            json.dumps(hb_body, ensure_ascii=False, sort_keys=True))
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            await client.post(f"{hub_url}/peers/heartbeat", json=hb_body)
                    except Exception:
                        logger.warning("hub heartbeat failed for %s", self.peer_id)

                # 管理底座：定期清理超过 5 分钟无心跳的 peer 为离线
                if self.role == "management":
                    from common.db import get_pool
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        await conn.execute(
                            f"UPDATE {hcfg.get_schema_name()}.peers "
                            f"SET status = 'offline' "
                            f"WHERE status = 'active' "
                            f"AND last_heartbeat < NOW() - INTERVAL '5 minutes' "
                            f"AND peer_id != $1",
                            self.peer_id,
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("peer heartbeat error")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.close()
            except Exception:
                pass
        if self._redis:
            try:
                await self._redis.aclose()
            except Exception:
                pass
        logger.info("peers engine stopped, peer_id=%s", self.peer_id)

    async def _listen(self):
        redis_reconnect_delay = 5.0  # 首次重连等待 5s，逐步增加
        while self._running:
            if not self._pubsub:
                # Redis 未连接/已断开，尝试重连
                logger.info("Redis 未连接，尝试重连...")
                if await self._reconnect_redis():
                    redis_reconnect_delay = 5.0  # 重置退避
                else:
                    await asyncio.sleep(redis_reconnect_delay)
                    redis_reconnect_delay = min(redis_reconnect_delay * 1.5, 60.0)
                continue

            try:
                msg = await self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and self._incoming_handler:
                    data = json.loads(msg["data"])
                    channel = msg["channel"]
                    asyncio.create_task(self._incoming_handler(channel, data))
                redis_reconnect_delay = 5.0  # 成功接收消息，重置退避
            except asyncio.CancelledError:
                break
            except (redis.ConnectionError, redis.TimeoutError, ConnectionError) as e:
                logger.warning("Redis 连接断开，将尝试重连: %s", e)
                self._pubsub = None
                self._redis = None
            except Exception:
                logger.exception("peers listen error")
                await asyncio.sleep(1)

    # ── 发布（仅通知类） ──────────────────────────────

    async def notify_base(self, target_role: str, data: dict) -> int:
        """通知指定底座有新消息（msg_id + to_agent，不含消息体）"""
        if not self._redis:
            return 0
        try:
            return await self._redis.publish(
                _notify_channel(target_role),
                json.dumps(data, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning("Redis notify_base failed (target_role=%s): %s", target_role, e)
            return 0

    async def broadcast(self, data: dict) -> int:
        """全底座广播（如升级通知、系统公告）"""
        if not self._redis:
            return 0
        try:
            return await self._redis.publish(
                BROADCAST_CHANNEL,
                json.dumps(data, ensure_ascii=False, default=str),
            )
        except Exception as e:
            logger.warning("Redis broadcast failed: %s", e)
            return 0

    async def notify_sync(self, agent_id: str, action: str) -> int:
        """目录变更提示"""
        if not self._redis:
            return 0
        try:
            return await self._redis.publish(
                SYNC_CHANNEL,
                json.dumps({"agent_id": agent_id, "action": action}, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning("Redis notify_sync failed: %s", e)
            return 0

    async def notify_upgrade(self, version: str, urgency: str, cve: str = "") -> int:
        """紧急升级通知"""
        if not self._redis:
            return 0
        try:
            return await self._redis.publish(
                UPGRADE_CHANNEL,
                json.dumps({"version": version, "urgency": urgency, "cve": cve}, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning("Redis notify_upgrade failed: %s", e)
            return 0

    # ── 查询 ──────────────────────────────────────────

    async def get_online_peers(self) -> list[dict]:
        """获取在线底座列表"""
        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT peer_id, host, port, name, agents_count, last_heartbeat "
                f"FROM {hcfg.get_schema_name()}.peers WHERE status = 'active'"
            )
            return [dict(r) for r in rows]


# ── 全局实例 ─────────────────────────────────────────

_engine: Optional[PeersEngine] = None


def get_engine() -> PeersEngine:
    global _engine
    if _engine is None:
        _engine = PeersEngine()
    return _engine


# ── Redis 通知处理器 ─────────────────────────────────

async def handle_incoming_notification(channel: str, data: dict):
    """处理从 Redis Pub/Sub 收到的通知（仅通知类，不含消息体）"""
    if channel.startswith(NOTIFY_PREFIX):
        # 本底座收到新消息通知 → WS 推送给对应 Agent
        await _handle_new_message_notify(data)
    elif channel == BROADCAST_CHANNEL:
        await _handle_broadcast(data)
    elif channel == SYNC_CHANNEL:
        await _handle_sync_notify(data)
    elif channel == UPGRADE_CHANNEL:
        await _handle_upgrade_notify(data)
    else:
        logger.warning("unknown notification channel: %s", channel)


async def _handle_new_message_notify(data: dict):
    """Redis 通知：有新消息到达 → 触发 WebSocket 推送提示"""
    to_agent = data.get("to_agent", "")
    msg_id = data.get("msg_id", "")
    if not to_agent:
        return
    from .api_ws import manager as ws_manager
    await ws_manager.send_to(to_agent, {
        "type": "new_message",
        "msg_id": msg_id,
        "hint": "check inbox",
    })


async def _handle_broadcast(data: dict):
    """全底座广播 → 按消息类型分派"""
    msg_type = data.get("type", "")

    if msg_type == "peer_hello":
        await _handle_peer_hello(data)
    elif msg_type == "peer_heartbeat":
        await _handle_peer_heartbeat(data)
    elif msg_type == "agent_register":
        await _handle_agent_register(data)
    elif msg_type == "system_notice":
        from .directory import discover_agents
        agents = await discover_agents(category=data.get("category", ""))
        from .api_ws import manager as ws_manager
        for agent in agents:
            await ws_manager.send_to(agent["agent_id"], data.get("payload", {}))
    else:
        logger.debug("broadcast type=%s ignored", msg_type)


async def _handle_peer_hello(data: dict):
    """收到新底座上线 → 写入本地 peers 表 + 回复 ack"""
    from common.db import get_pool
    peer_id = data.get("peer_id", "")
    host = data.get("host", "")
    port = data.get("port", 1996)
    name = data.get("name", peer_id)
    role = data.get("role", "")
    agents_count = data.get("agents_count", 0)

    if not peer_id or not host:
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 清理同 host:port 的旧 peer_id（peer_id 变更场景，如 IP→hostname）
        await conn.execute(
            f"DELETE FROM {hcfg.get_schema_name()}.peers "
            f"WHERE host = $1 AND port = $2 AND peer_id != $3",
            host, port, peer_id,
        )
        await conn.execute(
            f"""INSERT INTO {hcfg.get_schema_name()}.peers
                    (peer_id, host, port, name, last_heartbeat, status, agents_count)
                VALUES ($1, $2, $3, $4, NOW(), 'active', $5)
                ON CONFLICT (peer_id) DO UPDATE
                SET host = EXCLUDED.host,
                    port = EXCLUDED.port,
                    name = EXCLUDED.name,
                    last_heartbeat = NOW(),
                    status = 'active',
                    agents_count = EXCLUDED.agents_count""",
            peer_id, host, port, name, agents_count,
        )
    logger.info("peer discovered: %s (%s:%s) role=%s agents=%d", peer_id, host, port, role, agents_count)

    # 回复 ack
    engine = get_engine()
    ack = {
        "type": "peer_ack",
        "peer_id": engine.peer_id,
        "host": hcfg.get_peer_name(),
        "port": hcfg.get_peer_port(),
        "role": engine.role,
    }
    await engine._redis.publish(_notify_channel(role), json.dumps(ack, ensure_ascii=False))


async def _handle_peer_heartbeat(data: dict):
    """收到其他底座心跳 → 更新 last_heartbeat 和 agents_count"""
    from common.db import get_pool
    agents_count = data.get("agents_count")
    pool = await get_pool()
    async with pool.acquire() as conn:
        if agents_count is not None:
            await conn.execute(
                f"UPDATE {hcfg.get_schema_name()}.peers "
                f"SET last_heartbeat = NOW(), status = 'active', agents_count = $2 "
                f"WHERE peer_id = $1",
                data.get("peer_id", ""), agents_count,
            )
        else:
            await conn.execute(
                f"UPDATE {hcfg.get_schema_name()}.peers SET last_heartbeat = NOW(), status = 'active' "
                f"WHERE peer_id = $1",
                data.get("peer_id", ""),
            )


# ── 底座归属权威映射（方案甲加固：防止跨底座 server_ip 污染）──
# server_host（含旧名变体）→ 该底座权威 WG IP。
# 用于校验/纠正上报的 server_ip，避免采购/销售服 agent 被误填成其他底座 IP
# （历史问题：feishu:ou_69c9f 采购商 agent 被填成 10.0.100.3 销售服 IP → 报价跨底座转发错乱）。
# B5 (R11): 映射不再源码硬编码（泄露企业内网拓扑），改由部署方显式注入：
#   环境变量 QINGTIAN_BASE_IP_MAP（JSON）或 config.yaml huanyu.base_ip_by_host。
#   未配置时映射为空 → 归属校验自动跳过（不误伤正常注册）。


def _authoritative_ip_for_host(server_host: str) -> str:
    """给定 server_host，返回该底座权威 WG IP；无法识别返回 ''。"""
    if not server_host:
        return ""
    return hcfg.get_base_ip_map().get(server_host, "")


async def _handle_agent_register(data: dict):
    """收到跨底座 Agent 注册广播 → 写入本地 agents 表"""
    import json as _json
    agent = data.get("agent", {})
    if not agent or not agent.get("agent_id"):
        return

    # 🛡️ 加固：server_ip 归属校验（防跨底座污染）
    # 若 server_host 能识别底座，且上报 server_ip 与该底座权威 IP 矛盾，
    # 则拒绝采纳该 server_ip（置空，让 ON CONFLICT 保留已有值/后续兜底）。
    # 例：server_host=procurement-server 却带 server_ip=10.0.100.3 → 丢弃。
    supplied_ip = (agent.get("server_ip") or "").strip()
    authoritative_ip = _authoritative_ip_for_host(agent.get("server_host") or "")
    if supplied_ip and authoritative_ip and supplied_ip != authoritative_ip:
        logger.warning(
            "agent register server_ip sanitized: agent=%s server_host=%s server_ip=%s -> drop (authoritative=%s)",
            agent.get("agent_id"), agent.get("server_host"), supplied_ip, authoritative_ip,
        )
        agent["server_ip"] = ""  # 丢弃矛盾值，保留已有

    from common.db import get_pool
    capabilities = agent.get("capabilities", [])
    if isinstance(capabilities, str):
        capabilities = _json.loads(capabilities)
    metadata = agent.get("metadata", {})
    if isinstance(metadata, str):
        metadata = _json.loads(metadata)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO {hcfg.get_schema_name()}.agents (agent_id, ain, public_key, cert_fingerprint,
                name, category, subcategory, capabilities, server_host, server_ip, status, trust_level, metadata)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'active','basic',$11)
                ON CONFLICT (agent_id) DO UPDATE
                SET ain = COALESCE({hcfg.get_schema_name()}.agents.ain, EXCLUDED.ain),
                    public_key = COALESCE({hcfg.get_schema_name()}.agents.public_key, EXCLUDED.public_key),
                    cert_fingerprint = COALESCE({hcfg.get_schema_name()}.agents.cert_fingerprint, EXCLUDED.cert_fingerprint),
                    name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    capabilities = EXCLUDED.capabilities,
                    server_host = EXCLUDED.server_host,
                    -- 🧭 server_ip 仅接受非空权威值（方案甲）：COALESCE 会被 DB 默认 '' 挡住，
                    -- 导致整表快照/广播携带的 WG IP 永远落不了库 → 跨底座回发全走主机名失败。
                    server_ip = CASE WHEN EXCLUDED.server_ip <> '' THEN EXCLUDED.server_ip
                                     ELSE {hcfg.get_schema_name()}.agents.server_ip END,
                    -- 🧡 上报采纳 agent 状态(贪狼确认, 2026-08-08)：此前漏 status 列，
                    -- 历史 inactive 记录被 ON CONFLICT 保留，上报的 active 不被采纳 →
                    -- hub 永远 inactive → 采购侧发现不了。EXCLUDED.status 恒为 active(INSERT写死/快照只选active)，仅复活不反杀。
                    status = EXCLUDED.status,
                    last_heartbeat = NOW(),
                    updated_at = NOW()""",
            agent.get("agent_id"), agent.get("ain", ""), agent.get("public_key", ""),
            agent.get("cert_fingerprint", ""), agent.get("name"),
            agent.get("category"), agent.get("subcategory", ""), _json.dumps(capabilities),
            agent.get("server_host", ""), agent.get("server_ip", ""), _json.dumps(metadata),
        )
    logger.info("agent synced from peer: %s (%s)", agent.get("name"), agent.get("agent_id"))


async def check_peer_health() -> list[dict]:
    """HTTP 直连检查所有底座健康状态（不走 Redis，用于 Redis 宕机时的兜底）"""
    import httpx
    peers = await get_engine().get_online_peers()
    # P1 (R11): /peers/heartbeat 现已要求 peer_sig，健康检查请求也需签名。
    from .signing import sign_peer_message
    hb_body = {"peer_id": get_engine().peer_id}
    hb_body["peer_sig"] = sign_peer_message(
        json.dumps(hb_body, ensure_ascii=False, sort_keys=True))
    results = []
    for peer in peers:
        url = f"http://{peer['host']}:{peer['port']}/peers/heartbeat"
        try:
            # review(2026-08-24 P0-6): 健康探测心跳补 peer_sig（接收侧已收紧为缺签拒绝）
            from .signing import sign_peer_message
            _hb = {"peer_id": get_engine().peer_id}
            _hb["peer_sig"] = sign_peer_message(
                json.dumps(_hb, ensure_ascii=False, sort_keys=True)
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=hb_body)
                results.append({**peer, "reachable": resp.status_code == 200})
        except Exception:
            results.append({**peer, "reachable": False})
            # 标记为 inactive
            from common.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {hcfg.get_schema_name()}.peers SET status = 'inactive' WHERE host = $1 AND port = $2",
                    peer["host"], peer["port"],
                )
    return results


async def _handle_sync_notify(data: dict):
    """目录变更提示 → 触发本地缓存刷新"""
    action = data.get("action", "")
    agent_id = data.get("agent_id", "")
    logger.info("sync notify: action=%s agent_id=%s", action, agent_id)
    # 目录缓存刷新由 directory 模块的定时任务处理，这里仅打日志 + 通知管理员


async def _handle_upgrade_notify(data: dict):
    """紧急升级通知 → 立即拉取升级包"""
    version = data.get("version", "")
    urgency = data.get("urgency", "normal")
    logger.warning("upgrade notify: version=%s urgency=%s cve=%s",
                   version, urgency, data.get("cve", ""))
    # 升级拉取由底座 cron 任务处理，Redis 通知作为即时触发


# ── HTTP 跨底座投递辅助 ──────────────────────────────

async def deliver_to_peer(target_host: str, target_port: int, payload: dict) -> dict:
    """通过 HTTP POST /peers/route 向目标底座投递可靠消息"""
    import httpx
    from .signing import sign_peer_message

    payload_str = json.dumps(payload.get("payload", {}), ensure_ascii=False, sort_keys=True)
    peer_sig = sign_peer_message(payload_str)
    payload["peer_sig"] = peer_sig

    url = f"http://{target_host}:{target_port}/peers/route"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("peer delivery failed to %s:%s: %s", target_host, target_port, e)
        return {"status": "error", "error": str(e)}


# ── Agent 注册表同步（非管理底座 → 管理服拉取） ─────

async def pull_agent_registry_from_hub() -> dict:
    """从管理服 Hub 拉取全量 Agent 注册总表，批量 upsert 到本地 agents 表。

    管理服维护全局 Agent 注册总表，采购/销售底座定期从此拉取。
    启动时延迟 60s 首次拉取，之后每 24h 全量同步。
    """
    hub_url = hcfg.get_hub_endpoint()
    if not hub_url:
        return {"status": "skipped", "reason": "no hub configured"}

    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{hub_url}/peers/agents/registry")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("pull agent registry from hub failed: %s", e)
        return {"status": "error", "error": str(e)}

    agents = data.get("agents", [])
    # 兼容嵌套格式：{"agents": {"agents": [...], "count": N}}
    if isinstance(agents, dict):
        agents = agents.get("agents", [])
    if not agents:
        return {"status": "ok", "synced": 0}
    if isinstance(agents, str):
        return {"status": "error", "reason": f"unexpected agents type: {type(agents).__name__}"}

    from common.db import get_pool
    pool = await get_pool()
    schema = hcfg.get_schema_name()
    synced = 0
    async with pool.acquire() as conn:
        for agent in agents:
            capabilities = agent.get("capabilities", [])
            if isinstance(capabilities, str):
                capabilities = json.loads(capabilities)
            metadata = agent.get("metadata", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            try:
                await conn.execute(
                    f"""INSERT INTO {schema}.agents (agent_id, ain, public_key, cert_fingerprint,
                        name, category, subcategory, capabilities, server_host, server_ip, status, trust_level, metadata)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                        ON CONFLICT (agent_id) DO UPDATE
                        SET ain = COALESCE({schema}.agents.ain, EXCLUDED.ain),
                            public_key = COALESCE({schema}.agents.public_key, EXCLUDED.public_key),
                            cert_fingerprint = COALESCE({schema}.agents.cert_fingerprint, EXCLUDED.cert_fingerprint),
                            name = EXCLUDED.name,
                            category = EXCLUDED.category,
                            capabilities = EXCLUDED.capabilities,
                            server_host = EXCLUDED.server_host,
                            -- 🧭 注册表拉取同样写入 server_ip（方案甲）：此前 upsert 无此列，
                            -- 即使 SELECT 带出 IP 也会被丢弃 → pull 侧本地副本永远空
                            server_ip = CASE WHEN EXCLUDED.server_ip <> '' THEN EXCLUDED.server_ip
                                             ELSE {schema}.agents.server_ip END,
                            status = EXCLUDED.status,
                            updated_at = NOW()""",
                    agent.get("agent_id"), agent.get("ain", ""), agent.get("public_key", ""),
                    agent.get("cert_fingerprint", ""), agent.get("name"),
                    agent.get("category"), agent.get("subcategory", ""), json.dumps(capabilities),
                    agent.get("server_host", ""), agent.get("server_ip", ""),
                    agent.get("status", "active"),
                    agent.get("trust_level", "basic"), json.dumps(metadata),
                )
                synced += 1
            except Exception:
                logger.exception("upsert agent %s failed during registry pull", agent.get("agent_id"))

    logger.info("agent registry synced from hub: %d/%d agents", synced, len(agents))
    return {"status": "ok", "synced": synced, "total": len(agents)}


async def notify_hub_agent_registered(agent_data: dict) -> dict:
    """HTTP 直推：通知管理服同步新注册的 Agent（弥补 Redis 广播不可达）。

    在 register_agent() 事务提交后调用，确保管理服总表实时更新。
    """
    hub_url = hcfg.get_hub_endpoint()
    if not hub_url:
        return {"status": "skipped", "reason": "no hub configured"}

    from .signing import sign_peer_message
    agent_payload = json.dumps(agent_data, ensure_ascii=False, sort_keys=True)
    peer_sig = sign_peer_message(agent_payload)

    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{hub_url}/peers/sync",
                json={"agent": agent_data, "peer_sig": peer_sig},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("notify hub agent_register failed for %s: %s",
                       agent_data.get("agent_id", ""), e)
        return {"status": "error", "error": str(e)}


# ── 联邦 API 服务端（管理服 Hub 侧）─────────────────────


async def get_agents_registry() -> dict:
    """返回全量 Agent 注册表供采购/销售底座拉取（GET /peers/agents/registry）"""
    from common.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT agent_id, ain, name, category, subcategory, capabilities, "
            f"server_host, server_ip, status, trust_level, metadata "
            f"FROM {hcfg.get_schema_name()}.agents WHERE status = 'active'"
        )
    agents = [dict(r) for r in rows]
    # 序列化 JSON 字段
    for a in agents:
        if isinstance(a.get("capabilities"), str):
            a["capabilities"] = json.loads(a["capabilities"])
        if isinstance(a.get("metadata"), str):
            a["metadata"] = json.loads(a["metadata"])
    return {"agents": agents, "count": len(agents)}


async def sync_agent_directory(body: dict) -> bool:
    """接收 Agent 目录同步数据（POST /peers/sync）

    两种模式：
    - 单条注册：body = {"agent": {...}} （注册/心跳时推送单条）
    - 整表快照：body = {"sync_type": "full_snapshot", "host": 主机名,
      "host_ip": 内网IP, "agents": [{...}, ...]}（非管理服定时整表上报）
      管理服仅做目录同步（upsert），不修改 agent 状态。状态由各底座自己管理。
    """
    import json as _json

    # 整表快照模式
    agents = body.get("agents")
    if body.get("sync_type") == "full_snapshot" and isinstance(agents, list):
        peer_host = body.get("host", "")
        peer_host_ip = body.get("host_ip", "")
        from common.db import get_pool
        pool = await get_pool()
        schema = hcfg.get_schema_name()
        async with pool.acquire() as conn:
            for agent in agents:
                if not agent or not agent.get("agent_id"):
                    continue
                row_agent = dict(agent)
                if not row_agent.get("server_ip"):
                    row_agent["server_ip"] = peer_host_ip
                await _handle_agent_register({"agent": row_agent})
        logger.info("directory full_snapshot synced from %s (%s): %d agents",
                    peer_host, peer_host_ip, len(agents))
        return True

    # 单条注册模式（兼容原有调用）
    agent = body.get("agent", {})
    if not agent or not agent.get("agent_id"):
        return False
    await _handle_agent_register({"agent": agent})
    return True


async def list_active_peers() -> list[dict]:
    """返回在线 peer 列表（GET /peers/discover）"""
    engine = get_engine()
    return await engine.get_online_peers()


async def sync_negotiation(body: dict) -> bool:
    """谈判状态跨底座同步（POST /peers/negotiation/sync）"""
    # Phase 1: 占位，后续对接谈判引擎
    logger.info("negotiation sync received: %s", body.get("negotiation_id", ""))
    return True


async def _registry_sync_loop(peer_engine: PeersEngine):
    """后台任务：启动 60s 后首次拉取，之后每 24h 拉取管理服 Agent 注册总表。

    仅非管理底座执行。管理服自身是注册总表的权威来源，无需拉取。
    """
    if peer_engine.role == "management":
        logger.info("registry sync: management role, skip (authoritative source)")
        return

    INITIAL_DELAY = 60       # 启动后等待 60s（等待管理服处理完积压广播）
    DAILY_INTERVAL = 86400   # 24h 全量同步

    await asyncio.sleep(INITIAL_DELAY)
    logger.info("registry sync: initial pull from hub")
    await pull_agent_registry_from_hub()

    while peer_engine._running:
        await asyncio.sleep(DAILY_INTERVAL)
        if not peer_engine._running:
            break
        logger.info("registry sync: daily pull from hub")
        await pull_agent_registry_from_hub()
