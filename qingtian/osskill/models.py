"""
Skill 基类 — R3 兼容定义

包含 Skill（ABC 基类）、SkillContext（运行时上下文）、SkillManifest（skill.json 解析结果）。
所有 Skill 实现继承 Skill，实现 execute() 方法，可选覆盖生命周期钩子。

从 R2 到 R3 的变更有：
- BaseSkill → Skill（更名以匹配设计文档术语）
- 新增 on_data_purge()、on_upgrade() 生命周期方法
- 新增 SkillManifest 数据类（对应 skill.json 结构）
- SkillContext 扩展了 logger/cache/call_skill/llm/api 属性
"""

import asyncio
import logging
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from osskill.storage_quota import get_storage_quota

logger = logging.getLogger("osskill.models")


# ── SkillManifest：对应 skill.json 结构 ─────────────────────────

@dataclass
class SkillManifest:
    """从 skill.json 解析的完整元数据"""
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    description: str = ""
    icon: str = ""
    category: str = "tool"
    tags: list[str] = field(default_factory=list)
    author: dict = field(default_factory=dict)
    compliance: dict = field(default_factory=lambda: {"data_handling": "local"})
    copyright: dict = field(default_factory=lambda: {
        "declaration": "",
        "license": "",
    })
    license_info: dict = field(default_factory=lambda: {
        "type": "free",
        "retail_price_yuan": 0,
        "wholesale_ratio": 0.30,
        "trial_days": 7,
        "refund_days": 7,
    })
    lifecycle: str = "resident"
    """生命周期模式: "resident" 常驻内存, "on_demand" 空闲超时自动卸载"""
    entry: dict = field(default_factory=lambda: {"class": "", "file": "main.py"})
    permissions: list[str] = field(default_factory=list)
    resources: dict = field(default_factory=lambda: {
        "cpu": "low",
        "memory_mb": 128,
        "api_calls_per_minute": 20,
    })
    dependencies: dict = field(default_factory=lambda: {
        "qingtian": ">=2.0.0",
        "skills": {},
    })
    certificate: str = ""
    """Skill 证书 hex（平台 Ed25519 签名，IPC-034/035 专利方法验证）

    由 acssa.cn 在审核通过后签发，包含 Skill 名称、版本、有效期的签名载荷。
    未持证 Skill 该字段为空，验证时被判定为 untrusted。
    """


# ── 生命周期事件枚举 ─────────────────────────

class SkillLifecycleEvent:
    """Skill 生命周期事件常量，用于 Bus 通知"""
    LOADED = "skill:loaded"
    UNLOADED = "skill:unloaded"
    UPGRADED = "skill:upgraded"
    REVOKED = "skill:revoked"
    DEGRADED = "skill:degraded"
    ERROR = "skill:error"


# ── SkillContext：传递给 Skill 的运行时上下文 ─────────────────────────

