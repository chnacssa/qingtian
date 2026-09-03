"""
采购 Agent — 寰宇 HTTP 客户端

通过 REST API 调用 huanyu 目录服务，不直接导入 huanyu 模块。
"""

import logging
import os

import httpx

logger = logging.getLogger("sdk.huanyu_client")


def _auth_headers() -> dict:
    """内部 API 调用认证头

    - Bearer：HUANYU_ADMIN_TOKEN 优先，兜底 ZHENYUE_ADMIN_TOKEN（镇岳 bootstrap
      admin，走 authenticate 正常验证；小智 2026-08-25 本地改动审核合入——部分
      服只配标准镇岳 env，缺 HUANYU_ADMIN_TOKEN 时 skill 发消息 401）
    - QINGTIAN_INTERNAL_IPC_TOKEN：网关 A2 保护端点（/v1/huanyu/messages POST
      等）凭 loopback + X-Internal-Token 内部通道豁免（2026-08-25：R11 A2
      合入后 skill 子进程裸发 401，询价提示推送被拦）。skill 子进程继承
      羲和运行时 env，系统级设置即可。
    """
    headers = {}
    token = (os.environ.get("HUANYU_ADMIN_TOKEN", "")
             or os.environ.get("ZHENYUE_ADMIN_TOKEN", ""))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    internal = os.environ.get("QINGTIAN_INTERNAL_IPC_TOKEN", "")
    if internal:
        headers["X-Internal-Token"] = internal
    return headers


def _api_base() -> str:
    return os.environ.get("QINGTIAN_API_URL", "http://127.0.0.1:1996")


async def register_agent(
    name: str,
    category: str,
    subcategory: str | None = None,
    capabilities: list[str] | None = None,
    contact_info: str | None = None,
    server_host: str | None = None,
    metadata: dict | None = None,
    instance: str | None = None,
) -> dict:
    """通过 REST API 注册 Agent 到寰宇目录。"""
    body = {
        "name": name,
        "category": category,
    }
    if subcategory is not None:
        body["subcategory"] = subcategory
    if capabilities is not None:
        body["capabilities"] = capabilities
    if contact_info is not None:
        body["contact_info"] = contact_info
    if server_host is not None:
        body["server_host"] = server_host
    if metadata is not None:
        body["metadata"] = metadata
    if instance is not None:
        body["instance"] = instance

    # 1. 注册到本服
    url = f"{_api_base().rstrip('/')}/v1/huanyu/agents/register"
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        resp = await client.post(url, json=body, timeout=30)
        resp.raise_for_status()
        result = resp.json()

    # 2. 同步到管理服（全系目录）
    hub_url = os.environ.get("QINGTIAN_MANAGEMENT_URL", "")
    if hub_url and hub_url != _api_base().rstrip("/"):
        try:
            async with httpx.AsyncClient(timeout=10) as hub_client:
                hub_resp = await hub_client.post(
                    f"{hub_url}/v1/huanyu/agents/register",
                    json=body,
                )
                if hub_resp.status_code == 200:
                    logger.debug("Agent %s synced to hub", body.get("name", ""))
        except Exception:
            logger.debug("Agent hub sync deferred for %s", body.get("name", ""))
    return result


# ── Agent 搜索与发现 ─────────────────────────────


async def list_agents(category: str = "", status: str = "active") -> list[dict]:
    """获取 Agent 列表，可按品类过滤。"""
    params = {"category": category, "status": status}
    url = f"{_api_base().rstrip('/')}/v1/huanyu/agents"
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("agents", [])


async def search_agents(q: str) -> list[dict]:
    """按关键词搜索 Agent。"""
    params = {"q": q}
    url = f"{_api_base().rstrip('/')}/v1/huanyu/agents/search"
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("agents", [])


async def discover_agents_by_capability(capability: str, tag: str = "") -> list[dict]:
    """按能力搜索 Agent（用于匹配供应商）。

    调 GET /v1/huanyu/agents?capability={capability}&category={tag}
    tag 参数对应 category 筛选（如 biz:supplier）。
    """
    params: dict[str, str] = {"capability": capability}
    if tag:
        params["category"] = tag
    url = f"{_api_base().rstrip('/')}/v1/huanyu/agents"
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("agents", [])


async def get_agent_ratings(agent_ids: list[str]) -> list[dict]:
    """获取 Agent 信誉评分（agent_rating_summary 视图）。

    返回按 avg_score DESC 排列的评分列表。
    如果接口不可用，返回空列表，调用方 fallback 到 trust_level。
    """
    if not agent_ids:
        return []
    try:
        params = {"agent_ids": ",".join(agent_ids)}
        url = f"{_api_base().rstrip('/')}/v1/huanyu/agents/ratings"
        async with httpx.AsyncClient(headers=_auth_headers()) as client:
            resp = await client.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("ratings", [])
    except Exception:
        pass
    return []


# ── 消息 ────────────────────────────────────────


async def send_message(
    from_agent: str,
    to_agent: str,
    message_type: str = "info",
    payload: dict | None = None,
    priority: str = "normal",
    reply_to: str | None = None,
    negotiation_id: str | None = None,
) -> dict:
    """通过寰宇消息总线发送消息给指定 Agent。"""
    body = {
        "from_agent": from_agent,
        "to_agent": to_agent,
        "message_type": message_type,
        "payload": payload or {},
        "priority": priority,
    }
    if reply_to is not None:
        body["reply_to"] = reply_to
    if negotiation_id is not None:
        body["negotiation_id"] = negotiation_id

    url = f"{_api_base().rstrip('/')}/v1/huanyu/messages"
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        resp = await client.post(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()


async def get_inbox(agent_id: str, limit: int = 20, offset: int = 0, status: str | None = None) -> list[dict]:
    """获取 Agent 的收件箱消息。status: None=全部, 'unread'=未读, 'read'=已读。"""
    url = f"{_api_base().rstrip('/')}/v1/huanyu/inbox/{agent_id}"
    params = {"limit": limit, "offset": offset}
    if status:
        params["status"] = status
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        resp = await client.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("messages", [])


async def mark_read(message_id: str) -> dict:
    """将消息标记为已读（防未读消息被重复消费）。"""
    url = f"{_api_base().rstrip('/')}/v1/huanyu/messages/{message_id}/read"
    async with httpx.AsyncClient(headers=_auth_headers()) as client:
        resp = await client.post(url, timeout=15)
        resp.raise_for_status()
        return resp.json()
