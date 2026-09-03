"""技能自动升级 — 定时检查市场更新，有新版自动拉取升级。

流程：
  market API → 查询已安装 Skill 最新版本 → 比对本机 → 有新版本 → download → upgrade
  仅对 agent_skills.auto_update=true 的 Skill 生效；pinned_version 不为空的跳过。
"""

from __future__ import annotations

import logging
import os
import tempfile

logger = logging.getLogger("osskill.skill_updater")


async def check_and_upgrade_all(
    market_base_url: str | None = None,
    dry_run: bool = False,
) -> dict:
    """检查所有已安装且启用自动更新的 Skill，有新版则升级。

    Returns:
        {"checked": N, "upgraded": N, "skipped": N, "errors": N, "details": [...]}
    """
    from .database import SCHEMA
    from common.db import get_pool

    pool = await get_pool()
    # 查出所有绑定（agent_skills），取 DISTINCT 的 skill_id+name+version
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT DISTINCT sd.id AS skill_id, sd.name, sd.version, sd.auto_update
                FROM {SCHEMA}.skill_definitions sd
                JOIN {SCHEMA}.agent_skills ask ON ask.skill_id = sd.id
                WHERE sd.auto_update = TRUE
                  AND (ask.pinned_version IS NULL OR ask.pinned_version = '')
                  AND sd.status = 'active'""",
        )

    if not rows:
        return {"checked": 0, "upgraded": 0, "skipped": 0, "errors": 0, "details": []}

    market_url = market_base_url or os.environ.get(
        "ACSSA_BASE_URL", "https://acssa.cn",
    ).rstrip("/")

    checked = 0
    upgraded = 0
    skipped = 0
    errors = 0
    details: list[dict] = []

    for row in rows:
        skill_name = row["name"]
        current_version = row["version"] or "0.0.0"
        skill_id = row["skill_id"]

        checked += 1
        try:
            from sdk.market_client import MarketClient

            client = MarketClient(base_url=market_url)
            try:
                # 查市场最新版本
                market_info = await _get_market_version(client, skill_name)
                if not market_info:
                    skipped += 1
                    details.append({"skill": skill_name, "status": "no_market_info"})
                    continue

                latest_version = market_info.get("version", "")
                if not latest_version:
                    skipped += 1
                    details.append({"skill": skill_name, "status": "no_version_field"})
                    continue

                if _version_le(latest_version, current_version):
                    skipped += 1
                    details.append({
                        "skill": skill_name,
                        "status": "up_to_date",
                        "current": current_version,
                        "latest": latest_version,
                    })
                    continue

                if dry_run:
                    details.append({
                        "skill": skill_name,
                        "status": "would_upgrade",
                        "current": current_version,
                        "latest": latest_version,
                    })
                    skipped += 1
                    continue

                # 下载并升级
                logger.info(
                    "Auto-upgrade %s: %s → %s",
                    skill_name, current_version, latest_version,
                )
                success = await _download_and_upgrade(
                    client, skill_name, latest_version, str(skill_id), market_info,
                )
                if success:
                    upgraded += 1
                    details.append({
                        "skill": skill_name,
                        "status": "upgraded",
                        "from": current_version,
                        "to": latest_version,
                    })
                else:
                    errors += 1
                    details.append({
                        "skill": skill_name,
                        "status": "upgrade_failed",
                        "current": current_version,
                        "latest": latest_version,
                    })
            finally:
                await client.close()

        except Exception as e:
            errors += 1
            logger.warning("Auto-update check failed for %s: %s", skill_name, e)
            details.append({"skill": skill_name, "status": "error", "error": str(e)[:200]})

    logger.info(
        "Auto-update done: checked=%d upgraded=%d skipped=%d errors=%d",
        checked, upgraded, skipped, errors,
    )
    return {
        "checked": checked,
        "upgraded": upgraded,
        "skipped": skipped,
        "errors": errors,
        "details": details,
    }


async def _get_market_version(client, skill_name: str) -> dict | None:
    """从市场获取 latest 版本信息。

    先按名称精确匹配 search，再取第一条的 version。"""
    try:
        result = await client.list_skills(q=skill_name, limit=5)
        items = result.get("items") or result.get("skills") or []
        for item in items:
            if (item.get("name") or "").lower() == skill_name.lower():
                return {
                    "version": item.get("version") or item.get("latest_version", ""),
                    "skill_id": item.get("id") or item.get("skill_id", ""),
                    "name": item.get("name", skill_name),
                }
        # 模糊匹配：取第一条名称包含输入的
        for item in items:
            if skill_name.lower() in (item.get("name") or "").lower():
                return {
                    "version": item.get("version") or item.get("latest_version", ""),
                    "skill_id": item.get("id") or item.get("skill_id", ""),
                    "name": item.get("name", skill_name),
                }
    except Exception as e:
        logger.warning("Market search failed for %s: %s", skill_name, e)

    return None


async def _download_and_upgrade(
    client,
    skill_name: str,
    target_version: str,
    _skill_db_id: str,
    market_info: dict,
) -> bool:
    """下载 .skill 包并调用运行时升级。"""
    # C4 (R11): runtime_service 无 get_runtime 符号——经 admin_api 注入的
    # 全局 RuntimeService 取实例（main.py 启动时 init_admin_api 注入）。
    from .admin_api import get_runtime_service

    runtime = get_runtime_service()
    if not runtime:
        logger.error("Auto-upgrade: RuntimeService not available")
        return False

    # 临时下载目录
    with tempfile.TemporaryDirectory(prefix="skill_upgrade_") as tmpdir:
        try:
            pkg_path = await client.download_skill(
                skill_name=skill_name,
                version=target_version,
                license_id=market_info.get("license_id", ""),
                dest_dir=tmpdir,
            )
        except Exception as e:
            logger.warning("Download failed for %s@%s: %s", skill_name, target_version, e)
            return False

        try:
            # 获取该 skill 的所有绑定 agent
            from .database import get_skill_bindings
            bindings = await get_skill_bindings(int(_skill_db_id))
            if not bindings:
                logger.warning("Auto-upgrade: no bindings for skill %s", skill_name)
                return False

            for binding in bindings:
                agent_id = binding["agent_id"]
                result = await runtime.upgrade(
                    skill_name=skill_name,
                    new_version=target_version,
                    package_path=str(pkg_path),
                    agent_id=agent_id,
                )
                if result.get("status") != "upgraded":
                    logger.warning(
                        "Upgrade failed for %s agent=%s: %s",
                        skill_name, agent_id, result,
                    )
                    return False

            return True

        except Exception as e:
            logger.error("Upgrade failed for %s: %s", skill_name, e)
            return False


def _version_le(a: str, b: str) -> bool:
    """比较版本号 a <= b（语义化版本，简单实现）。"""
    try:
        pa = [int(x) for x in a.replace("v", "").split(".")]
        pb = [int(x) for x in b.replace("v", "").split(".")]
        while len(pa) < len(pb):
            pa.append(0)
        while len(pb) < len(pa):
            pb.append(0)
        return pa <= pb
    except (ValueError, TypeError):
        return a <= b  # 回退字符串比较