class SkillContext:
    """Skill 运行时上下文

    通过 IPC 通道将底座能力暴露给 Skill 实例。
    子进程模式下，call_skill/llm/api 通过 IPC 消息委托到底座处理。
    """

    def __init__(
        self,
        agent_id: str,
        config: dict | None = None,
        skill_name: str = "",
        skill_version: str = "",
        _ipc_client: Any = None,
        permissions: list[str] | None = None,
    ):
        self.agent_id = agent_id
        self.skill_name = skill_name
        self.skill_version = skill_version
        self.config = config or {}
        self._ipc_client = _ipc_client
        self._cache: dict[str, Any] = {}
        self._permissions = set(permissions or [])

    @property
    def logger(self):
        """Skill 专属日志器（自动附带 skill_name + version）"""
        name = f"skill.{self.skill_name}.{self.skill_version}" if self.skill_name else "skill"
        return logging.getLogger(name)

    @property
    def cache(self) -> dict[str, Any]:
        """进程内缓存字典。底座重启后清空。"""
        return self._cache

    async def call_skill(self, name: str, method: str, *args: Any, **kwargs: Any) -> Any:
        """调用另一个 Skill 的方法

        通过 IPC 消息到底座，底座调度到目标子进程执行。
        需要 "skills" 权限，未声明时抛出 PermissionError。
        如果 IPC 未初始化（单体模式），抛出 RuntimeError。
        """
        if "skills" not in self._permissions:
            raise PermissionError("此 Skill 未声明 skills 权限，禁止调用其他 Skill")
        if self._ipc_client is None:
            raise RuntimeError(
                "IPC 客户端未初始化，call_skill 不可用。"
                "此 Skill 可能在单体模式下运行。"
            )
        return await self._ipc_client.call("skill.call", {
            "target": name,
            "method": method,
            "args": args,
            "kwargs": kwargs,
        })

    @property
    def llm(self) -> "_LLMProxy":
        """LLM 代理层访问器。需要 "llm" 权限。"""
        if "llm" not in self._permissions:
            raise PermissionError("此 Skill 未声明 llm 权限，禁止调用 LLM")
        return _LLMProxy(self._ipc_client, self._permissions)

    @property
    def api(self) -> "_APIProxy":
        """底座公开 API 访问器。需要 "network" 权限。"""
        if "network" not in self._permissions:
            raise PermissionError("此 Skill 未声明 network 权限，禁止调底座 API")
        return _APIProxy(self._ipc_client, self._permissions)

    @property
    def filesystem(self) -> "_FilesystemProxy":
        """Skill data 目录文件操作。需要 "filesystem" 权限。"""
        if "filesystem" not in self._permissions:
            raise PermissionError("此 Skill 未声明 filesystem 权限，禁止文件操作")
        return _FilesystemProxy()


