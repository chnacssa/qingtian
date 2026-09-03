#!/usr/bin/env python3
"""种子数据初始化脚本 — 注册所有 bundled Skill 到数据库

用法:
    python scripts/seed_skills.py                        # 注册全部
    python scripts/seed_skills.py --skill meeting_summary # 注册单个

自动完成:
    1. 确保 skills schema（DDL）
    2. 扫描 osskill/implementations/ 下所有 skill.json
    3. 注册到 skill_definitions 表（UPSERT，幂等）
    4. 可选绑定到所有活跃 Agent

注意:
    此脚本需要数据库连接（common.db）。
    生产环境中由 main.py 启动时自动注册，此脚本仅用于开发/部署/测试。
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# 确保从项目根目录可以 import
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("seed_skills")

# 跳过注册的目录（系统内部类，非独立 Skill）
SKIP_DIRS = {"__pycache__", "workflow", "_base"}

# 默认角色绑定映射
ROLE_BINDINGS = {
    "company": ["procurement", "sales", "bidding", "work_secretary"],
    "management": ["procurement", "sales", "bidding", "work_secretary"],
    "procurement": ["procurement", "bidding", "work_secretary"],
    "sales": ["sales", "bidding", "work_secretary"],
}
# 自动注册但默认不自动绑定的 Skill
EXTRA_REGISTER = [
    "clawhub_adapter",
    "regulatory_adapter",
    "document",
    "price_list",
    "product_catalog",
    "product_image",
    "excel_generator",
    "word_generator",
    "pdf_generator",
    "ppt_generator",
    "csv_analyzer",
    "file_converter",
    "email_drafter",
    "meeting_summary",
]


async def seed_all(bind_to_agents: bool = True, skill_name: str | None = None):
    """注册 bundled Skill 到数据库

    Args:
        bind_to_agents: 是否自动绑定到活跃 Agent
        skill_name: 如指定，只注册该 Skill（用于增量注册）
    """
    # 1. 确保 schema
    from osskill.database import ensure_schema, register_bundled_skill, bind_skill

    logger.info("[1/3] 确保 skills schema...")
    await ensure_schema()
    logger.info("  schema 就绪")

    # 2. 扫描并注册
    impl_dir = PROJECT_ROOT / "osskill" / "implementations"
    skill_jsons: list[Path] = []

    if skill_name:
        # 注册单个
        sj = impl_dir / skill_name / "skill.json"
        if sj.exists():
            skill_jsons.append(sj)
            logger.info(f"[2/3] 注册单个 Skill: {skill_name}")
        else:
            logger.error(f"  [!] skill.json 不存在: {sj}")
            return
    else:
        # 扫描全部
        for entry in sorted(impl_dir.iterdir()):
            if not entry.is_dir() or entry.name in SKIP_DIRS:
                continue
            sj = entry / "skill.json"
            if sj.exists():
                skill_jsons.append(sj)

        # 确保 extra skills 也包含（即使目录无 skill.json 也尝试）
        for extra in EXTRA_REGISTER:
            sj = impl_dir / extra / "skill.json"
            if sj.exists() and sj not in skill_jsons:
                skill_jsons.append(sj)

        logger.info(f"[2/3] 扫描到 {len(skill_jsons)} 个 skill.json")

    registered = []
    for sj in skill_jsons:
        try:
            result = await register_bundled_skill(str(sj))
            if result:
                registered.append(result)
                logger.info(f"  [+] {result['name']} → id={result['id']} status={result['status']}")
            else:
                logger.warning(f"  [?] {sj.name}: 注册返回空（可能是重复跳过）")
        except Exception as e:
            logger.error(f"  [!] {sj.name}: 注册失败 - {e}")

    logger.info(f"  注册完成: {len(registered)}/{len(skill_jsons)}")

    # 3. 绑定到所有活跃 Agent
    if not bind_to_agents or not registered:
        logger.info("[3/3] 跳过 Agent 绑定（--no-bind 或未注册任何 Skill）")
        return

    logger.info("[3/3] 绑定到活跃 Agent...")
    try:
        from common.db import get_pool
        from huanyu.config import get_schema_name as huanyu_schema

        pool = await get_pool()
        async with pool.acquire() as conn:
            h_schema = huanyu_schema()
            agent_rows = await conn.fetch(
                f"SELECT agent_id, role FROM {h_schema}.agents WHERE status = 'active'"
            )

            if not agent_rows:
                logger.info("  无活跃 Agent，跳过绑定")
                return

            bound_total = 0
            for ar in agent_rows:
                agent_role = ar.get("role", "")
                allowed_skills = ROLE_BINDINGS.get(agent_role, [])
                for skill_record in registered:
                    if allowed_skills and skill_record["name"] not in allowed_skills:
                        continue
                    if await bind_skill(ar["agent_id"], skill_record["id"]):
                        bound_total += 1
            logger.info(f"  已绑定 {bound_total} 个 Agent-Skill 关联")
    except Exception as e:
        logger.warning(f"  Agent 绑定失败（可能是 Agent 表未就绪）: {e}")
        logger.info("  Skill 已注册，稍后 main.py 启动时会自动绑定")


def main():
    parser = argparse.ArgumentParser(description="擎天 Skill 种子数据初始化")
    parser.add_argument("--skill", type=str, default="", help="仅注册指定 Skill（如 meeting_summary）")
    parser.add_argument("--no-bind", action="store_true", help="不绑定到 Agent")
    parser.add_argument("--list", action="store_true", help="列出可注册的 Skill 目录")
    args = parser.parse_args()

    if args.list:
        impl_dir = PROJECT_ROOT / "osskill" / "implementations"
        print("可注册的 Skill 目录:")
        for entry in sorted(impl_dir.iterdir()):
            if not entry.is_dir() or entry.name in SKIP_DIRS:
                continue
            sj = entry / "skill.json"
            if sj.exists():
                try:
                    with open(sj, encoding="utf-8") as f:
                        data = json.load(f)
                    name = data.get("name", entry.name)
                    desc = data.get("description", "")[:60]
                    print(f"  {entry.name:25s} → {name:25s} {desc}")
                except (json.JSONDecodeError, OSError):
                    print(f"  {entry.name:25s} → [skill.json 解析失败]")
        return

    asyncio.run(seed_all(
        bind_to_agents=not args.no_bind,
        skill_name=args.skill or None,
    ))


if __name__ == "__main__":
    main()
