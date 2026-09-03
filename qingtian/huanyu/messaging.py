"""
寰宇 — 消息服务
收发、收件箱、对话历史、批量操作、幂等保证
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from . import config as hcfg
from .signing import sign_message, verify_message, sign_peer_message, verify_peer_message
from .gbz_protocol import GBZEnvelope, get_dual_identity

logger = logging.getLogger("huanyu.messaging")

SCHEMA = hcfg.get_schema_name()


def _trace_enabled() -> bool:
    from common.config import get as _cfg
    return _cfg("gateway.trace.enabled", True)  # 联调期默认开


def _trace(msg: str, *args) -> None:
    if _trace_enabled():
        logger.warning("[trace] " + msg, *args)

# trust_level → tier 映射
TRUST_TO_TIER = {"basic": "free", "verified": "pro", "trusted": "enterprise", "admin": "alliance"}


async def _get_agent_tier(agent_id: str) -> str:
    """查询 agent 的 trust_level → tier"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        trust = await conn.fetchval(
            f"SELECT trust_level FROM {SCHEMA}.agents WHERE agent_id = $1", agent_id)
    return TRUST_TO_TIER.get(trust, "free")


def _now():
    return datetime.now(timezone.utc)


async def _resolve_agent_id(conn, identifier: str) -> str:
    """将 agent 名称/ain/agent_id 统一解析为 agent_id

    查找顺序：agent_id → ain → name。均未匹配则返回原值。
    """
    row = await conn.fetchrow(
        f"SELECT agent_id FROM {SCHEMA}.agents WHERE agent_id = $1", identifier)
    if row:
        return identifier
    row = await conn.fetchrow(
        f"SELECT agent_id FROM {SCHEMA}.agents WHERE ain = $1", identifier)
    if row:
        return row["agent_id"]
    row = await conn.fetchrow(
        f"SELECT agent_id FROM {SCHEMA}.agents WHERE name = $1", identifier)
    if row:
        return row["agent_id"]
    return identifier


def _make_idempotency_key(from_agent: str, to_agent: str, message_type: str, payload_str: str) -> str:
    """SHA256(from:to:type:payload) — 基于消息内容去重，不含随机值"""
    raw = f"{from_agent}:{to_agent}:{message_type}:{payload_str}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _build_qacp_nonce(peer_id: str = "") -> str:
    """生成 QACP nonce：base64url(timestamp_ms || random_8bytes || peer_hash_4bytes) — 20 字节"""
    import base64
    import os
    import struct
    import time
    ts = int(time.time() * 1000)
    rand = os.urandom(8)
    ts_bytes = struct.pack(">Q", ts)
    if peer_id:
        peer_hash = hashlib.sha256(peer_id.encode()).digest()[:4]
    else:
        peer_hash = os.urandom(4)
    raw = ts_bytes + rand + peer_hash
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def serialize_datetimes(d: dict) -> dict:
    """将 dict 中的 datetime/UUID 转为可 JSON 序列化的字符串"""
    from datetime import datetime
    from uuid import UUID
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, UUID):
            d[k] = str(v)
    return d


def _to_qacp_response(result: dict, from_agent: str, to_agent: str, tier: str = "free") -> dict:
    """将消息结果丰富为 QACP 兼容格式"""
    return {
        **serialize_datetimes(result),
        "qacp": {
            "version": "0.4",
            "algorithm": "ed25519",
            "nonce": _build_qacp_nonce(),
            "from_ain": from_agent,
            "to_ain": to_agent,
            "tier": tier,
        },
    }


# ── 异步跨服转发（后台任务，不阻塞调用方）─────────────


async def _resolve_forward_ip(conn, target_host: str, peer_host: str) -> str:
    """方案甲 — 跨底座精准回发地址：优先用 agents.server_ip（WG 内网 IP）覆盖主机名，
    解决 peers.host 为主机名导致 DNS 解析失败的问题。

    - target_host = 目标 agent 的 server_host（主机名）
    - peer_host   = peers 表里的 host（可能与 server_host 一致或不同）
    返回非空 WG IP，否则返回 ""（调用方退回 peer 主机名）。
    """
    try:
        return await conn.fetchval(
            f"SELECT server_ip FROM {SCHEMA}.agents "
            f"WHERE (server_host = $1 OR server_host = $2) AND server_ip <> '' "
            f"LIMIT 1",
            target_host, peer_host or target_host,
        ) or ""
    except Exception:
        return ""


async def _resolve_forward_target(conn, target_host: str) -> tuple[str, int]:
    """解析跨底座转发目标 (route_host, route_port)。

    对 agent.server_host 与 peers.host 不一致（旧主机名残留）免疫：
    1. 优先用 agents.server_ip（方案甲权威 WG IP）作路由地址；
       port 从 peers 表按 host/IP 双匹配查，查不到用统一端口（1996）。
    2. 无 server_ip → 退回 peers 表按 hostname 查（active，再降级不限 status）。
    3. 两者都查不到 → 返回 ("", 0)，调用方标记 failed。
    """
    from common.config import get as root_get

    forward_ip = await _resolve_forward_ip(conn, target_host, target_host)
    if forward_ip:
        peer = await conn.fetchrow(
            f"SELECT port FROM {SCHEMA}.peers "
            f"WHERE (name = $1 OR peer_id = $1 OR host = $1 "
            f"   OR name = $2 OR peer_id = $2 OR host = $2) "
            f"AND status = 'active' LIMIT 1",
            target_host, forward_ip,
        )
        port = peer["port"] if peer else (root_get("huanyu.peer_port", 1996) or 1996)
        return forward_ip, port

    peer = await conn.fetchrow(
        f"SELECT host, port FROM {SCHEMA}.peers "
        f"WHERE (name = $1 OR peer_id = $1 OR host = $1) AND status = 'active'",
        target_host,
    )
    if not peer:
        # 降级：peer 因心跳超时被标记 offline/inactive，但 WG 仍可能通
        peer = await conn.fetchrow(
            f"SELECT host, port FROM {SCHEMA}.peers "
            f"WHERE (name = $1 OR peer_id = $1 OR host = $1) LIMIT 1",
            target_host,
        )
    if not peer:
        return "", 0
    return peer["host"], peer["port"]


