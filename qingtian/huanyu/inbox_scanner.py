"""
寰宇 InboxScanner — 共享轮询 + IPC 直派

设计:
  - 单例 asyncio task，挂在 huanyu 层
  - 每 30s 扫描所有 agent inbox 的未读消息
  - 按消息类型分派到目标 Skill 的 per-agent 实例
  - inquiry     → sales:generate_quote → sales:inquiry_submit_quote
  - quote       → procurement:inquiry_add_quote
  - counter     → sales:respond_counter（采购还盘 → 销售决策后回 counter_response）
  - counter_response → procurement:inquiry_consume_counter_response（销售还盘决策 → 采购入账/成交）
  - deal_closed → sales:record_deal（采购成交 → 销售记录客户画像）

与 Skill 层解耦：Skill 只需实现对应 action，由 scanner 通过 IPC 调用。
"""

import asyncio
import json
import logging
from typing import Optional

from common.db import get_pool
from . import config as hcfg

logger = logging.getLogger("huanyu.inbox_scanner")


def _trace_enabled() -> bool:
    from common.config import get as _cfg
    return _cfg("gateway.trace.enabled", True)  # 联调期默认开，上线后配false关闭


def _trace(msg: str, *args) -> None:
    if _trace_enabled():
        logger.warning("[trace] " + msg, *args)

SCAN_INTERVAL = 30  # 秒

# 消息类型 → (目标 Skill, actions 链, 角色筛选)
_ROUTES = {
    # inquiry → sales:order_ingest（接单闸门，2026-08-09）：未委托自主成交则通知销售用户等接单决定，
    # 约束进谈判前提；委托了自主成交则内部走 generate_quote → inquiry_submit_quote 报价链。
    "inquiry": {
        "skill": "sales",
        "actions": ["order_ingest"],
        "agent_category": "biz:seller",
    },
    "quote": {
        "skill": "procurement",
        "action": "inquiry_add_quote",
        "agent_category": "biz:buyer",
    },
    "counter": {
        "skill": "sales",
        "action": "respond_counter",
        "agent_category": "biz:seller",
    },
    "counter_response": {
        "skill": "procurement",
        "action": "inquiry_consume_counter_response",
        "agent_category": "biz:buyer",
    },
    "deal_closed": {
        "skill": "sales",
        "action": "record_deal",
        "agent_category": "biz:seller",
    },
    # 履约回访询问：管理服发给买卖双方 agent，按收件人分类分 skill（buyer→procurement, seller→sales）
    "fulfillment_ask": {
        "action": "fulfillment_ask",
        "skills_by_category": {"biz:buyer": "procurement", "biz:seller": "sales"},
    },
}


