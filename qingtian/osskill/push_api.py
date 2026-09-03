"""内部推送 API — 子进程 → 父进程 → Bus → WS → Agent 推送桥。

秘书子进程的 background 任务通过此端点在父进程中调用 bus.publish()，
绕过子进程 Bus 实例不同的问题。

v2.0: 增加 OpenClaw Gateway HTTP 推送通道（bus.publish 的并行备选）。
当 OPENCLAW_API_TOKEN 和 OPENCLAW_ENDPOINT 配置后，同时将事件投递到
OpenClaw 会话 API，Agent 可通过飞书/企微等 channel 推送给用户。
"""

import datetime
import hashlib
import hmac
import json
import logging
import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("osskill.push_api")

router = APIRouter(prefix="/api/v1/internal", tags=["内部推送"])

_PUSH_SECRET = os.environ.get("QINGTIAN_PUSH_SECRET", "")
if not _PUSH_SECRET:
    logger.warning("QINGTIAN_PUSH_SECRET 未配置，推送功能将不可用！")
    _PUSH_SECRET = None

# ── OpenClaw Gateway 推送通道 ──────────────────────────

# ── 框架适配层：统一推送通道（配置驱动，框架无关） ──

# 格式: JSON 字符串，每个目标的 key 是框架标识（openclaw / hermes / ...）
# 示例: [{"key":"openclaw","endpoint":"http://127.0.0.1:18789","token":"xxx","path":"/api/sessions/{agent_id}/messages"}]
# 环境变量 PUSH_TARGETS 未配时不启用
_push_targets = []
_raw_targets = os.environ.get("PUSH_TARGETS", "")
if _raw_targets:
    try:
        _push_targets = json.loads(_raw_targets)
    except Exception:
        logger.warning("PUSH_TARGETS 格式错误，应为 JSON 数组，已禁用框架推送")
        _push_targets = []

def _get_push_targets() -> list[dict]:
    """获取推送目标列表（兼容旧环境变量 fallback）"""
    if _push_targets:
        return _push_targets

    # 向后兼容：旧版 OPENCLAW_API_TOKEN 环境变量
    oc_token = os.environ.get("OPENCLAW_API_TOKEN", "")
    if oc_token:
        oc_endpoint = os.environ.get("OPENCLAW_ENDPOINT", "http://127.0.0.1:18789")
        oc_path = os.environ.get("OPENCLAW_MESSAGE_PATH", "/api/sessions/{agent_id}/messages")
        return [{"key": "openclaw", "endpoint": oc_endpoint, "token": oc_token, "path": oc_path}]
    return []


async def _push_to_framework(agent_id: str, event_type: str,
                              payload: dict) -> dict[str, dict]:
    """向所有配置的 Agent 框架投递事件。

    优先走 Agent 适配器层，失败时 fallback 到 PUSH_TARGETS 旧路径。

    Returns:
        {"openclaw": {"ok": True}, "hermes": {"ok": False, "error": "..."}}
    """
    results = {}

    # Phase 3: 优先走适配器层推送
    try:
        from gateway.adapters.registry import get_registry
        registry = get_registry()
        for adapter in registry.all():
            if not hasattr(adapter, 'push'):
                continue
            result = await adapter.push(agent_id, event_type, payload)
            results[adapter.name] = {
                "ok": result.ok,
                "error": result.error,
            }
    except Exception as e:
        logger.warning("Adapter push 异常: %s", e)

    # 适配器无结果时 fallback 到旧 PUSH_TARGETS
    if not results:
        results = await _push_to_targets_fallback(agent_id, event_type, payload)

    return results


async def _push_to_targets_fallback(agent_id: str, event_type: str,
                                     payload: dict) -> dict[str, dict]:
    """旧 PUSH_TARGETS 路径 — 适配器不可用时兜底。"""
    results = {}
    targets = _get_push_targets()

    async with httpx.AsyncClient(timeout=5) as client:
        for target in targets:
            key = target.get("key", "unknown")
            endpoint = target.get("endpoint", "")
            token = target.get("token", "")
            path_tpl = target.get("path", "/api/sessions/{agent_id}/messages")

            if not endpoint or not token:
                results[key] = {"ok": False, "error": "missing endpoint or token"}
                continue

            content = payload.get("content", "") or payload.get("body", "")
            title = payload.get("title", "")
            if title:
                content = f"[{title}]\n{content}"

            url = f"{endpoint.rstrip('/')}{path_tpl.replace('{agent_id}', agent_id)}"

            try:
                resp = await client.post(
                    url,
                    json={
                        "content": content,
                        "metadata": {"source": "work_secretary", "event_type": event_type},
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code in (200, 202):
                    results[key] = {"ok": True}
                else:
                    results[key] = {"ok": False, "error": f"status={resp.status_code}"}
            except Exception as e:
                results[key] = {"ok": False, "error": str(e)[:200]}

    return results


def _verify_push_token(agent_id: str, token: str) -> bool:
    """验证推送令牌 — 防止未授权的子进程推送。"""
    if _PUSH_SECRET is None:
        raise HTTPException(status_code=503, detail="push 未启用（未配置 QINGTIAN_PUSH_SECRET）")
    if not token:
        return False
    expected = hmac.new(
        _PUSH_SECRET.encode(), agent_id.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, token)


class PushRequest(BaseModel):
    agent_id: str
    user_id: str = ""
    event_type: str
    payload: dict = {}
    token: str = ""


@router.post("/skill-push")
async def api_skill_push(body: PushRequest):
    """子进程 → 父进程推送桥。

    秘书子进程调用此端点，父进程内通过 bus.publish() 经 WS 推给 Agent。
    """
    if not body.agent_id:
        return {"ok": False, "error": "agent_id 不能为空"}

    # 令牌验证
    if not _verify_push_token(body.agent_id, body.token):
        logger.warning("Push rejected: invalid token for agent=%s", body.agent_id)
        return {"ok": False, "error": "无效的推送令牌"}

    try:
        from common.bus import bus
        await bus.publish(body.agent_id, {
            "type": body.event_type,
            "source": "work_secretary",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "payload": {
                "user_id": body.user_id,
                **body.payload,
            },
        })
        # 并行推送到各 Agent 框架（非阻塞，失败不影响主通道）
        fw_results = await _push_to_framework(
            body.agent_id, body.event_type, body.payload,
        )

        return {"ok": True, "frameworks": fw_results}
    except Exception as e:
        logger.warning("Push failed for %s: %s", body.agent_id, e)
        return {"ok": False, "error": str(e)}
