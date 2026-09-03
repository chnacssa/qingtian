"""
吸星 — url.md 解析器

用户通过 IM 向 Agent 发送 url.md 文件，定义需要采集的 URL 清单。
Agent 接收后调此模块解析并写入 xixing.sources 表。

url.md 格式:
    # 注释行
    https://example.com @tags: 行业, 价格 P1
    https://example2.com @tags: 技术 P0

优先级: P0(立即) / P1(今日) / P2(本周) — 默认 P1
"""

import hashlib
import logging
import re
from typing import Optional

from common.db import get_pool
from . import config as xcfg

logger = logging.getLogger("xixing.urlmd_parser")

# 优先级映射（天）
PRIORITY_INTERVAL: dict[str, int] = {
    "P0": 0,    # 立即：下次调度优先爬
    "P1": 1,    # 今日：24 小时内
    "P2": 7,    # 本周：7 天内
}

DEFAULT_PRIORITY = "P1"

# 预定义标签分类映射
TAG_CATEGORIES: dict[str, str] = {
    "行业": "industry",
    "技术": "tech",
    "标准": "standard",
    "价格": "price",
    "竞品": "competitor",
}


def _generate_source_id(url: str) -> str:
    """从 URL 生成稳定的 source ID"""
    return f"urlmd_{hashlib.sha256(url.encode()).hexdigest()[:16]}"


def _parse_tags(tag_str: str) -> tuple[list[str], str]:
    """解析 @tags: 后面的标签和优先级

    Returns:
        (tags: list[str], priority: str)
    """
    if not tag_str:
        return [], DEFAULT_PRIORITY

    parts = re.split(r"[,，\s]+", tag_str.strip())
    tags = []
    priority = DEFAULT_PRIORITY

    for part in parts:
        part = part.strip()
        if not part:
            continue
        if part.upper() in PRIORITY_INTERVAL:
            priority = part.upper()
        else:
            tags.append(part)

    return tags, priority


def _get_category(tags: list[str]) -> str:
    """从标签推断知识分类"""
    for tag in tags:
        mapped = TAG_CATEGORIES.get(tag)
        if mapped:
            return mapped
    return "general"


def _parse_line(line: str) -> Optional[dict]:
    """解析单行 url.md 条目

    Returns:
        {"url": str, "tags": list[str], "priority": str, "notes": str}
        或 None（注释/空行/格式错误）
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # 提取末尾注释（# 后面的内容）
    notes = ""
    if " #" in line:
        line, _, notes = line.partition(" #")
        notes = notes.strip()

    # 提取标签标记（@tags: ...）
    # 注意：匹配用 lower()，但 URL 部分必须保留原始大小写（路径大小写敏感）
    tags = []
    priority = DEFAULT_PRIORITY
    lower_line = line.lower()
    if "@tags:" in lower_line:
        tag_index = lower_line.find("@tags:")
        before = line[:tag_index].strip()          # 原 line 切片，保留 URL 大小写
        after = line[tag_index + len("@tags:"):].strip()
        # 从 tag_str 中提取到行尾或下一个 @
        tag_str = after.split("@")[0].strip()
        tags, priority = _parse_tags(tag_str)
        line = before

    # 提取 URL
    url_match = re.search(
        r"https?://[^\s]+", line, re.IGNORECASE
    )
    if not url_match:
        # 尝试补 https://
        url_match = re.search(r"[^\s]+\.[^\s]+", line)
        if url_match:
            candidate = url_match.group().strip()
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            elif not candidate.startswith(("http://", "https://")):
                candidate = "https://" + candidate
            url = candidate
        else:
            return None
    else:
        url = url_match.group().strip().rstrip(",.!?;")

    return {
        "url": url,
        "tags": tags,
        "priority": priority,
        "notes": notes,
    }


async def parse_urlmd(content: str, agent_id: str = "") -> dict:
    """解析 url.md 内容并写入 xixing.sources 表

    Args:
        content: url.md 文件全文
        agent_id: 来源 Agent ID（供追溯）

    Returns:
        {"status": "ok", "added": int, "skipped": int, "errors": list[str]}
    """
    pool = await get_pool()
    schema = xcfg.get_schema_name()

    added = 0
    skipped = 0
    errors = []

    async with pool.acquire() as conn:
        for line in content.split("\n"):
            parsed = _parse_line(line)
            if not parsed:
                continue

            source_id = _generate_source_id(parsed["url"])
            category = _get_category(parsed["tags"])

            # 2026-08-28 P0 修复（SSRF 源头拦截）：urlmd 零校验入库是采集链
            # 读原语的源头，入库前过 url_guard（async 版含 DNS 解析私网拦截），
            # 不合规 skip 并计数，不阻断其余行
            try:
                from common.url_guard import check_external_url_async
                ok, reason = await check_external_url_async(parsed["url"])
            except Exception as e:
                ok, reason = False, f"url_guard 不可用: {e}"
            if not ok:
                skipped += 1
                errors.append(f"{parsed['url']}: URL 被安全策略拒绝 ({reason})")
                logger.warning("[urlmd] 拒绝入库 %s: %s", parsed["url"], reason)
                continue

            # 去重：相同 URL 在 dedup_days 内不重复添加
            existing = await conn.fetchval(
                f"SELECT id FROM {schema}.sources "
                "WHERE id = $1",
                source_id,
            )
            if existing:
                skipped += 1
                continue

            try:
                await conn.execute(
                    f"""INSERT INTO {schema}.sources
                        (id, name, url, source_type, schedule, categories,
                         notes, enabled, created_at)
                        VALUES ($1, $2, $3, 'urlmd', $4, $5, $6, TRUE, NOW())""",
                    source_id,
                    parsed["url"].rsplit("/", 1)[-1][:64] or "source",
                    parsed["url"],
                    "immediate" if parsed["priority"] == "P0" else "daily",
                    parsed["tags"],
                    parsed["notes"],
                )
                added += 1
                logger.info(
                    "[urlmd] 新增采集源: %s (tags=%s, priority=%s, agent=%s)",
                    parsed["url"], parsed["tags"], parsed["priority"], agent_id,
                )
            except Exception as e:
                errors.append(f"{parsed['url']}: {e}")
                logger.warning("[urlmd] 写入失败 %s: %s", parsed["url"], e)

    return {
        "status": "ok" if not errors else "partial",
        "added": added,
        "skipped": skipped,
        "errors": errors,
    }


async def parse_urlmd_file(file_path: str, agent_id: str = "") -> dict:
    """从文件路径读取并解析 url.md"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return await parse_urlmd(content, agent_id)
    except FileNotFoundError:
        return {"status": "error", "error": f"File not found: {file_path}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
