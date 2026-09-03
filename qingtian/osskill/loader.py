"""Skill 加载器 — 支持两种加载模式

1. 直接导入模式（开发/测试，子进程使用）
   SkillLoader.load("bidding") → import skills.bidding.bidding.BiddingSkill

信任验证体系（专利保护）：
  验证链 S1→S2→S4→S5 的实现代码在开源目录下，但验证方法本身受 IPC-034/035 保护。
  fork 者删除验证步骤 → 验证失败 → untrusted
  fork 者保留验证步骤 → 使用专利方法 → 侵权

2. 包文件模式（市场分发）
   ManifestLoader.from_skill_json(path) → SkillManifest
   SkillLoader.load_from_package(path) → Skill 类
"""

import hashlib
import importlib
import json
import logging
import os
import time
from typing import Awaitable, Callable, Optional

from .models import Skill, SkillManifest
from .database import SCHEMA

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════
# 专利保护的证书验证（开源代码，方法受 IPC-034/035 保护）
# ════════════════════════════════════════════════════════
#
# 验证链 S1→S2→S4→S5 全部实现于此。
# 此代码故意开源 —— 即使看到代码，未经授权使用即侵犯专利权。
# fork 者不能删（删了验证失败），也不能留（留了侵权）。
#
# S1: 本地证书验签（专利解密方法）
# S2: 在线许可校验（通过闭源钩子调 acssa.cn）
# S4: 吊销状态检查
# S5: 时钟防回拨检测
# ════════════════════════════════════════════════════════

# 平台预置锚点公钥（IPC-034 权1-S1）
_PLATFORM_PUBKEY_HEX = ""
"""平台 Ed25519 公钥十六进制。构建时由 CI/CD 注入，fork 者可看到此值但无法伪造对应私钥的签名。

开发模式：如果 scripts/dev_platform_pubkey.hex 存在且 _PLATFORM_PUBKEY_HEX 为空，
自动加载开发公钥。生产环境由 CI/CD 或环境变量 QINGTIAN_PLATFORM_PUBKEY 注入。
"""

# 开发模式自动加载（仅当 _PLATFORM_PUBKEY_HEX 为空时）
if not _PLATFORM_PUBKEY_HEX:
    _dev_pubkey_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "scripts", "dev_platform_pubkey.hex"
    )
    _pubkey_env = os.environ.get("QINGTIAN_PLATFORM_PUBKEY", "")
    if _pubkey_env:
        _PLATFORM_PUBKEY_HEX = _pubkey_env.strip()
    elif os.path.isfile(_dev_pubkey_path):
        try:
            with open(_dev_pubkey_path, "r") as f:
                _PLATFORM_PUBKEY_HEX = f.read().strip()
        except OSError:
            pass


def _s1_verify_cert(cert_hex: str, skill_name: str) -> tuple[bool, str]:
    """S1: 本地证书验签 — 专利保护的解密方法

    对应 IPC-035 权1-S1。
    使用平台公钥验证 Skill 证书的 Ed25519 签名。
    验证内容包括：
      1. 签名者身份（必须是平台私钥签名）
      2. 证书主体（Skill 名称与 manifest 一致，防止证书盗用）
      3. 有效期（证书未过期）

    Args:
        cert_hex: 证书 hex（含签名 + 上下文字段）
        skill_name: Skill 名称，用于验证证书主体

    Returns:
        (is_valid, error_message)
    """
    if not _PLATFORM_PUBKEY_HEX:
        return False, "platform pubkey not configured"
    if not cert_hex:
        return False, "missing certificate"

    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.hazmat.primitives import serialization

        pubkey_bytes = bytes.fromhex(_PLATFORM_PUBKEY_HEX)
        if len(pubkey_bytes) != 32:
            return False, "invalid pubkey length"
        pubkey = ed25519.Ed25519PublicKey.from_public_bytes(pubkey_bytes)

        cert_bytes = bytes.fromhex(cert_hex)
        if len(cert_bytes) < 64:
            return False, "certificate too short"

        # 证书结构：[64字节签名] + [证书载荷]
        signature = cert_bytes[:64]
        payload = cert_bytes[64:]

        # 验证签名（载荷必须由平台私钥签名）
        try:
            pubkey.verify(signature, payload)
        except Exception:
            return False, "signature verification failed"

        # 解析载荷 JSON
        try:
            payload_data = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False, "invalid certificate payload"

        # 验证 Skill 名称匹配
        cert_skill = payload_data.get("skill", "")
        if cert_skill != skill_name:
            return False, f"certificate skill mismatch: {cert_skill} != {skill_name}"

        # 验证有效期
        now = time.time()
        not_before = payload_data.get("not_before", 0)
        not_after = payload_data.get("not_after", 0)
        if not_before > 0 and now < not_before:
            return False, "certificate not yet valid"
        if not_after > 0 and now > not_after:
            return False, "certificate expired"

        return True, ""

    except ImportError:
        return False, "cryptography library not available"
    except Exception as e:
        return False, f"cert verification error: {e}"