class InboxScanner:
    """共享 inbox 轮询器 — 一个 task 扫描所有 agent，IPC 直派到对应 Skill。"""

    def __init__(self):
        self._runtime = None  # XiheRuntime 引用，由 main.py 注入
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stats = {"scanned": 0, "dispatched": 0, "errors": 0}

    def set_runtime(self, runtime):
        """注入 XiheRuntime（main.py 初始化后调用）。"""
        self._runtime = runtime

    async def _is_local_agent(self, conn, agent_id: str) -> bool:
        """判断 agent 是否归属本底座。

        以 server_ip == 本机 host_ip 为准（方案甲权威标识）；server_ip 为空时退回
        server_host 匹配本机主机名。两者都无法判定（目录里查不到 / IP 主机名都为空）
        → 保守按本地处理，避免误丢消息。跨底座消息由目标服 scanner 处理，本端只留档。
        """
        import socket
        from common.config import get as root_get
        schema = hcfg.get_schema_name()
        host_ip = (root_get("host_ip", "") or "").strip()
        this_host = (socket.gethostname() or "").strip()
        cfg_host = (root_get("host", "") or "").strip()
        local_hosts = {h for h in (this_host, cfg_host) if h}

        row = await conn.fetchrow(
            f"SELECT server_ip, server_host FROM {schema}.agents WHERE agent_id = $1",
            agent_id,
        )
        if not row:
            return True
        sip = (row["server_ip"] or "").strip()
        shost = (row["server_host"] or "").strip()
        # C12 (R11): 单底座/本地部署 server_host 可能为 localhost/127.0.0.1——
        # 此前不识别导致所有消息被判非本地而静默丢弃。回环地址恒判本地。
        loopback = {"localhost", "127.0.0.1", "::1"}
        if sip in loopback:
            pass  # 回环 IP 恒本地
        elif sip and host_ip and sip != host_ip:
            return False
        if shost and local_hosts and shost not in local_hosts and shost not in loopback:
            return False
        return True

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._scan_loop(), name="inbox-scanner")
        logger.info("InboxScanner started (interval=%ds)", SCAN_INTERVAL)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("InboxScanner stopped (scanned=%d dispatched=%d errors=%d)",
                     self._stats["scanned"], self._stats["dispatched"], self._stats["errors"])

    async def _scan_loop(self):
        """主循环：扫描 → 分派 → 等待。"""
        await asyncio.sleep(5)  # 启动后先等 runtime 完全就绪
        while self._running:
            try:
                await self._scan_once()
            except Exception:
                logger.exception("InboxScanner scan failed")
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan_once(self):
        """单次扫描：查未读消息 → 按类型分派到 Skill。"""
        if self._runtime is None:
            return

        pool = await get_pool()
        schema = hcfg.get_schema_name()

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                # message_id::text 必须显式转字符串：asyncpg 对 uuid 列默认返回 UUID 对象，
                # 下游 row["message_id"][:8] 切片会 TypeError → 整个 scan 崩溃（d1d08f0 回归）。
                # 同时保证 base_context["message_id"] 是字符串，进 skill payload 可被 json 序列化。
                f"SELECT message_id::text AS message_id, to_agent_id, from_agent_id, message_type, payload "
                f"FROM {schema}.messages "
                f"WHERE status = 'unread' AND message_type IN ('inquiry','quote','counter','counter_response','deal_closed','fulfillment_ask') "
                f"ORDER BY created_at ASC "
                f"LIMIT 100"
            )

            self._stats["scanned"] += 1

            dispatched = 0
            for row in rows:
                msg_type = row["message_type"]
                route = _ROUTES.get(msg_type)
                if not route:
                    continue

                skill_name = route.get("skill", "")
                agent_id = row["to_agent_id"]
                expected_cat = route.get("agent_category", "")

                # 跳过分类标签（如 biz:seller）
                if agent_id.startswith("biz:"):
                    continue

                # 只处理归属本底座的 agent 消息：跨底座消息由目标服 scanner 处理，
                # 本端只是出站留档/转发副本。否则会双端消费（如采购服扫到销售服 inquiry，
                # 因无 sales skill 反复报 "Skill 'sales' not found" 刷屏）。
                if not await self._is_local_agent(conn, agent_id):
                    _trace("InboxScanner skip non-local msg=%s type=%s to=%s",
                           row["message_id"][:8], msg_type, agent_id)
                    await conn.execute(
                        f"UPDATE {schema}.messages SET status = 'read' "
                        f"WHERE message_id = $1 AND status = 'unread'",
                        row["message_id"],
                    )
                    continue

                # 检查 agent 分类是否匹配（如 inquiry 只发给 biz:seller）
                if expected_cat:
                    agent_cat = await conn.fetchval(
                        f"SELECT category FROM {schema}.agents WHERE agent_id = $1",
                        agent_id,
                    )
                    if ((agent_cat or "").strip()) != expected_cat:
                        # 分类不匹配 → mark read 跳过,不重试（记日志便于排查 quote 未落库）
                        logger.warning(
                            "InboxScanner skip %s msg=%s agent=%s: category=%r != expected %r",
                            msg_type, row["message_id"][:8], agent_id, agent_cat, expected_cat,
                        )
                        await conn.execute(
                            f"UPDATE {schema}.messages SET status = 'read' "
                            f"WHERE message_id = $1 AND status = 'unread'",
                            row["message_id"],
                        )
                        continue

                # 按 agent 分类分 skill 的路由（如 fulfillment_ask：buyer→procurement, seller→sales）
                skills_by_cat = route.get("skills_by_category")
                if skills_by_cat:
                    agent_cat = await conn.fetchval(
                        f"SELECT category FROM {schema}.agents WHERE agent_id = $1",
                        agent_id,
                    )
                    skill_name = skills_by_cat.get((agent_cat or "").strip(), "")
                    if not skill_name:
                        logger.warning(
                            "InboxScanner skip %s msg=%s agent=%s: category=%r 无对应 skill",
                            msg_type, row["message_id"][:8], agent_id, agent_cat,
                        )
                        await conn.execute(
                            f"UPDATE {schema}.messages SET status = 'read' "
                            f"WHERE message_id = $1 AND status = 'unread'",
                            row["message_id"],
                        )
                        continue

                # 解析 payload
                payload = row["payload"]
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (json.JSONDecodeError, TypeError):
                        payload = {}
                if not isinstance(payload, dict):
                    payload = {}

                base_context = {
                    "from_agent": row["to_agent_id"],    # 发件人 = 本端 agent
                    "to_agent": row["from_agent_id"],    # 收件人 = 询价发起方
                    "message_id": row["message_id"],
                }

                try:
                    handle = await self._runtime.get_handle(skill_name, agent_id)

                    # 支持 actions 链（如 inquiry: generate_quote → inquiry_submit_quote）
                    actions = route.get("actions")
                    if actions:
                        accumulated = {}
                        for action in actions:
                            _trace("InboxScanner chain [%s] agent=%s msg=%s action=%s",
                                   msg_type, agent_id, row["message_id"], action)
                            result = await handle.execute({
                                "action": action,
                                "agent_id": agent_id,
                                "payload": {
                                    **payload,
                                    **base_context,
                                    **accumulated,
                                },
                            })
                            _trace("InboxScanner chain [%s] agent=%s action=%s result=%s",
                                   msg_type, agent_id, action,
                                   "ok" if (isinstance(result, dict) and result.get("ok"))
                                   else f"fail:{str(result)[:100]}")
                            # 合并上一步输出到下一步输入
                            if isinstance(result, dict):
                                data = result.get("data", {})
                                if isinstance(data, dict):
                                    # 展平 product 子对象（generate_quote 产出）
                                    if "product" in data and isinstance(data["product"], dict):
                                        accumulated.update(data["product"])
                                    accumulated.update(data)
                    else:
                        action = route.get("action")
                        _trace("InboxScanner dispatch [%s] agent=%s msg=%s action=%s",
                               msg_type, agent_id, row["message_id"][:8], action)
                        result = await handle.execute({
                            "action": action,
                            "agent_id": agent_id,
                            "payload": {
                                **payload,
                                **base_context,
                            },
                        })
                        if not (isinstance(result, dict) and result.get("ok")):
                            # 单 action 路径此前无任何日志 → add_quote 等静默失败不可见
                            logger.warning(
                                "InboxScanner dispatch %s → %s agent=%s msg=%s 未成功: %s",
                                msg_type, action, agent_id, row["message_id"][:8],
                                str(result)[:200],
                            )

                    # 连接仍在 with 块内有效，直接 mark read
                    await conn.execute(
                        f"UPDATE {schema}.messages SET status = 'read' "
                        f"WHERE message_id = $1 AND status = 'unread'",
                        row["message_id"],
                    )
                    dispatched += 1
                except Exception as e:
                    self._stats["errors"] += 1
                    logger.warning(
                        "InboxScanner dispatch failed: %s:%s → agent=%s msg=%s: %s",
                        skill_name, routing_key(route), agent_id, row["message_id"], e,
                    )

            if dispatched:
                self._stats["dispatched"] += dispatched
                logger.info("InboxScanner dispatched %d messages", dispatched)

    def stats(self) -> dict:
        return dict(self._stats)


def routing_key(route: dict) -> str:
    """日志可读的路由 key"""
    return route.get("actions", [route.get("action", "?")])[0] if isinstance(route.get("actions"), list) else route.get("action", "?")


# 全局单例
_scanner: Optional[InboxScanner] = None


def get_scanner() -> InboxScanner:
    global _scanner
    if _scanner is None:
        _scanner = InboxScanner()
    return _scanner