class _FilesystemProxy:
    """Skill data 目录文件操作代理

    所有文件路径经过 _safe 安全校验，防止越权访问底座敏感目录。
    """

    @staticmethod
    def _safe(path: str) -> str:
        """防路径穿越：确保访问路径在 SKILL_HOME 内"""
        skill_home = os.environ.get("SKILL_HOME", "")
        if not skill_home:
            logging.getLogger("osskill.context").warning(
                "SKILL_HOME 未设置，filesystem 路径校验降级（开发模式）",
            )
            return os.path.abspath(path)
        real = os.path.realpath(os.path.join(skill_home, path))
        if not real.startswith(skill_home + os.sep) and real != skill_home:
            raise PermissionError(f"越权文件访问: {path}")
        return real

    def read(self, path: str, encoding: str = "utf-8", size: int | None = None) -> str:
        """读取文件内容（可选 size 限制，防止 OOM）

        Args:
            path: 相对 SKILL_HOME 的文件路径
            encoding: 编码
            size: 最大读取字节数，超过则截断。None 表示不限（注意 OOM 风险）。

        Returns:
            文件内容（超过 size 时截断）
        """
        safe = self._safe(path)
        with open(safe, "r", encoding=encoding) as f:
            return f.read(size)

    def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        """写入文件，写入前检查配额（原子 check+reserve，消除 TOCTOU）

        B8: try_write_sync 预留后实际写入失败时调 release_sync 回滚，防配额泄漏。
        """
        safe = self._safe(path)

        # 配额检查
        skill_home = os.environ.get("SKILL_HOME", "")
        if skill_home:
            size = len(content.encode(encoding))
            try:
                quota = get_storage_quota()

                # 原子 check + reserve（锁内完成，消除 TOCTOU）
                allowed, msg = quota.try_write_sync(skill_home, size)
                if not allowed:
                    raise PermissionError(f"存储配额不足: {msg}")

                # 写入磁盘（B8：写入失败时回滚配额）
                try:
                    os.makedirs(os.path.dirname(safe), exist_ok=True)
                    with open(safe, "w", encoding=encoding) as f:
                        f.write(content)
                except (OSError, IOError):
                    quota.release_sync(skill_home, size)
                    raise
                # try_write_sync 已记账，无需再调 on_write
            except PermissionError:
                raise
            except ImportError:
                # storage_quota 不可用（开发环境），回退到磁盘使用率检查
                self._disk_usage_fallback(skill_home, path)
                os.makedirs(os.path.dirname(safe), exist_ok=True)
                with open(safe, "w", encoding=encoding) as f:
                    f.write(content)
        else:
            os.makedirs(os.path.dirname(safe), exist_ok=True)
            with open(safe, "w", encoding=encoding) as f:
                f.write(content)

    @staticmethod
    def _disk_usage_fallback(skill_home: str, path: str) -> None:
        """回退方案：磁盘使用率检查（storage_quota 不可用时）"""
        try:
            usage = shutil.disk_usage(skill_home)
            pct = usage.used / usage.total * 100
            if pct > 95:
                try:
                    from common.admin_message import create_admin_bus, AdminMessage
                    bus = create_admin_bus()
                    loop = asyncio.get_running_loop()
                    loop.create_task(bus.send(AdminMessage(
                        level="warning",
                        source="storage",
                        title="存储空间不足",
                        body=f"磁盘使用率 {pct:.0f}%，拒绝写入: {path}",
                        dedup_key=f"storage:quota:95:{skill_home}",
                    )))
                except (RuntimeError, ImportError):
                    pass
                raise PermissionError(
                    f"存储空间不足 ({pct:.0f}% 已用)，拒绝写入: {path}",
                )
        except OSError:
            logger = logging.getLogger("osskill.context")
            logger.warning("磁盘 I/O 异常，无法检查 %s 的使用率，拒绝写入", skill_home)
            raise PermissionError(
                f"磁盘 I/O 异常，无法确认存储空间，拒绝写入: {path}",
            )

    def listdir(self, path: str = "") -> list[str]:
        """列出目录内容"""
        safe = self._safe(path)
        return os.listdir(safe)

    def exists(self, path: str) -> bool:
        """检查路径是否存在"""
        try:
            safe = self._safe(path)
            return os.path.exists(safe)
        except PermissionError:
            return False

    def delete(self, path: str) -> None:
        """删除文件或目录"""
        safe = self._safe(path)
        if os.path.isdir(safe):
            shutil.rmtree(safe)
        else:
            os.remove(safe)


class _LLMProxy:
    """LLM 代理层代理（通过 IPC 到底座）"""

    def __init__(self, ipc_client: Any, permissions: set[str] | None = None):
        self._ipc = ipc_client
        self._permissions = permissions or set()

    async def chat(self, messages: list, **kwargs: Any) -> str:
        """调底座 LLM 代理层"""
        if self._ipc is None:
            raise RuntimeError("LLM 代理在 IPC 模式下不可用")
        result = await self._ipc.call("llm.chat", {
            "messages": messages,
            **kwargs,
        })
        return result.get("content", "")


class _APIProxy:
    """底座公开 API 代理（通过 IPC 到底座）"""

    def __init__(self, ipc_client: Any, permissions: set[str] | None = None):
        self._ipc = ipc_client
        self._permissions = permissions or set()

    @staticmethod
    def _unwrap(result: Any) -> Any:
        """解包 IPC 代理返回的信封 {"status": int, "data": ...} → data。

        父进程代理（xihe/agent_runtime）对 api.* 请求统一包装为
        {"status": resp.status_code, "data": <body>} 信封。
        此处解包后，Skill 调用方直接读取业务 payload（.get("results") 等），
        避免所有读路径因信封未解包而静默取空。
        仅当 result 是 dict 且含整型 status + data 键时才解包，
        与业务 payload 天然携带 {"status": "ok"}（字符串）等场景区分。
        """
        if isinstance(result, dict) and isinstance(result.get("status"), int) and "data" in result:
            return result["data"]
        return result

    async def get(self, path: str, params: dict | None = None) -> Any:
        """调底座 GET API"""
        return self._unwrap(await self._ipc.call("api.get", {"path": path, "params": params or {}}))

    async def post(self, path: str, body: dict | None = None) -> Any:
        """调底座 POST API"""
        return self._unwrap(await self._ipc.call("api.post", {"path": path, "body": body or {}}))

    async def put(self, path: str, body: dict | None = None) -> Any:
        """调底座 PUT API"""
        return self._unwrap(await self._ipc.call("api.put", {"path": path, "body": body or {}}))

    async def delete(self, path: str, params: dict | None = None) -> Any:
        """调底座 DELETE API"""
        return self._unwrap(await self._ipc.call("api.delete", {"path": path, "params": params or {}}))


