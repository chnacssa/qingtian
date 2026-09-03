"""
联邦路由 — 跨底座通信/同步/心跳（企业版）
从 api_rest.py 迁移的 /peers/* 接口。
"""

import json
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

federation_router = APIRouter(prefix="/peers", tags=["Federation"])

# 全局 peer router（不带 prefix，兼容 api_rest.py 的 peer_router）
peer_router = APIRouter()

logger = logging.getLogger("huanyu.federation")


def _require_fed_sig(body: dict, endpoint: str) -> None:
    """review(2026-08-24 P0-6): 联邦写端点统一验签（缺签/错签 403 拒绝）。"""
    from .signing import verify_fed_body
    if not verify_fed_body(body or {}):
        logger.warning("[trace] %s peer_sig 验签失败/缺失，拒绝", endpoint)
        raise HTTPException(status_code=403, detail="peer_sig verification failed")


def _verify_peer_sig(body: dict, sig_field: str = "peer_sig",
                     sign_scope: str = "body") -> bool:
    """跨底座请求签名校验（fail-closed）。

    P1 (R11): /peers/ 系列写接口此前全公开无验签，攻击者可目录投毒/
    伪造谈判/伪造转发目标。发送端（messaging._async_forward/retry_delivery、
    peers.push_local_agents_to_hub、negotiation._replicate_negotiation、心跳上报）
    均已带 peer_sig。此处统一校验：缺失或校验失败一律拒绝。

    sign_scope 决定签名覆盖范围（与发送端保持一致）：
      - "body"   : 整个 body（剔除 sig 字段）——心跳/整表快照/hub 中继
      - "agent"  : 仅 body["agent"]（/peers/sync 单条注册模式）
      - "record" : 仅 body["record"]（/peers/negotiation/sync）
    """
    from .signing import verify_peer_message

    sig = body.get(sig_field, "")
    if not sig:
        return False
    if sign_scope == "agent":
        canonical = body.get("agent", {})
    elif sign_scope == "record":
        canonical = body.get("record", {})
    else:
        canonical = {k: v for k, v in body.items() if k != sig_field}
    try:
        payload_str = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return False
    return verify_peer_message(payload_str, sig, peer_host="")


@federation_router.post("/route")
async def route_message(request: Request):
    """跨底座消息投递 — 接收方写入 + 幂等 + 回执"""
    try:
        body = await request.json()
        from .messaging import receive_peer_message
        result = await receive_peer_message(body)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@federation_router.post("/hub/forward")
async def hub_forward(request: Request):
    """Hub 消息中转 — 采购/销售服 → 管理服 → 目标服

    采购/销售服因容器网络限制无法直连其他 WG IP 时，
    通过管理服中转。管理服收到后转发到目标底座。

    安全：仅允许转发到 peers 表已注册且 active 的底座，
    端口以注册值为准（忽略客户端传入），防止 SSRF。
    """
    import httpx
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    # P1 (R11): hub 中转入口本不该公开。发送端（messaging._async_forward/
    # retry_delivery）走 hub 中转时带 _relay_sig（全局密钥，签名覆盖含
    # _relay_target_* 的整个请求体）→ 校验失败拒绝，杜绝伪造转发目标/滥用中转。
    _relay_sig = body.get("_relay_sig", "")
    if not _relay_sig:
        raise HTTPException(status_code=401, detail="_relay_sig missing")
    if not _verify_peer_sig(body, sig_field="_relay_sig", sign_scope="body"):
        raise HTTPException(status_code=403, detail="_relay_sig verification failed")

    target_host = body.pop("_relay_target_host", None)
    body.pop("_relay_target_port", None)  # 端口以 peers 表注册值为准
    if not target_host:
        raise HTTPException(status_code=400, detail="_relay_target_host required")

    # 校验目标底座在 peers 表注册且 active
    from common.db import get_pool
    from . import config as hcfg
    pool = await get_pool()
    async with pool.acquire() as conn:
        peer = await conn.fetchrow(
            f"SELECT port FROM {hcfg.get_schema_name()}.peers "
            f"WHERE (host = $1 OR name = $1 OR peer_id = $1) AND status = 'active'",
            target_host,
        )
        if not peer:
            # 🧭 方案甲反查：发送端以 agents.server_ip（WG 内网 IP）作 relay 目标时，
            # peers 表只有主机名，需反查 server_ip → server_host 再定位 peers 条目。
            route_host = await conn.fetchval(
                f"SELECT server_host FROM {hcfg.get_schema_name()}.agents "
                f"WHERE server_ip = $1 AND server_ip <> '' LIMIT 1",
                target_host,
            )
            if route_host:
                peer = await conn.fetchrow(
                    f"SELECT port FROM {hcfg.get_schema_name()}.peers "
                    f"WHERE (host = $1 OR name = $1 OR peer_id = $1) AND status = 'active'",
                    route_host,
                )
    # review(2026-08-24 P0-6): 删除"反查命中 server_ip 即绕过 peers 表直连该 IP"容错——
    # agents 表可经 /peers/sync 写入，该分支等于允许向任意注册 IP 发起请求（SSRF）。
    # 反查必须落到 active peers 条目才算通过；对不上一律 403。
    if not peer:
        raise HTTPException(status_code=403, detail="relay target not in active peers")

    target_url = f"http://{target_host}:{peer['port']}/peers/route"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(target_url, json=body)
            return JSONResponse(
                content=resp.json() if resp.content else {"status": "relayed"},
                status_code=resp.status_code,
            )
    except Exception as e:
        logger = logging.getLogger("huanyu.federation")
        logger.warning("HubForward: relay to %s failed: %s", target_host, e)
        raise HTTPException(status_code=502, detail=f"relay failed: {str(e)}")


