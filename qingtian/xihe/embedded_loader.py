"""EmbeddedLoader — embedded 模式 Skill 加载器

职责:
  1. 安全校验（已签名 + 无 system 权限）
  2. 实例化入口类（entry.class / entry.file）
  3. 生命周期钩子（优先调用实例方法，回退模块级函数）
  4. 路由注册（优先使用 Skill 自有 router）
  5. 后台任务注册（Skill 未自管时兜底）
  6. 实例跟踪，支持优雅关闭

用法:
    loader = EmbeddedLoader(app)
    manifest = load_manifest("/path/to/skill")
    await loader.load_skill(manifest)   # 读 skill.json 一键部署
    await loader.shutdown()              # 卸载全部
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
from typing import Any

from common.skill_manifest import SkillManifest

logger = logging.getLogger("xihe.embedded_loader")


class SkillLoadError(Exception):
    """Skill 加载失败"""


class EmbeddedLoader:
    """embedded 模式 Skill 加载器"""

    def __init__(self, app: Any, pool: Any = None):
        """
        Args:
            app: FastAPI 应用实例
            pool: 数据库连接池（可选，当 Skill 不自管 DB 时备用）
        """
        self._app = app
        self._pool = pool
        self._bg_tasks: dict[str, asyncio.Task] = {}
        self._loaded_skills: dict[str, SkillManifest] = {}
        self._skill_instances: dict[str, Any] = {}

    # ═══════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════

    async def load_skill(self, manifest: SkillManifest) -> None:
        """从 SkillManifest 加载一个 embedded Skill

        流程: 安全校验 → 实例化 → on_startup → 注册路由
        """
        name = manifest.name
        logger.info("Loading embedded skill: %s v%s", name, manifest.version)

        # 1. 安全校验
        self._check_security(manifest)

        # 2. 实例化入口类（不会抛出 ImportError，不可导入时返回 None）
        instance = self._instantiate_skill(manifest)
        if instance is not None:
            self._skill_instances[name] = instance

        # 3. 生命周期钩子（on_startup）
        await self._run_lifecycle(manifest, "on_startup", instance)

        # 4. 路由注册
        self._register_routes(manifest, instance)

        # 5. 后台任务（C15 R11: _register_bg_tasks 此前从未被调用，
        #    Skill 声明的 background_tasks 永不启动——补上启动点）
        self._register_bg_tasks(manifest)

        self._loaded_skills[name] = manifest
        logger.info("Embedded skill loaded: %s", name)

    async def unload_skill(self, name: str) -> None:
        """卸载 embedded Skill"""
        manifest = self._loaded_skills.pop(name, None)
        instance = self._skill_instances.pop(name, None)
        if not manifest and not instance:
            logger.warning("Skill not found: %s", name)
            return

        # 停止后台任务
        for task_name in list(self._bg_tasks.keys()):
            if task_name.startswith(f"{name}:"):
                task = self._bg_tasks.pop(task_name)
                task.cancel()

        # 生命周期钩子（on_shutdown）
        if manifest:
            await self._run_lifecycle(manifest, "on_shutdown", instance)
        logger.info("Skill unloaded: %s", name)

    # ═══════════════════════════════════════════════════════════
    # 实例化
    # ═══════════════════════════════════════════════════════════

    def _resolve_module_path(self, manifest: SkillManifest) -> str | None:
        """解析 entry.file 为完整模块路径

        优先：相对于项目根目录（cwd）的完整模块路径，支持 skill.py 内部相对导入。
        兜底：直接使用 entry.file 转 module path（适用于独立模块）。
        """
        entry_mod = manifest.entry.file.replace(".py", "").replace("/", ".")
        skill_dir = getattr(manifest, "_manifest_dir", "")
        if not skill_dir:
            return entry_mod

        try:
            rel = os.path.relpath(skill_dir, os.getcwd())
            if not rel.startswith(".."):
                # 相对路径 → 完整模块路径，例如 skills.workflow.skill（商业 Skill 已迁仓库根）
                pkg = rel.replace("\\", "/").replace("/", ".")
                return f"{pkg}.{entry_mod}" if entry_mod != "__init__" else pkg
        except Exception:
            pass
        return entry_mod

    def _instantiate_skill(self, manifest: SkillManifest) -> Any | None:
        """导入 entry 类并实例化

        从 manifest.entry.file 中导入 manifest.entry.class_name，
        返回实例。导入失败时返回 None（非致命，生命周期回退到模块级函数）。
        """
        if not manifest.entry.file or not manifest.entry.class_name:
            return None

        module_path = self._resolve_module_path(manifest)

        # 确保项目根目录在 sys.path 首位（防止 tests/ 中的同名包影子导入）
        cwd = os.getcwd()
        if cwd in sys.path:
            sys.path.remove(cwd)
        sys.path.insert(0, cwd)

        try:
            module = importlib.import_module(module_path)
        except ImportError:
            logger.debug(
                "Entry module '%s' not importable for %s, "
                "lifecycle will fall back to module-level lookup",
                module_path, manifest.name,
            )
            return None

        class_ = getattr(module, manifest.entry.class_name, None)
        if class_ is None:
            raise SkillLoadError(
                f"Entry class '{manifest.entry.class_name}' not found in {module_path}"
            )

        try:
            instance = class_()
        except Exception as e:
            raise SkillLoadError(
                f"Failed to instantiate {manifest.entry.class_name}: {e}"
            ) from e

        logger.debug("Instantiated %s for skill: %s", manifest.entry.class_name, manifest.name)
        return instance

    # ═══════════════════════════════════════════════════════════
    # 安全校验
    # ═══════════════════════════════════════════════════════════

    def _check_security(self, manifest: SkillManifest) -> None:
        """验证 Skill 签名（所有模式均需 Ed25519 签名）"""
        sig_hex = getattr(manifest, "_signature_hex", "")
        payload = getattr(manifest, "_canonical_payload", b"")

        if not sig_hex or not payload:
            raise SkillLoadError(
                f"Skill 缺少签名: {manifest.name}，请先用 skill_signer 签名"
            )

        try:
            sig = bytes.fromhex(sig_hex)
        except ValueError:
            raise SkillLoadError(
                f"Skill 签名格式错误: {manifest.name} (certificate 不是合法 hex)"
            )

        from common.crypto import DEV_PUBLIC_KEY_HEX, verify

        # R11 修复：验证公钥可由环境变量 QINGTIAN_SKILL_SIGN_PUBKEY 注入
        # （生产须指向正式签发公钥）。
        # P1 (2026-08-27 review #2): 原实现未配 env 时默认回退 DEV 公钥——历史开发
        # 私钥曾随旧版本公开，持有者可签发任意 Skill 过验签。改：DEV 锚仅显式
        # opt-in（QINGTIAN_ALLOW_DEV_TRUST=1，本地开发用），否则 fail-closed 拒载。
        import os

        pub_hex = os.environ.get("QINGTIAN_SKILL_SIGN_PUBKEY", "")
        if not pub_hex:
            if os.environ.get("QINGTIAN_ALLOW_DEV_TRUST", "") == "1":
                pub_hex = DEV_PUBLIC_KEY_HEX
                logger.warning(
                    "Skill '%s' 使用 DEV 信任锚验签（QINGTIAN_ALLOW_DEV_TRUST=1，"
                    "仅限本地开发——生产必须配置 QINGTIAN_SKILL_SIGN_PUBKEY）",
                    manifest.name,
                )
            else:
                raise SkillLoadError(
                    f"未配置 QINGTIAN_SKILL_SIGN_PUBKEY，拒绝加载 Skill: {manifest.name}"
                    f"（本地开发可设 QINGTIAN_ALLOW_DEV_TRUST=1 临时启用 DEV 信任锚）"
                )
        pub_key = bytes.fromhex(pub_hex)
        if not verify(pub_key, payload, sig):
            raise SkillLoadError(
                f"Skill 签名验证失败: {manifest.name}，证书可能已过期或被篡改"
            )

        # 签名验证通过后标记
        manifest._cert_verified = True

        # embedded 模式额外限制：禁止 system 权限
        mode = manifest.runtime.get("mode", "subprocess")
        if mode == "embedded" and "system" in manifest.permissions:
            raise SkillLoadError(
                f"embedded mode forbids 'system' permission: {manifest.name}"
                f"，请改用 subprocess 模式"
            )

        logger.debug("Security check passed: %s", manifest.name)

    # ═══════════════════════════════════════════════════════════
    # 路由注册
    # ═══════════════════════════════════════════════════════════

    def _register_routes(self, manifest: SkillManifest, instance: Any = None) -> None:
        """注册 Skill 路由

        优先使用 Skill 实例的 .router 属性（由 Skill 自主管理），
        否则按 manifest.routes 注册占位路由。
        """
        from fastapi import APIRouter

        # 收集已有路由用于冲突检测
        existing_paths: dict[str, list[str]] = {}
        for r in self._app.routes:
            if hasattr(r, "path") and hasattr(r, "methods"):
                existing_paths[r.path] = existing_paths.get(r.path, []) + list(r.methods)

        # 尝试使用 Skill 自有的 router
        if instance and hasattr(instance, "router") and instance.router is not None:
            router = instance.router
            # 从 Skill 的 router 提取路由声明做冲突检测
            for r in router.routes:
                path = getattr(r, "path", "")
                methods = list(getattr(r, "methods", set()) or set())
                for method in methods:
                    if path in existing_paths and method in existing_paths[path]:
                        raise SkillLoadError(
                            f"Route conflict: {method} {path} "
                            f"already registered by another skill"
                        )
            self._app.include_router(router)
            logger.info(
                "Registered %d routes from skill router: %s",
                len(router.routes),
                manifest.name,
            )
            return

        # 兜底：注册占位路由
        router = APIRouter(tags=[manifest.name])
        for route in manifest.routes:
            if route.path in existing_paths:
                if route.method.upper() in existing_paths[route.path]:
                    raise SkillLoadError(
                        f"Route conflict: {route.method} {route.path} "
                        f"already registered by another skill"
                    )
            self._register_single_route(router, route)

        self._app.include_router(router)
        logger.debug(
            "Registered %d placeholder routes for skill: %s",
            len(manifest.routes),
            manifest.name,
        )

    def _register_single_route(self, router: Any, route: Any) -> None:
        """注册单个占位路由"""
        method = route.method.upper()

        async def _placeholder(**kwargs):
            logger.info("Route called: %s %s (handler=%s)", method, route.path, route.handler)
            return {"status": "ok", "handler": route.handler}

        handlers = {"GET": router.get, "POST": router.post, "PUT": router.put,
                     "DELETE": router.delete, "PATCH": router.patch}
        handler = handlers.get(method)
        if handler:
            handler(route.path)(_placeholder)
        else:
            logger.warning("Unsupported HTTP method: %s", method)

    # ═══════════════════════════════════════════════════════════
    # 后台任务兜底
    # ═══════════════════════════════════════════════════════════

    def _register_bg_tasks(self, manifest: SkillManifest) -> None:
        """注册 Skill 声明的后台定时任务

        仅用于 Skill 不自管 bg task 时的兜底。
        若 Skill 实例有 start_bg_tasks 方法，跳过（由 Skill 自主管理）。
        """
        instance = self._skill_instances.get(manifest.name)
        if instance and hasattr(instance, "start_bg_tasks"):
            logger.debug("Skill %s manages own bg tasks, skipping loader fallback", manifest.name)
            return

        for task in manifest.background_tasks:
            task_name = f"{manifest.name}:{task.name}"
            if task_name in self._bg_tasks:
                continue

            # C15 (R11): 原 wrapper 只 sleep+log，从不调用 handler——
            # 启动即解析 handler（实例方法优先，其次模块级函数，与 _run_lifecycle 同规），
            # 循环内真正执行。
            handler_ref = None
            if instance:
                handler_ref = getattr(instance, task.handler, None)
            if not handler_ref and manifest.entry.file:
                try:
                    module = importlib.import_module(self._resolve_module_path(manifest))
                    handler_ref = getattr(module, task.handler, None)
                except ImportError:
                    pass

            async def _wrapper(
                _name: str = task.name,
                _interval: int = task.interval_seconds,
                _handler: str = task.handler,
                _handler_ref: Any = handler_ref,
            ):
                while True:
                    try:
                        await asyncio.sleep(_interval)
                        if _handler_ref and callable(_handler_ref):
                            if asyncio.iscoroutinefunction(_handler_ref):
                                await _handler_ref()
                            else:
                                _handler_ref()
                            logger.debug("Ran bg task: %s (handler=%s)", _name, _handler)
                        else:
                            logger.debug(
                                "Running bg task: %s (handler=%s 未解析到可调用对象)",
                                _name, _handler,
                            )
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.error("Background task %s failed: %s", _name, e)

            task_obj = asyncio.create_task(_wrapper(), name=task_name)
            self._bg_tasks[task_name] = task_obj

    # ═══════════════════════════════════════════════════════════
    # 生命周期钩子
    # ═══════════════════════════════════════════════════════════

    async def _run_lifecycle(self, manifest: SkillManifest, hook: str, instance: Any = None) -> None:
        """执行生命周期钩子

        解析顺序:
          1. 实例方法（如果 instance 有同名方法）
          2. 模块级函数（从 entry.file 中 import）
        """
        handler_name = getattr(manifest.lifecycle, hook, "")
        if not handler_name:
            return

        logger.debug("Running lifecycle %s → %s for %s", hook, handler_name, manifest.name)

        handler = None

        # 1. 实例方法
        if instance:
            handler = getattr(instance, handler_name, None)

        # 2. 模块级函数
        if not handler and manifest.entry.file:
            module_path = self._resolve_module_path(manifest)
            try:
                module = importlib.import_module(module_path)
                handler = getattr(module, handler_name, None)
            except ImportError:
                logger.debug("Module %s not importable for lifecycle hook %s", module_path, hook)

        if handler and callable(handler):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler()
                else:
                    handler()
                logger.info("Lifecycle %s completed for %s", hook, manifest.name)
            except Exception as e:
                logger.error("Lifecycle %s failed for %s: %s", hook, manifest.name, e)
        else:
            logger.debug("Lifecycle handler %s not found for %s", handler_name, manifest.name)

    # ═══════════════════════════════════════════════════════════
    # 查询 & 关闭
    # ═══════════════════════════════════════════════════════════

    def get_loaded_skills(self) -> dict[str, SkillManifest]:
        """返回已加载的 Skill 清单"""
        return dict(self._loaded_skills)

    def get_skill_instance(self, name: str) -> Any | None:
        """返回 Skill 实例"""
        return self._skill_instances.get(name)

    async def shutdown(self) -> None:
        """优雅关闭所有 embedded Skill"""
        for name in list(self._loaded_skills.keys()):
            await self.unload_skill(name)
        logger.info("All embedded skills shut down")
