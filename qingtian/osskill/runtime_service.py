from __future__ import annotations

"""运行时服务 — Skill 生命周期编排

职责：
- 安装/升级/卸载全流程编排
- 依赖级联管理（start/stop 顺序）
- 版本兼容性检查
- 通知 XiheRuntime 启动/停止子进程
- 发布生命周期事件到 Bus

卸载流程：
  1. 检查依赖（是否有其他 Skill 依赖本 Skill）
  2. 停止子进程（发 on_unload）
  3. 上报 deactivate 到 acssa.cn
  4. 调用 on_data_purge（清理用户数据，个保法合规）
  5. 清理包文件
  6. 更新数据库状态
  7. 发布 skill:unloaded 事件

升级流程：
  1. 检查版本兼容性
  2. 停止旧版本子进程
  3. 调用 on_upgrade(from_ver, to_ver)
  4. 加载新版本包
  5. 启动新版本子进程
  6. 发布 skill:upgraded 事件
"""

import asyncio
import json
import logging
import os
import shutil
from typing import Optional

from .database import SCHEMA
from .models import SkillLifecycleEvent

logger = logging.getLogger("osskill.runtime")


class LifecycleError(Exception):
    """生命周期编排异常"""
    pass


class DependencyError(LifecycleError):
    """依赖冲突"""
    pass


class VersionError(LifecycleError):
    """版本不兼容"""
    pass