@federation_router.post("/heartbeat")
async def peer_heartbeat(request: Request):
    """OSPF DR 心跳上报（peer_sig 验签）"""
    try:
        from .peers import receive_heartbeat
        body = await request.json()
        # P1 (R11): 心跳伪造会把任意 host/port 投毒进 peers 目录（路由劫持），
        # 强制 peer_sig 校验（fail-closed）。
        if not _verify_peer_sig(body, sign_scope="body"):
            raise HTTPException(status_code=403, detail="peer_sig missing or verification failed")
        ok = await receive_heartbeat(body)
        return {"status": "ok" if ok else "error"}
    except ImportError:
        raise HTTPException(status_code=503, detail="Federation not available in community edition")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@federation_router.get("/discover")
async def discover_peers():
    try:
        from .peers import list_active_peers
        peers = await list_active_peers()
        return {"peers": peers}
    except ImportError:
        raise HTTPException(status_code=503, detail="Federation not available in community edition")


@federation_router.post("/sync")
async def sync_directory(request: Request):
    """Agent 目录同步（peer_sig 验签）"""
    try:
        from .peers import sync_agent_directory
        body = await request.json()
        # P1 (R11): 目录投毒入口。发送端两种格式签名覆盖范围不同：
        # 整表快照（push_local_agents_to_hub）覆盖整个 body，单条注册
        # （notify_hub_agent_registered）仅覆盖 body["agent"]，二者任一生效即可。
        if not (_verify_peer_sig(body, sign_scope="body")
                or _verify_peer_sig(body, sign_scope="agent")):
            raise HTTPException(status_code=403, detail="peer_sig missing or verification failed")
        ok = await sync_agent_directory(body)
        return {"status": "ok" if ok else "error"}
    except ImportError:
        raise HTTPException(status_code=503, detail="Federation not available in community edition")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@federation_router.get("/agents/registry")
async def agents_registry():
    """跨底座 Agent 注册表"""
    try:
        from .peers import get_agents_registry
        agents = await get_agents_registry()
        return {"agents": agents}
    except ImportError:
        raise HTTPException(status_code=503, detail="Federation not available in community edition")


@federation_router.post("/negotiation/sync")
async def negotiation_sync(request: Request):
    """谈判状态跨底座同步（peer_sig 验签）"""
    try:
        from .peers import sync_negotiation
        body = await request.json()
        # P1 (R11): 伪造谈判同步（status=accepted）会走通签约，强制 peer_sig 校验
        # （发送端 negotiation._replicate_negotiation 对 body["record"] 签名）。
        if not _verify_peer_sig(body, sign_scope="record"):
            raise HTTPException(status_code=403, detail="peer_sig missing or verification failed")
        ok = await sync_negotiation(body)
        return {"status": "ok" if ok else "error"}
    except ImportError:
        raise HTTPException(status_code=503, detail="Federation not available in community edition")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@federation_router.post("/check-upgrade")
async def check_upgrade(request: Request):
    """升级检查"""
    body = await request.json()
    return {"status": "ok", "upgrade_available": False}