async def _async_forward(
    msg_id: str,
    resolved_from: str,
    resolved_to: str,
    message_type: str,
    payload: dict,
    priority: str,
    sig: str,
    idem_key: str,
    reply_to: Optional[str],
    negotiation_id: Optional[str],
    target_host: str,
):
    """后台异步跨底座转发。

    由 send_message 创建为 asyncio.Task，不阻塞调用方。
    转发成功 → delivery_status = delivered
    转发失败 → delivery_status = failed（留待 cron 重试）
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 🧭 目标解析对 hostname 不一致免疫：优先 agents.server_ip（WG IP）直连，
            # 避免 agent.server_host（旧名）与 peers.host（新名）对不上就转发失败。
            route_host, route_port = await _resolve_forward_target(conn, target_host)
            if not route_host:
                logger.warning("AsyncForward: 目标底座 %s 无法解析（peers 无此 host，且无 server_ip）", target_host)
                await mark_delivery_status(msg_id, "failed")
                return

            # ── 确定转发目标 ──────────────────────────────────
            # Hub relay 模式：走管理服中转（解决容器无法直连其他 WG IP 的问题）
            hub_url = hcfg.get_hub_endpoint()
            hub_relay = hcfg.get_hub_relay_enabled()
            if hub_relay and hub_url:
                forward_url = f"{hub_url}/peers/hub/forward"
                relay_target_host = route_host
                relay_target_port = route_port
            else:
                forward_url = f"http://{route_host}:{route_port}/peers/route"
                relay_target_host = None
                relay_target_port = None

            forward_data = {
                "msg_id": msg_id,
                "from": resolved_from,
                "to": resolved_to,
                "message_type": message_type,
                "payload": payload,
                "priority": priority,
                "signature": sig,
                "idempotency_key": idem_key,
                "reply_to": reply_to,
                "negotiation_id": negotiation_id,
                "nonce": _build_qacp_nonce(),
            }
            if relay_target_host:
                forward_data["_relay_target_host"] = relay_target_host
                forward_data["_relay_target_port"] = relay_target_port
                _trace("_async_forward relay via hub → %s:%s msg=%s", relay_target_host, relay_target_port, msg_id[:8])
            else:
                _trace("_async_forward direct → %s msg=%s", forward_url, msg_id[:8])

            payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            # review(2026-08-25 Bug③ 统一口径): peer_sig 一律全局密钥，不再按 target_host 派生。
            # 派生密钥本就由三台同值的全局密钥派生，不提供真实隔离，只贡献"发送端
            # target_host(如目录残留 agent_id)≠接收端本地 host"的配对失败面——quote
            # 回传被拒即此。接收端多候选验证(本地派生→全局)保持不动，全局签名平滑命中。
            forward_data["peer_sig"] = sign_peer_message(payload_str)
            if relay_target_host:
                # P1 (R11): hub 中转入口本不该公开，中继请求本身也需签名
                # （_relay_sig，全局密钥；签名覆盖含 _relay_target_* 的整个请求体）。
                relay_canonical = {k: v for k, v in forward_data.items() if k != "_relay_sig"}
                forward_data["_relay_sig"] = sign_peer_message(
                    json.dumps(relay_canonical, ensure_ascii=False, sort_keys=True))

            # ── GBZ 协议封装：跨底座转发包裹国标格式 ──
            async with pool.acquire() as conn2:
                # agent_id → ain → gbz185_id
                from_ain = await conn2.fetchval(
                    f"SELECT ain FROM {SCHEMA}.agents WHERE agent_id = $1",
                    resolved_from,
                )
                to_ain = await conn2.fetchval(
                    f"SELECT ain FROM {SCHEMA}.agents WHERE agent_id = $1",
                    resolved_to,
                )
                from_gbz = None
                to_gbz = None
                if from_ain:
                    from_gbz = await conn2.fetchval(
                        f"SELECT gbz185_id FROM {SCHEMA}.gbz185_mappings "
                        f"WHERE ain = $1 ORDER BY issued_at DESC LIMIT 1",
                        from_ain,
                    )
                if to_ain:
                    to_gbz = await conn2.fetchval(
                        f"SELECT gbz185_id FROM {SCHEMA}.gbz185_mappings "
                        f"WHERE ain = $1 ORDER BY issued_at DESC LIMIT 1",
                        to_ain,
                    )
            forward_data["from_gbz185_id"] = from_gbz or ""
            forward_data["to_gbz185_id"] = to_gbz or ""

            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(forward_url, json=forward_data)
                if resp.status_code == 200:
                    await mark_delivery_status(msg_id, "delivered")
                    logger.info("AsyncForward: %s → %s delivered", msg_id[:8], target_host)
                else:
                    await mark_delivery_status(msg_id, "failed")
                    logger.warning(
                        "AsyncForward: %s → %s HTTP %d",
                        msg_id[:8], target_host, resp.status_code,
                    )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        import traceback
        logger.warning("AsyncForward: %s → %s failed: %s\n%s", msg_id[:8], target_host, e, traceback.format_exc())
        try:
            await mark_delivery_status(msg_id, "failed")
        except Exception:
            pass


# ── 跨企业通讯（Hub E2EE 信封通道，2026-08-20 贪狼接线）──────

async def _ain_of(conn, agent_id: str) -> str:
    """取 agent 的 AIN（信封路由头用）；查不到回落 agent_id。"""
    return await conn.fetchval(
        f"SELECT ain FROM {SCHEMA}.agents WHERE agent_id = $1", agent_id) or agent_id


async def _remote_org_of_agent(agent_id: str) -> str:
    """Hub 目录查目标 agent 归属企业码（目标企业在别的底座）。"""
    hub = hcfg.get_hub_endpoint()
    if not hub:
        return ""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as hc:
            resp = await hc.get(f"{hub}/v1/huanyu/agents/{agent_id}")
            if resp.status_code == 200:
                agent = (resp.json().get("agent") or {})
                return agent.get("organization_id") or ""
    except Exception:
        pass
    return ""


# 企业公钥目录缓存：org_id → (keys_dict, fetched_monotonic)，TTL 5min（降 Hub 热路径查询）
_org_keys_cache: dict[str, tuple[dict | None, float]] = {}
_ORG_KEYS_TTL = 300
_ORG_KEYS_CACHE_MAX = 10000  # 容量上限：防恶意/大量企业导致进程内存膨胀


async def _get_org_pubkeys(org_id: str) -> dict | None:
    """Hub 目录取企业公钥 {ed25519_pubkey, x25519_static_pub, status}，带 5min 进程内缓存。"""
    if not org_id:
        return None
    hub = hcfg.get_hub_endpoint()
    if not hub:
        return None
    now = time.monotonic()
    hit = _org_keys_cache.get(org_id)
    if hit and now - hit[1] < _ORG_KEYS_TTL:
        return hit[0]
    import httpx
    keys: dict | None = None
    try:
        async with httpx.AsyncClient(timeout=5) as hc:
            resp = await hc.get(f"{hub}/v1/huanyu/orgs/{org_id}/keys")
            if resp.status_code == 200:
                org = (resp.json().get("org") or {})
                keys = {
                    "ed25519_pubkey": org.get("ed25519_pubkey", ""),
                    "x25519_static_pub": org.get("x25519_static_pub", ""),
                    "status": org.get("status", "active"),
                }
    except Exception:
        pass
    # 失败（None）不缓存：避免 Hub 短暂不可用时 5min 内持续失败；仅成功缓存
    if keys is not None:
        if len(_org_keys_cache) >= _ORG_KEYS_CACHE_MAX:
            stale = [k for k, (_, t) in _org_keys_cache.items() if now - t >= _ORG_KEYS_TTL]
            for k in stale:
                del _org_keys_cache[k]
            if len(_org_keys_cache) >= _ORG_KEYS_CACHE_MAX:
                # 清最旧一半（dict 按插入序，近似 LRU）
                _org_keys_cache = dict(list(_org_keys_cache.items())[-_ORG_KEYS_CACHE_MAX // 2:])
        _org_keys_cache[org_id] = (keys, now)
    return keys


# 本底座信封 nonce 去重（进程内 LRU，TTL 10min；跨企业权威去重在 Hub 端 Redis）
_envelope_nonce_lru = None


def _seen_envelope_nonce(nonce: str) -> bool:
    """记录信封 nonce 并判断是否重放。返回 False=首次放行，True=重放拒绝。"""
    global _envelope_nonce_lru
    if _envelope_nonce_lru is None:
        from .e2ee import NonceLRU
        _envelope_nonce_lru = NonceLRU()
    return _envelope_nonce_lru.seen(nonce)


async def hub_client_send(msg: dict) -> bool:
    """经企业→Hub 长连发消息。未连接/发送失败 → False（调用方标 failed）。"""
    from .hub_client import get_hub_client
    client = get_hub_client()
    if not client:
        _trace("hub_client_send no client (org 未接入 Hub 长连)")
        return False
    return await client.send_json(msg)


# ── ack 等待机制（可靠投递：两层 ack，设计 §十五）────────────────
# nonce → {ack_type: Future}；_send_cross_org 等 ack，hub_client 收到 ack 时 resolve
_ack_waiters: dict[str, dict[str, asyncio.Future]] = {}


def _register_ack(nonce: str, ack_type: str) -> asyncio.Future:
    fut = asyncio.get_running_loop().create_future()
    _ack_waiters.setdefault(nonce, {})[ack_type] = fut
    return fut


def _resolve_ack(nonce: str, ack_type: str) -> None:
    """收到 ack 时触发等待方（幂等，重复 ack 无害）。"""
    waiters = _ack_waiters.get(nonce)
    if waiters:
        fut = waiters.get(ack_type)
        if fut and not fut.done():
            fut.set_result(True)


async def _wait_ack(nonce: str, ack_type: str, timeout: float) -> bool:
    """等待指定 nonce 的指定类型 ack，超时返回 False。"""
    fut = _register_ack(nonce, ack_type)
    try:
        await asyncio.wait_for(fut, timeout=timeout)
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return False
    finally:
        waiters = _ack_waiters.get(nonce)
        if waiters:
            waiters.pop(ack_type, None)
            if not waiters:
                _ack_waiters.pop(nonce, None)


async def _next_org_seq() -> int:
    """org 内自增 seq（resync 对齐点，设计 §15.4 / 贪狼决策2）。

    Redis INCR 跨进程可靠；Redis 缺失 fallback 时间戳（微秒，非严格单调但可用）。
    """
    try:
        from .pubsub import _get_redis
        redis = await _get_redis()
        if redis is not None:
            return int(await redis.incr(f"hub:seq:{hcfg.get_org_id()}"))
    except Exception:
        pass
    return int(time.time() * 1_000_000)


async def _send_cross_org(msg_id: str, from_org: str, to_org: str,
                          from_ain: str, to_ain: str, payload: dict) -> None:
    """跨企业发送核心（可靠投递状态机，设计 §十五/落地 §8.2）。

    pending → sent → hub_acked → delivered；任一步失败 → failed（待重试）。

    阶段1：离线静态公钥加密（每条独立一次性密钥）；会话密钥（在线 PFS）阶段2 接入。
    """
    from . import e2ee
    try:
        await mark_delivery_status(msg_id, "pending")
        # 1. 加密 payload：取目标企业 X25519 静态公钥（Hub 目录）
        keys = await _get_org_pubkeys(to_org)
        to_static = (keys or {}).get("x25519_static_pub", "")
        if not to_static:
            _trace("[trace] _send_cross_org NO_STATIC_PUB org=%s msg=%s", to_org, msg_id[:8])
            await mark_delivery_status(msg_id, "failed")
            return
        # nonce 先定：同时用作离线 KDF info 与信封 nonce（收发两边一致）
        nonce = e2ee.crypto.generate_msg_nonce()
        enc = e2ee.encrypt_offline_message(
            bytes.fromhex(to_static),
            json.dumps(payload, ensure_ascii=False).encode(),
            from_org, to_org, nonce,
        )
        body_b64 = base64.b64encode(json.dumps(enc, ensure_ascii=False).encode()).decode()
        # 2. 组信封 + 企业 Ed25519 签名
        seq = await _next_org_seq()
        env = e2ee.build_envelope(
            from_org, to_org, from_ain, to_ain, body_b64,
            hcfg.get_org_sign_key(), nonce=nonce, msg_id=msg_id, seq=seq,
        )
        # 3. 经 WS 发 Hub
        ok = await hub_client_send({"type": "msg", "envelope": env})
        if not ok:
            await mark_delivery_status(msg_id, "failed")
            return
        await mark_delivery_status(msg_id, "sent")
        # 4. 等 hub_ack（Hub 已收）
        if not await _wait_ack(nonce, "hub_ack", timeout=10):
            _trace("[trace] _send_cross_org NO_HUB_ACK msg=%s", msg_id[:8])
            await mark_delivery_status(msg_id, "failed")
            return
        await mark_delivery_status(msg_id, "hub_acked")
        # 5. 等 end_ack（B 已收并处理）
        if not await _wait_ack(nonce, "end_ack", timeout=60):
            _trace("[trace] _send_cross_org NO_END_ACK msg=%s", msg_id[:8])
            await mark_delivery_status(msg_id, "failed")
            return
        await mark_delivery_status(msg_id, "delivered")
        _trace("[trace] _send_cross_org delivered msg=%s org=%s->%s", msg_id[:8], from_org, to_org)
    except Exception as e:
        logger.warning("send_cross_org failed msg=%s err=%s", msg_id[:8], e)
        try:
            await mark_delivery_status(msg_id, "failed")
        except Exception:
            pass


def _decrypt_org_body(from_org: str, to_org: str, env: dict) -> dict:
    """解密跨企业信封 body。阶段1：离线一次性密钥（本企业静态私钥解）。"""
    from . import e2ee
    static_priv = hcfg.get_org_static_priv()
    if not static_priv:
        raise ValueError("HUANYU_ORG_STATIC_PRIV 未配置，无法解密跨企业消息")
    body = json.loads(base64.b64decode(env["body"]))
    plain = e2ee.decrypt_offline_message(
        static_priv, body["o_pub"], body, from_org, to_org, env["nonce"])
    return json.loads(plain)


# 连续窗口扫描上限（贪狼 P2：大连续块收尾避免单消息 O(块长) Redis 往返）
_RECV_SCAN_MAX = 512


async def _record_received_seq(from_org: str, seq: int) -> None:
    """B 端记录已收 seq，维护连续窗口 last_continuous（设计 §15.4 / 贪狼决策3）。

    连续窗口（非 max）：B 收到 seq=5 但 3 丢，last_continuous 停在 2，resync 补推 3 起。
    扫描带上限（贪狼 P2）：超限停止，剩余留待下次补收触发，避免单消息 O(块长)。
    """
    if seq <= 0:
        return
    my_org = hcfg.get_org_id()
    try:
        from .pubsub import _get_redis
        redis = await _get_redis()
        if redis is None:
            return
        zkey = f"hub:recv:{my_org}:{from_org}"
        ckey = f"hub:lastc:{my_org}:{from_org}"
        await redis.zadd(zkey, {str(seq): seq})
        # TTL 7 天（贪狼 P2：只增不清会线性涨，定期清理旧 seq）
        await redis.expire(zkey, 7 * 86400)
        last = int(await redis.get(ckey) or 0)
        cursor = last + 1
        scanned = 0
        while scanned < _RECV_SCAN_MAX and await redis.zscore(zkey, str(cursor)) is not None:
            cursor += 1
            scanned += 1
        last_continuous = cursor - 1
        if last_continuous > last:
            await redis.set(ckey, str(last_continuous))
    except Exception:
        pass


async def _get_all_last_continuous() -> dict:
    """返回本企业从所有合作方收到的连续窗口 {from_org: last_continuous}（resync 上报用）。"""
    my_org = hcfg.get_org_id()
    result: dict[str, int] = {}
    try:
        from .pubsub import _get_redis
        redis = await _get_redis()
        if redis is None:
            return result
        keys = await redis.keys(f"hub:lastc:{my_org}:*")
        for key in keys:
            from_org = key.rsplit(":", 1)[-1]
            val = await redis.get(key)
            if val:
                result[from_org] = int(val)
    except Exception:
        pass
    return result


async def handle_hub_envelope(msg: dict) -> None:
    """企业底座收 Hub 反向推的信封 → 验签 → 去重 → 解密 → 落库投递。

    hub_client.on_message 回调入口（main.py 启动时接线）。
    """
    from . import e2ee
    env = msg.get("envelope") or {}
    # end_ack 处理：B 确认收到，验 B 签名后 resolve 发送方等待
    if env.get("type") == "end_ack":
        from_org = env.get("from_org", "")
        keys = await _get_org_pubkeys(from_org)
        if keys and keys.get("status") == "active" and e2ee.verify_envelope(keys.get("ed25519_pubkey", ""), env):
            _resolve_ack(env.get("nonce", ""), "end_ack")
            _trace("[trace] hub_client end_ack nonce=%s", env.get("nonce", "")[:8])
        else:
            logger.warning("[trace] hub_client end_ack BAD_SIG/UNKNOWN from=%s", from_org)
        return
    if not e2ee.verify_envelope_schema(env):
        logger.warning("[trace] hub_client envelope BAD_SCHEMA from=%s", env.get("from_org", ""))
        return
    my_org = hcfg.get_org_id()
    from_org = env.get("from_org", "")
    if not my_org or from_org == my_org:
        logger.warning("[trace] hub_client envelope SELF/NO_ORG from=%s", from_org)
        return
    # 1. 验来源企业签名（Hub 目录取公钥，5min 缓存）
    keys = await _get_org_pubkeys(from_org)
    if not keys or keys.get("status") != "active":
        logger.warning("[trace] hub_client envelope UNKNOWN_ORG from=%s", from_org)
        return
    if not e2ee.verify_envelope(keys.get("ed25519_pubkey", ""), env):
        logger.warning("[trace] hub_client envelope BAD_SIG from=%s", from_org)
        return
    # 2. ts 窗口 ±5min
    if not e2ee.envelope_ts_valid(env.get("ts", "")):
        logger.warning("[trace] hub_client envelope BAD_TS from=%s", from_org)
        return
    # 3. nonce 去重（本底座只收一次）
    if _seen_envelope_nonce(env["nonce"]):
        logger.info("[trace] hub_client envelope REPLAY from=%s nonce=%s", from_org, env["nonce"][:8])
        return
    # 4. 解密 payload（阶段1：离线静态私钥）
    try:
        payload = _decrypt_org_body(from_org, my_org, env)
    except Exception as e:
        logger.warning("[trace] hub_client decrypt fail from=%s err=%s", from_org, e)
        return
    # 5. 落库 + 推本地 agent（idempotency 键 = org:{nonce}，重放不重复落库）
    _trace("[trace] hub_client received msg from=%s to_ain=%s nonce=%s",
           from_org, env.get("to_ain", ""), env["nonce"][:8])
    await insert_incoming_peer_message(
        msg_id=env.get("nonce", ""),
        from_agent=env.get("from_ain", ""),
        to_agent=env.get("to_ain", ""),
        message_type="info",
        payload=payload,
        priority="normal",
        signature=env.get("sig", ""),
        # 幂等键：优先用原始 msg_id（重试时不变，恰好一次）；无 msg_id 回落 nonce
        idempotency_key=f"msg:{env.get('msg_id')}" if env.get("msg_id") else f"org:{env.get('nonce', '')}",
    )
    # 记录已收 seq（维护连续窗口 last_continuous，供重连 resync 对齐）
    try:
        await _record_received_seq(from_org, int(env.get("seq", "0") or 0))
    except (ValueError, TypeError):
        pass
    # 回 end_ack 给 Hub（B 已收并处理，经 WS，Hub 转发给 A）
    try:
        end_ack_env = e2ee.build_envelope(
            my_org, from_org, env.get("to_ain", ""), env.get("from_ain", ""),
            base64.b64encode(b"{}").decode(),
            hcfg.get_org_sign_key(), mtype="end_ack", nonce=env["nonce"],
        )
        await hub_client_send({"type": "msg", "envelope": end_ack_env})
        _trace("[trace] hub_client sent end_ack nonce=%s", env["nonce"][:8])
    except Exception as e:
        logger.warning("[trace] hub_client end_ack send fail err=%s", e)


# ── 发送 ──────────────────────────────────────────────

async def send_message(
    from_agent: str,
    to_agent: str,
    message_type: str = "info",
    payload: Optional[dict] = None,
    priority: str = "normal",
    reply_to: Optional[str] = None,
    negotiation_id: Optional[str] = None,
    topic: str = "",
    idempotency_key: Optional[str] = None,
) -> dict:
    content = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    # 调用方显式幂等键优先（2026-08-11：投标 skill 进度广播按 (agent,target,text) 派生，
    # 同文本重复投递 → 同键 → 落库去重）；未提供时按 (from,to,type,content) 自动派生。
    idem_key = idempotency_key or _make_idempotency_key(from_agent, to_agent, message_type, content)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 幂等检查
        existing = await conn.fetchrow(
            f"SELECT message_id::text, status, delivery_status, created_at "
            f"FROM {SCHEMA}.messages WHERE idempotency_key = $1",
            idem_key,
        )
        if existing:
            tier = await _get_agent_tier(from_agent)
            return _to_qacp_response(
                {
                    "status": "duplicate",
                    "message_id": existing["message_id"],
                    "created_at": existing["created_at"].isoformat(),
                },
                from_agent, to_agent, tier,
            )

        resolved_from = await _resolve_agent_id(conn, from_agent)
        resolved_to = await _resolve_agent_id(conn, to_agent)

        # review(2026-08-16): 签名必须基于解析后的真实 agent_id——verify_message_integrity
        # 用 row['from_agent_id']/row['to_agent_id']（已解析值）验签；原实现用原始入参
        # （可能是名称/别名/ain）签名 → 一旦调用方传非规范 ID，验签恒失败。
        sig = sign_message(resolved_from, resolved_to, message_type, content)

        # 判断目标底座
        from common.config import get as root_get
        my_host = root_get("host", "")
        target_host = await conn.fetchval(
            f"SELECT server_host FROM {SCHEMA}.agents WHERE agent_id = $1", resolved_to)
        # 本地查不到 → fallback 到管理服全系目录
        if not target_host:
            try:
                hub = hcfg.get_hub_endpoint()
                if hub:
                    import httpx
                    async with httpx.AsyncClient(timeout=5) as hc:
                        resp = await hc.get(f"{hub}/v1/huanyu/agents/{resolved_to}")
                        if resp.status_code == 200:
                            agent_data = resp.json()
                            target_host = agent_data.get("server_host", "")
            except Exception:
                pass
        is_cross_base = target_host and target_host != my_host

        # ── 跨企业通讯判定（优先于跨底座 WG 判定，2026-08-20 贪狼接线）──
        # 目标 agent 归属另一企业码 → 走 Hub E2EE 信封通道（不经 WG IP 直连）。
        my_org = hcfg.get_org_id()
        target_org = ""
        is_cross_org = False
        if my_org and hcfg.get_cross_org_enabled():
            target_org = await conn.fetchval(
                f"SELECT organization_id FROM {SCHEMA}.agents "
                f"WHERE agent_id = $1 AND organization_id IS NOT NULL AND organization_id <> ''",
                resolved_to,
            )
            if not target_org:
                # 本地查不到 → Hub 目录兜底（目标企业在别的底座）
                target_org = await _remote_org_of_agent(resolved_to)
            is_cross_org = bool(target_org and target_org != my_org)

        if is_cross_org:
            delivery = "cross_org"
        elif is_cross_base:
            delivery = "pending"
        else:
            delivery = "local"
        _trace("send_message from=%s to=%s type=%s delivery=%s target_org=%s target_host=%s",
               resolved_from, resolved_to, message_type, delivery, target_org or "", target_host or "local")

        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.messages
                (from_agent_id, to_agent_id, message_type, payload, priority,
                 reply_to, negotiation_id, signature, idempotency_key, delivery_status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT DO NOTHING
                RETURNING message_id::text, from_agent_id::text, to_agent_id::text,
                          message_type, status, delivery_status, created_at""",
            resolved_from, resolved_to, message_type, json.dumps(payload or {}),
            priority, reply_to, negotiation_id, sig, idem_key, delivery,
        )
        if not row:
            row = await conn.fetchrow(
                f"SELECT message_id::text, from_agent_id::text, to_agent_id::text, "
                f"message_type, status, delivery_status, created_at "
                f"FROM {SCHEMA}.messages WHERE idempotency_key = $1",
                idem_key,
            )
        result = dict(row)
        result["idempotency_key"] = idem_key

        # 跨企业转发：Hub E2EE 信封通道（异步后台任务，不阻塞调用方）
        if is_cross_org:
            asyncio.create_task(_send_cross_org(
                msg_id=result["message_id"],
                from_org=my_org,
                to_org=target_org,
                from_ain=await _ain_of(conn, resolved_from),
                to_ain=await _ain_of(conn, resolved_to),
                payload=payload or {},
            ))
        # 跨底座转发：异步后台任务，不阻塞调用方
        elif is_cross_base and target_host:
            asyncio.create_task(_async_forward(
                msg_id=result["message_id"],
                resolved_from=resolved_from,
                resolved_to=resolved_to,
                message_type=message_type,
                payload=payload or {},
                priority=priority,
                sig=sig,
                idem_key=idem_key,
                reply_to=reply_to,
                negotiation_id=negotiation_id,
                target_host=target_host,
            ))
        elif is_cross_base and not target_host:
            result["forward_error"] = f"目标底座未在 agents 表中注册 server_host"

        # ── 推送通知目标 Agent（WS 在线 → 实时推，离线 → inbox）──
        if delivery == "local":
            asyncio.create_task(_push_to_agent(
                agent_id=resolved_to,
                event={
                    "type": message_type,
                    "from_agent": resolved_from,
                    "payload": payload or {},
                    "message_id": result["message_id"],
                },
            ))

    tier = await _get_agent_tier(resolved_from)
    return _to_qacp_response(result, resolved_from, resolved_to, tier)