# ── 在线验证钩子（闭源 osskill-acssa .whl 注册，对应 S2+S4）──

VerifyHook = Callable[[str, SkillManifest, Optional[dict]], Awaitable[tuple[str, str]]]
"""验证钩子签名: (skill_name, manifest, license_data) → (trust_level, error)
trust_level: "trusted" | "untrusted" | "revoked"
"""

_verify_hook: Optional[VerifyHook] = None


def register_verify_hook(hook: VerifyHook) -> None:
    """注册在线验证钩子（闭源 osskill-acssa 在 install() 中调用）

    对应 IPC-035 权1-S2（在线许可校验）+ 权1-S4（吊销列表）。
    钩子负责向 acssa.cn 查询许可状态和吊销状态。
    即使没有钩子，S1 本地验签通过后 Skill 仍可 trusted。
    """
    global _verify_hook
    _verify_hook = hook
    logger.info("在线验证钩子已注册: %s", hook.__name__ if hasattr(hook, "__name__") else "unknown")


# ── S5 时钟防回拨检测 ──────────────────────────────────────

_S5_STATE_FILE: str = os.path.join(
    os.environ.get("QINGTIAN_SKILL_DATA_DIR", "/opt/qingtian/skills/data"),
    "s5_clock_state.json",
)
"""S5 时钟状态文件路径（P2 (R11)）。

R11 修复前此值为空字符串且没有任何调用方 set_s5_state_path() → S5 持久化
状态从未启用：_s5_load_state() 恒返回 0、_s5_save_state() 恒提前返回，
跨进程/跨重启/快照回滚检测全部失效。现改为有意义的默认路径（与
market_integration._DATA_DIR 同源，QINGTIAN_SKILL_DATA_DIR 可覆盖），
仍可用 set_s5_state_path() 显式指定（测试/嵌入方）。写盘失败静默降级
（_s5_save_state 捕获 OSError），不阻塞验证。
"""

_S5_MAX_SKEW: int = 300
"""允许的最大时钟回拨（秒）。默认 300 秒（5分钟）。
超过此值视为时钟回拨攻击，拒绝验证。
"""

_S5_LAST_TIME: float = 0.0
"""最近一次验证时的系统时间戳（单调时钟）。"""


def set_s5_state_path(path: str) -> None:
    """设置 S5 状态文件路径。

    Args:
        path: 状态文件路径（JSON，存 last_monotonic/last_wall 记录）
    """
    global _S5_STATE_FILE
    _S5_STATE_FILE = path


def set_s5_max_skew(seconds: int) -> None:
    """设置 S5 允许的最大时钟回拨（秒）。"""
    global _S5_MAX_SKEW
    _S5_MAX_SKEW = seconds


def _s5_load_state() -> tuple[float, float]:
    """从状态文件加载上次 (单调时钟, 墙钟) 记录。"""
    if not _S5_STATE_FILE:
        return 0.0, 0.0
    try:
        if os.path.isfile(_S5_STATE_FILE):
            with open(_S5_STATE_FILE, "r") as f:
                data = json.load(f)
            return (
                float(data.get("last_monotonic", 0.0)),
                float(data.get("last_wall", 0.0)),
            )
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return 0.0, 0.0


def _s5_save_state(monotonic: float, wall: float) -> None:
    """保存当前 (单调时钟, 墙钟) 到状态文件。"""
    if not _S5_STATE_FILE:
        return
    try:
        os.makedirs(os.path.dirname(_S5_STATE_FILE) or ".", exist_ok=True)
        with open(_S5_STATE_FILE, "w") as f:
            json.dump({"last_monotonic": monotonic, "last_wall": wall}, f)
    except OSError:
        pass


