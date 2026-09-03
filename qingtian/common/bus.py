"""
ACSSA 智能体操作系统 — 消息总线（Bus）

核心调度引擎 + Agent 生命周期状态管理。
在网关层主动拦截 Agent 请求，自动完成注册/接管/上下文注入。
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("common.bus")


def _trace_enabled() -> bool:
    from common.config import get as _cfg
    return _cfg("gateway.trace.enabled", True)  # 联调期默认开


def _trace(msg: str, *args) -> None:
    if _trace_enabled():
        # msg 是 %s 占位符格式串，args 为其参数；仅启用时拼接并惰性格式化
        logger.info("[trace] " + msg, *args)


class AgentBusState(str, Enum):
    """Agent 总线生命周期状态"""
    UNKNOWN = "unknown"        # 总线从未见过此 agent
    REGISTERED = "registered"  # 已在寰宇注册，但未被羲和接管
    ADOPTED = "adopted"        # 已被羲和接管，但尚未健康检查通过
    READY = "ready"            # 完全就绪，正常路由消息
    PAUSED = "paused"          # 用户暂停，总线停止路由
    STOPPED = "stopped"        # 用户停止


class BusStateInfo:
    """Per-agent 总线状态 + 元数据"""
    def __init__(self, state: AgentBusState = AgentBusState.UNKNOWN,
                 metadata: Optional[dict] = None):
        self.state = state
        self.metadata = metadata or {}
        self.last_active = datetime.now(timezone.utc)


class BusStateTable:
    """总线状态表 — 内存缓存 + DB 持久化

    启动时从 huanyu.bus_states 表预热，运行时双写（DB + 缓存）。
    缓存 TTL 300s，超时后从 DB 重新加载。
    """

    SCHEMA = "huanyu"
    CACHE_TTL_SECONDS = 300

    def __init__(self):
        self._states: dict[str, BusStateInfo] = {}
        self._loaded_at: Optional[datetime] = None
        self._lock = asyncio.Lock()

    async def load_from_db(self):
        """启动时从 DB 重建所有已知状态"""
        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT agent_id, state, metadata FROM {self.SCHEMA}.bus_states"
            )
        async with self._lock:
            self._states.clear()
            for row in rows:
                self._states[row["agent_id"]] = BusStateInfo(
                    state=AgentBusState(row["state"]),
                    metadata=row.get("metadata") or {},
                )
            self._loaded_at = datetime.now(timezone.utc)
        logger.info("[BusStateTable] 从 DB 加载 %d 条状态", len(self._states))

    async def get(self, agent_id: str) -> AgentBusState:
        """获取状态（读缓存，TTL 超时后从 DB 重载）"""
        now = datetime.now(timezone.utc)
        if (self._loaded_at is not None
                and (now - self._loaded_at).total_seconds() > self.CACHE_TTL_SECONDS):
            await self.load_from_db()  # 内部持锁，并发调用自动串行化
        info = self._states.get(agent_id)
        if info is None:
            return AgentBusState.UNKNOWN
        return info.state

    async def set(self, agent_id: str, state: AgentBusState,
                  metadata: Optional[dict] = None):
        """设置状态（双写 DB + 缓存）"""
        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {self.SCHEMA}.bus_states
                    (agent_id, state, metadata, last_active_at)
                    VALUES ($1, $2, $3::jsonb, NOW())
                    ON CONFLICT (agent_id) DO UPDATE
                    SET state = $2,
                        metadata = COALESCE($3::jsonb,
                            {self.SCHEMA}.bus_states.metadata),
                        last_active_at = NOW()""",
                agent_id, state.value, json.dumps(metadata or {}),
            )
        async with self._lock:
            info = self._states.get(agent_id)
            if info:
                info.state = state
                if metadata:
                    info.metadata.update(metadata)
                info.last_active = datetime.now(timezone.utc)
            else:
                self._states[agent_id] = BusStateInfo(state, metadata)

    def is_known(self, agent_id: str) -> bool:
        info = self._states.get(agent_id)
        return info is not None and info.state != AgentBusState.UNKNOWN

    def get_by_state(self, state: AgentBusState) -> list[str]:
        return [aid for aid, info in self._states.items()
                if info.state == state]

    async def touch(self, agent_id: str):
        """更新活跃时间"""
        info = self._states.get(agent_id)
        if info:
            info.last_active = datetime.now(timezone.utc)

    @property
    def count(self) -> int:
        return len(self._states)

    @property
    def state_counts(self) -> dict:
        counts = {}
        for info in self._states.values():
            counts[info.state.value] = counts.get(info.state.value, 0) + 1
        return counts