async def broadcast_to_category(
    from_agent: str,
    target_category: str,
    message_type: str = "info",
    payload: Optional[dict] = None,
    priority: str = "normal",
    exclude_self: bool = True,
) -> dict:
    """按 category 广播消息给所有 active Agent。

    遍历所有 status='active' 且 category=target_category 的 agent，
    逐个发 send_message。返回成功/失败统计。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        resolved_from = await _resolve_agent_id(conn, from_agent)
        rows = await conn.fetch(
            f"SELECT agent_id FROM {SCHEMA}.agents "
            f"WHERE category = $1 AND status = 'active'",
            target_category,
        )

    targets = [r["agent_id"] for r in rows]
    results = {"total": len(targets), "sent": 0, "failed": 0, "results": []}

    for target in targets:
        try:
            r = await send_message(
                from_agent=resolved_from,
                to_agent=target,
                message_type=message_type,
                payload=payload,
                priority=priority,
            )
            results["sent"] += 1
            results["results"].append({"to": target, "status": r.get("status")})
        except Exception as e:
            results["failed"] += 1
            results["results"].append({"to": target, "status": "error", "error": str(e)})

    return results


async def mark_delivery_status(message_id: str, delivery: str) -> dict:
    """标记跨底座投递状态：delivered / failed"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.messages SET delivery_status = $1 WHERE message_id = $2",
            delivery, message_id,
        )
    return {"status": "ok", "message_id": message_id, "delivery_status": delivery}


