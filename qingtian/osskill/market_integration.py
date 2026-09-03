"""开源版市场集成模块 — 底座 ↔ acssa.cn 通信。

当闭源 `osskill_acssa` 未安装时作为默认实现。
提供 AcssaClient / SkillLicenseManager / RevocationService 等价功能。

架构：
  - MarketGateway         — 市场 HTTP 通信（可被 osskill_acssa 覆盖）
  - LicenseManager        — 本地 License 存储 + 校验 + 激活上报
  - RevocationManager     — 黑板名单本地管理 + 在线同步
  - SkillPackageManager   — .skill 包的下载、校验、安装
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import httpx

logger = logging.getLogger("osskill.market_integration")

# ── 路径常量 ──

_DATA_DIR = Path(os.environ.get(
    "QINGTIAN_SKILL_DATA_DIR", "/opt/qingtian/skills/data"))
_LICENSE_DIR = _DATA_DIR / "licenses"
_PACKAGE_DIR = _DATA_DIR / "packages"
_REVOCATION_DIR = _DATA_DIR / "revocations"
_LICENSE_STATE_FILE = _DATA_DIR / "license_state.json"
_MAX_OFFLINE_STARTS = 7  # 离线豁免次数


def _ensure_dirs():
    for d in (_LICENSE_DIR, _PACKAGE_DIR, _REVOCATION_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ════════════════════════════════════════════════════════
# MarketGateway — acssa.cn HTTP 通信
# ════════════════════════════════════════════════════════


class MarketGateway:
    """与 acssa.cn 市场的 HTTP 通信层。

    可被闭源 osskill_acssa.AcSSA客户端 替换注入以添加额外安全校验。
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        market_client: Any | None = None,
    ):
        self.base_url = (base_url or os.environ.get(
            "ACSSA_BASE_URL", "https://acssa.cn")).rstrip("/")
        self.api_key = api_key or os.environ.get("ACSSA_API_KEY", "")
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._market_client = market_client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    headers = {"User-Agent": "qingtian-osskill/1.0"}
                    if self.api_key:
                        headers["Authorization"] = f"Bearer {self.api_key}"
                    self._client = httpx.AsyncClient(
                        base_url=self.base_url,
                        timeout=30,
                        headers=headers,
                    )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def verify_license(self, license_id: str, platform_key: str = "") -> dict:
        """联网校验 License。"""
        if self._market_client:
            return await self._market_client.verify_license(license_id, platform_key)
        client = await self._get_client()
        resp = await client.post("/v1/licenses/verify", json={
            "license_id": license_id,
            "platform_key": platform_key,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        resp.raise_for_status()
        return resp.json()

    async def report_activate(self, license_id: str, platform_key: str) -> dict:
        """上报激活事件。"""
        if self._market_client:
            return await self._market_client.report_activate(license_id, platform_key)
        client = await self._get_client()
        resp = await client.post("/v1/licenses/activate", json={
            "license_id": license_id,
            "platform_key": platform_key,
        })
        resp.raise_for_status()
        return resp.json()

    async def report_deactivate(self, license_id: str, platform_key: str) -> dict:
        """上报卸载事件。"""
        if self._market_client:
            return await self._market_client.report_deactivate(license_id, platform_key)
        client = await self._get_client()
        resp = await client.post("/v1/licenses/deactivate", json={
            "license_id": license_id,
            "platform_key": platform_key,
        })
        resp.raise_for_status()
        return resp.json()

    async def fetch_revocations(self) -> dict:
        """拉取黑板名单。"""
        if self._market_client:
            return await self._market_client.fetch_revocations()
        client = await self._get_client()
        resp = await client.get("/v1/licenses/revocations")
        resp.raise_for_status()
        return resp.json()

    async def download_package(
        self,
        download_url: str,
        expected_sha256: str,
        dest_path: Path,
    ) -> Path:
        """下载 .skill 包并校验 SHA256。"""
        if download_url.startswith("/"):
            download_url = self.base_url + download_url
        client = await self._get_client()
        resp = await client.get(download_url)
        resp.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        actual = hashlib.sha256(resp.content).hexdigest()
        if actual != expected_sha256:
            raise ValueError(
                f"SHA256 mismatch: expected {expected_sha256[:16]}..., "
                f"got {actual[:16]}..."
            )
        logger.info("Package downloaded: %s", dest_path)
        return dest_path


# ════════════════════════════════════════════════════════
# LicenseManager — 本地 License 管理
# ════════════════════════════════════════════════════════


def _get_trial_days_from_manifest(manifest) -> int:
    """从 SkillManifest 中提取试用期天数。"""
    if manifest is None:
        return 7  # 缺省 7 天
    li = getattr(manifest, "license_info", None) or {}
    if isinstance(li, dict):
        return int(li.get("trial_days", 7))
    return int(getattr(li, "get", lambda k, d=7: d)("trial_days", 7))


# ════════════════════════════════════════════════════════
# Skill 名安全校验（防路径穿越）— P2 (R11)
# ════════════════════════════════════════════════════════


def _validate_skill_name(skill_name: str) -> str:
    """校验 Skill 名安全，拒绝可引发路径穿越的名称。

    规则：非空、非 "." / ".."、不含路径分隔符（/、\\）与 ".."。
    返回原名（供后续拼接使用）；非法名称抛 ValueError（fail-closed）。
    """
    if not skill_name or skill_name in (".", ".."):
        raise ValueError(f"invalid skill_name: {skill_name!r}")
    if "/" in skill_name or "\\" in skill_name or ".." in skill_name:
        raise ValueError(f"unsafe skill_name: {skill_name!r}")
    return skill_name


def _license_file_path(skill_name: str) -> Path:
    """计算 Skill 的 License 文件路径（白名单校验 + 包含性双保险）。

    除字符白名单校验外，再做 resolve() 后 os.path.commonpath 包含性校验——
    即便校验规则未来有疏漏，也能确保解析后的最终路径仍落在 licenses 目录内。
    """
    _validate_skill_name(skill_name)
    root = _LICENSE_DIR.resolve()
    path = (_LICENSE_DIR / f"{skill_name}.license").resolve()
    if os.path.commonpath([str(path), str(root)]) != str(root):
        raise ValueError(f"unsafe skill_name: {skill_name!r}")
    return path


_license_mgr: LicenseManager | None = None


def _get_license_manager() -> LicenseManager:
    """惰性获取 LicenseManager 单例。"""
    global _license_mgr
    if _license_mgr is None:
        _license_mgr = LicenseManager()
    return _license_mgr


class LicenseManager:
    """本地 License 存储 + 校验 + 激活上报 + 离线豁免。

    对应设计文档 §四（License 系统）的全部功能：
      - 本地存储/读取 License 文件
      - 启动时校验（本地 Ed25519 + 网络校验）
      - 时间倒流检测
      - 离线豁免（7 次）
      - 激活/卸载上报
      - 管理员签发/吊销 License
    """

    def __init__(self, gateway: MarketGateway | None = None):
        _ensure_dirs()
        self._gateway = gateway or MarketGateway()
        self._state = self._load_state()

    # ── License 文件管理 ──

    def save_license(self, skill_name: str, license_data: dict):
        """保存 License 到本地。"""
        path = _license_file_path(skill_name)  # P2 (R11): 防路径穿越
        # 保留 signature 字段不缩进，其余格式化
        sig = license_data.pop("signature", "")
        path.write_text(
            json.dumps(license_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if sig:
            license_data["signature"] = sig
        logger.info("License saved: %s", path)

    def load_license(self, skill_name: str) -> dict | None:
        """读取本地 License。"""
        path = _license_file_path(skill_name)  # P2 (R11): 防路径穿越
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load license for %s: %s", skill_name, e)
            return None

    def list_licenses(self) -> list[dict]:
        """列出所有本地 License。"""
        result = []
        for path in sorted(_LICENSE_DIR.glob("*.license")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["_file"] = path.name
                result.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return result

    def delete_license(self, skill_name: str):
        """删除本地 License 文件。"""
        path = _license_file_path(skill_name)  # P2 (R11): 防路径穿越
        if path.exists():
            path.unlink()
            logger.info("License deleted: %s", path)

    # ── 启动校验 ——

    def ensure_skill_license(self, skill_name: str, manifest=None) -> dict | None:
        """确保 Skill 有 License 文件。无则按 manifest.trial_days 签发试用 License。

        试用期策略（skill.json license_info.trial_days）：
          - 首次激活自动签发试用 License，有效期 trial_days 天
          - 试用期内：全功能可用，license_type = "trial"
          - 试用到期：验证失败，需购买后替换 License 文件
          - 已有 License（含购买的正版）不受影响

        Args:
            skill_name: Skill 名称
            manifest: SkillManifest 实例（含 license_info.trial_days）

        Returns:
            License 字典，或 None（签发失败时）
        """
        existing = self.load_license(skill_name)
        if existing:
            return existing

        trial_days = _get_trial_days_from_manifest(manifest)
        return self._issue_trial_license(skill_name, trial_days)

    def _issue_trial_license(self, skill_name: str, trial_days: int) -> dict | None:
        """签发试用 License 文件到本地。"""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=trial_days)

        license_data = {
            "license_id": f"trial_{uuid.uuid4().hex[:12]}",
            "skill_name": skill_name,
            "license_type": "trial",
            "issued_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "trial": True,
        }
        try:
            self.save_license(skill_name, license_data)
            logger.info(
                "试用 License 已签发: skill=%s trial_days=%d expires=%s",
                skill_name, trial_days, expires_at.isoformat(),
            )
            return license_data
        except Exception as e:
            logger.error("试用 License 签发失败: skill=%s err=%s", skill_name, e)
            return None

    def grant_license(
        self, skill_name: str, enterprise_id: str = "",
        days: int = 0, perpetual: bool = False,
    ) -> dict:
        """管理员签发 License：指定免费期(days) 或 永久(perpetual)。

        - days>0 + perpetual=False: 试用 License，expires_at=NOW()+days
        - perpetual=True: 永久 License，expires_at=2099-12-31
        - 两者都未指定: 回退 manifest trial_days（与 ensure_skill_license 一致）
        """
        now = datetime.now(timezone.utc)
        if perpetual:
            expires_at = datetime(2099, 12, 31, tzinfo=timezone.utc)
            lic_type = "perpetual"
        elif days > 0:
            expires_at = now + timedelta(days=days)
            lic_type = "trial"
        else:
            # 回退：从 skill.json 读 trial_days
            from .models import SkillManifest
            from .loader import SkillLoader
            try:
                manifest = SkillLoader.load(skill_name)
                days = _get_trial_days_from_manifest(manifest)
            except Exception:
                days = 7
            expires_at = now + timedelta(days=days)
            lic_type = "trial"

        license_data = {
            "license_id": f"admin_{uuid.uuid4().hex[:12]}",
            "skill_name": skill_name,
            "enterprise_id": enterprise_id,
            "license_type": lic_type,
            "issued_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "issued_by": "admin",
        }
        self.save_license(skill_name, license_data)
        logger.info(
            "Admin granted license: skill=%s enterprise=%s type=%s expires=%s",
            skill_name, enterprise_id, lic_type, expires_at.isoformat(),
        )
        return license_data

    def revoke_license(self, skill_name: str) -> bool:
        """管理员吊销 License（删除本地文件）。"""
        path = _license_file_path(skill_name)  # P2 (R11): 防路径穿越
        if path.exists():
            path.unlink()
            logger.info("Admin revoked license: %s", skill_name)
            return True
        return False

    def verify_local(self, skill_name: str) -> tuple[bool, str]:
        """本地校验（无网络）：检查文件存在 + 有效期 + 时间倒流。

        Returns:
            (is_valid, reason)
        """
        lic = self.load_license(skill_name)
        if not lic:
            return False, "license_not_found"

        # 有效期检查
        # P2 (R11)：外部导入/旧版签发的 License 可能不带时区（naive ISO），
        # 与 aware 的 now 直接比较会抛 TypeError。统一归一化为 UTC aware：
        # naive 按 UTC 补 tzinfo，Z 后缀转 +00:00。
        expires_at = lic.get("expires_at", "")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < datetime.now(timezone.utc):
                    return False, "expired"
            except (ValueError, TypeError):
                logger.warning("Invalid expires_at for %s: %s", skill_name, expires_at)
                return False, "expired"

        # 时间倒流检测
        last_time = self._state.get("last_verified_at", "")
        if last_time:
            try:
                last = datetime.fromisoformat(last_time)
                now = datetime.now(timezone.utc)
                diff_hours = (now - last).total_seconds() / 3600
                if diff_hours < -24:
                    return False, "time_rewound"
            except (ValueError, TypeError):
                pass

        return True, "ok"

    async def verify_online(self, skill_name: str, platform_key: str = "") -> dict:
        """联网校验 License。

        Returns:
            {"valid": bool, "reason": str, ...}
        """
        lic = self.load_license(skill_name)
        if not lic:
            return {"valid": False, "reason": "license_not_found"}

        license_id = lic.get("license_id", "")
        try:
            result = await self._gateway.verify_license(license_id, platform_key)
            if result.get("valid"):
                self._update_state()
            return result
        except Exception as e:
            logger.warning("Online verify failed for %s: %s", skill_name, e)
            return {"valid": False, "reason": f"network_error: {e}"}

    async def verify_and_report(
        self,
        skill_name: str,
        platform_key: str = "",
    ) -> dict:
        """完整校验 + 激活上报（对应设计文档 §4.2 启动校验流程）。"""
        # 1. 本地校验（快速失败）
        valid, reason = self.verify_local(skill_name)
        if not valid:
            return {"valid": False, "reason": reason}

        # 2. 离线豁免检查
        if self._state.get("offline_start_count", 0) >= _MAX_OFFLINE_STARTS:
            # 超过离线次数，必须联网校验
            result = await self.verify_online(skill_name, platform_key)
            if not result.get("valid"):
                # 联网失败 → 降级
                return {
                    "valid": False,
                    "reason": result.get("reason", "offline_limit_exceeded"),
                    "degraded": True,
                }
            # 联网成功 → 重置离线计数
            self._state["offline_start_count"] = 0
        else:
            # 尝试联网但失败不计入离线次数（短时宕机情形）
            result = await self.verify_online(skill_name, platform_key)
            if result.get("valid"):
                self._state["offline_start_count"] = 0
            elif result.get("reason", "").startswith("network_error"):
                # 网络不可达 → 消耗离线次数（verify_online 不抛异常，靠 reason 前缀判断）
                self._state["offline_start_count"] = (
                    self._state.get("offline_start_count", 0) + 1
                )
                logger.warning(
                    "Offline start #%d for %s",
                    self._state["offline_start_count"], skill_name,
                )
            else:
                # 服务器明确拒绝（过期/吊销），直接返回
                return result

        self._save_state()

        # 3. 上报激活（非阻塞）
        lic = self.load_license(skill_name)
        if lic:
            license_id = lic.get("license_id", "")
            try:
                await self._gateway.report_activate(license_id, platform_key)
            except Exception:
                logger.debug("Activate report deferred (offline): %s", skill_name)

        return {"valid": True, "reason": "ok", "degraded": False}

    # ── 卸载上报 ──

    async def report_deactivation(self, skill_name: str, platform_key: str = ""):
        """上报 Skill 卸载事件。"""
        lic = self.load_license(skill_name)
        if lic:
            try:
                await self._gateway.report_deactivate(
                    lic["license_id"], platform_key)
            except Exception as e:
                logger.warning("Deactivate report failed: %s", e)

    # ── 持久化状态 ──

    def _load_state(self) -> dict:
        if _LICENSE_STATE_FILE.exists():
            try:
                return json.loads(_LICENSE_STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_verified_at": "", "offline_start_count": 0}

    def _update_state(self):
        self._state["last_verified_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state()

    def _save_state(self):
        _LICENSE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LICENSE_STATE_FILE.write_text(
            json.dumps(self._state, ensure_ascii=False), encoding="utf-8",
        )


# ════════════════════════════════════════════════════════
# RevocationManager — 黑板名单管理
# ════════════════════════════════════════════════════════


class RevocationManager:
    """黑板名单本地管理 + 在线同步 + 离线导入。

    对应设计文档 §九（应急吊销与黑板名单）。
    """

    def __init__(self, gateway: MarketGateway | None = None):
        _ensure_dirs()
        self._gateway = gateway or MarketGateway()
        self._local_file = _REVOCATION_DIR / "blacklist.json"
        self._blacklist: dict[str, dict] = {}
        self._load_local()

    # ── 本地管理 ──

    def _load_local(self):
        if self._local_file.exists():
            try:
                data = json.loads(self._local_file.read_text(encoding="utf-8"))
                self._blacklist = data.get("revocations", {})
            except (json.JSONDecodeError, OSError):
                self._blacklist = {}

    def _save_local(self):
        self._local_file.parent.mkdir(parents=True, exist_ok=True)
        self._local_file.write_text(
            json.dumps({
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "revocations": self._blacklist,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def is_blacklisted(self, skill_name: str) -> bool:
        """检查 Skill 是否在黑名单中。"""
        return skill_name in self._blacklist

    def get_blacklisted(self) -> dict[str, dict]:
        """获取全部黑板名单。"""
        return dict(self._blacklist)

    def get_blacklist_entry(self, skill_name: str) -> dict | None:
        """获取指定 Skill 的黑名单条目。"""
        return self._blacklist.get(skill_name)

    def add_entry(self, skill_name: str, reason: str, severity: str = "high"):
        """本地添加黑板名单条目。"""
        self._blacklist[skill_name] = {
            "reason": reason,
            "severity": severity,
            "revoked_at": int(time.time()),
        }
        self._save_local()
        logger.warning("Blacklist added: %s (%s)", skill_name, reason)
        # 同步到镇岳审计日志
        self._notify_zhenyue(skill_name, reason, severity)

    def remove_entry(self, skill_name: str):
        """移除黑板名单条目。"""
        self._blacklist.pop(skill_name, None)
        self._save_local()
        logger.info("Blacklist removed: %s", skill_name)

    # ── 镇岳审计集成 ──

    def _notify_zhenyue(self, skill_name: str, reason: str, severity: str):
        """写入镇岳审计日志。

        镇岳审计日志是可信存储（哈希链），吊销事件写入后不可篡改。
        """
        try:
            # 检查是否有运行中的事件循环
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running event loop, skipping zhenyue audit")
            return

        try:
            from zhenyue.audit_service import write_audit
            from common.db import get_pool

            async def _do_notify():
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await write_audit(conn, {
                        "audit_uid": str(uuid.uuid4()),
                        "action": "skill_revoked",
                        "actor": "revocation_manager",
                        "target": skill_name,
                        "detail": json.dumps({
                            "reason": reason,
                            "severity": severity,
                            "source": "blacklist",
                        }),
                    })

            async def _do_notify_safe():
                try:
                    await _do_notify()
                except Exception as e:
                    logger.warning("notify zhenyue failed: %s", e)

            asyncio.ensure_future(_do_notify_safe())
        except ImportError:
            logger.debug("zhenyue.audit_service not available, skipping audit")
        except Exception as e:
            logger.debug("Failed to notify zhenyue: %s", e)

    @staticmethod
    def process_zhenyue_event(event: dict) -> dict | None:
        """处理来自镇岳的吊销事件。

        镇岳可以通过 Webhook 发送吊销事件，此方法处理并返回
        需要添加到黑名单的条目。

        Args:
            event: 镇岳事件 dict，格式：
                {"action": "skill_revoked", "target": "skill_name",
                 "detail": {"reason": "...", "severity": "high"}}

        Returns:
            黑名单条目 dict（若需要添加到黑名单），否则 None
        """
        action = event.get("action", "")
        if action != "skill_revoked":
            return None

        skill_name = event.get("target", "")
        detail_raw = event.get("detail", "{}")
        if isinstance(detail_raw, str):
            try:
                detail = json.loads(detail_raw)
            except (json.JSONDecodeError, TypeError):
                detail = {}
        else:
            detail = detail_raw

        return {
            "skill_name": skill_name,
            "reason": detail.get("reason", "zhenyue revocation"),
            "severity": detail.get("severity", "high"),
        }

    # ── 在线同步 ──

    async def sync_from_server(self) -> int:
        """从 acssa.cn 拉取黑板名单并合并。返回新增数量。"""
        try:
            data = await self._gateway.fetch_revocations()
        except Exception as e:
            logger.warning("Revocation sync failed: %s", e)
            return 0

        revocations = data.get("revocations", [])
        count = 0
        for entry in revocations:
            sid = entry.get("skill_id", "")
            name = entry.get("skill_name", "")
            version = entry.get("version", "")
            reason = entry.get("reason", "unknown")
            # 优先 skill_name 作为 key（与 is_blacklisted/add_entry/remove_entry
            # 的 skill_name 契约对齐，杜绝"吊销名单永远空"）；后端未返回
            # skill_name（旧版）时退回 skill_id 兼容存量数据。
            key = name if name else (f"{sid}:{version}" if version and version != "*" else sid)
            if key not in self._blacklist:
                self._blacklist[key] = {
                    "reason": reason,
                    "severity": entry.get("severity", "high"),
                    "revoked_at": int(time.time()),
                }
                count += 1

        if count > 0:
            self._save_local()
            logger.info("Synced %d new revocations", count)

        return count

    # ── 离线导入 ──

    def import_file(self, file_path: str) -> int:
        """导入离线吊销文件（Ed25519 签名 JSON）。

        Args:
            file_path: .revoke.json 文件路径

        Returns:
            导入的条目数量
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"吊销文件不存在: {file_path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"吊销文件格式错误: {e}")

        # 校验签名（使用内置平台公钥）
        signature = data.pop("signature", "")
        if not signature:
            raise ValueError("吊销文件缺少签名，已拒绝")

        # 由 loader._PLATFORM_PUBKEY_HEX 验证
        from osskill.loader import _s1_verify_cert
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        # cert_hex 格式: signature_hex + payload_hex（无分隔符，bytes.fromhex 要求纯 hex）
        cert_hex = signature + payload.encode().hex()
        valid, err = _s1_verify_cert(cert_hex, "__revocation_file__")
        if not valid:
            raise ValueError(f"吊销文件签名验证失败: {err}")

        data["signature"] = signature

        # 有效期检查
        expires_at = data.get("expires_at", "")
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if exp < datetime.now(timezone.utc):
                    raise ValueError(f"吊销文件已过期: {expires_at}")
            except (ValueError, TypeError) as e:
                if "已过期" in str(e):
                    raise
                logger.warning("Could not parse expires_at: %s", expires_at)

        entries = data.get("revocations", [])
        count = 0
        for entry in entries:
            sid = entry.get("skill_id", "")
            name = entry.get("skill_name", "")
            version = entry.get("version", "")
            reason = entry.get("reason", "unknown")
            # P1 (R11): key 契约与 sync_from_server 对齐——优先 skill_name
            # （is_blacklisted/add_entry 的契约），否则离线吊销永远拦不住执行。
            key = name if name else (f"{sid}:{version}" if version and version != "*" else sid)
            if key not in self._blacklist:
                self._blacklist[key] = {
                    "reason": reason,
                    "severity": entry.get("severity", "high"),
                    "revoked_at": int(time.time()),
                }
                count += 1

        if count > 0:
            self._save_local()

        return count

    def import_blacklist_file(self, file_path: str) -> int:
        """导入黑板名单文件（与闭源 osskill_acssa.RevocationService 契约一致）。

        P2 (R11)：开源版此前只有 import_file，admin_api.import_blacklist_file
        在注入开源 RevocationManager 时会 AttributeError。补统一入口，
        内部委托 import_file（含 Ed25519 签名校验 + 有效期检查）。
        """
        return self.import_file(file_path)


# ════════════════════════════════════════════════════════
# SkillPackageManager — 包管理
# ════════════════════════════════════════════════════════


class SkillPackageManager:
    """.skill 包下载、解压、安装到目标目录。"""

    def __init__(self, gateway: MarketGateway | None = None):
        _ensure_dirs()
        self._gateway = gateway or MarketGateway()

    async def download(
        self,
        skill_name: str,
        version: str,
        download_url: str,
        expected_sha256: str,
    ) -> Path:
        """下载 .skill 包到本地缓存。"""
        dest = _PACKAGE_DIR / f"{skill_name}-{version}.skill"
        return await self._gateway.download_package(
            download_url, expected_sha256, dest)

    def _safe_extract(self, zf: ZipFile, entry: str, target: Path):
        """安全提取 zip 条目，防止 Zip Slip 路径穿越。"""
        # 路径穿越检测
        clean = Path(entry)
        if ".." in clean.parts or entry.startswith("/"):
            raise ValueError(f"Zip entry '{entry}' attempts path traversal")
        if entry.endswith("/"):
            return  # 目录条目，跳过
        dest = target / clean
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = zf.read(entry)
        dest.write_bytes(data)

    def install(self, package_path: Path, target_dir: str | Path) -> Path:
        """解压 .skill 包到目标安装目录。"""
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)

        with ZipFile(package_path, "r") as zf:
            namelist = zf.namelist()

            # 情形 A：所有文件在单一顶层目录下（如 myskill-v1/skill.json, myskill-v1/code/...）
            top_level = set()
            for name in namelist:
                parts = name.split("/")
                if len(parts) > 1:
                    top_level.add(parts[0])

            extracted = False
            if len(top_level) == 1:
                prefix = top_level.pop() + "/"
                if prefix + "skill.json" in namelist:
                    for name in namelist:
                        if name.startswith(prefix):
                            rel = name[len(prefix):]
                            if rel:
                                self._safe_extract(zf, name, target)
                    extracted = True

            # 情形 B：平铺结构，skill.json 在根
            if not extracted:
                if "skill.json" in namelist:
                    for name in namelist:
                        self._safe_extract(zf, name, target)
                    extracted = True

            if not extracted:
                raise ValueError(".skill 包缺少 skill.json")

        # 平台签名校验（fail-closed）：信任链闭环，市场包必须带平台证书。
        # 证书由 build_skill 注入（cert_hex = 签名hex + JSON payload hex），
        # 此处与 loader._s1_verify_cert 校验，格式严格一致。
        from osskill.loader import _s1_verify_cert
        try:
            manifest = json.loads((target / "skill.json").read_text(encoding="utf-8"))
            cert_hex = manifest.get("certificate", "")
            ok, err = _s1_verify_cert(cert_hex, manifest.get("name", ""))
        except Exception as e:
            raise ValueError(f"平台签名校验异常，拒绝安装: {e}")
        if not ok:
            raise ValueError(f"平台签名校验失败，拒绝安装: {err}")

        logger.info("Skill installed: %s → %s", package_path, target)
        return target

    def uninstall(self, target_dir: str | Path):
        """卸载 Skill（删除安装目录）。"""
        target = Path(target_dir)
        if target.exists():
            shutil.rmtree(target)
            logger.info("Skill uninstalled: %s", target)
