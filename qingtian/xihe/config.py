"""xihe 配置常量"""

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class XiheConfig:
    """羲和子进程管理器配置"""

    # ── 进程限制 ──
    max_processes: int = 64
    """最大并发 Skill 子进程数（含常驻 + on_demand 临时进程）"""

    max_processes_per_agent: int = 16
    """单个 Agent 的最大 Skill 子进程数"""

    # ── 常驻进程槽位 ──
    resident_slots: int = 32
    """常驻 Skill 保留的最大进程槽位数（多租户共享底座按内存档位约定：
    16G→48、32G→96、64G→192；main.py 启动时按 /proc/meminfo 自动推断，
    config.yaml xihe.resident_slots 可显式覆盖）。"""

    # ── on_demand 空闲释放 ──
    idle_timeout_seconds: int = 300
    """on_demand 模式下 Skill 空闲超时（秒），超时自动 unload"""

    # ── 按 Skill 独立 idle_timeout（秒）──
    per_skill_idle_timeout: dict[str, int] = field(default_factory=dict)
    """按 Skill 覆盖默认 idle_timeout。长任务 Skill（bidding/procurement）设 1800s
    （30min）避免生成/谈判中途被误杀；未在此映射的 Skill 走 idle_timeout_seconds。"""

    # ── 共享单例 Skill ──
    singleton_skills: list[str] = field(default_factory=lambda: ["workflow"])
    """这些 Skill 所有 agent 共享同一个进程（agent_id=_shared_），不按 agent 起多个。
    适用于逻辑无状态/同构的 Skill（如 workflow 工作流引擎）。"""

    # ── 常驻白名单 ──
    resident_skill_whitelist: list[str] = field(default_factory=lambda: [
        "work_secretary", "sales", "workflow",
    ])
    """常驻白名单：名单内的 Skill 无论声明/调用方如何指定，强制 resident 常驻。
    默认 work_secretary/sales/workflow 永不卸载；bidding/procurement 按需
    on_demand 启动（per_skill_idle_timeout 设 30min 长驻留防误杀）。"""

    # ── 收费 Skill 到期检查 ──
    license_checked_skills: list[str] = field(default_factory=list)
    """需要到期检查的收费 Skill（如 bidding/sales 按月收费）。到期后三处拦截：
    常驻巡检 stop 进程、launch 拒绝启动、execute 返回 402。"""

    license_check_interval: float = 900.0
    """常驻收费 Skill 到期巡检间隔（秒），默认 15 分钟。"""

    license_check_callback: Optional[Callable] = None
    """到期检查回调，由 main.py 注入。签名: async (skill_name, agent_id) -> bool。
    True=订阅/许可有效；False=到期。None 时不检查（放行）。"""

    # ── 内存限制（字节） ──
    memory_limit_bytes: int = 512 * 1024 * 1024  # 512 MiB
    """每个子进程的内存上限（POSIX RLIMIT_AS）"""

    # ── 按 Skill 内存上限覆盖（字节） ──
    per_skill_memory_limit_bytes: dict[str, int] = field(default_factory=lambda: {
        "bidding": 2 * 1024 * 1024 * 1024,  # 2 GiB
        "bid_prep": 2 * 1024 * 1024 * 1024,  # 2 GiB
    })
    """按 Skill 覆盖 memory_limit_bytes（_spawn 注入子进程 config.memory_limit_bytes，
    巡检 check_memory_pressure 同口径取有效限额）。
    bidding 2GiB：标书生成是图片/文档内存密集型（rapidocr+numpy/OpenBLAS 虚拟内存
    + PyMuPDF 业绩 PDF 逐页转图 + docx 批量嵌证照图），全局 512MiB 下 OpenBLAS
    直接 Memory allocation failed 崩进程（2026-08-27 线上实锤，靠重启才跑完）。
    bid_prep 2GiB：LibreOffice 无头转 .doc 是内存大头（153MB 大 zip 解出 49 文件，
    512MiB 下 std::bad_alloc 整链中断、.doc 全崩只剩 8 spec；2GiB 后 49 文件零失败
    18 spec——2026-08-29 小智线上实锤，临时 config.local.yaml override 验证后写死）。
    config.yaml xihe.per_skill_memory_limit_bytes 可整体覆盖（键=skill 名，
    值=字节数）。"""

    # ── 重启策略 ──
    restart_max_attempts: int = 5
    """最大重启尝试次数（超过后不再自动重启）"""

    restart_base_delay: float = 1.0
    """重启指数退避初始间隔（秒）"""

    restart_max_delay: float = 60.0
    """重启指数退避最大间隔（秒）"""

    # ── 启动超时 ──
    startup_timeout: float = 60.0
    """子进程启动超时（秒），包括 import 和 on_load"""

    # ── IPC ──
    ipc_request_timeout: float = 900.0
    """IPC 请求超时（秒）。长任务 Skill（bidding 标书生成：分章生成+AI 评审内循环
    10+ 次 LLM 调用）远超通用调用耗时，30s 会导致 execute 超时被误杀（2026-08-06
    线上 500 根因）。放宽到 15 分钟；后续异步化后可回退。"""

    # ── 健康检查 ──
    health_check_interval: float = 15.0
    """健康检查间隔（秒），0 = 禁用"""

    health_check_method: str = "ping"
    """健康检查 IPC 方法名"""

    # ── 技能查找路径 ──
    skill_search_paths: list[str] = field(default_factory=lambda: [
        "osskill.implementations",
    ])
    """Skill 实现搜索路径（Python 模块路径）"""

    # ── 存储隔离 ──
    data_dir: str = "/opt/qingtian/skills/data"
    """Skill 数据目录根路径。每个 Skill 在此下建 agent_id/skill_name/ 子目录"""

    # ── 出站连接白名单 ──
    egress_whitelist: list[str] = field(default_factory=lambda: [
        "127.0.0.1",             # 本地底座 API
        # 联邦 WireGuard 网段按部署自配（9-2 敏感清理：默认不预置生产网段，
        # 在 config.yaml xihe.egress_whitelist 追加本部署的网段，如 10.x.x.0/24）
        "open.bigmodel.cn",      # zhipu glm-5.3-flash（主 LLM，2026-08-26）
        "api.deepseek.com",      # deepseek（备用 LLM）
        "dashscope.aliyuncs.com",
    ])
    """异常出站检测白名单，支持 IP 前缀（如 10.0.0.0/8）和域名（启动时解析为 IP）"""