async def get_pending_deliveries(limit: int = 50) -> list[dict]:
    """查询待重试的跨底座消息（delivery_status = 'pending' 或 'failed'）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT message_id::text, from_agent_id::text, to_agent_id::text,
                       message_type, payload::text, priority, signature,
                       idempotency_key, reply_to, negotiation_id, created_at
                FROM {SCHEMA}.messages
                WHERE delivery_status IN ('pending', 'failed')
                ORDER BY created_at
                LIMIT $1""",
            limit,
        )
        result = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("payload"), str):
                try:
                    d["payload"] = json.loads(d["payload"])
                except (json.JSONDecodeError, TypeError):
                    d["payload"] = {}
            result.append(d)
        return result


async def retry_delivery(msg_id: str) -> dict:
    """重试单条跨底座消息投递。

    供 cron 重试任务调用。查找目标 peer 并 POST 转发。
    成功 → delivered，失败 → 保持 failed 状态下次再试。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT message_id::text, from_agent_id::text, to_agent_id::text,
                       message_type, payload, priority, signature,
                       idempotency_key, reply_to, negotiation_id
                FROM {SCHEMA}.messages WHERE message_id = $1
                AND delivery_status IN ('pending', 'failed')""",
            msg_id,
        )
        if not row:
            return {"status": "skipped", "reason": "not_found_or_already_delivered"}

        msg = dict(row)
        payload = msg["payload"] or {}
        if isinstance(payload, str):
            payload = json.loads(payload)

        target_host = await conn.fetchval(
            f"SELECT server_host FROM {SCHEMA}.agents WHERE agent_id = $1",
            msg["to_agent_id"],
        )
        if not target_host:
            # P2 (R11): 无投递目标（agent 无 server_host）→ 永久无法投递，标记 failed，
            # 交给日清任务 7 天后清理——此前返回 skipped 且不改状态，永远卡 pending 无限累积。
            await conn.execute(
                f"UPDATE {SCHEMA}.messages SET delivery_status = 'failed' WHERE message_id = $1",
                msg_id,
            )
            return {"status": "failed", "reason": "target_agent_has_no_server_host"}

        # 🧭 与 _async_forward 一致：目标解析对 hostname 不一致免疫（优先 WG IP）
        route_host, route_port = await _resolve_forward_target(conn, target_host)
        if not route_host:
            # P2 (R11): 目标底座未注册/不可解析 → 同样标记 failed 待日清清理
            await conn.execute(
                f"UPDATE {SCHEMA}.messages SET delivery_status = 'failed' WHERE message_id = $1",
                msg_id,
            )
            return {"status": "failed", "reason": f"peer_not_found:{target_host}"}

    # 事务外执行 HTTP POST
    hub_url = hcfg.get_hub_endpoint()
    hub_relay = hcfg.get_hub_relay_enabled()
    if hub_relay and hub_url:
        forward_url = f"{hub_url}/peers/hub/forward"
        relay_target_host = route_host
        relay_target_port = route_port
    else:
        forward_url = f"http://{route_host}:{route_port}/peers/route"
        relay_target_host = None
        relay_target_port = None

    forward_data = {
        "msg_id": msg["message_id"],
        "from": msg["from_agent_id"],
        "to": msg["to_agent_id"],
        "message_type": msg["message_type"],
        "payload": payload,
        "priority": msg["priority"],
        "signature": msg["signature"],
        "idempotency_key": msg["idempotency_key"],
        "reply_to": msg.get("reply_to"),
        "negotiation_id": msg.get("negotiation_id"),
        "nonce": _build_qacp_nonce(),
    }
    if relay_target_host:
        forward_data["_relay_target_host"] = relay_target_host
        forward_data["_relay_target_port"] = relay_target_port

    payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    # review(2026-08-25 Bug③ 统一口径): 与 _async_forward 一致，peer_sig 一律全局密钥
    forward_data["peer_sig"] = sign_peer_message(payload_str)
    if relay_target_host:
        # P1 (R11): hub 中转入口本不该公开，中继请求本身也需签名
        # （_relay_sig，全局密钥；签名覆盖含 _relay_target_* 的整个请求体）。
        relay_canonical = {k: v for k, v in forward_data.items() if k != "_relay_sig"}
        forward_data["_relay_sig"] = sign_peer_message(
            json.dumps(relay_canonical, ensure_ascii=False, sort_keys=True))

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(forward_url, json=forward_data)
            if resp.status_code == 200:
                await mark_delivery_status(msg["message_id"], "delivered")
                logger.info("RetryDelivery: %s → %s delivered", msg_id[:8], target_host)
                return {"status": "delivered", "message_id": msg["message_id"]}
            else:
                logger.warning(
                    "RetryDelivery: %s → %s HTTP %d",
                    msg_id[:8], target_host, resp.status_code,
                )
                return {"status": "failed", "http_status": resp.status_code}
    except Exception as e:
        logger.warning("RetryDelivery: %s → %s error: %s", msg_id[:8], target_host, e)
        return {"status": "failed", "error": str(e)}


