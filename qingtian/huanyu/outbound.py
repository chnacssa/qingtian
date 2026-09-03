"""寰宇 OutboundPusher — skill 后台 info 消息出站投递到飞书用户

背景（2026-08-14）：采购谈判清单（_push_negotiation_summary）、销售每日日报
（daily_report）等 skill 后台主动推送的
huanyu.send_message(message_type="info", to_agent=真实飞书用户) 消息，
InboxScanner 只消费 inquiry/quote/counter/counter_response/deal_closed/
fulfillment_ask 六类业务消息，info 类型无消费端 → 用户收不到。
本模块新增出站投递器：轮询 messages 表里 to 为飞书用户身份（ou_xxx /
feishu:ou_xxx）的未读 info 消息 → 调飞书 OpenAPI 发 DM → 标已读 + delivered。

对齐 inbox_scanner / orders_cron 守护任务模式（main.py 挂载 start()）。

配置（common.config.get 点路径，config.yaml 可覆盖）：
  huanyu.outbound.enabled            默认 true（凭据缺失只告警不空转）
  huanyu.outbound.interval_seconds   默认 20
  huanyu.outbound.max_age_hours      默认 48（只投近 48h，防旧消息积压）
  huanyu.outbound.max_attempts       默认 10（单条消息最多尝试，超限放弃置 failed）
  huanyu.outbound.feishu.app_id / app_secret  覆盖；兜底环境变量 FEISHU_APP_ID/SECRET

已知边界：
  - 只投 to_agent 为飞书用户身份的消息；to 为岗位 agent（如 procurement-feishu）
    不投递（owner 身份落库问题，属部署验证项）。
  - 微信/企微通道未实现，非飞书用户目标跳过（预留扩展点）。
  - 飞书 open_id 失效 → 重试 max_attempts 后放弃标记 failed（cron 转发一次无害）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from common.config import get as cfg_get
from common.db import get_pool

from . import config as hcfg

logger = logging.getLogger("huanyu.outbound")

DEFAULT_INTERVAL = 20
DEFAULT_MAX_AGE_HOURS = 48
DEFAULT_MAX_ATTEMPTS = 10
COOLDOWN_SECONDS = 300  # 发送失败后冷却，避免同一消息每轮都打

_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_FEISHU_SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

# 真实飞书用户身份：裸 open_id（ou_...）或带通道前缀（feishu:ou_...）
_USER_TARGET_RE = r"^(?:feishu:)?ou_[A-Za-z0-9_-]+$"

_task: asyncio.Task | None = None
_token: str = ""
_token_exp: float = 0.0  # epoch seconds，到期前 60s 刷新
_token_warned: bool = False  # 凭据缺失只告警一次


def _config() -> dict:
    """读取 outbound 配置（每轮读取，支持配置热更新）。"""
    return {
        "enabled": bool(cfg_get("huanyu.outbound.enabled", True)),
        "interval": int(cfg_get("huanyu.outbound.interval_seconds", DEFAULT_INTERVAL) or DEFAULT_INTERVAL),
        "max_age_hours": int(cfg_get("huanyu.outbound.max_age_hours", DEFAULT_MAX_AGE_HOURS) or DEFAULT_MAX_AGE_HOURS),
        "max_attempts": int(cfg_get("huanyu.outbound.max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS),
        "app_id": cfg_get("huanyu.outbound.feishu.app_id", "") or os.getenv("FEISHU_APP_ID", ""),
        "app_secret": cfg_get("huanyu.outbound.feishu.app_secret", "") or os.getenv("FEISHU_APP_SECRET", ""),
    }


def _open_id_of(to_agent: str) -> str:
    """剥离飞书通道前缀 → 裸 open_id。"""
    if to_agent.lower().startswith("feishu:"):
        return to_agent[7:]
    return to_agent


async def _get_tenant_token(app_id: str, app_secret: str) -> str:
    """获取飞书 tenant_access_token（缓存 2h，到期前 60s 刷新）。"""
    global _token, _token_exp
    if _token and time.time() < _token_exp - 60:
        return _token
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _FEISHU_TOKEN_URL,
                json={"app_id": app_id, "app_secret": app_secret},
            )
            data = resp.json()
        if data.get("code") == 0 and data.get("tenant_access_token"):
            _token = data["tenant_access_token"]
            _token_exp = time.time() + int(data.get("expire", 7200))
            return _token
        logger.warning("outbound 飞书 token 失败: code=%s msg=%s", data.get("code"), data.get("msg"))
    except Exception as e:
        logger.warning("outbound 飞书 token 异常: %s", e)
    return ""


async def _feishu_send_text(open_id: str, text: str) -> bool:
    """飞书 OpenAPI 发 text DM；成功返回 True。"""
    cfg = _config()
    app_id, app_secret = cfg["app_id"], cfg["app_secret"]
    if not app_id or not app_secret:
        return False
    token = await _get_tenant_token(app_id, app_secret)
    if not token:
        return False
    try:
        content = json.dumps({"text": text}, ensure_ascii=False)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{_FEISHU_SEND_URL}?receive_id_type=open_id",
                headers={"Authorization": f"Bearer {token}"},
                json={"receive_id": open_id, "msg_type": "text", "content": content},
            )
            data = resp.json()
        ok = data.get("code") == 0
        if not ok:
            logger.warning(
                "outbound 飞书发送失败: code=%s msg=%s open_id=%s",
                data.get("code"), data.get("msg"), open_id[:12],
            )
        return ok
    except Exception as e:
        logger.warning("outbound 飞书发送异常: %s", e)
        return False


class OutboundPusher:
    """出站投递器 — 由 main.py 挂载 start()。"""

    def __init__(self):
        self._running = False
        self._cooldown: dict[str, float] = {}  # message_id -> 下次重试 epoch
        self._attempts: dict[str, int] = {}
        self._stats = {"scanned": 0, "delivered": 0, "failed": 0}

    # ── 生命周期 ──

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        global _task
        _task = asyncio.create_task(self._loop(), name="huanyu-outbound")
        logger.info("OutboundPusher started")

    async def stop(self) -> None:
        self._running = False
        global _task
        if _task:
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass
            _task = None
        logger.info("OutboundPusher stopped (delivered=%d failed=%d)",
                    self._stats["delivered"], self._stats["failed"])

    def stats(self) -> dict:
        return dict(self._stats)

    # ── 主循环 ──

    async def _loop(self) -> None:
        await asyncio.sleep(5)  # 启动后等 schema 就绪（对齐 InboxScanner）
        while self._running:
            try:
                await self._once()
            except Exception:
                # 整轮扫描异常兜底（状态一致性）：单条消息异常已在 _once 内隔离，
                # 到此处仅剩 fetch/连接等系统性错误，记录后进入下一轮，不做其他状态变更。
                logger.exception("OutboundPusher scan failed")
            await asyncio.sleep(_config()["interval"])

    async def _once(self) -> None:
        cfg = _config()
        if not cfg["enabled"]:
            return
        if not cfg["app_id"] or not cfg["app_secret"]:
            global _token_warned
            if not _token_warned:
                _token_warned = True
                logger.warning(
                    "outbound 未配置飞书凭据（huanyu.outbound.feishu.app_id/secret 或环境变量 FEISHU_APP_ID/SECRET），"
                    "info 消息不投递"
                )
            return

        now = time.time()
        # 清理已过期 cooldown（防内存无限增长）
        self._cooldown = {k: v for k, v in self._cooldown.items() if v > now}

        pool = await get_pool()
        schema = hcfg.get_schema_name()
        # P2 (R11): 先取数据立即释放连接，再在连接外并发调飞书 API——
        # 原实现 pool.acquire 持连接内串行调飞书，慢则长期占用连接池连接。
        async with pool.acquire() as conn:
            # 2026-08-14 假delivered 修复（小智实锤：用户收不到谈判清单）：
            # AsyncForward（messaging.py:280）转发到 target_host 成功即标 delivered，但
            # 转发成功≠已发飞书。若仍排除 delivered，info→真实飞书用户 消息被 AsyncForward
            # 抢标后 outbound 永不投递 → 用户收不到。本查询已限定 to_agent_id ~ _USER_TARGET_RE
            # （真实飞书用户）且 message_type='info'，故豁免 delivered 状态（只认 status='unread'），
            # 由 _deliver 真实发飞书成功后才统一标 delivered+read（outbound.py 下，无双发）。
            rows = await conn.fetch(
                f"""SELECT message_id::text, to_agent_id::text, message_type, payload
                    FROM {schema}.messages
                    WHERE status='unread' AND message_type='info'
                      AND to_agent_id ~ $1
                      AND created_at >= NOW() - make_interval(hours => $2)
                    ORDER BY created_at ASC
                    LIMIT 50""",
                _USER_TARGET_RE, cfg["max_age_hours"],
            )
        self._stats["scanned"] += 1

        # 连接外并发投递（semaphore 限流；DB 落库仅在每单条内短时 acquire 连接）
        sem = asyncio.Semaphore(5)

        async def _safe_deliver(row) -> None:
            msg_id = row["message_id"]
            if self._cooldown.get(msg_id, 0) > now:
                return
            async with sem:
                try:
                    await self._deliver(pool, schema, msg_id, row["to_agent_id"], row["payload"])
                except Exception as e:
                    # 单条异常不中断整批（状态一致性）：记冷却+次数，防该消息每轮立即重打
                    self._cooldown[msg_id] = time.time() + COOLDOWN_SECONDS
                    self._attempts[msg_id] = self._attempts.get(msg_id, 0) + 1
                    logger.warning("[trace] outbound deliver exception msg=%s: %s", msg_id[:8], e)

        if rows:
            await asyncio.gather(*[_safe_deliver(r) for r in rows])

    async def _deliver(self, pool, schema: str, msg_id: str, to_agent: str, payload) -> None:
        """单条投递：发飞书 → 成功标已读+delivered；失败冷却重试，超限放弃。

        P2 (R11): 首参由 conn 改为 pool——仅在飞书发送完成后的落库阶段才短时
        acquire 连接，HTTP 调用期间不再占用连接池连接。
        """
        open_id = _open_id_of(to_agent)
        if not open_id.startswith("ou_"):
            return
        text = self._extract_text(payload)
        # ① delivered 只在飞书真实投递成功（_feishu_send_text 返回 True）后标记；
        #    发送失败走下方失败分支（冷却重试/超限 failed），绝不提前标 delivered。
        ok = await _feishu_send_text(open_id, text)
        now = time.time()
        if ok:
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {schema}.messages SET status='read', delivery_status='delivered', read_at=NOW() "
                    f"WHERE message_id=$1 AND status='unread'",
                    msg_id,
                )
            self._cooldown.pop(msg_id, None)
            self._attempts.pop(msg_id, None)
            self._stats["delivered"] += 1
            logger.info("[trace] outbound deliver msg=%s to=%s ok", msg_id[:8], to_agent)
            return

        # 失败：冷却 + 计数；超限放弃（防死循环，cron 对 failed 的一次转发无害）
        attempts = self._attempts.get(msg_id, 0) + 1
        self._attempts[msg_id] = attempts
        self._cooldown[msg_id] = now + COOLDOWN_SECONDS
        if attempts >= _config()["max_attempts"]:
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {schema}.messages SET status='read', delivery_status='failed' "
                    f"WHERE message_id=$1 AND status='unread'",
                    msg_id,
                )
            self._cooldown.pop(msg_id, None)
            self._attempts.pop(msg_id, None)
            self._stats["failed"] += 1
            logger.warning(
                "outbound 投递放弃 msg=%s to=%s attempts=%d", msg_id[:8], to_agent, attempts,
            )
        else:
            logger.warning(
                "[trace] outbound deliver fail msg=%s to=%s attempts=%d（冷却后重试）",
                msg_id[:8], to_agent, attempts,
            )

    @staticmethod
    def _extract_text(payload) -> str:
        """取可投递文本：payload.text 优先，否则序列化 payload。"""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        if isinstance(payload, dict) and payload.get("text"):
            return str(payload["text"])
        return json.dumps(payload, ensure_ascii=False)[:20000]


# ── 模块级单例 + 挂载入口 ──

_pusher: OutboundPusher | None = None


def get_pusher() -> OutboundPusher:
    global _pusher
    if _pusher is None:
        _pusher = OutboundPusher()
    return _pusher


async def start() -> None:
    await get_pusher().start()


async def stop() -> None:
    await get_pusher().stop()