def _s5_check_clock_skew(skill_name: str) -> tuple[bool, str]:
    """S5: 时钟防回拨检测

    对应 IPC-035 权1-S5。

    利用 Python time.monotonic()（系统启动以来的单调时钟，不受系统时间调整影响）
    检测时钟回拨。如果单调时钟回拨（通常不会发生），说明系统时间被篡改。

    P2 (R11) 修复说明：
      - 持久化状态现同时记录 monotonic 与墙钟（time.time()）。
        单看 monotonic 无法区分"重启归零"（monotonic 回退但墙钟前进，正常）
        与"快照回滚/时钟篡改"（monotonic 与墙钟同时回退，攻击）。
        墙钟方向用于消歧：monotonic 回退而墙钟前进 → 重启，重建基准放行；
        两者同时回退 → 判定时钟回滚，拒绝。
      - 额外独立检查墙钟回退（即使 monotonic 未回退也拒绝——证书有效期校验
        依赖墙钟，攻击者仅回拨墙钟即可让过期证书看起来有效）。

    Args:
        skill_name: Skill 名称（仅用于日志）

    Returns:
        (ok, error_message)
    """
    global _S5_LAST_TIME

    now_mono = time.monotonic()
    now_wall = time.time()

    # 加载持久化值（进程重启后取上次记录）
    last_persisted_mono, last_persisted_wall = _s5_load_state()

    # 第一次检测（本进程无内存状态）：仍比对持久化墙钟，
    # 防"快照回滚 + 服务重启"绕过（状态文件留存回滚前的墙钟记录）。
    if _S5_LAST_TIME == 0.0:
        if last_persisted_wall > 0 and now_wall < last_persisted_wall - _S5_MAX_SKEW:
            skew = last_persisted_wall - now_wall
            logger.critical(
                "S5 WALL-CLOCK ROLLBACK DETECTED for '%s': rolled back %.1f seconds",
                skill_name, skew,
            )
            return False, f"clock rollback detected ({skew:.0f}s)"
        _S5_LAST_TIME = now_mono
        _s5_save_state(now_mono, now_wall)
        return True, ""

    # 同进程单调时钟回拨 → 真回拨（进程存活期间时钟被篡改）
    if now_mono < _S5_LAST_TIME - _S5_MAX_SKEW:
        skew = _S5_LAST_TIME - now_mono
        logger.critical(
            "S5 CLOCK SKEW DETECTED for '%s': monotonic rolled back %.1f seconds",
            skill_name, skew,
        )
        return False, f"clock skew detected ({skew:.0f}s rollback)"

    # 持久化单调时钟回退（跨进程/跨重启/快照回滚）——需区分重启 vs 攻击：
    #   重启：monotonic 归零但墙钟前进 → 重建基准放行
    #   快照回滚：monotonic 与墙钟同时回退 → 拒绝
    if last_persisted_mono > 0 and now_mono < last_persisted_mono - _S5_MAX_SKEW:
        wall_rolled_back = (
            last_persisted_wall > 0
            and now_wall < last_persisted_wall - _S5_MAX_SKEW
        )
        if wall_rolled_back:
            skew = last_persisted_mono - now_mono
            logger.critical(
                "S5 CLOCK ROLLBACK DETECTED for '%s': monotonic rolled back %.1fs "
                "and wall-clock rolled back %.1fs",
                skill_name, skew, last_persisted_wall - now_wall,
            )
            return False, f"clock rollback detected ({skew:.0f}s)"
        # 仅 monotonic 回退而墙钟前进 → 系统重启，重建基准
        logger.info("S5 monotonic reset for '%s' (reboot), re-baselining", skill_name)
        _S5_LAST_TIME = now_mono
        _s5_save_state(now_mono, now_wall)
        return True, ""

    # 墙钟相对持久化值回退（即使 monotonic 未回退也拒绝——证书有效期依赖墙钟）
    if last_persisted_wall > 0 and now_wall < last_persisted_wall - _S5_MAX_SKEW:
        skew = last_persisted_wall - now_wall
        logger.critical(
            "S5 WALL-CLOCK ROLLBACK DETECTED for '%s': rolled back %.1f seconds",
            skill_name, skew,
        )
        return False, f"clock rollback detected ({skew:.0f}s)"

    # 更新状态
    _S5_LAST_TIME = now_mono
    _s5_save_state(now_mono, now_wall)

    return True, ""