async def receive_peer_message(body: dict) -> dict:
    """接收跨底座消息 — /peers/route 入口

    自动识别格式：
      - 国标格式（含 senderRole）→ decode 解包为内部格式
      - 自有格式 → 直接使用（向后兼容）
    """
    # ── GBZ 格式检测与解码 ──
    if GBZEnvelope.is_gbz_format(body):
        logger.debug("收到国标格式跨底座消息，自动解码")
        internal = GBZEnvelope.decode(body)
        return await insert_incoming_peer_message(
            msg_id=body.get("idempotencyKey", ""),
            from_agent=internal["from_agent_id"],
            to_agent=internal["to_agent_id"],
            message_type=internal["message_type"],
            payload=internal["payload"],
            priority=internal["priority"],
            signature=internal["signature"],
            idempotency_key=internal["idempotency_key"],
        )

    # review(2026-08-16): 自有格式 peer_sig 验签（首次接入）。签名覆盖 payload 的规范
    # 序列化 json.dumps(payload, ensure_ascii=False, sort_keys=True)，与发送端
    # _async_forward/retry_delivery/peers.py 一致。GBZ 格式走 decode 后内部 signature，
    # 不在此处验。
    # review(2026-08-24 P0-6 收紧): 签名缺失原为"警告放行（兼容旧版转发端）"——/peers/*
    # 在网关白名单内无 Bearer 鉴权，缺签放行等于开放注入面。所有在用发送端
    # （deliver_to_peer/_async_forward/retry_delivery）均已带 peer_sig，收紧为缺失即拒绝。
    _payload_str = json.dumps(body.get("payload", {}), ensure_ascii=False, sort_keys=True)
    _peer_sig = body.get("peer_sig", "")
    if _peer_sig:
        from common.config import get as _root_get
        _peer_host = _root_get("host", "")
        _peer_ok = verify_peer_message(_payload_str, _peer_sig, _peer_host)
        if not _peer_ok and _peer_host:
            # 兼容发送端 target_host（agents 表 server_host）与本地 host 配置不一致的场景
            _peer_ok = verify_peer_message(_payload_str, _peer_sig, "")
        if not _peer_ok:
            logger.warning(
                "receive_peer_message: peer_sig 校验失败，拒绝 from=%s to=%s type=%s",
                body.get("from", ""), body.get("to", ""), body.get("message_type", ""),
            )
            return {"status": "error", "error": "peer_sig verification failed"}
    else:
        # P1 (R11): 缺失 peer_sig 从"警告放行"收紧为拒绝（fail-closed）——
        # 缺失即视为伪造注入面，杜绝跨底座伪造消息。opensource 发送端
        # （_async_forward/retry_delivery/peers.py）均已带 peer_sig。
        logger.warning(
            "receive_peer_message: 消息缺少 peer_sig，拒绝 from=%s to=%s",
            body.get("from", ""), body.get("to", ""),
        )
        return {"status": "error", "error": "peer_sig missing"}

    # ── P2 (R11): 防重放（replay_guard 首次接入实际消息路径）──
    # peer_sig 只覆盖 payload，不覆盖 nonce/msg_id——被捕获的整条请求可被原样重放
    # （无 idempotency_key 时 insert 会重复落库）。发送端（_async_forward/
    # retry_delivery）每条消息带唯一 nonce（重试时重新生成），此处滑动窗口查重：
    # 已见/过期 → 拒绝。国标格式不带 nonce，跳过（走国标自己的防重放语义）。
    from .replay_guard import get_replay_guard
    nonce = body.get("nonce", "")
    if nonce and not get_replay_guard().check_and_record(nonce):
        logger.warning(
            "receive_peer_message: nonce 重放或过期，拒绝 from=%s to=%s type=%s",
            body.get("from", ""), body.get("to", ""), body.get("message_type", ""),
        )
        return {"status": "error", "error": "nonce replay detected"}

    # ── 自有格式（向后兼容）──
    # 多字段尝试提取 from/to，兼容不同转发路径的字段名差异
    from_agent = (
        body.get("from", "") or
        body.get("from_agent", "") or
        body.get("from_agent_id", "") or
        body.get("sender", "") or
        ""
    )
    to_agent = (
        body.get("to", "") or
        body.get("to_agent", "") or
        body.get("to_agent_id", "") or
        body.get("recipient", "") or
        ""
    )
    if not from_agent or not to_agent:
        logger.warning(
            "receive_peer_message: empty from/to — from=%s to=%s body_keys=%s",
            from_agent, to_agent, list(body.keys())[:10],
        )
    return await insert_incoming_peer_message(
        msg_id=body.get("msg_id", ""),
        from_agent=from_agent,
        to_agent=to_agent,
        message_type=body.get("message_type", "info"),
        payload=body.get("payload", {}),
        priority=body.get("priority", "normal"),
        signature=body.get("signature", ""),
        idempotency_key=body.get("idempotency_key") or None,
    )


