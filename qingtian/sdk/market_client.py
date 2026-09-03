"""
acssa.cn Skill 市场 HTTP 客户端 — 底座 → 市场通信

提供底座侧调 acssa.cn 市场 API 的全部方法：
  - 开发提交流程：submit_skill / check_scan_status
  - 购买流程：purchase / get_my_licenses
  - 安装流程：download_skill / report_activate / report_deactivate
  - 校验流程：verify_license / fetch_revocations

用法:
    from sdk.market_client import MarketClient
    client = MarketClient(base_url="https://acssa.cn")
    result = await client.verify_license("lic_xxx", "base_xxx")
"""
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("sdk.market_client")


class MarketClient:
    """acssa.cn 市场 API 客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: int = 30,
    ):
        self.base_url = (base_url or os.environ.get(
            "ACSSA_BASE_URL", "https://acssa.cn")).rstrip("/")
        self.api_key = api_key or os.environ.get("ACSSA_API_KEY", "")
        self.timeout = timeout
        headers = {"User-Agent": "qingtian-base/1.0"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
        )

    async def close(self):
        await self._client.aclose()

    # ── 开发者提交流程 ──────────────────────────────────

    async def submit_skill(
        self,
        author_acct: str,
        name: str,
        display_name: str,
        source_url: str,
        source_sha256: str,
        developer_signature: str,
        developer_pubkey: str,
        version: str = "1.0.0",
        description: str = "",
        category: str = "tool",
        retail_price_yuan: int = 0,
        wholesale_ratio: float = 0.30,
        trial_days: int = 7,
        refund_days: int = 7,
        maintenance_commitment: str = "active",
        min_qt_version: str = "2.0.0",
        tags: list[str] | None = None,
        compliance: dict | None = None,
        copyright: dict | None = None,
        declared_permissions: list[str] | None = None,
        icon: str = "",
        screenshots: list[str] | None = None,
        changelog_url: str = "",
    ) -> dict:
        """提交 Skill 到市场。"""
        payload = {
            "author_acct": author_acct,
            "name": name,
            "display_name": display_name,
            "version": version,
            "description": description,
            "category": category,
            "tags": tags or [],
            "compliance": compliance or {},
            "copyright": copyright or {},
            "retail_price_yuan": retail_price_yuan,
            "wholesale_ratio": wholesale_ratio,
            "trial_days": trial_days,
            "refund_days": refund_days,
            "maintenance_commitment": maintenance_commitment,
            "min_qt_version": min_qt_version,
            "icon": icon,
            "screenshots": screenshots or [],
            "changelog_url": changelog_url,
            "source_url": source_url,
            "source_sha256": source_sha256,
            "developer_signature": developer_signature,
            "developer_pubkey": developer_pubkey,
            "declared_permissions": declared_permissions or [],
        }
        resp = await self._client.post("/v1/skills/submit", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def check_scan_status(self, submission_id: str) -> dict:
        """查询提交的扫描状态。"""
        resp = await self._client.get(
            f"/v1/admin/reviews/{submission_id}")
        resp.raise_for_status()
        return resp.json()

    # ── 商店浏览 ──────────────────────────────────────

    async def list_skills(
        self,
        category: str = "",
        q: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """浏览商店已上架 Skill。"""
        params = {"status": "approved", "limit": limit, "offset": offset}
        if category:
            params["category"] = category
        if q:
            params["q"] = q
        resp = await self._client.get("/v1/skills/", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_skill_detail(self, skill_id: str) -> dict:
        """获取 Skill 详情。"""
        resp = await self._client.get(f"/v1/skills/{skill_id}")
        resp.raise_for_status()
        return resp.json()

    # ── 购买流程 ──────────────────────────────────────

    async def purchase_license(
        self,
        skill_id: str,
        buyer_acct: str,
        license_type: str = "retail",
        platform_key: str = "",
        price_yuan: int = 0,
        fingerprint: str = "",
    ) -> dict:
        """购买 Skill License。

        fingerprint：服务器机器指纹（sha256），trial 必填——服务端按此键防白嫖去重。
        """
        payload = {
            "skill_id": skill_id,
            "buyer_acct": buyer_acct,
            "buyer_type": "enterprise",
            "license_type": license_type,
            "platform_key": platform_key,
            "price_yuan": price_yuan,
            "fingerprint": fingerprint,
        }
        resp = await self._client.post("/v1/licenses/purchase", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def get_my_licenses(self, account_id: str) -> list[dict]:
        """查询我的 License 列表。"""
        resp = await self._client.get("/v1/licenses", params={"buyer_acct": account_id})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        return data.get("licenses", [])

    # ── 安装流程 ──────────────────────────────────────

    async def download_skill(
        self,
        skill_name: str,
        version: str,
        license_id: str,
        dest_dir: str | Path,
    ) -> Path:
        """下载 Skill 包到本地目录。返回 .skill 文件路径。"""
        resp = await self._client.get(
            f"/v1/licenses/download/{skill_name}/version/{version}",
            params={"license_id": license_id},
        )
        resp.raise_for_status()
        data = resp.json()
        download_url = data["download_url"]
        expected_sha256 = data["sha256"]

        # 下载包文件
        if download_url.startswith("/"):
            download_url = self.base_url + download_url
        dl_resp = await self._client.get(download_url)
        dl_resp.raise_for_status()

        dest = Path(dest_dir) / f"{skill_name}-{version}.skill"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(dl_resp.content)

        # 校验 SHA256
        actual = hashlib.sha256(dl_resp.content).hexdigest()
        if actual != expected_sha256:
            raise ValueError(
                f"SHA256 mismatch: expected {expected_sha256[:16]}..., "
                f"got {actual[:16]}..."
            )
        logger.info("Downloaded %s v%s → %s", skill_name, version, dest)
        return dest

    async def report_activate(self, license_id: str, platform_key: str) -> dict:
        """上报 Skill 激活事件。"""
        resp = await self._client.post("/v1/licenses/activate", json={
            "license_id": license_id,
            "platform_key": platform_key,
        })
        resp.raise_for_status()
        return resp.json()

    async def report_deactivate(self, license_id: str, platform_key: str) -> dict:
        """上报 Skill 卸载事件。"""
        resp = await self._client.post("/v1/licenses/deactivate", json={
            "license_id": license_id,
            "platform_key": platform_key,
        })
        resp.raise_for_status()
        return resp.json()

    # ── 校验流程 ──────────────────────────────────────

    async def verify_license(
        self,
        license_id: str,
        platform_key: str = "",
    ) -> dict:
        """校验 License 有效性。底座启动时调用。"""
        resp = await self._client.post("/v1/licenses/verify", json={
            "license_id": license_id,
            "platform_key": platform_key,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        resp.raise_for_status()
        return resp.json()

    async def renew_license(self, license_id: str) -> dict:
        """续费 License。"""
        resp = await self._client.post("/v1/licenses/renew", json={
            "license_id": license_id,
        })
        resp.raise_for_status()
        return resp.json()

    async def relocate_license(self, license_id: str, new_platform_key: str) -> dict:
        """迁移批发 License 绑定到新底座。"""
        resp = await self._client.post("/v1/licenses/relocate", json={
            "license_id": license_id,
            "new_platform_key": new_platform_key,
        })
        resp.raise_for_status()
        return resp.json()

    async def fetch_revocations(self) -> dict:
        """拉取黑板名单（底座每日定时调用）。"""
        resp = await self._client.get("/v1/licenses/revocations")
        resp.raise_for_status()
        return resp.json()

    # ── 信任验证 ──────────────────────────────────────

    async def get_trust_status(self, token: str) -> dict:
        """查询账号信任等级。"""
        resp = await self._client.get(
            "/v1/trust/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def apply_trust(self, token: str, level: str) -> dict:
        """申请信任等级升级。"""
        resp = await self._client.post(
            "/v1/trust/apply",
            json={"level": level},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()