async def verify_skill(
    skill_name: str,
    manifest: SkillManifest,
    license_data: dict | None = None,
) -> tuple[str, str]:
    """验证 Skill 信任级别（完整验证链 S1→S2→S4→S5）

    对应 IPC-035 权1 完整流程。

    Args:
        skill_name: Skill 名称
        manifest: SkillManifest（须含 certificate 字段）
        license_data: License 数据（可选，用于许可校验）

    Returns:
        (trust_level, error)
        "trusted"  / ""                     — 验证通过
        "untrusted" / reason                — 验证失败
        "revoked" / "revoked: reason"       — 已被吊销
    """
    # ── S1: 本地证书验签（专利保护方法） ──
    cert_hex = getattr(manifest, "certificate", "") or ""
    s1_ok, s1_err = _s1_verify_cert(cert_hex, skill_name)

    if not s1_ok:
        # S1 失败：尝试在线验证（闭源钩子可能知道其他验证方式）
        if _verify_hook is not None:
            return await _verify_hook(skill_name, manifest, license_data)
        return "untrusted", f"S1 failed: {s1_err}"

    # ── S5: 时钟防回拨检测（S1 通过后才检查，减少无意义检测） ──
    s5_ok, s5_err = _s5_check_clock_skew(skill_name)
    if not s5_ok:
        return "untrusted", f"S5 failed: {s5_err}"

    # S1 通过：证书有效，进行 S2+S4 在线状态检查
    if _verify_hook is not None:
        try:
            hook_result = await _verify_hook(skill_name, manifest, license_data)
            hook_level, hook_err = hook_result
            if hook_level == "revoked":
                return "revoked", f"revoked: {hook_err}"
            # S2/S4 返回 untrusted 但 S1 通过 → 可能是离线环境，仍 trusted
            if hook_level == "untrusted":
                logger.warning(
                    "S1 passed but S2/S4 rejected '%s' — possible network issue",
                    skill_name,
                )
        except Exception as e:
            logger.warning("S2/S4 check failed for '%s': %s", skill_name, e)
            # 在线检查失败不阻塞，S1+已通过，仍 trusted
            pass

    return "trusted", ""


def _to_pascal(snake: str) -> str:
    """snake_case -> PascalCase"""
    return "".join(word.capitalize() for word in snake.split("_"))


