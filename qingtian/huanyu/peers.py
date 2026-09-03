"""
寰宇 — 跨底座通信（社区版：基础 HTTP 路由）

社区版提供基础 HTTP 直连路由：
  - deliver_to_peer()        HTTP POST /peers/route 投递
  - check_peer_health()       HTTP 健康检查
  - notify_hub_agent_registered()  HTTP 通知管理服
  - pull_agent_registry_from_hub() 从管理服拉取注册表

企业版（peers_enterprise.py）增加：
  - Redis Pub/Sub 联邦通知
  - OSPF DR 选举（管理底座为 DR）
  - Gossip 注册表同步

加载策略：自动检测 peers_enterprise.py，有则加载企业版，无则使用社区版。
所有调用方统一 from huanyu.peers import ...，无需感知差异。
"""

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger("huanyu.peers")


# ── HTTP 路由（基础） ──────────────────────────────────


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


async def check_peer_health() -> list[dict]:
    """HTTP 直连检查所有在线底座健康状态"""
    import httpx
    from common.db import get_pool
    from . import config as hcfg

    pool = await get_pool()
    schema = hcfg.get_schema_name()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT peer_id, host, port, name, agents_count, last_heartbeat "
            f"FROM {schema}.peers WHERE status = 'active'"
        )
        peers = [dict(r) for r in rows]

    # P1 (R11): /peers/heartbeat 现已要求 peer_sig，健康检查请求也需签名。
    from .signing import sign_peer_message
    hb_body = {
        "peer_id": hcfg.get_peer_id(),
        "host": hcfg.get_peer_name(),
        "port": hcfg.get_peer_port(),
        "name": hcfg.get_peer_name(),
    }
    hb_body["peer_sig"] = sign_peer_message(
        json.dumps(hb_body, ensure_ascii=False, sort_keys=True))

    results = []
    for peer in peers:
        url = f"http://{peer['host']}:{peer['port']}/peers/heartbeat"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=hb_body)
                results.append({**peer, "reachable": resp.status_code == 200})
        except Exception:
            results.append({**peer, "reachable": False})
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {schema}.peers SET status = 'inactive' WHERE host = $1 AND port = $2",
                    peer["host"], peer["port"],
                )
    return results