class BusScheduler:
    """主动调度引擎

    每个 Agent 请求经过 gateway middleware 时排程：
      1. 查状态
      2. 自动处理未就绪状态（注册/接管）
      3. 路由到目标模块
      4. 注入上下文
    """

    SKIP_PREFIXES = (
        "/health", "/favicon", "/.well-known",
        "/v1/auth", "/v1/zhenyue/approvals",
        "/v1/xihe", "/docs", "/openapi",
    )

    def __init__(self):
        self._state_table = BusStateTable()
        self._register_locks: dict[str, asyncio.Lock] = {}
        self._host = ""
        self._port = 0

    def _get_register_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._register_locks:
            self._register_locks[agent_id] = asyncio.Lock()
        return self._register_locks[agent_id]

    # ── 主调度入口 ────────────────────────────────────

    async def dispatch(self, request: Request, call_next) -> ...:
        """调度入口 — 在 gateway middleware 中调用"""
        agent_id = getattr(request.state, "agent_id", None)
        if not agent_id:
            return await call_next(request)

        # 更新 host/port
        if request.client:
            self._host = request.client.host or self._host

        # === 步骤 1: 状态检查与自动处理 ===
        state = await self._state_table.get(agent_id)
        _trace("BusScheduler state=%s agent=%s", state.name if state else "?", agent_id)

        if state == AgentBusState.UNKNOWN:
            _trace("BusScheduler UNKNOWN → 自动注册+接管 agent=%s", agent_id)
            async with self._get_register_lock(agent_id):
                # 双重检查（并发保护）
                state = await self._state_table.get(agent_id)
                if state == AgentBusState.UNKNOWN:
                    await self._auto_register(agent_id, request)
                    await self._state_table.set(
                        agent_id, AgentBusState.REGISTERED,
                        {"first_seen_from": request.url.path,
                         "first_seen_at": datetime.now(timezone.utc).isoformat()},
                    )
            # 注册成功后尝试接管
            await self._auto_adopt(agent_id)
            await self._state_table.set(agent_id, AgentBusState.READY)
            _trace("BusScheduler UNKNOWN → READY agent=%s", agent_id)

        elif state == AgentBusState.REGISTERED:
            _trace("BusScheduler REGISTERED → 自动接管 agent=%s", agent_id)
            await self._auto_adopt(agent_id)
            await self._state_table.set(agent_id, AgentBusState.READY)
            _trace("BusScheduler REGISTERED → READY agent=%s", agent_id)

        elif state == AgentBusState.PAUSED:
            _trace("BusScheduler PAUSED → 403 agent=%s", agent_id)
            return JSONResponse(status_code=403, content={
                "error": "agent_paused",
                "message": f"Agent {agent_id} 已被暂停，无法处理请求",
            })

        elif state == AgentBusState.STOPPED:
            _trace("BusScheduler STOPPED → 410 agent=%s", agent_id)
            return JSONResponse(status_code=410, content={
                "error": "agent_stopped",
                "message": f"Agent {agent_id} 已停止",
            })

        # FATAL 检查：ARM 标记为 fatal 的 agent 等同于停止
        if state in (AgentBusState.ADOPTED, AgentBusState.READY):
            try:
                from common.db import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT status FROM huanyu.agent_processes "
                        "WHERE agent_id = $1 AND status = 'fatal'",
                        agent_id,
                    )
                if row:
                    logger.warning("[Bus] agent %s 处于 FATAL 状态，路由阻断", agent_id)
                    return JSONResponse(status_code=410, content={
                        "error": "agent_fatal",
                        "message": f"Agent {agent_id} 已进入 fatal 状态，无法处理请求",
                    })
            except Exception:
                pass  # 检查失败不阻断请求

        # ADOPTED / READY — 正常放行

        # === 步骤 2: 路由到目标模块 ===
        response = await call_next(request)

        # === 步骤 3: 注入上下文 ===
        self._inject_context(agent_id, response)

        return response

    # ── 自动注册 ───────────────────────────────────────

    async def _auto_register(self, agent_id: str, request: Request):
        """自动注册未知 agent 到寰宇（Phase 2: 无感注册）"""
        from huanyu.directory import register_agent_silent

        try:
            name = agent_id.split(":", 1)[-1] if ":" in agent_id else agent_id
            category = agent_id.rsplit(":", 1)[0] if ":" in agent_id else "unknown"
            host = request.client.host if request.client else self._host

            result = await register_agent_silent(
                agent_id=agent_id,
                name=name,
                category=category,
                server_host=host,
                metadata={
                    "auto_registered": True,
                    "bus_version": "2.0",
                },
            )
            logger.info("[Bus] 无感注册 %s (category=%s) → agent_id=%s",
                        agent_id, category, result.get("agent_id", ""))
        except Exception as e:
            # 注册失败不阻塞请求，下次 dispatch 重试
            logger.warning("[Bus] 自动注册 %s 失败，下次重试: %s", agent_id, e)

    # ── 自动接管（Phase 2: PID 发现 + 羲和接入）────────

    async def _find_agent_pid(self, agent_id: str) -> dict | None:
        """查 agent_processes 表，返回已记录的 PID 和配置（如有）

        返回 None = 无进程记录或 PID 已失效。
        """
        from common.db import get_pool

        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT pid, config_json, status FROM huanyu.agent_processes "
                    "WHERE agent_id = $1 AND pid IS NOT NULL",
                    agent_id,
                )
            if not row:
                return None

            pid = row["pid"]
            # 验证 PID 是否真实有效
            try:
                import os as _os
                _os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return None
            except OSError:
                return None

            return {
                "pid": pid,
                "config_json": row.get("config_json") or {},
                "status": row.get("status", ""),
            }
        except Exception:
            return None

    async def _auto_adopt(self, agent_id: str):
        """自动接管已注册 agent（Phase 2）

        从 agent_processes 查找已有 PID，发现存活进程则接入羲和接管。
        无进程记录时仅标记 ready，Agent 后续可自行 adopt-self。
        """
        pid_info = await self._find_agent_pid(agent_id)
        if not pid_info:
            logger.info("[Bus] agent %s 无存活进程，标记 ready（可后期 adopt-self）", agent_id)
            return

        try:
            from huanyu.agent_runtime import get_manager as get_arm

            mgr = get_arm()
            result = await mgr.adopt_external(agent_id, {
                "pid": pid_info["pid"],
                "launch_command": pid_info["config_json"].get("executable", ""),
                "health_check": {"type": "process"},
            })
            if result.get("status") == "ok":
                logger.info("[Bus] 自动接管 %s (pid=%d) → 羲和接管成功", agent_id, pid_info["pid"])
            else:
                logger.warning("[Bus] 自动接管 %s 失败: %s", agent_id, result)
        except Exception as e:
            logger.warning("[Bus] 自动接管 %s 异常: %s", agent_id, e)

    # ── 上下文注入 ─────────────────────────────────────

    def _inject_context(self, agent_id: str, response) -> None:
        """向响应注入总线上下文

        Agent 每次请求的响应中都附带当前状态和上下文，不依赖 Agent 记忆。
        注入内容：X-Bus-* 响应头 + JSON body 的 _bus_context 字段。
        """
        info = self._state_table._states.get(agent_id)
        state = info.state.value if info else AgentBusState.UNKNOWN.value

        # 响应头注入（始终可用）
        response.headers["X-Bus-State"] = state
        response.headers["X-Bus-Agent-Id"] = agent_id

        # JSON body 注入（完整上下文）
        try:
            import json as _json
            from common.config import get as cfg_get
            base_url = cfg_get("service.base_url", "http://localhost:1996")
            body = _json.loads(response.body)
            if isinstance(body, dict):
                body["_bus_context"] = {
                    "agent_id": agent_id,
                    "state": state,
                    "modules": {
                        "memory": {"endpoint": "/v1/yongheng", "namespace": f"agent:{agent_id}"},
                        "knowledge": {"endpoint": "/v1/huichuan"},
                        "tasks": {"endpoint": "/v1/zhice", "assignment_mode": "push"},
                        "billing": {"endpoint": "/v1/siku"},
                        "inbox": {"endpoint": "/v1/huanyu"},
                    },
                    "peers": {
                        "base_host": cfg_get("service.host", "localhost"),
                        "base_port": cfg_get("service.port", 1996),
                    },
                    "timestamp": __import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ).isoformat(),
                }
                response.body = _json.dumps(
                    body, ensure_ascii=False, default=str
                ).encode("utf-8")
                response.headers["Content-Length"] = str(len(response.body))
        except Exception:
            pass  # 非 JSON 响应或注入失败不阻塞

    # ── 启动 Reconciliation ────────────────────────────

    async def startup_reconcile(self):
        """底座启动时重建所有已知 Agent 状态"""
        await self._state_table.load_from_db()

        # 交叉验证：READY 状态的 agent 是否仍在寰宇中 active
        from common.db import get_pool
        pool = await get_pool()
        ready_agents = self._state_table.get_by_state(AgentBusState.READY)

        if ready_agents:
            async with pool.acquire() as conn:
                for agent_id in ready_agents:
                    row = await conn.fetchrow(
                        "SELECT agent_id FROM huanyu.agents "
                        "WHERE agent_id = $1 AND status = 'active'",
                        agent_id,
                    )
                    if not row:
                        await self._state_table.set(
                            agent_id, AgentBusState.REGISTERED)
                        logger.info(
                            "[Bus] reconcile: %s 已不活跃，降级为 registered",
                            agent_id)

        logger.info("[Bus] 启动 reconcile 完成: %d agents 已恢复",
                    self._state_table.count)

    # ── 健康/统计 ──────────────────────────────────────

    async def get_stats(self) -> dict:
        """羲和 统计信息"""
        return {
            "managed_agents": self._state_table.count,
            "state_counts": self._state_table.state_counts,
        }