class ManifestLoader:
    """Skill 清单解析器（解析 skill.json / manifest）"""

    @staticmethod
    def from_skill_json(path: str) -> Optional[SkillManifest]:
        """从 skill.json 文件解析 SkillManifest

        Args:
            path: skill.json 的完整路径

        Returns:
            SkillManifest 实例，解析失败返回 None
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning("Failed to load skill.json from %s: %s", path, e)
            return None

        return SkillManifest(
            name=data.get("name", ""),
            display_name=data.get("display_name", ""),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            icon=data.get("icon", ""),
            category=data.get("category", "tool"),
            tags=data.get("tags", []),
            author=data.get("author", {}),
            compliance=data.get("compliance", {"data_handling": "local"}),
            copyright=data.get("copyright", {"declaration": "", "license": ""}),
            license_info=data.get("license_info", {
                "type": "free",
                "retail_price_yuan": 0,
                "wholesale_ratio": 0.30,
                "trial_days": 7,
                "refund_days": 7,
            }),
            entry=data.get("entry", {"class": "", "file": "main.py"}),
            permissions=data.get("permissions", []),
            resources=data.get("resources", {
                "cpu": "low",
                "memory_mb": 128,
                "api_calls_per_minute": 20,
            }),
            dependencies=data.get("dependencies", {
                "qingtian": ">=2.0.0",
                "skills": {},
            }),
            certificate=data.get("certificate", ""),
        )

    @staticmethod
    def from_package_dir(package_dir: str) -> Optional[SkillManifest]:
        """从包目录加载 skill.json

        Args:
            package_dir: 包含 skill.json 的目录

        Returns:
            SkillManifest 实例
        """
        manifest_path = os.path.join(package_dir, "skill.json")
        return ManifestLoader.from_skill_json(manifest_path)


class SkillLoader:
    """动态加载 Skill 实现类

    支持开发模式（直接 import）和部署模式（从包加载）。
    """

    _base_path = "osskill.implementations"
    _skills_path = "skills"

    @classmethod
    def load(cls, skill_name: str, quiet: bool = False) -> Optional[type[Skill]]:
        """按 name 加载 Skill 类。

        搜索顺序：osskill.implementations.{name}（开源）→ skills.{name}（闭源）

        约定：
        - 类名: {SkillName}Skill（PascalCase）
        - 未找到时返回 None，不抛出异常
        - 异常隔离：import/语法错误被捕获，不影响其他 Skill 加载

        Args:
            skill_name: Skill 名称
            quiet: 未找到时不打 warning（warmup 会对非 {name}.{name} 形态的
                目录走 skill.json entry 分支，避免误报警告）
        """
        class_name = _to_pascal(skill_name) + "Skill"
        paths = [f"{cls._base_path}.{skill_name}.{skill_name}",
                 f"{cls._skills_path}.{skill_name}.{skill_name}"]

        for module_path in paths:
            try:
                module = importlib.import_module(module_path)
                skill_cls = getattr(module, class_name, None)
                if skill_cls is None:
                    continue
                if not issubclass(skill_cls, Skill):
                    continue
                return skill_cls
            except ModuleNotFoundError:
                continue
            except Exception:
                logger.exception("Unexpected error loading skill: %s", skill_name)
                return None
        if not quiet:
            logger.warning("Skill module not found for '%s' (tried %s)", skill_name, paths)
        return None

    @classmethod
    def load_from_package(cls, package_path: str) -> Optional[type[Skill]]:
        """从打包目录加载 Skill 类（部署模式）

        约定：
        - 包目录应包含 skill.json 和入口文件
        - skill.json 的 entry.file 指定入口文件名（默认 main.py）
        - entry.class 指定类名
        - package_path 会被临时添加到 sys.path
        """
        manifest = ManifestLoader.from_package_dir(package_path)
        if manifest is None:
            return None

        entry_file = manifest.entry.get("file", "main.py")
        entry_class = manifest.entry.get("class", "")

        if not entry_class:
            logger.warning("skill.json missing entry.class field")
            return None

        # 移除 .py 后缀得到模块名
        module_name = entry_file.removesuffix(".py")

        # 临时添加包目录到 sys.path
        old_path = list(__import__("sys").path)
        sys_mod = __import__("sys").modules
        try:
            __import__("sys").path.insert(0, package_path)
            # 不同包可能共用同名入口文件（如 main.py），importlib.import_module
            # 会命中 sys.modules 里上一个包缓存的模块 → 加载前先弹出缓存，
            # 保证读到的是当前包的真实代码。
            sys_mod.pop(module_name, None)
            module = importlib.import_module(module_name)
            skill_cls = getattr(module, entry_class, None)
            if skill_cls is None:
                logger.warning(
                    "Class %s not found in %s of package %s",
                    entry_class, entry_file, package_path,
                )
                return None
            if not issubclass(skill_cls, Skill):
                logger.warning("%s is not a Skill subclass", entry_class)
                return None
            return skill_cls
        except Exception:
            logger.exception("Failed to load skill from package: %s", package_path)
            return None
        finally:
            __import__("sys").path = old_path
            # 加载完成即移出缓存，避免影响后续同名入口的包加载
            sys_mod.pop(module_name, None)


class SkillRegistry:
    """Skill 类注册表（单例，LRU 缓存）

    缓存 Skill 类（不是实例），实例管理在 XiheRuntime。
    开发模式下使用直接导入，部署模式下使用包加载。
    """

    _instance = None
    _cache: dict[str, type[Skill]] = {}
    _MAX_CACHE = 64

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get(self, skill_name: str) -> Optional[type[Skill]]:
        """获取 Skill 类（缓存命中直接返回）

        优先从包缓存目录加载，找不到时回退到直接导入。
        """
        if skill_name not in self._cache:
            # 尝试包加载
            skill_cls = self._try_load_package(skill_name)
            if skill_cls is None:
                # 回退到直接导入
                skill_cls = SkillLoader.load(skill_name)
            if skill_cls is None:
                return None
            # LRU 淘汰
            if len(self._cache) >= self._MAX_CACHE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[skill_name] = skill_cls
        return self._cache[skill_name]

    def _try_load_package(self, skill_name: str) -> Optional[type[Skill]]:
        """尝试从包目录加载"""
        from common.config import get as cfg_get
        pkg_root = cfg_get("skill.package_dir")
        if not pkg_root:
            return None
        pkg_path = os.path.join(pkg_root, skill_name)
        if not os.path.isdir(pkg_path):
            return None
        return SkillLoader.load_from_package(pkg_path)

    def reload(self, skill_name: str):
        """强制重新加载（版本升级后调用）"""
        self._cache.pop(skill_name, None)

    def clear(self):
        """清空缓存"""
        self._cache.clear()


def get_registry() -> SkillRegistry:
    """获取全局注册表单例"""
    return SkillRegistry()


def _classify_skill_dir(scan_dir: str, entry: str, import_base: str) -> str:
    """判定单个 Skill 目录的形态。

    系统存在两种 Skill 形态：
      1. Agent 绑定型：`{name}.py` + `{name}Skill` 继承 Skill → SkillLoader.load
      2. 嵌入式常驻型：skill.json 声明 entry.file/entry.class（如 workflow，
         入口是 skill.py，类不继承 Skill，由 main.py 按 workflow.enabled 直挂）
    既无 `{name}.py` 形态也无 skill.json entry 的目录（模板/演示，如
    examples/portal）不是 Skill 包，跳过。

    Args:
        scan_dir: 扫描根目录（implementations/ 或 skills/）
        entry: 目录名
        import_base: 扫描目录对应的 import 前缀（osskill.implementations / skills）

    Returns:
        "loaded" | "failed" | "skipped"
    """
    if SkillLoader.load(entry, quiet=True) is not None:
        return "loaded"
    skill_dir = os.path.join(scan_dir, entry)
    manifest = ManifestLoader.from_package_dir(skill_dir)
    manifest_entry = getattr(manifest, "entry", None) or {}
    entry_file = manifest_entry.get("file", "")
    entry_class = manifest_entry.get("class", "")
    if not (entry_file and entry_class):
        # 既非 Agent 绑定型、也无 manifest 入口 → 非 Skill 目录
        logger.debug("skip non-skill dir: %s", entry)
        return "skipped"
    # 嵌入式常驻型：按 skill.json entry 验证入口模块可导入 + 类存在
    module_name = entry_file.removesuffix(".py")
    try:
        module = importlib.import_module(f"{import_base}.{entry}.{module_name}")
    except Exception as e:
        logger.warning("Manifest skill '%s' entry import failed: %s", entry, str(e)[:120])
        return "failed"
    if getattr(module, entry_class, None) is None:
        logger.warning("Manifest skill '%s' class %s not found in %s",
                       entry, entry_class, entry_file)
        return "failed"
    logger.info("Manifest skill loaded: %s (entry %s, class %s)", entry, entry_file, entry_class)
    return "loaded"


async def warmup_skills():
    """预热所有已注册的 Skill，失败不阻塞启动。

    扫描 open-source（osskill/implementations/）+ 闭源（skills/）两目录。
    识别两种 Skill 形态（Agent 绑定型 + 嵌入式常驻型）；既非两种形态的
    模板/演示目录跳过不计 failed。
    """
    import os as _os
    loaded = 0
    failed = 0
    skipped = 0
    # 开源 Skill
    _base = _os.path.dirname(__file__)
    impl_dir = _os.path.join(_base, "implementations")
    # 闭源 Skill：仓库根 skills/（__file__→osskill/loader.py→parent→parent=opensource/qingtian）
    skills_dir = _os.path.join(_os.path.dirname(_base), "..", "..", "skills")
    for scan_dir, import_base in (
        (impl_dir, SkillLoader._base_path),
        (_os.path.normpath(skills_dir), SkillLoader._skills_path),
    ):
        if not _os.path.isdir(scan_dir):
            continue
        for entry in _os.listdir(scan_dir):
            if entry.startswith("_"):
                continue
            skill_dir = _os.path.join(scan_dir, entry)
            if not _os.path.isdir(skill_dir):
                continue
            init_file = _os.path.join(skill_dir, "__init__.py")
            if not _os.path.isfile(init_file):
                continue
            result = _classify_skill_dir(scan_dir, entry, import_base)
            if result == "loaded":
                loaded += 1
            elif result == "failed":
                failed += 1
            else:
                skipped += 1
    # warmup 后同步 DB schema（修复语义路由 action 枚举过期问题）
    await _sync_skill_schemas_to_db()
    logger.info("Skill warmup: %d loaded, %d failed, %d skipped", loaded, failed, skipped)


async def _sync_skill_schemas_to_db() -> None:
    """从 Skill 类的 input_schema/output_schema 属性同步到 DB。

    语义路由 _llm_semantic_probe 从 skill_definitions.input_schema 读 action 枚举。
    注意：商业 Skill（procurement/sales/bidding 等）的权威 action 枚举定义在
    Skill 类属性（如 ProcurementSkill.input_schema.properties.action.enum），
    不在 skill.json（skill.json 用 commands 结构，无 input_schema 字段）。
    故此处通过 SkillLoader.load 加载 Skill 类，从类的 input_schema/output_schema
    属性提取回写 DB，幂等（COALESCE 不覆盖已有非空字段）。
    """
    from common.db import get_pool

    pool = await get_pool()
    synced = 0
    # 与 warmup_skills 相同的扫描来源：开源 implementations + 闭源 skills/
    import os as _os
    _base = _os.path.dirname(__file__)
    impl_dir = _os.path.join(_base, "implementations")
    skills_dir = _os.path.join(_os.path.dirname(_base), "..", "..", "skills")
    candidates = set()
    for scan_dir in (impl_dir, _os.path.normpath(skills_dir)):
        if not _os.path.isdir(scan_dir):
            continue
        for entry in _os.listdir(scan_dir):
            if entry.startswith("_"):
                continue
            if not _os.path.isdir(_os.path.join(scan_dir, entry)):
                continue
            candidates.add(entry)
    for entry in sorted(candidates):
        try:
            cls = SkillLoader.load(entry)
            if cls is None:
                continue
            in_schema = getattr(cls, "input_schema", None)
            out_schema = getattr(cls, "output_schema", None)
            if not in_schema and not out_schema:
                continue
            async with pool.acquire() as conn:
                await conn.execute(
                    f"""UPDATE {SCHEMA}.skill_definitions
                        SET input_schema = COALESCE($2::jsonb, input_schema),
                            output_schema = COALESCE($3::jsonb, output_schema),
                            updated_at = NOW()
                        WHERE name = $1""",
                    entry,
                    json.dumps(in_schema) if in_schema else None,
                    json.dumps(out_schema) if out_schema else None,
                )
            synced += 1
            logger.info("Skill schema synced: %s (actions=%d)", entry,
                        len(in_schema.get("properties", {}).get("action", {}).get("enum", [])) if isinstance(in_schema, dict) else 0)
        except Exception as e:
            logger.warning("Skill schema sync failed for %s: %s", entry, e)
    if synced:
        logger.info("Skill schema sync: %d skills updated to DB", synced)


async def load_agent_skills(agent_id: str) -> dict[str, type[Skill]]:
    """Agent 启动时加载绑定的 Skill（管理服未部署时返回空列表）"""
    from common.db import get_pool

    pool = await get_pool()
    try:
        rows = await pool.fetch(f"""
            SELECT sd.name, sd.input_schema, sd.output_schema,
                   ask.config, ask.pinned_version
            FROM {SCHEMA}.agent_skills ask
            JOIN {SCHEMA}.skill_definitions sd ON sd.id = ask.skill_id
            WHERE ask.agent_id = $1 AND ask.is_active = TRUE
              AND sd.status = 'active'
        """, agent_id)
    except Exception:
        logger.warning("agent_skills table not ready (management server not deployed yet)")
        return {}

    registry = get_registry()
    skills = {}
    for row in rows:
        skill_cls = registry.get(row["name"])
        if skill_cls is not None:
            skills[row["name"]] = skill_cls
    return skills


async def reload_agent_skills(agent_id: str, context):
    """运行时重新加载绑定 Skill，不重启进程"""
    from common.db import get_pool

    pool = await get_pool()
    try:
        rows = await pool.fetch(f"""
            SELECT sd.name, sd.input_schema, sd.output_schema,
                   ask.config, ask.pinned_version
            FROM {SCHEMA}.agent_skills ask
            JOIN {SCHEMA}.skill_definitions sd ON sd.id = ask.skill_id
            WHERE ask.agent_id = $1 AND ask.is_active = TRUE
              AND sd.status = 'active'
        """, agent_id)
    except Exception:
        logger.warning("agent_skills table not ready during reload")
        return

    registry = get_registry()
    loaded = 0
    for row in rows:
        name = row["name"]
        skill_cls = registry.get(name)
        if skill_cls is not None:
            if hasattr(context, "register_skill"):
                context.register_skill(name, skill_cls)
            loaded += 1
        else:
            logger.warning("Reload: skill %s not found in registry", name)
    logger.info("Agent %s: reloaded %d skills", agent_id, loaded)