# ── Skill 基类 ─────────────────────────

class Skill(ABC):
    """所有 Skill 的基类（R3 兼容）

    子类需覆盖：
      name / display_name / description / version — 类元数据
      execute(params) → dict — 执行入口

    可选覆盖生命周期钩子：
      on_load(ctx)       — Skill 被底座加载时调用。初始化资源、建立连接
      on_unload()        — Skill 被底座卸载时调用。释放资源、关闭连接
      on_data_purge()    — 卸载时调用（强制）。清理所有用户数据，个保法合规
      on_upgrade(from_ver, to_ver) — 版本升级前调用。迁移数据、处理不兼容变更
    """

    # ── 元数据（子类覆盖） ──
    name: str = ""
    display_name: str = ""
    description: str = ""
    category: str = ""
    version: str = "1.0.0"

    # Schema
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None

    # 依赖
    knowledge_deps: Optional[list[str]] = None
    tool_deps: Optional[list[str]] = None
    model_deps: str = ""

    def __init__(self):
        self._ctx: SkillContext | None = None
        self._manifest: SkillManifest | None = None
        # 初始化可变默认值（避免 dataclass field 与非 dataclass 子类不兼容）
        if self.input_schema is None:
            self.input_schema = {"type": "object", "properties": {}}
        if self.output_schema is None:
            self.output_schema = {"type": "object", "properties": {}}
        if self.knowledge_deps is None:
            self.knowledge_deps = []
        if self.tool_deps is None:
            self.tool_deps = []

    # ── 生命周期钩子 ─────────────────────

    async def on_load(self, ctx: SkillContext) -> None:
        """Skill 被底座加载时调用。初始化资源、建立连接。"""
        self._ctx = ctx

    async def on_unload(self) -> None:
        """Skill 被底座卸载时调用。释放资源、关闭连接。"""
        pass

    async def on_data_purge(self) -> None:
        """卸载时调用（强制）。清理所有用户数据，个保法合规。"""
        pass

    async def on_upgrade(self, from_version: str, to_version: str) -> None:
        """版本升级前调用。迁移数据、处理不兼容变更。"""
        pass

    # ── 进度播报（长操作通用）───────────

    @staticmethod
    def _progress_idempotency_key(agent_id: str, target: str, message: str) -> str:
        """进度消息幂等键：按 (agent, target, 消息文本) 派生。

        2026-08-11 大师实锤：进度消息重复投递（idempotency_key 空、无幂等去重）叠加
        语义路由误判 → 投标生成死循环。同文本重复投递 → 同键 → 下游可去重；不同进度
        文本 → 不同键，各自正常投递。
        """
        import hashlib
        raw = f"progress:{agent_id}:{target}:{message}"
        return "prog_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]

    def _start_progress(self, target_agent_id: str, initial_message: str):
        """开始进度播报：每 10s 向 target_agent_id 发送进度消息。

        用于直接 Skill 调用（非执策 task）路径的长操作反馈。
        Skill 在 execute() 内长操作前调用，操作中调 _update_progress()，
        完成后调 _stop_progress()。
        """
        import asyncio as _asyncio
        self._progress_target = target_agent_id
        self._progress_message = initial_message
        self._progress_active = True
        self._progress_task = _asyncio.create_task(self._progress_loop())

    def _update_progress(self, message: str):
        """更新进度消息文本，下个 10s 周期生效。"""
        self._progress_message = message

    async def _stop_progress(self, final_message: str = ""):
        """停止进度播报，可选发送最后一条完成消息。"""
        self._progress_active = False
        if hasattr(self, "_progress_task") and self._progress_task:
            self._progress_task.cancel()
            try:
                await self._progress_task
            except (asyncio.CancelledError, Exception):
                # 2026-08-06 fix: 原 except (Exception,) 接不住 CancelledError（BaseException），
                # bidding 前台路径 _stop_progress 的进度 task 取消会穿透到 skill_runner 主循环被吞 → exit 0
                pass
        if final_message:
            await self._send_progress_msg(final_message)

    async def _progress_loop(self):
        """进度播报后台循环。"""
        import asyncio as _asyncio
        while getattr(self, "_progress_active", False):
            await self._send_progress_msg(self._progress_message)
            await _asyncio.sleep(10)

    async def _send_progress_msg(self, message: str):
        """通过 IPC 代理发送进度消息给目标用户。

        IPC 优先（走父进程已建立的连接池）；IPC 不可用时回退到直接 HTTP
        （子进程内自行建连，QINGTIAN_API_URL 必须可达）。
        """
        if not message:
            return
        target = getattr(self, "_progress_target", "")
        if not target:
            return
        if not self._ctx:
            self._warn_progress_once("_ctx 未初始化，无法发送进度")
            return

        # 进度消息自描述为"非指令"（2026-08-11 大师实锤：进度消息被 execute_api 语义路由
        # 误判成新 generate_bid 死循环）：
        #   type=progress + kind=progress + source=work → 下游可识别为 skill 自身广播、
        #   非用户发起；idempotency_key 按 (agent,target,text) 派生 → 重复投递可去重。
        _idem = self._progress_idempotency_key(self._ctx.agent_id, target, message)
        _payload = {"text": f"⏳ {message}", "type": "progress",
                    "kind": "progress", "source": "work"}
        # 优先 IPC
        if self._ctx._ipc_client is not None:
            try:
                await self.ctx.api.post("/v1/huanyu/messages", {
                    "from_agent": self._ctx.agent_id,
                    "to_agent": target,
                    "message_type": "info",
                    "payload": _payload,
                    "subject": "进度播报",
                    "idempotency_key": _idem,
                })
                self._clear_progress_warned()  # 成功后重置 warning 标记
                return
            except Exception as e:
                self._warn_progress_once(f"IPC 发送进度失败: {e}，回退 HTTP")

        # 回退：直接 HTTP（子进程内自行建连）
        try:
            import os as _os
            import httpx
            base = _os.environ.get("QINGTIAN_API_URL", "http://127.0.0.1:1996")
            url = f"{base.rstrip('/')}/v1/huanyu/messages"
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={
                    "from_agent": self._ctx.agent_id,
                    "to_agent": target,
                    "message_type": "info",
                    "payload": _payload,
                    "subject": "进度播报",
                    "idempotency_key": _idem,
                })
        except Exception as e:
            self._warn_progress_once(f"进度消息发送失败(HTTP): {e}")

    def _warn_progress_once(self, msg: str):
        """进度相关的 warning 只打一次，避免每 10s 刷屏。"""
        if getattr(self, "_progress_warned", False):
            return
        self._progress_warned = True
        logger.warning("进度播报: %s", msg)

    def _clear_progress_warned(self):
        """进度发送成功后重置 warning 标记（下次失败仍会告警）。"""
        self._progress_warned = False

    # ── 公开执行入口 ─────────────────────

    @abstractmethod
    async def execute(self, params: dict) -> dict:
        """执行 Skill 逻辑，返回结构化结果"""
        ...

    async def validate(self, params: dict) -> list[str]:
        """参数校验，返回错误列表"""
        errors = []
        for required in self.input_schema.get("required", []):
            if required not in params:
                errors.append(f"缺少必填参数: {required}")
        return errors

    # ── 内部属性 ─────────────────────

    @property
    def ctx(self) -> SkillContext:
        if self._ctx is None:
            raise RuntimeError("Skill 尚未加载，ctx 不可用")
        return self._ctx

    @property
    def manifest(self) -> SkillManifest:
        if self._manifest is None:
            raise RuntimeError("Skill 尚未加载，manifest 不可用")
        return self._manifest


