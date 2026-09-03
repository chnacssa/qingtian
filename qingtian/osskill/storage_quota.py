"""Skill 存储配额管理 — 记账式写入检查 + 阈值告警 + 临时扩容

配额等级（skill.json resources.storage_mb）:
  tiny=50, small=200, medium=500, large=2000, unlimited=0

三级弹性（10% 缓冲空间，不粗暴禁止）：
  90%  → 预警（日志告警，正常写入）
  100% → 紧急告警（通知管理员，仍可写入）
  110% → 弹性上限（拒绝写入，等管理员扩容或清理）

扩容 API: POST /api/v1/skills/{id}/storage/expand
    倍数 1.5-3x，有效期 1-72h，每个版本仅限 1 次
"""

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("osskill.storage_quota")

# ── 配额级别定义 ──────────────────────────────────

QUOTA_LEVELS = {
    "tiny": 50,
    "small": 200,
    "medium": 500,
    "large": 2000,
    "unlimited": 0,  # 0 = 不限
}

_DEFAULT_QUOTA = "medium"  # 未声明的 Skill 默认 500MB

_ELASTIC_RATIO = 1.1  # 10% 弹性空间：100% 告警但不断写，到 110% 才拒绝


def quota_from_skill_json(skill_dir: str) -> tuple[int, str]:
    """从 skill.json 解析配额

    Args:
        skill_dir: Skill 目录（含 skill.json）

    Returns:
        (quota_bytes, level_name)
        quota_bytes=0 表示不限
    """
    import json
    manifest_path = os.path.join(skill_dir, "skill.json")
    if not os.path.isfile(manifest_path):
        return QUOTA_LEVELS[_DEFAULT_QUOTA] * 1024 * 1024, _DEFAULT_QUOTA

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        resources = manifest.get("resources", {})
        storage_mb = resources.get("storage_mb", QUOTA_LEVELS[_DEFAULT_QUOTA])
        if storage_mb <= 0:
            return 0, "unlimited"

        # 匹配到最近的等级名
        for level_name, level_mb in sorted(QUOTA_LEVELS.items(),
                                            key=lambda x: x[1]):
            if level_mb > 0 and storage_mb <= level_mb:
                return storage_mb * 1024 * 1024, level_name

        return storage_mb * 1024 * 1024, "large"
    except (json.JSONDecodeError, OSError):
        return QUOTA_LEVELS[_DEFAULT_QUOTA] * 1024 * 1024, _DEFAULT_QUOTA


@dataclass
class QuotaState:
    """单个 Skill 的配额状态"""
    skill_home: str
    quota_bytes: int  # 字节，0=不限
    level: str  # tiny/small/medium/large/unlimited
    usage_bytes: int = 0  # 当前记账用量
    last_calibrated: float = 0.0  # 上次校准时间戳
    expanded: bool = False
    expand_ratio: float = 1.0
    expand_until: float = 0.0  # 扩容到期时间戳