async def receive_heartbeat(body: dict) -> bool:
    """社区版心跳接收：更新 peer 状态为 active。

    管理服收到子底座的心跳上报，将相应 peer 标记为 active。
    """
    peer_id = body.get("peer_id", "")
    host = body.get("host", "")
    port = body.get("port", 1996)
    if not peer_id:
        logger.warning("receive_heartbeat: missing peer_id")
        return False

    try:
        from common.db import get_pool
        from . import config as hcfg
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {hcfg.get_schema_name()}.peers
                    (peer_id, host, port, name, status, last_heartbeat)
                    VALUES ($1, $2, $3, $4, 'active', NOW())
                    ON CONFLICT (peer_id)
                    DO UPDATE SET
                        host = EXCLUDED.host,
                        port = EXCLUDED.port,
                        status = 'active',
                        last_heartbeat = NOW()
                """,
                peer_id, host, port, body.get("name", peer_id),
            )

        # ── 广播 peer 上线到 Redis，其他底座同步学习 ──
        try:
            from .peers import get_engine
            engine = get_engine()
            if engine._redis:
                await engine._redis.publish(
                    "huanyu:broadcast",
                    json.dumps({
                        "type": "peer_hello",
                        "peer_id": peer_id,
                        "host": host,
                        "port": port,
                        "name": body.get("name", peer_id),
                        "role": body.get("role", ""),
                        "agents_count": body.get("agents_count", 0),
                    }, ensure_ascii=False),
                )
        except Exception:
            pass
        logger.info(
            "heartbeat received from %s (%s:%d)",
            peer_id, host, port
        )
        return True
    except Exception as e:
        logger.exception(
            "receive_heartbeat failed for %s", peer_id
        )
        return False


async def notify_hub_agent_registered(agent_data: dict) -> dict:
    """HTTP 直推：通知管理服新 Agent 注册"""
    from . import config as hcfg
    from .signing import sign_peer_message

    hub_url = hcfg.get_hub_endpoint()
    if not hub_url:
        return {"status": "skipped", "reason": "no hub configured"}

    import httpx
    agent_payload = json.dumps(agent_data, ensure_ascii=False, sort_keys=True)
    peer_sig = sign_peer_message(agent_payload)
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


async def push_local_agents_to_hub() -> dict:
    """非管理底座：定时把本地全部 active agent（含 server_host 主机名 + server_ip 内网 IP）
    POST 到管理服 /peers/sync 整表上报。管理服比对后：新表内 agent 汇总，同底座不在新表 → inactive。
    """
    from . import config as hcfg
    from .signing import sign_peer_message
    from common.config import get as root_get, is_management

    # 管理服是 hub（汇总方），自身不上报
    if is_management():
        return {"status": "skipped", "reason": "management is hub"}

    hub_url = hcfg.get_hub_endpoint()
    if not hub_url:
        return {"status": "skipped", "reason": "no hub configured"}

    from common.db import get_pool
    peer_id = root_get("huanyu.peer_id", "") or root_get("peer_id", "") or root_get("host", "")
    port = root_get("huanyu.peer_port", 1996) or 1996
    host_name = root_get("host", peer_id)
    host_ip = root_get("host_ip", "")

    pool = await get_pool()
    schema = hcfg.get_schema_name()
    my_org = hcfg.get_org_id()  # 跨企业通讯：本底座企业码（P1-1 数据源）
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT agent_id, ain, name, category, subcategory, capabilities, "
            f"server_host, server_ip, status, trust_level, metadata, organization_id "
            f"FROM {schema}.agents WHERE status = 'active'"
        )
        # 批量补全跨底座 agent 的 server_ip：用管理服已同步的已知 IP
        # （DB 里跨底座 agent 此前可能已通过 pull 写入过 server_ip）。
        agent_ips: dict[str, str] = {}
        cross_rows = await conn.fetch(
            f"SELECT agent_id, server_ip FROM {schema}.agents "
            f"WHERE status = 'active' AND server_ip <> '' AND server_ip IS NOT NULL"
        )
        for cr in cross_rows:
            if cr["server_ip"]:
                agent_ips[cr["agent_id"]] = cr["server_ip"]

    agents = []
    for r in rows:
        d = dict(r)
        # 跨企业通讯（P1-1）：本地 agent 归属本底座企业码（DB 未回填时兜底），
        # Hub 侧据此按企业码寻址（cross_org_route.resolve_target_org）。
        if not d.get("organization_id"):
            d["organization_id"] = my_org
        # 上报时确保携带回发地址：server_host=主机名(定位), server_ip=内网IP(路由)。
        # 优先用 DB 已知 IP（跨底座 agent 此前已同步过），其次用本底座 IP（自有 agent）。
        if not d.get("server_ip"):
            known = agent_ips.get(d["agent_id"], "")
            if known:
                d["server_ip"] = known
            elif d.get("server_host") in (host_name, peer_id):
                d["server_ip"] = host_ip
        if isinstance(d.get("capabilities"), str):
            try:
                d["capabilities"] = json.loads(d["capabilities"])
            except Exception:
                d["capabilities"] = []
        if isinstance(d.get("metadata"), str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except Exception:
                d["metadata"] = {}
        agents.append(d)

    body = {
        "peer_id": peer_id,
        "host": host_name,
        "host_ip": host_ip,
        "port": port,
        "agents": agents,
        "sync_type": "full_snapshot",
    }
    payload_str = json.dumps(body, ensure_ascii=False, sort_keys=True)
    body["peer_sig"] = sign_peer_message(payload_str)

    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{hub_url}/peers/sync", json=body)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.warning("push local agents to hub failed: %s", e)
        return {"status": "error", "error": str(e)}


async def pull_agent_registry_from_hub() -> dict:
    """从管理服拉取全量 Agent 注册表，批量 upsert 到本地"""
    from . import config as hcfg

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
                            server_ip = EXCLUDED.server_ip,
                            status = EXCLUDED.status,
                            updated_at = NOW()""",
                    agent.get("agent_id"), agent.get("ain", ""), agent.get("public_key", ""),
                    agent.get("cert_fingerprint", ""), agent.get("name"),
                    agent.get("category"), agent.get("subcategory", ""), json.dumps(capabilities),
                    agent.get("server_host", ""), agent.get("server_ip", ""), agent.get("status", "active"),
                    agent.get("trust_level", "basic"), json.dumps(metadata),
                )
                synced += 1
            except Exception:
                logger.exception("upsert agent %s failed during registry pull", agent.get("agent_id"))
    return {"status": "ok", "synced": synced, "total": len(agents)}


async def get_online_peers() -> list[dict]:
    """获取在线底座列表"""
    from common.db import get_pool
    from . import config as hcfg
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT peer_id, host, port, name, agents_count, last_heartbeat "
            f"FROM {hcfg.get_schema_name()}.peers WHERE status = 'active'"
        )
        return [dict(r) for r in rows]


# ── 轻型引擎 ──────────────────────────────────────────


