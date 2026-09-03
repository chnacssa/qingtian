"""Skill 清单解析器 — skill.json → SkillManifest dataclass

用法:
    manifest = load_manifest("/path/to/skill/dir")
    errors = validate_manifest(manifest)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("skill_manifest")


# ═══════════════════════════════════════════════════════════
# 嵌套 Dataclass
# ═══════════════════════════════════════════════════════════


@dataclass
class RouteDecl:
    """API 路由声明"""
    path: str = ""
    method: str = "GET"
    handler: str = ""
    auth: str = "token"
    rate_limit: int = 0  # 0 = unlimited


@dataclass
class EntryDecl:
    """入口声明"""
    class_name: str = ""
    file: str = ""


@dataclass
class AuthorDecl:
    """作者信息"""
    type: str = "enterprise"
    name: str = ""
    contact: str = ""
    website: str = ""


@dataclass
class Compliance:
    """合规声明"""
    data_handling: str = "local"
    gdpr: bool = False
    audit_log: bool = True


@dataclass
class LicenseInfo:
    """许可信息"""
    type: str = "subscription"
    retail_price_yuan: int = 0
    trial_days: int = 0
    refund_days: int = 0


@dataclass
class ResourceSpec:
    """资源规格"""
    cpu: str = "low"
    memory_mb: int = 128
    disk_mb: int = 50
    api_calls_per_minute: int = 100
    max_concurrent_requests: int = 10


@dataclass
class NetworkPolicy:
    """网络策略"""
    outbound_allowed: bool = False
    outbound_domains: list[str] = field(default_factory=list)
    inbound_port_required: bool = False
    inbound_port_range: list[int] = field(default_factory=list)


@dataclass
class EventDecl:
    """事件声明（emit）"""
    event: str = ""
    description: str = ""
    payload: dict[str, str] = field(default_factory=dict)


@dataclass
class EventSubscription:
    """事件订阅"""
    event: str = ""
    description: str = ""
    handler: str = ""


@dataclass
class BackgroundTask:
    """后台任务声明"""
    name: str = ""
    interval_seconds: int = 300
    handler: str = ""
    description: str = ""


@dataclass
class ConfigField:
    """配置项声明"""
    type: str = "string"
    default: Any = None
    description: str = ""


@dataclass
class SecretField:
    """密钥声明"""
    description: str = ""
    required: bool = False


@dataclass
class LifecycleHooks:
    """生命周期钩子"""
    on_install: str = ""
    on_uninstall: str = ""
    on_upgrade: str = ""
    on_schema: str = ""
    on_startup: str = ""
    on_shutdown: str = ""
    on_health_check: str = ""


@dataclass
class HealthCheck:
    """健康检查配置"""
    endpoint: str = "/health"
    interval_seconds: int = 30
    timeout_seconds: int = 5
    initial_delay_seconds: int = 5


@dataclass
class UpgradeConfig:
    """升级策略"""
    strategy: str = "graceful"
    max_downtime_seconds: int = 10
    rollback_enabled: bool = True


@dataclass
class DatabaseDecl:
    """数据库声明"""
    schema: str = "qingtian"
    tables: list[str] = field(default_factory=list)
    init_sql: str = ""
    migrations_dir: str = ""
    requires_pool: bool = True


@dataclass
class Monitoring:
    """监控配置"""
    metrics_port: int = 0
    export_prometheus: bool = False
    log_level: str = "info"


# ═══════════════════════════════════════════════════════════
# 主 Dataclass
# ═══════════════════════════════════════════════════════════


@dataclass
class SkillManifest:
    """完整 Skill 清单

    对应 skill.json 全部字段，含嵌套 dataclass。
    """
    # ── 基本信息 ──
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    description: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    icon: str = ""
    permissions: list[str] = field(default_factory=list)

    # ── 作者与合规 ──
    author: AuthorDecl = field(default_factory=AuthorDecl)
    compliance: Compliance = field(default_factory=Compliance)
    copyright: dict = field(default_factory=dict)
    license_info: LicenseInfo = field(default_factory=LicenseInfo)
    certificate: str = ""

    # ── 入口与兼容性 ──
    entry: EntryDecl = field(default_factory=EntryDecl)
    compatibility: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=lambda: {
        "mode": "subprocess",
        "lifecycle": "on_demand",
    })

    # ── 资源与网络 ──
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    network: NetworkPolicy = field(default_factory=NetworkPolicy)

    # ── 接口声明 ──
    routes: list[RouteDecl] = field(default_factory=list)
    events: dict = field(default_factory=lambda: {"emits": [], "subscribes": []})
    database: DatabaseDecl = field(default_factory=DatabaseDecl)
    config: dict[str, ConfigField] = field(default_factory=dict)
    secrets: dict[str, SecretField] = field(default_factory=dict)

    # ── 生命周期与后台任务 ──
    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)
    background_tasks: list[BackgroundTask] = field(default_factory=list)
    dependencies: dict = field(default_factory=dict)

    # ── 运维 ──
    health_check: HealthCheck = field(default_factory=HealthCheck)
    upgrade: UpgradeConfig = field(default_factory=UpgradeConfig)
    monitoring: Monitoring = field(default_factory=Monitoring)
    data_dirs: list[dict] = field(default_factory=list)

    # ── 内部状态（非 JSON 字段） ──
    _cert_verified: bool = False
    _manifest_dir: str = ""  # skill.json 所在目录（load_manifest 自动填充）
    _signature_hex: str = ""  # certificate 字段原始 hex（load_manifest 自动填充）
    _canonical_payload: bytes = b""  # 签名的正文明文（load_manifest 自动填充）


# ═══════════════════════════════════════════════════════════
# 解析
# ═══════════════════════════════════════════════════════════


def _parse_manifest(raw: dict) -> SkillManifest:
    """解析原始 JSON 字典为 SkillManifest"""
    m = SkillManifest()

    # 基本信息
    m.name = raw.get("name", "")
    m.display_name = raw.get("display_name", "")
    m.version = raw.get("version", "1.0.0")
    m.description = raw.get("description", "")
    m.category = raw.get("category", "")
    m.tags = raw.get("tags", [])
    m.icon = raw.get("icon", "")
    m.permissions = raw.get("permissions", [])
    m.certificate = raw.get("certificate", "")

    # Author
    author = raw.get("author", {})
    if author:
        m.author = AuthorDecl(
            type=author.get("type", "enterprise"),
            name=author.get("name", ""),
            contact=author.get("contact", ""),
            website=author.get("website", ""),
        )

    # Compliance
    comp = raw.get("compliance", {})
    if comp:
        m.compliance = Compliance(
            data_handling=comp.get("data_handling", "local"),
            gdpr=comp.get("gdpr", False),
            audit_log=comp.get("audit_log", True),
        )

    # Copyright / License
    m.copyright = raw.get("copyright", {})
    li = raw.get("license_info", {})
    if li:
        m.license_info = LicenseInfo(
            type=li.get("type", "subscription"),
            retail_price_yuan=li.get("retail_price_yuan", 0),
            trial_days=li.get("trial_days", 0),
            refund_days=li.get("refund_days", 0),
        )

    # Entry
    entry = raw.get("entry", {})
    if entry:
        m.entry = EntryDecl(
            class_name=entry.get("class", ""),
            file=entry.get("file", ""),
        )

    # Compatibility / Runtime
    m.compatibility = raw.get("compatibility", {})
    m.runtime = raw.get("runtime", {"mode": "subprocess", "lifecycle": "on_demand"})

    # Resources
    res = raw.get("resources", {})
    if res:
        m.resources = ResourceSpec(
            cpu=res.get("cpu", "low"),
            memory_mb=res.get("memory_mb", 128),
            disk_mb=res.get("disk_mb", 50),
            api_calls_per_minute=res.get("api_calls_per_minute", 100),
            max_concurrent_requests=res.get("max_concurrent_requests", 10),
        )

    # Network
    net = raw.get("network", {})
    if net:
        out = net.get("outbound", {})
        inp = net.get("inbound", {})
        m.network = NetworkPolicy(
            outbound_allowed=out.get("allowed", False),
            outbound_domains=out.get("allowed_domains", []),
            inbound_port_required=inp.get("port_required", False),
            inbound_port_range=inp.get("port_range", []),
        )

    # Routes
    for r in raw.get("routes", []):
        m.routes.append(RouteDecl(
            path=r.get("path", ""),
            method=r.get("method", "GET"),
            handler=r.get("handler", ""),
            auth=r.get("auth", "token"),
            rate_limit=r.get("rate_limit", 0),
        ))

    # Events
    ev = raw.get("events", {})
    if ev:
        emits = []
        for e in ev.get("emits", []):
            emits.append(EventDecl(
                event=e.get("event", ""),
                description=e.get("description", ""),
                payload=e.get("payload", {}),
            ))
        subs = []
        for s in ev.get("subscribes", []):
            subs.append(EventSubscription(
                event=s.get("event", ""),
                description=s.get("description", ""),
                handler=s.get("handler", ""),
            ))
        m.events = {"emits": emits, "subscribes": subs}

    # Database
    db = raw.get("database", {})
    if db:
        m.database = DatabaseDecl(
            schema=db.get("schema", "qingtian"),
            tables=db.get("tables", []),
            init_sql=db.get("init_sql", ""),
            migrations_dir=db.get("migrations_dir", ""),
            requires_pool=db.get("requires_pool", True),
        )

    # Config
    for key, val in raw.get("config", {}).items():
        m.config[key] = ConfigField(
            type=val.get("type", "string"),
            default=val.get("default"),
            description=val.get("description", ""),
        )

    # Secrets
    for key, val in raw.get("secrets", {}).items():
        m.secrets[key] = SecretField(
            description=val.get("description", ""),
            required=val.get("required", False),
        )

    # Lifecycle
    lc = raw.get("lifecycle", {})
    if lc:
        m.lifecycle = LifecycleHooks(
            on_install=lc.get("on_install", ""),
            on_uninstall=lc.get("on_uninstall", ""),
            on_upgrade=lc.get("on_upgrade", ""),
            on_schema=lc.get("on_schema", ""),
            on_startup=lc.get("on_startup", ""),
            on_shutdown=lc.get("on_shutdown", ""),
            on_health_check=lc.get("on_health_check", ""),
        )

    # Background tasks
    for t in raw.get("background_tasks", []):
        m.background_tasks.append(BackgroundTask(
            name=t.get("name", ""),
            interval_seconds=t.get("interval_seconds", 300),
            handler=t.get("handler", ""),
            description=t.get("description", ""),
        ))

    # Dependencies
    m.dependencies = raw.get("dependencies", {})

    # Health check
    hc = raw.get("health_check", {})
    if hc:
        m.health_check = HealthCheck(
            endpoint=hc.get("endpoint", "/health"),
            interval_seconds=hc.get("interval_seconds", 30),
            timeout_seconds=hc.get("timeout_seconds", 5),
            initial_delay_seconds=hc.get("initial_delay_seconds", 5),
        )

    # Upgrade
    ug = raw.get("upgrade", {})
    if ug:
        m.upgrade = UpgradeConfig(
            strategy=ug.get("strategy", "graceful"),
            max_downtime_seconds=ug.get("max_downtime_seconds", 10),
            rollback_enabled=ug.get("rollback_enabled", True),
        )

    # Monitoring
    mon = raw.get("monitoring", {})
    if mon:
        m.monitoring = Monitoring(
            metrics_port=mon.get("metrics_port", 0),
            export_prometheus=mon.get("export_prometheus", False),
            log_level=mon.get("log_level", "info"),
        )

    # Data dirs
    m.data_dirs = raw.get("data_dirs", [])

    return m


# ═══════════════════════════════════════════════════════════
# 签名载荷
# ═══════════════════════════════════════════════════════════


def canonical_json(data: dict) -> str:
    """产生确定性 JSON（排序 key，无多余空白），用作签名/验签的载荷"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ═══════════════════════════════════════════════════════════
