"""技能库 — Skill 定义、动态加载、运行时管理（R3）"""

from .models import (
    Skill,
    SkillContext,
    SkillManifest,
    SkillLifecycleEvent,
)
from .loader import (
    ManifestLoader,
    SkillLoader,
    SkillRegistry,
    get_registry,
    warmup_skills,
    load_agent_skills,
)
from .runtime_service import RuntimeService
# 闭源模块（osskill-acssa .whl 提供），开发环境可能缺失
try:
    from osskill_acssa.acssa_client import AcssaClient  # type: ignore
except ImportError:
    AcssaClient = None  # type: ignore
try:
    from osskill_acssa.license_manager import SkillLicenseManager, verify_skill_license  # type: ignore
except ImportError:
    SkillLicenseManager = None  # type: ignore
    verify_skill_license = None  # type: ignore
try:
    from osskill_acssa.revocation_service import RevocationService  # type: ignore
except ImportError:
    RevocationService = None  # type: ignore
from .scheduler import SkillScheduler
from .monitor import Monitor
from .backup import create_backup, restore_backup, list_backups
from .deps import DependencyGraph, check_version_compatible, resolve_load_order

# 开源版市场集成（osskill_acssa 未安装时的备用实现）
try:
    from .market_integration import (
        MarketGateway,
        LicenseManager,
        RevocationManager,
        SkillPackageManager,
    )
except ImportError:
    MarketGateway = LicenseManager = RevocationManager = SkillPackageManager = None

__all__ = [
    "Skill",
    "SkillContext",
    "SkillManifest",
    "SkillLifecycleEvent",
    "ManifestLoader",
    "SkillLoader",
    "SkillRegistry",
    "get_registry",
    "warmup_skills",
    "load_agent_skills",
    "RuntimeService",
    "SkillLicenseManager",
    "verify_skill_license",
    "AcssaClient",
    "RevocationService",
    "SkillScheduler",
    "Monitor",
    "create_backup",
    "restore_backup",
    "list_backups",
    "DependencyGraph",
    "check_version_compatible",
    "resolve_load_order",
    # 开源版市场集成
    "MarketGateway",
    "LicenseManager",
    "RevocationManager",
    "SkillPackageManager",
]