class _PeersEngine:
    """社区版引擎：仅提供 HTTP 路由，无 Redis 订阅。

    属性 _redis = None 供外部代码做能力检测：
      if engine._redis is not None → 有 Redis 能力
      if engine._redis is None     → 仅有 HTTP 路由
    """

    _redis = None
    _pubsub = None
    _running = False
    _task: Optional[asyncio.Task] = None
    _heartbeat_task: Optional[asyncio.Task] = None
    _incoming_handler = None

    # 心跳间隔（秒），应小于 peers_enterprise.py 的 5 分钟超时阈值
    HEARTBEAT_INTERVAL = 120

    @property
    def peer_id(self) -> str:
        from . import config as hcfg
        return hcfg.get_peer_id()

    @property
    def role(self) -> str:
        from common.config import get
        return get("role", "company")

    async def start(self):
        self._running = True
        logger.info(
            "peers engine started (community edition, peer_id=%s role=%s)",
            self.peer_id, self.role,
        )
        # 启动心跳发送器（向 hub 上报在线状态，防止被踢 offline）
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self):
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        logger.info("peers engine stopped (community edition, peer_id=%s)", self.peer_id)

    async def _heartbeat_loop(self):
        """后台心跳循环：定期向 hub POST /peers/heartbeat，防止被标记 offline。

        仅在配置了 hub 且 role != 'management' 时发送（管理服自身不需要给自己发心跳）。
        """
        # 启动后先等一小段时间，让服务完全就绪
        await asyncio.sleep(10)

        from . import config as hcfg
        hub_url = hcfg.get_hub_endpoint()
        if not hub_url:
            logger.debug("heartbeat: no hub configured, skipping heartbeat sender")
            return

        import httpx
        from .signing import sign_peer_message
        while self._running:
            try:
                _hb = {
                    "peer_id": self.peer_id,
                    "host": self.peer_id,
                    "port": hcfg.get_peer_port(),
                    "name": hcfg.get_peer_name(),
                    "role": self.role,
                }
                # review(2026-08-24 P0-6): 心跳补 peer_sig（接收侧已收紧为缺签拒绝）
                _hb["peer_sig"] = sign_peer_message(
                    json.dumps(_hb, ensure_ascii=False, sort_keys=True)
                )
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        f"{hub_url}/peers/heartbeat",
                        json=_hb,
                    )
                    if resp.status_code != 200:
                        logger.warning("heartbeat to hub failed: HTTP %s", resp.status_code)
            except Exception:
                logger.debug("heartbeat to hub unreachable (will retry in %ss)", self.HEARTBEAT_INTERVAL)

            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    def set_incoming_handler(self, handler):
        self._incoming_handler = handler

    async def notify_base(self, target_role: str, data: dict) -> int:
        """社区版：无 Redis，通知静默丢弃"""
        return 0

    async def broadcast(self, data: dict) -> int:
        """社区版：无 Redis，广播静默丢弃"""
        return 0

    async def notify_sync(self, agent_id: str, action: str) -> int:
        """社区版：无 Redis，同步通知静默丢弃"""
        return 0

    async def notify_upgrade(self, version: str, urgency: str, cve: str = "") -> int:
        """社区版：无 Redis，升级通知静默丢弃"""
        return 0


_engine: Optional[_PeersEngine] = None


def get_engine() -> _PeersEngine:
    """获取引擎单例

    社区版返回轻型 _PeersEngine（无 Redis）。
    企业版（自动加载）返回完整 PeersEngine。
    """
    global _engine
    if _engine is None:
        _engine = _PeersEngine()
    return _engine


# ── 企业版扩展 ─────────────────────────────────────────
# 如果 peers_enterprise.py 存在且依赖满足，自动加载企业版函数覆盖社区版。
# 调用方统一 from huanyu.peers import ...，无需感知差异。

try:
    # 使用相对导入，避免触发 peers_enterprise.py 顶层 import 提前执行
    import importlib
    _ent = importlib.import_module(".peers_enterprise", __package__)
    # 企业版优先使用的函数
    _ENT_NAMES = [
        "PeersEngine",
        "get_engine",
        "handle_incoming_notification",
        "receive_heartbeat",
        "list_active_peers",
        "sync_agent_directory",
        "get_agents_registry",
        "sync_negotiation",
        "_registry_sync_loop",
    ]
    for _name in _ENT_NAMES:
        if hasattr(_ent, _name):
            globals()[_name] = getattr(_ent, _name)
    logger.info("peers: enterprise edition loaded (OSPF DR + Gossip + Redis)")
except Exception:
    logger.info("peers: community edition (basic HTTP routing)")