# 加载
# ═══════════════════════════════════════════════════════════


def load_manifest(skill_dir: str) -> SkillManifest | None:
    """从目录加载 skill.json 文件

    Args:
        skill_dir: Skill 目录路径（包含 skill.json），或 skill.json 的路径

    Returns:
        SkillManifest 或 None（加载失败）
    """
    path = Path(skill_dir)
    if path.is_dir():
        path = path / "skill.json"

    if not path.exists():
        logger.error("skill.json not found: %s", path)
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Invalid skill.json: %s", e)
        return None
    except Exception as e:
        logger.error("Failed to read skill.json: %s", e)
        return None

    # 提取 certificate 后构建签名载荷（证书本身不参与签名）
    sig_hex = raw.get("certificate", "") or ""
    raw.pop("certificate", None)  # 移除后做 canonical JSON
    payload = canonical_json(raw).encode("utf-8")

    manifest = _parse_manifest(raw)
    manifest._manifest_dir = str(path.parent)
    manifest._signature_hex = sig_hex
    manifest._canonical_payload = payload
    manifest.certificate = sig_hex  # 恢复 certificate 字段（被 pop 移除）
    return manifest


# ═══════════════════════════════════════════════════════════
# 校验
# ═══════════════════════════════════════════════════════════


def validate_manifest(manifest: SkillManifest) -> list[str]:
    """校验 SkillManifest 的跨字段约束

    Returns:
        错误信息列表（空列表 = 通过）
    """
    errors: list[str] = []

    if not manifest.name:
        errors.append("name is required")
    if not manifest.entry.class_name:
        errors.append("entry.class is required")
    if not manifest.entry.file:
        errors.append("entry.file is required")

    # runtime mode 约束
    mode = manifest.runtime.get("mode", "subprocess")
    if mode == "embedded":
        if not manifest._cert_verified:
            errors.append("embedded mode requires certificate verification")
        if "system" in manifest.permissions:
            errors.append("embedded mode forbids 'system' permission")

    # 路由校验
    for r in manifest.routes:
        if not r.path:
            errors.append("route with empty path")
        if r.method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            errors.append(f"invalid HTTP method: {r.method}")

    # 事件命名规范检查
    for e in manifest.events.get("emits", []):
        if isinstance(e, EventDecl):
            parts = e.event.split(":")
            if len(parts) < 2:
                errors.append(
                    f"event name should follow domain:action convention: {e.event}"
                )

    return errors