# ── 消息总线 MessageBus ──────────────────────────────


class MessageBus:
    """统一消息总线 — 全模块异步事件通道 + Per-Agent 每日缓冲区

    使用方法:
        from common.bus import bus  # 全局单例
        await bus.publish("biz:buyer-01", {"type": "task_assigned", ...})

    WS Manager 引用通过 set_ws_manager() 注入，避免循环 import。
    """

    def __init__(self):
        # per-agent 缓冲区: agent_id → list[event]
        self._buffers: dict[str, list[dict]] = {}
        # per-agent seq_id 计数器
        self._seq_counters: dict[str, int] = {}
        # per-agent 待推送队列（离线缓冲）
        self._pending_queues: dict[str, list[dict]] = {}
        # 并发锁
        self._buffer_locks: dict[str, asyncio.Lock] = {}
        self._publish_locks: dict[str, asyncio.Lock] = {}
        # WS Manager 引用（set_ws_manager 注入）
        self._ws_manager = None
        # 单 Agent 推送队列上限
        self.MAX_PENDING_PER_AGENT = 200
        # 全局 pending 消息总数上限
        self.MAX_GLOBAL_PENDING = 10000
        # 全局发布 seq（_next_publish_seq 使用）
        self._global_seq = 0

    # ── WS Manager 注入 ──────────────────────────────────

    def set_ws_manager(self, ws_mgr):
        """注入 WSManager 引用（启动时调用，避免循环 import）"""
        self._ws_manager = ws_mgr

    def _get_ws(self):
        """获取 WSManager 引用"""
        return self._ws_manager

    # ── 发布 ─────────────────────────────────────────────

    async def publish(self, agent_id: str, event: dict) -> bool:
        """推送事件到目标 Agent。

        路由逻辑:
          1. WS 在线 → ws_manager.send() → 成功 return True
          2. WS 不在线 / 发送失败 → 写入 huanyu inbox
          3. 紧急事件 → 异步通知 infra:notifier → IM 推送
          4. 全部失败 → return False

        Returns:
            True = 已送达或已降级写入 inbox; False = 全通道失败
        """
        # 标准化事件格式（拷贝后再 pop，不修改调用方的 event 字典）
        event = dict(event)
        seq = self._next_publish_seq()
        full_event = {
            "seq_id": seq,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": event.pop("source", "bus"),
            **event,
        }

        # Step 1: WS 在线 → 实时推
        ws = self._get_ws()
        if ws and ws.is_online(agent_id):
            try:
                sent = await ws.send(agent_id, full_event)
                if sent:
                    return True
            except Exception:
                logger.warning("[MessageBus] WS send failed for %s, fallback inbox", agent_id)

        # Step 2: 写入 huanyu inbox
        try:
            from huanyu.directory import write_inbox

            await write_inbox(agent_id, full_event)
            return True
        except Exception as e:
            logger.warning("[MessageBus] inbox write failed for %s: %s", agent_id, e)

        # Step 3: 紧急事件 → 通知 infra:notifier
        emergency_types = {"task_assigned", "billing_alert", "security_alert", "approval_result"}
        if event.get("type") in emergency_types:
            try:
                await self._notify_emergency(agent_id, full_event)
            except Exception:
                logger.error("[MessageBus] emergency notify failed for %s", agent_id)

        return False

    async def publish_topic(self, topic: str, event: dict):
        """向订阅了某 topic 的所有 Agent 推送事件。"""
        try:
            from huanyu.directory import get_topic_subscribers

            subs = await get_topic_subscribers(topic)
            for sub in subs:
                asyncio.create_task(self.publish(sub["agent_id"], event))
        except Exception as e:
            logger.error("[MessageBus] publish_topic %s failed: %s", topic, e)

    async def emit(self, event_type: str, data: dict) -> None:
        """发布事件（给所有在线 Agent 广播）

        Phase 4: Skill 生命周期事件通过此方法发布。
        """
        await self.broadcast(event_type, data)

    async def broadcast(self, event_type: str, payload: dict):
        """广播事件给所有已接管且 WS 在线的 Agent。"""
        ws = self._get_ws()
        if not ws:
            return
        await ws.broadcast(event_type, payload)

    def _next_publish_seq(self) -> int:
        """全局单调递增 seq_id"""
        seq = getattr(self, "_global_seq", 0) + 1
        self._global_seq = seq
        return seq

    async def _notify_emergency(self, agent_id: str, event: dict):
        """紧急事件通知 infra:notifier 做 IM 推送。

        始终写 logger.critical 兜底（不依赖 notifier 模块可用）。
        C14 (R11): 原实现 3 处失效——
          1. 列名 from_agent/to_agent 不存在（表为 from_agent_id/to_agent_id）→ 每次必抛 42703；
          2. 收件人写成目标 agent_id，notifier 收不到（应为 infra:notifier-01，由 notifier 轮询收件箱推送）；
          3. message_type='emergency_notify' 不在 CHECK 约束内 → INSERT 被拒；status='pending'
             也不是 'unread'，轮询端永不消费。
        现改为：写 to_agent_id=infra:notifier-01，message_type='notification'（已加入 CHECK），
        status='unread'，payload 按 notifier.handle_notification_request 契约
        （agent_id/title/content/priority）构造。
        """
        logger.critical(
            "[Emergency] %s 紧急事件未送达: type=%s seq=%s",
            agent_id, event.get("type", "?"), event.get("seq_id", "?"),
        )
        try:
            from common.db import get_pool

            payload = {
                "agent_id": agent_id,
                "title": f"[紧急] {event.get('type', '事件')}",
                "content": f"事件未送达: seq={event.get('seq_id', '?')} type={event.get('type', '?')}",
                "priority": "high",
            }
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO huanyu.messages
                       (from_agent_id, to_agent_id, message_type, payload, status)
                       VALUES ($1, $2, $3, $4, $5)""",
                    "bus",
                    "infra:notifier-01",
                    "notification",
                    json.dumps(payload, ensure_ascii=False),
                    "unread",
                )
        except Exception:
            pass

    # ── Per-Agent 每日缓冲区（写前日志） ─────────────────

    def _get_buffer_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._buffer_locks:
            self._buffer_locks[agent_id] = asyncio.Lock()
        return self._buffer_locks[agent_id]

    def _next_buffer_seq(self, agent_id: str) -> int:
        self._seq_counters[agent_id] = self._seq_counters.get(agent_id, 0) + 1
        return self._seq_counters[agent_id]

    async def buffer_append(self, agent_id: str, event: dict):
        """追加事件到 Agent 的当日缓冲区（append-only，不限长度）"""
        seq = self._next_buffer_seq(agent_id)
        stamped = {
            "seq_id": seq,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        async with self._get_buffer_lock(agent_id):
            if agent_id not in self._buffers:
                self._buffers[agent_id] = []
            self._buffers[agent_id].append(stamped)

    def buffer_snapshot(self, agent_id: str) -> list[dict]:
        """取 Agent 缓冲区快照（只读，不删除）。供 Xixing 每日拉取"""
        return list(self._buffers.get(agent_id, []))

    def buffer_clear(self, agent_id: str):
        """清空 Agent 缓冲区。Xixing 确认写入 Yongheng 后调用"""
        self._buffers.pop(agent_id, None)
        self._seq_counters.pop(agent_id, None)

    def get_buffer_agents(self) -> list[str]:
        """返回所有缓冲区非空的 Agent ID 列表"""
        return [aid for aid, buf in self._buffers.items() if buf]

    def buffer_events_count(self, agent_id: str) -> int:
        """返回 Agent 缓冲区事件数"""
        return len(self._buffers.get(agent_id, []))

    # ── 待推送队列（离线缓冲） ───────────────────────────

    def _get_publish_lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._publish_locks:
            self._publish_locks[agent_id] = asyncio.Lock()
        return self._publish_locks[agent_id]

    @property
    def _global_pending_count(self) -> int:
        return sum(len(q) for q in self._pending_queues.values())

    async def enqueue_pending(self, agent_id: str, event: dict):
        """将事件加入 Agent 的待推送队列（WS 离线时缓冲）"""
        async with self._get_publish_lock(agent_id):
            if agent_id not in self._pending_queues:
                self._pending_queues[agent_id] = []
            queue = self._pending_queues[agent_id]

            # 限长检查
            if len(queue) >= self.MAX_PENDING_PER_AGENT:
                dropped = queue.pop(0)
                logger.warning(
                    "[MessageBus] %s pending queue full (%d), dropping oldest seq=%s",
                    agent_id, self.MAX_PENDING_PER_AGENT, dropped.get("seq_id", "?")
                )
            if self._global_pending_count >= self.MAX_GLOBAL_PENDING:
                # P2 (R11): 全局超限必须拒绝入队——原实现只打日志仍 append，队列无界膨胀
                logger.error("[MessageBus] global pending queue full (%d), dropping %s event",
                             self.MAX_GLOBAL_PENDING, agent_id)
                return

            queue.append(event)

    async def flush_pending(self, agent_id: str) -> list[dict]:
        """取出并清空 Agent 的待推送队列（WS 重连后调用）"""
        async with self._get_publish_lock(agent_id):
            return self._pending_queues.pop(agent_id, [])


# ── 全局单例 ─────────────────────────────────────────

bus = MessageBus()
bus_scheduler = BusScheduler()


__all__ = [
    "AgentBusState",
    "BusStateTable",
    "BusScheduler",
    "MessageBus",
    "bus",
    "bus_scheduler",
]