async def insert_incoming_peer_message(
    msg_id: str, from_agent: str, to_agent: str,
    message_type: str, payload: dict, priority: str,
    signature: str, idempotency_key: str,
) -> dict:
    """接收方写入跨底座消息（/peers/route 处理）"""
    try:
        uuid.UUID(msg_id)
    except (ValueError, AttributeError):
        msg_id = str(uuid.uuid4())

    pool = await get_pool()
    async with pool.acquire() as conn:
        if idempotency_key:
            existing = await conn.fetchrow(
                f"SELECT message_id::text FROM {SCHEMA}.messages WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing:
                return _to_qacp_response(
                    {"status": "duplicate", "message_id": existing["message_id"]},
                    from_agent, to_agent,
                )

        row = await conn.fetchrow(
            f"""INSERT INTO {SCHEMA}.messages
                (message_id, from_agent_id, to_agent_id, message_type, payload,
                 priority, signature, idempotency_key, delivery_status, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'delivered','unread')
                ON CONFLICT DO NOTHING
                RETURNING message_id::text, status""",
            msg_id, from_agent, to_agent, message_type,
            json.dumps(payload or {}), priority, signature, idempotency_key,
        )
        if not row:
            existing = await conn.fetchrow(
                f"SELECT message_id::text FROM {SCHEMA}.messages WHERE idempotency_key = $1",
                idempotency_key,
            )
            return _to_qacp_response(
                {"status": "duplicate", "message_id": existing["message_id"] if existing else None},
                from_agent, to_agent,
            )
        # ── 推送通知目标 Agent（跨底座消息接收后）──
        _trace("insert_incoming_peer_message received msg=%s from=%s to=%s type=%s",
               row["message_id"][:8], from_agent, to_agent, message_type)
        asyncio.create_task(_push_to_agent(
            agent_id=to_agent,
            event={
                "type": message_type,
                "from_agent": from_agent,
                "payload": payload or {},
                "message_id": row["message_id"],
            },
        ))

    return _to_qacp_response({"status": "ok", "message_id": row["message_id"]}, from_agent, to_agent)


async def _push_to_agent(agent_id: str, event: dict) -> None:
    """推送事件到目标 Agent — WS 在线实时推，离线写入 inbox"""
    try:
        from common.bus import bus
        await bus.publish(agent_id, event)
    except Exception:
        logger.debug("Push to agent %s failed (agent may be offline)", agent_id)


async def _enrich_agent_names(conn, rows: list[dict], *id_fields: str) -> list[dict]:
    """批量查询 agent_id → name，为每条记录追加 {field}_name 字段"""
    if not rows or not id_fields:
        return rows

    ids = set()
    for r in rows:
        for f in id_fields:
            v = r.get(f)
            if v:
                ids.add(v)
    if not ids:
        return rows

    name_rows = await conn.fetch(
        f"SELECT agent_id, name FROM {SCHEMA}.agents WHERE agent_id = ANY($1)",
        list(ids),
    )
    id_to_name = {r["agent_id"]: r["name"] for r in name_rows}

    for r in rows:
        for f in id_fields:
            v = r.get(f)
            name_key = f.replace("_id", "_name")
            r[name_key] = id_to_name.get(v, v) if v else v

    return rows


# ── 查询 ──────────────────────────────────────────────

async def get_inbox(
    agent_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    max_age_days: Optional[int] = None,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        resolved = await _resolve_agent_id(conn, agent_id)
        base_where = f"to_agent_id = $1"
        params: list = [resolved]
        idx = 2

        if status:
            base_where += f" AND status = ${idx}"
            params.append(status)
            idx += 1

        if max_age_days is not None:
            base_where += f" AND created_at >= NOW() - ${idx}::interval"
            params.append(f"{max_age_days} days")
            idx += 1

        rows = await conn.fetch(
            f"""SELECT message_id::text, from_agent_id::text, to_agent_id::text,
                       message_type, payload, priority, status, delivery_status,
                       created_at, read_at
                FROM {SCHEMA}.messages
                WHERE {base_where}
                ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}""",
            *params, limit, offset,
        )
        rows = [dict(r) for r in rows]
        return await _enrich_agent_names(conn, rows, "from_agent_id", "to_agent_id")


async def get_unread_count(agent_id: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        resolved = await _resolve_agent_id(conn, agent_id)
        return await conn.fetchval(
            f"SELECT COUNT(*) FROM {SCHEMA}.messages WHERE to_agent_id = $1 AND status = 'unread'",
            resolved,
        )


async def get_conversation(
    agent_a: str,
    agent_b: str,
    limit: int = 50,
) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        resolved_a = await _resolve_agent_id(conn, agent_a)
        resolved_b = await _resolve_agent_id(conn, agent_b)
        rows = await conn.fetch(
            f"""SELECT message_id::text, from_agent_id::text, to_agent_id::text,
                       message_type, payload, priority, status, delivery_status,
                       created_at, read_at
                FROM {SCHEMA}.messages
                WHERE (from_agent_id = $1 AND to_agent_id = $2)
                   OR (from_agent_id = $2 AND to_agent_id = $1)
                ORDER BY created_at DESC LIMIT $3""",
            resolved_a, resolved_b, limit,
        )
        rows = [dict(r) for r in rows]
        return await _enrich_agent_names(conn, rows, "from_agent_id", "to_agent_id")


async def mark_read(message_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.messages SET status = 'read', read_at = NOW() WHERE message_id = $1",
            message_id,
        )
    return {"status": "ok", "message_id": message_id}


async def batch_mark_read(message_ids: list[str]) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.messages SET status = 'read', read_at = NOW() WHERE message_id = ANY($1)",
            message_ids,
        )
    return {"status": "ok", "count": len(message_ids)}


async def archive_message(message_id: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.messages SET status = 'archived' WHERE message_id = $1",
            message_id,
        )
    return {"status": "ok", "message_id": message_id}


# ── 验证 ──────────────────────────────────────────────

async def verify_message_integrity(message_id: str) -> dict:
    """验证消息签名"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.messages WHERE message_id = $1", message_id)
        if not row:
            return {"valid": False, "error": "消息不存在"}

        payload = row["payload"] or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        valid = verify_message(
            row["from_agent_id"], row["to_agent_id"],
            row["message_type"], payload_str, row["signature"],
        )
        return {"valid": valid, "message_id": message_id}


# ── 失败重试（可靠投递，设计 §15.5）────────────────

MAX_CROSS_ORG_RETRY = 5


async def _bump_retry(msg_id: str, retry_count: int) -> None:
    """递增重试计数。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.messages SET retry_count = $1 WHERE message_id = $2",
            retry_count, msg_id,
        )


async def retry_cross_org_deliveries() -> None:
    """定时任务：扫 failed 跨企业消息，重发；超 MAX_RETRY → dead。

    复用 _send_cross_org（重新解析 org、重新加密、新 nonce）；idempotency_key 不变，
    接收方 nonce 去重 + idempotency 去重保证恰好一次。
    """
    my_org = hcfg.get_org_id()
    if not my_org:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT message_id::text, from_agent_id, to_agent_id, payload, retry_count
                FROM {SCHEMA}.messages
                WHERE delivery_status = 'failed' AND retry_count < $1
                ORDER BY created_at LIMIT 100""",
            MAX_CROSS_ORG_RETRY,
        )
    for r in rows:
        retry_count = (r["retry_count"] or 0) + 1
        if retry_count >= MAX_CROSS_ORG_RETRY:
            await mark_delivery_status(r["message_id"], "dead")
            _trace("[trace] retry_cross_org DEAD msg=%s", r["message_id"][:8])
            continue
        # 解析目标 org + ain
        pool2 = await get_pool()
        async with pool2.acquire() as conn:
            target_org = await conn.fetchval(
                f"SELECT organization_id FROM {SCHEMA}.agents "
                f"WHERE agent_id = $1 AND organization_id IS NOT NULL AND organization_id <> ''",
                r["to_agent_id"],
            )
            from_ain = await _ain_of(conn, r["from_agent_id"])
            to_ain = await _ain_of(conn, r["to_agent_id"])
        if not target_org:
            target_org = await _remote_org_of_agent(r["to_agent_id"])
        if not target_org or target_org == my_org:
            # 目标 org 无法解析：保留 failed（下轮重试），仅递增计数
            await _bump_retry(r["message_id"], retry_count)
            continue
        try:
            payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
            await _bump_retry(r["message_id"], retry_count)
            await _send_cross_org(r["message_id"], my_org, target_org, from_ain, to_ain, payload)
        except Exception as e:
            logger.warning("retry_cross_org send fail msg=%s err=%s", r["message_id"][:8], e)