class RuntimeService:
    """运行时服务 — Skill 生命周期编排

    用法:
        service = RuntimeService(runtime, registry)
        await service.install(skill_name, package_path)
        await service.uninstall(skill_name, agent_id)
        await service.upgrade(skill_name, new_version, package_path)
    """

    def __init__(self, xihe_runtime=None, skill_registry=None, bus=None,
                 acssa_client=None):
        self._runtime = xihe_runtime
        self._registry = skill_registry
        self._bus = bus  # common.bus.Bus 实例
        self._acssa_client = acssa_client

    # ── 安装流程 ─────────────────────────────

    async def install(
        self,
        skill_name: str,
        package_path: str,
        agent_id: str = "",
        version: str = "1.0.0",
        config: dict | None = None,
    ) -> dict:
        """安装 Skill 包

        Args:
            skill_name: Skill 名称
            package_path: 包目录路径
            agent_id: 所属 Agent
            version: 版本号
            config: 配置

        Returns:
            安装结果元数据

        Raises:
            DependencyError: 依赖不满足
            LifecycleError: 安装失败
        """
        # 1. 检查依赖
        await self._check_dependencies(skill_name, package_path)

        # 1.5 确保 License 存在（首次安装自动签发试用）
        manifest = None
        try:
            from .loader import ManifestLoader
            manifest = ManifestLoader.from_package_dir(package_path)
        except Exception:
            pass
        await self._ensure_skill_license(skill_name, manifest)

        # 2. 通知注册表重新加载
        if self._registry:
            self._registry.reload(skill_name)

        # 3. 启动子进程
        handle = None
        if self._runtime:
            handle = await self._runtime.launch_skill(
                skill_name=skill_name,
                agent_id=agent_id,
                config=config or {},
                version=version,
            )

        # 4. 上报激活事件到 acssa.cn
        if self._acssa_client:
            license_id = await self._get_license_id(skill_name)
            if license_id:
                try:
                    await self._acssa_client.report_activate(
                        license_id=license_id,
                        platform_key=self._get_platform_key(),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to report activate for '%s': %s",
                        skill_name, e,
                    )

        # 5. 发布事件
        await self._emit_event(SkillLifecycleEvent.LOADED, {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "version": version,
        })

        logger.info("Skill '%s' installed (v%s)", skill_name, version)
        return {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "version": version,
            "status": "installed",
        }

    # ── 卸载流程 ─────────────────────────────

    async def uninstall(
        self,
        skill_name: str,
        agent_id: str = "",
        purge_data: bool = True,
    ) -> dict:
        """卸载 Skill（完整卸载流程）

        Args:
            skill_name: Skill 名称
            agent_id: 所属 Agent
            purge_data: 是否清理用户数据（个保法合规）

        Returns:
            卸载结果

        Raises:
            DependencyError: 有其他 Skill 依赖本 Skill
            LifecycleError: 卸载失败
        """
        # 1. 检查反向依赖
        dependents = await self._find_dependents(skill_name)
        if dependents:
            raise DependencyError(
                f"Cannot uninstall '{skill_name}': "
                f"depended on by {dependents}",
            )

        # 2. 停止子进程（发送 on_unload）
        if self._runtime:
            try:
                await self._runtime.stop_skill(skill_name, agent_id=agent_id)
            except Exception as e:
                logger.warning(
                    "Error stopping skill '%s': %s", skill_name, e,
                )

        # 3. 上报去激活事件到 acssa.cn
        if self._acssa_client:
            license_id = await self._get_license_id(skill_name)
            if license_id:
                try:
                    await self._acssa_client.report_deactivate(
                        license_id=license_id,
                        platform_key=self._get_platform_key(),
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to report deactivate for '%s': %s",
                        skill_name, e,
                    )

        # 5. 清理用户数据（on_data_purge）
        if purge_data:
            await self._purge_skill_data(skill_name, agent_id)

        # 6. 通知注册表清理缓存
        if self._registry:
            self._registry.reload(skill_name)

        # 7. 发布事件
        await self._emit_event(SkillLifecycleEvent.UNLOADED, {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "purge_data": purge_data,
        })

        logger.info("Skill '%s' uninstalled (purge=%s)", skill_name, purge_data)
        return {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "status": "uninstalled",
            "purge_data": purge_data,
        }

    # ── 升级流程 ─────────────────────────────

    async def upgrade(
        self,
        skill_name: str,
        new_version: str,
        package_path: str,
        agent_id: str = "",
        config: dict | None = None,
        force: bool = False,
    ) -> dict:
        """升级 Skill 到新版本

        Args:
            skill_name: Skill 名称
            new_version: 目标版本号
            package_path: 新版本包目录路径
            agent_id: 所属 Agent
            config: 新配置（可选）
            force: 强制升级（跳过兼容性检查）

        Returns:
            升级结果
        """
        old_version = ""

        # 1. 检查版本兼容性
        if not force:
            old_version = await self._get_current_version(skill_name)
            if old_version:
                compat = self._check_version_compatibility(
                    old_version, new_version,
                )
                if not compat:
                    raise VersionError(
                        f"Version {old_version} -> {new_version} "
                        f"not compatible",
                    )

        # 2. 停止旧版本子进程
        if self._runtime:
            try:
                await self._runtime.stop_skill(skill_name, agent_id=agent_id)
            except Exception:
                pass  # 可能还未启动

        # 3. 调用 on_upgrade 钩子（如果子进程已响应）
        if old_version and self._runtime:
            try:
                handle = await self._runtime.get_handle(skill_name, agent_id)
                await handle._ipc_call("on_upgrade", {
                    "from_version": old_version,
                    "to_version": new_version,
                })
            except Exception:
                logger.info(
                    "on_upgrade not called (process may not be running)",
                )

        # 4. 加载新版本
        if self._registry:
            self._registry.reload(skill_name)

        # 5. 启动新版本子进程
        if self._runtime:
            await self._runtime.launch_skill(
                skill_name=skill_name,
                agent_id=agent_id,
                config=config or {},
                version=new_version,
            )

        # 6. 发布事件
        await self._emit_event(SkillLifecycleEvent.UPGRADED, {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "from_version": old_version,
            "to_version": new_version,
        })

        logger.info(
            "Skill '%s' upgraded: %s -> %s",
            skill_name, old_version or "?", new_version,
        )
        return {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "from_version": old_version,
            "to_version": new_version,
            "status": "upgraded",
        }

    # ── 吊销流程 ─────────────────────────────

    async def revoke(
        self,
        skill_name: str,
        agent_id: str = "",
        reason: str = "",
    ) -> dict:
        """吊销 Skill（License 违规或黑名单触发）

        与 uninstall 的区别：revoke 强制清理，不检查依赖。
        """
        # 强制停止 + 清理
        if self._runtime:
            try:
                await self._runtime.stop_skill(skill_name, agent_id=agent_id)
            except Exception:
                pass

        await self._purge_skill_data(skill_name, agent_id)

        if self._registry:
            self._registry.reload(skill_name)

        await self._emit_event(SkillLifecycleEvent.REVOKED, {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "reason": reason,
        })

        logger.warning(
            "Skill '%s' revoked (agent=%s, reason=%s)",
            skill_name, agent_id, reason,
        )
        return {
            "skill_name": skill_name,
            "agent_id": agent_id,
            "status": "revoked",
            "reason": reason,
        }

    # ── 依赖级联 ─────────────────────────────

    async def start_skill_with_deps(
        self,
        skill_name: str,
        agent_id: str = "",
    ) -> list[str]:
        """按依赖顺序启动 Skill（先启动依赖，再启动本 Skill）"""
        deps = await self._get_dependency_chain(skill_name)
        started = []

        for dep_name in deps:
            if self._runtime:
                try:
                    handle = await self._runtime.get_handle(dep_name, agent_id)
                    if not handle.is_running:
                        await self._runtime.launch_skill(
                            skill_name=dep_name, agent_id=agent_id,
                        )
                        started.append(dep_name)
                except Exception:
                    # 可能已经在运行
                    pass

        return started

    async def stop_skill_with_dependents(
        self,
        skill_name: str,
        agent_id: str = "",
    ) -> list[str]:
        """按依赖顺序停止 Skill（先停依赖者，再停本 Skill）"""
        dependents = await self._find_dependents(skill_name)
        stopped = []

        # 先停依赖本 Skill 的
        for dep_name in dependents:
            if self._runtime:
                try:
                    await self._runtime.stop_skill(dep_name, agent_id=agent_id)
                    stopped.append(dep_name)
                except Exception:
                    pass

        # 再停本 Skill
        if self._runtime:
            try:
                await self._runtime.stop_skill(skill_name, agent_id=agent_id)
                stopped.append(skill_name)
            except Exception:
                pass

        return stopped

    # ── 内部方法 ─────────────────────────────

    async def _check_dependencies(self, skill_name: str, package_path: str) -> None:
        """检查 Skill 的依赖是否满足"""
        from .loader import ManifestLoader

        manifest = ManifestLoader.from_package_dir(package_path)
        if manifest is None:
            return  # 无 manifest 跳过依赖检查

        deps = manifest.dependencies or {}
        skill_deps = deps.get("skills", {})

        if not skill_deps:
            return

        missing = []
        for dep_name, dep_version in skill_deps.items():
            current = await self._get_current_version(dep_name)
            if current is None:
                missing.append(f"{dep_name} (required: {dep_version})")
                continue
            if not self._check_version_compatibility(current, dep_version):
                missing.append(
                    f"{dep_name} (installed: {current}, required: {dep_version})",
                )

        if missing:
            raise DependencyError(
                f"Dependencies not satisfied for '{skill_name}': {missing}",
            )

    async def _find_dependents(self, skill_name: str) -> list[str]:
        """查找依赖本 Skill 的其他 Skill（基于 deps.py 拓扑图）"""
        from .deps import DependencyGraph

        graph = await self._build_full_graph()
        return graph.get_dependents(skill_name)

    async def _get_dependency_chain(self, skill_name: str) -> list[str]:
        """获取依赖链（从根依赖到本 Skill）"""
        from .deps import DependencyGraph

        graph = await self._build_full_graph()
        if not graph.has_node(skill_name):
            return [skill_name]
        return graph.load_order(skill_name)

    async def _build_full_graph(self) -> DependencyGraph:
        """从数据库构建完整的依赖图"""
        from .deps import DependencyGraph

        graph = DependencyGraph()
        try:
            from common.db import get_pool
            pool = await get_pool()
            rows = await pool.fetch(
                f"SELECT name, version FROM {SCHEMA}.skill_definitions WHERE status = 'active'",
            )
            for row in rows:
                graph.add_node(row["name"], version=row["version"])
            # 补充依赖关系（从 package manifest）
            rows2 = await pool.fetch(
                f"SELECT sd.name, sp.version FROM {SCHEMA}.skill_packages sp "
                f"JOIN {SCHEMA}.skill_definitions sd ON sd.id = sp.skill_id "
                "WHERE sp.status = 'installed'",
            )
            for row in rows2:
                if not graph.has_node(row["name"]):
                    graph.add_node(row["name"], version=row.get("version", "1.0.0"))
        except Exception:
            pass
        return graph

    async def _purge_skill_data(self, skill_name: str, agent_id: str) -> None:
        """清理 Skill 用户数据

        1. 调用 Skill.on_data_purge() 生命周期方法（个保法合规）
        2. 清理文件系统残留
        """
        # 1. 调用 on_data_purge 生命周期钩子
        try:
            from .loader import SkillLoader
            skill_cls = SkillLoader.load(skill_name)
            if skill_cls is not None:
                skill_instance = skill_cls()
                # 调用 on_data_purge（Skill 基类默认 pass，子类覆盖才生效）
                await skill_instance.on_data_purge()
                logger.info(
                    "Called on_data_purge() for skill '%s'", skill_name,
                )
        except Exception as e:
            logger.warning(
                "Failed to call on_data_purge() for '%s': %s", skill_name, e,
            )

        # 2. 清理文件系统残留
        from common.config import get as cfg_get

        data_dir = cfg_get("skill.data_dir", "")
        if not data_dir:
            return

        # C5 (R11): skill_name 未清洗直接拼路径 → 路径穿越可删任意目录。
        # 拒绝含分隔符/.. 的名称，且解析后路径必须仍在 data_dir 内（双保险）。
        if (not skill_name or skill_name in (".", "..")
                or "/" in skill_name or "\\" in skill_name):
            logger.warning("Refusing to purge data for unsafe skill_name %r", skill_name)
            return
        data_dir = os.path.abspath(data_dir)
        skill_data_path = os.path.abspath(os.path.join(data_dir, skill_name))
        if os.path.commonpath([data_dir]) != os.path.commonpath([data_dir, skill_data_path]):
            logger.warning(
                "Refusing to purge data outside data_dir for skill_name %r", skill_name,
            )
            return

        if os.path.isdir(skill_data_path):
            try:
                await asyncio.to_thread(shutil.rmtree, skill_data_path)
                logger.info("Purged filesystem data for skill '%s'", skill_name)
            except Exception as e:
                logger.warning("Failed to purge data for '%s': %s", skill_name, e)

    async def _get_current_version(self, skill_name: str) -> Optional[str]:
        """查询当前安装的 Skill 版本"""
        try:
            from common.db import get_pool
            pool = await get_pool()
            row = await pool.fetchval(
                f"SELECT version FROM {SCHEMA}.skill_definitions WHERE name = $1",
                skill_name,
            )
            return row
        except Exception:
            return None

    async def _get_license_id(self, skill_name: str) -> Optional[str]:
        """从本地 License 文件获取 license_id"""
        try:
            # P2 (R11): skill_name 未清洗直接拼路径 → 路径穿越可读取任意目录下
            # 的 .license 文件。复用 market_integration 的白名单校验 + 本地再做
            # commonpath 包含性校验（与 _purge_skill_data 的 C5 口径一致）。
            from .market_integration import _validate_skill_name
            _validate_skill_name(skill_name)
            from common.config import get as cfg_get
            data_dir = cfg_get("skill.data_dir", "")
            if not data_dir:
                return None
            data_dir = os.path.abspath(data_dir)
            path = os.path.abspath(os.path.join(data_dir, f"{skill_name}.license"))
            if os.path.commonpath([data_dir]) != os.path.commonpath([data_dir, path]):
                logger.warning(
                    "Refusing to read license outside data_dir for skill_name %r", skill_name,
                )
                return None
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                return data.get("license_id", "")
        except Exception:
            pass
        return None

    async def _ensure_skill_license(self, skill_name: str, manifest=None):
        """确保 Skill 有有效 License（试用或已购），首次安装自动签发试用。"""
        try:
            from .market_integration import LicenseManager
            mgr = LicenseManager()
            result = mgr.ensure_skill_license(skill_name, manifest)
            if result:
                logger.info("License 就绪: skill=%s type=%s expires=%s",
                            skill_name, result.get("license_type"),
                            result.get("expires_at", ""))
            return result
        except Exception as e:
            logger.warning("License 初始化跳过: skill=%s err=%s", skill_name, e)
            return None

    def _get_platform_key(self) -> str:
        """获取当前底座的 platform_key"""
        try:
            from common.config import get as cfg_get
            machine_id = cfg_get("machine_id", "")
            install_uuid = cfg_get("install_uuid", "")
            from common.crypto import platform_key
            return platform_key(machine_id, install_uuid)
        except Exception:
            return ""

    def _check_version_compatibility(self, current: str, required: str) -> bool:
        """检查版本兼容性（简化版：semver major 必须匹配）"""
        try:
            cur_parts = [int(p) for p in current.split(".")[:2]]
            req_parts = [int(p) for p in required.lstrip(">=^~").split(".")[:2]]
            # major 必须相同
            return cur_parts[0] == req_parts[0]
        except (ValueError, IndexError):
            return True  # 解析失败视为兼容

    async def _emit_event(self, event: str, data: dict) -> None:
        """发布事件到 Bus"""
        if self._bus is None:
            return
        try:
            await self._bus.emit(event, data)
        except Exception as e:
            logger.warning("Failed to emit event %s: %s", event, e)