# ── 向后兼容别名 ─────────────────────────

BaseSkill = Skill
"""BaseSkill 已更名为 Skill，此别名供过渡期使用。"""


# ── 认知原语基类（设计文档 P1 §3.1，opt-in） ─────────────────

class CognizantSkill(Skill):
    """可选基类：给 skill 提供 ReAct 循环（设计文档 §3.1）。

    不继承本类的 skill 完全不受影响（opt-in）。子类可覆盖：
      - tools()         暴露内部动作作为 ReAct 工具
      - on_execution_failure()  执行彻底失败后的兜底（默认仅记录日志）
    """

    def tools(self) -> dict[str, Callable]:
        """子类暴露内部动作为工具，如 {'报价': self._action_generate_quote}。"""
        return {}

    async def react(self, goal: str, context: dict | None = None,
                    llm_call: Callable | None = None,
                    max_steps: int = 8) -> dict:
        """用 ReAct 循环执行多步目标。"""
        from common.cognition import CognitionRunner, run_with_replay
        runner = CognitionRunner(
            llm_call=llm_call or self._default_llm,
            tools=await self._build_tools(),
            max_steps=max_steps,
        )
        return await run_with_replay(runner, goal, context)

    async def _build_tools(self) -> dict:
        """默认：self.tools() + 内置 final_answer / llm_chat / recall。"""
        tools = dict(self.tools())
        tools["final_answer"] = self._tool_final_answer
        tools["llm_chat"] = self._tool_llm_chat
        tools["recall"] = self._tool_recall
        return tools

    async def _default_llm(self, goal, history, tools_desc, system_prompt=""):
        """默认 ReAct LLM 适配：走 common.llm 的 llm_call_react。"""
        from common.llm import llm_call_react
        return await llm_call_react(goal, history, tools_desc, system_prompt)

    async def _tool_final_answer(self, params: dict) -> dict:
        return {"ok": True, "answer": params.get("summary", "")}

    async def _tool_llm_chat(self, params: dict) -> dict:
        from common.llm import llm_chat
        text = await llm_chat(
            [{"role": "user", "content": params.get("prompt", "")}],
            caller=f"cognition:{self.name}",
        )
        return {"ok": True, "text": text}

    async def _tool_recall(self, params: dict) -> dict:
        """召回永恒记忆（设计文档 §3.1 内置工具）。

        响应为 SearchResponse{"results": [...]}，解包后仅把 results 列表喂给
        LLM，避免冗余 wrapper 噪音。
        """
        try:
            result = await self.ctx.api.post(
                "/v1/yongheng/memories/search",
                {"namespace": params.get("namespace", "default"),
                 "query": params.get("query", "")},
            )
            memories = result.get("results", result) if isinstance(result, dict) else result
            return {"ok": True, "memories": memories}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    async def on_execution_failure(self, params: dict, error: str,
                                   steps: list[dict]) -> dict:
        """执行彻底失败后的兜底（设计文档 §3.2）。
        默认：记录日志 + 返回失败。业务 skill 可覆盖升级人工（镇岳）。"""
        logger.warning("CognizantSkill %s 执行失败: %s", self.name, error)
        return {"ok": False, "error": error, "steps": steps}