class StorageQuota:
    """存储配额管理器（进程内单例）

    用例:
        quota = StorageQuota()
        quota.register("home/agent_x/skill_a", quota_bytes=500*1024*1024)

        # 原子检查+记账（推荐）
        allowed, msg = quota.try_write_sync("home/agent_x/skill_a", 1024)
        if not allowed:
            raise PermissionError("配额不足")
        # try_write_sync 已记账，无需再调 on_write

        # 写入失败时回滚
        quota.release_sync("home/agent_x/skill_a", 1024)

        # 后台校准
        await quota.calibrate("home/agent_x/skill_a")
    """

    def __init__(self):
        self._states: dict[str, QuotaState] = {}
        self._lock = threading.Lock()

    def register(
        self,
        skill_home: str,
        quota_bytes: int,
        level: str = "medium",
    ) -> None:
        """注册一个 Skill 的配额

        首次注册或配额变更时调用。已存在的 skill_home 会更新配额。
        """
        self._states[skill_home] = QuotaState(
            skill_home=skill_home,
            quota_bytes=quota_bytes,
            level=level,
        )
        logger.info(
            "Quota registered for %s: %d bytes (level=%s)",
            skill_home, quota_bytes, level,
        )

    def unregister(self, skill_home: str) -> None:
        """卸载 Skill 时清理配额状态"""
        self._states.pop(skill_home, None)

    def get_state(self, skill_home: str) -> Optional[QuotaState]:
        """获取配额状态"""
        return self._states.get(skill_home)

    def _effective_quota(self, skill_home: str) -> int:
        """计算有效配额（考虑临时扩容）"""
        state = self._states.get(skill_home)
        if state is None:
            return 0  # 未注册 → 不限

        base = state.quota_bytes
        if state.expanded and time.time() < state.expand_until:
            return int(base * state.expand_ratio)
        elif state.expanded:
            # 扩容已过期
            state.expanded = False
            state.expand_ratio = 1.0
        return base

    def try_write_sync(self, skill_home: str, size: int) -> tuple[bool, str]:
        """原子检查+预留配额（线程安全）

        在锁内同时完成配额检查和记账，消除 check→write 之间的 TOCTOU 窗口。
        写入失败时调用 release_sync() 回滚。

        Returns:
            (allowed, message)
            (True, "") — 预留成功，usage_bytes 已递增
            (False, "原因") — 配额不足，未记账
        """
        with self._lock:
            effective = self._effective_quota(skill_home)
            if effective == 0:
                state = self._states.get(skill_home)
                if state:
                    state.usage_bytes += size
                return True, ""  # 不限

            state = self._states.get(skill_home)
            if state is None:
                return True, ""  # 未注册 → 放行（开发模式）

            current = state.usage_bytes
            strict_limit = int(effective * _ELASTIC_RATIO)  # 110% 弹性上限
            if current + size > strict_limit:
                return False, (
                    f"存储配额已超弹性上限 ({current + size} > {strict_limit})"
                )

            # 原子记账（预留配额）
            state.usage_bytes += size

            # 阈值告警（基于原始配额，不断写，只通知管理员）
            ratio = (current + size) / effective
            if ratio >= 1.0:
                self._trigger_alert(skill_home, "critical",
                                    f"存储配额已用尽，进入弹性空间 ({current}/{effective})")
            elif ratio >= 0.90:
                self._trigger_alert(skill_home, "warning",
                                    f"存储配额已达 90% ({current}/{effective})")

            return True, ""

    def release_sync(self, skill_home: str, size: int) -> None:
        """回滚预留的配额（写入失败时调用）"""
        with self._lock:
            state = self._states.get(skill_home)
            if state is None:
                return
            state.usage_bytes = max(0, state.usage_bytes - size)

    def check_write(self, skill_home: str, size: int) -> tuple[bool, str]:
        """检查写入是否可行

        内部调用 try_write_sync，但马上回滚 — 仅用于检查，不做预留。
        新代码应直接调用 try_write_sync() 做原子检查+记账。

        Deprecated:
            保留供外部读取当前状态。新代码请使用 try_write_sync()。
        """
        allowed, msg = self.try_write_sync(skill_home, size)
        if allowed:
            self.release_sync(skill_home, size)
        return allowed, msg

    def on_write(self, skill_home: str, size: int) -> None:
        """写入后记账（仅当未使用 try_write_sync 时调用）

        Deprecated:
            新代码已由 try_write_sync 在锁内原子记账，无需再调 on_write。
        """
        logger.warning(
            "on_write() called for %s (+%d bytes) — "
            "caller should use try_write_sync() instead",
            skill_home, size,
        )
        state = self._states.get(skill_home)
        if state is None:
            return
        state.usage_bytes += size

    def on_delete(self, skill_home: str, size: int) -> None:
        """删除后释放记账（🟡 F3：加锁保护）"""
        with self._lock:
            state = self._states.get(skill_home)
            if state is None:
                return
            state.usage_bytes = max(0, state.usage_bytes - size)

    async def calibrate(self, skill_home: str) -> int:
        """校准实际用量（通过 du 命令或 os.walk 统计）

        🟡 F1：_du() 在锁外运行（可能慢），锁内更新 usage_bytes 防竞态。

        Returns:
            校准后的实际字节数
        """
        state = self._states.get(skill_home)
        if state is None:
            return 0

        if not os.path.isdir(skill_home):
            with self._lock:
                state = self._states.get(skill_home)
                if state:
                    state.usage_bytes = 0
            return 0

        try:
            total = self._du(skill_home)
            # F1: 锁内更新，防止与 try_write_sync 竞态覆盖
            with self._lock:
                state = self._states.get(skill_home)
                if state is None:
                    return 0
                state.usage_bytes = total
                state.last_calibrated = time.time()
            logger.debug("Calibrated %s: %d bytes", skill_home, total)
            return total
        except OSError as e:
            logger.warning("Calibration failed for %s: %s", skill_home, e)
            cur = self._states.get(skill_home)
            return cur.usage_bytes if cur else 0

    def _du(self, path: str) -> int:
        """计算目录实际大小（慢但精确）"""
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        return total

    def expand_storage(
        self,
        skill_home: str,
        ratio: float = 2.0,
        duration_hours: int = 24,
    ) -> tuple[bool, str]:
        """临时扩容（管理员操作）

        Args:
            skill_home: Skill 目录
            ratio: 扩容倍数 1.5-3x
            duration_hours: 有效期 1-72h

        Returns:
            (success, message)
        """
        state = self._states.get(skill_home)
        if state is None:
            return False, "Skill 未注册配额"

        if not 1.5 <= ratio <= 3.0:
            return False, "扩容倍数必须在 1.5-3x 之间"

        if not 1 <= duration_hours <= 72:
            return False, "有效期必须在 1-72 小时之间"

        if state.expanded and time.time() < state.expand_until:
            return False, "该 Skill 已在扩容期间，不能重复扩容"

        with self._lock:
            state.expanded = True
            state.expand_ratio = ratio
            state.expand_until = time.time() + duration_hours * 3600
            new_quota = int(state.quota_bytes * ratio)

        self._trigger_alert(skill_home, "info", f"管理员已扩容至 {new_quota} 字节，有效期 {duration_hours} 小时")

        return True, f"已扩容至 {new_quota} 字节 (ratio={ratio}, duration={duration_hours}h)"

    def _trigger_alert(self, skill_home: str, level: str, message: str) -> None:
        """触发配额告警（异步推送管理员消息）"""
        try:
            from common.admin_message import create_admin_bus, AdminMessage
            bus = create_admin_bus()
            loop = asyncio.get_running_loop()
            dedup_key = f"quota:{level}:{skill_home}"
            loop.create_task(bus.send(AdminMessage(
                level=level,
                source="storage",
                title=f"存储配额 {level}: {os.path.basename(skill_home)}",
                body=message,
                dedup_key=dedup_key,
            )))
        except (RuntimeError, ImportError):
            logger.warning("[%s] %s: %s", level, skill_home, message)


# ── 全局限量实例（🟡 F2：模块级初始化，消除线程竞态） ─────

_quota = StorageQuota()


def get_storage_quota() -> StorageQuota:
    """获取全局 StorageQuota 单例"""
    return _quota
