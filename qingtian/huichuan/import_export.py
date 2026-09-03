"""汇川 — 批量导入（设计文档 §4.1）

两阶段处理：先校验后写入，保证数据一致性。
支持格式：JSON (.json), Markdown (.md), CSV (.csv), 纯文本 (.txt)
"""

import csv
import hashlib
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from common.db import get_pool
from . import config as kcfg
from .database import SCHEMA

logger = logging.getLogger("huichuan.import_export")

MAX_FILES_PER_BATCH = 100

# ── 文件解析 ──────────────────────────────────────────


def parse_json(content: str) -> list[dict]:
    """解析 JSON 文件。支持单个对象或对象数组。"""
    data = json.loads(content)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise ValueError("JSON 文件必须是对象或对象数组")


def parse_markdown(content: str) -> list[dict]:
    """解析 Markdown 文件。提取 ## 标题为 title，正文为 content。"""
    result = {}
    # 第一个 # 标题作为 title
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    result["title"] = title_match.group(1).strip() if title_match else "Untitled"
    result["content"] = content
    return [result]


def parse_csv(content: str) -> list[dict]:
    """解析 CSV 文件。第一行为列名，后续行为数据。"""
    reader = csv.reader(io.StringIO(content))
    rows_list = list(reader)
    if len(rows_list) < 2:
        raise ValueError("CSV 文件至少需要标题行和一行数据")
    headers = [h.strip() for h in rows_list[0]]
    rows = []
    for values in rows_list[1:]:
        row = dict(zip(headers, [v.strip() for v in values]))
        rows.append(row)
    return rows


def parse_file_content(filename: str, content: str) -> list[dict]:
    """根据文件扩展名解析内容，返回标准化 dict 列表。"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    parsers = {
        "json": parse_json,
        "md": parse_markdown,
        "markdown": parse_markdown,
        "csv": parse_csv,
        "txt": parse_markdown,
    }
    parser = parsers.get(ext, parse_markdown)
    items = parser(content)

    # 标准化字段
    for item in items:
        if "title" not in item:
            item["title"] = filename.rsplit(".", 1)[0] if "." in filename else filename
        if "content" not in item:
            item["content"] = json.dumps(item, ensure_ascii=False, default=str)
        item.setdefault("domain", "general")
        item.setdefault("tags", [])
        item.setdefault("visibility", "public")
        item.setdefault("source", "import")
        item.setdefault("quality", 3)

    return items


# ── 内容 hash ─────────────────────────────────────────


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ── 去重检测 ──────────────────────────────────────────


async def _check_duplicate(conn, title: str, content: str,
                           owner_agent: str | None = None) -> dict:
    """检测重复知识。返回 {action, reason, existing_id, existing_version}。

    策略（设计文档 §4.1）：
    - 相同 title + content hash 一致 → skip
    - title 近似匹配 → 比较 version，新 > 旧则 update，否则 conflict
    - 无匹配 → create

    P2 (R11): 去重键按 owner_agent 范围（IS NOT DISTINCT FROM 兼容 NULL owner），
    避免跨企业/跨 Agent 同标题被误判为重复。
    """
    content_hash = _content_hash(content)

    # 精确 title 匹配（限定同一 owner 范围）
    existing = await conn.fetchrow(
        f"SELECT knowledge_id, title, version, content, updated_at "
        f"FROM {SCHEMA}.knowledge_entries "
        f"WHERE title = $1 AND owner_agent IS NOT DISTINCT FROM $2",
        title, owner_agent,
    )

    if existing:
        existing_hash = _content_hash(existing["content"])
        if content_hash == existing_hash:
            return {"action": "skip", "reason": "exact_duplicate",
                    "existing_id": str(existing["knowledge_id"])}
        # 同一标题不同内容 → 版本更新
        return {"action": "update", "reason": "title_match_new_version",
                "existing_id": str(existing["knowledge_id"]),
                "existing_version": existing["version"]}

    # 模糊 title 匹配（前 20 字相似，限定同一 owner 范围）
    title_prefix = title[:20]
    similar = await conn.fetchrow(
        f"SELECT knowledge_id, title, version FROM {SCHEMA}.knowledge_entries "
        f"WHERE title LIKE $1 AND owner_agent IS NOT DISTINCT FROM $2 LIMIT 1",
        f"{title_prefix}%", owner_agent,
    )

    if similar:
        return {"action": "conflict", "reason": "similar_title",
                "existing_id": str(similar["knowledge_id"]),
                "existing_title": similar["title"]}

    return {"action": "create"}


# ── 准入校验 §5.9 ────────────────────────────────────


# PII 关键词黑名单（使用 lookbehind/lookahead 替代 \b，
# Python 3 Unicode 模式 \b 在 CJK 字符和数字之间不识别为边界）
_PII_PATTERNS = [
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),       # 身份证号
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),          # 手机号
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),             # 银行卡号
]

_ENTRY_BLACKLIST_KEYWORDS = [
    "合同原文", "合同编号", "合同金额",
    "个人身份信息", "身份证号", "手机号码",
]


def validate_entry(title: str, content: str, domain: str) -> Optional[str]:
    """准入校验（设计文档 §5.9）。返回 None 表示通过，否则返回拒绝原因。"""
    max_size = kcfg.get_max_knowledge_size()
    if len(content) > max_size:
        return f"内容长度 {len(content)} 超过上限 {max_size}"

    if len(content.strip()) < 10:
        return "内容过短，最少 10 字符"

    # PII 检测
    for pattern in _PII_PATTERNS:
        if pattern.search(content):
            return f"内容疑似含有个人身份信息（匹配: {pattern.pattern}）"

    # 黑名单关键词
    combined = f"{title} {content[:500]}"
    for kw in _ENTRY_BLACKLIST_KEYWORDS:
        if kw in combined:
            return f"内容含不适宜入库的关键词: {kw}"

    return None


# ── 两阶段批量导入 ────────────────────────────────────


async def batch_import(
    files: list[tuple[str, str]],
    domain: Optional[str] = None,
    visibility: str = "public",
    auto_confirm: bool = False,
    owner_agent: Optional[str] = None,
) -> dict:
    """两阶段批量导入（设计文档 §4.1）。

    Args:
        files: [(filename, content), ...]
        domain: 默认 domain
        visibility: 默认可见性
        auto_confirm: 跳过确认，直接写入
        owner_agent: 归属 agent/企业（P2 R11）——去重按此范围 + 新条目落 owner_agent

    Returns:
        {total_files, created, updated, skipped, conflicted, failed, results}
    """
    if len(files) > MAX_FILES_PER_BATCH:
        return {"error": f"单次最多 {MAX_FILES_PER_BATCH} 个文件，收到 {len(files)}"}

    pool = await get_pool()
    async with pool.acquire() as conn:

        # ═══ 阶段一：全量校验 ═══
        plan: list[dict] = []

        for filename, content in files:
            try:
                items = parse_file_content(filename, content)
            except Exception as e:
                plan.append({"filename": filename, "action": "failed",
                             "reason": f"解析失败: {e}"})
                continue

            for item in items:
                title = item.get("title", filename)
                body = item.get("content", "")

                if domain:
                    item["domain"] = domain
                if visibility:
                    item["visibility"] = visibility

                # 准入校验
                reject_reason = validate_entry(title, body, item["domain"])
                if reject_reason:
                    plan.append({"filename": filename, "title": title, "action": "failed",
                                 "reason": reject_reason})
                    continue

                # 去重检测（P2 R11: 按 owner_agent 范围，防跨企业同标题误判）
                dup = await _check_duplicate(conn, title, body, owner_agent=owner_agent)
                plan.append({
                    "filename": filename,
                    "title": title,
                    "domain": item.get("domain", "general"),
                    "tags": item.get("tags", []),
                    "visibility": item.get("visibility", "public"),
                    "content": body,
                    **dup,
                })

        # ═══ 阶段二：写入 ═══
        if not auto_confirm:
            # 返回导入计划供用户确认
            _ACTION_MAP = {"create": "created", "update": "updated", "skip": "skipped", "conflict": "conflicted", "failed": "failed"}
            summary = {"created": 0, "updated": 0, "skipped": 0, "conflicted": 0, "failed": 0}
            for p in plan:
                action = p.get("action", "failed")
                mapped = _ACTION_MAP.get(action, "failed")
                summary[mapped] += 1
            return {
                "status": "plan_ready",
                "summary": summary,
                "plan": [
                    {k: v for k, v in p.items() if k != "content"}
                    for p in plan
                ],
            }

        results: list[dict] = []
        created = updated = skipped = conflicted = failed = 0

        for p in plan:
            action = p.get("action", "failed")
            if action == "failed":
                failed += 1
                results.append({"title": p.get("title"), "action": "failed",
                                "reason": p.get("reason")})
                continue

            try:
                if action == "skip":
                    skipped += 1
                    results.append({"title": p.get("title"), "action": "skipped",
                                    "reason": p.get("reason")})

                elif action == "update":
                    existing_id = p["existing_id"]
                    old_version = p.get("existing_version", 1)
                    new_version = old_version + 1

                    # P1-4（9-1 修复日）：属主断言——UPDATE 此前仅按 knowledge_id
                    # 定位，伪造调用方身份即可对同标题条目整体覆写+版本顶替。
                    # 断言：条目 owner_agent 为空（旧数据）允许接管；有主则须本人
                    # （owner_agent 为 NULL 的历史条目保持兼容）。未匹配行 = 非本人 → 拒。
                    row = await conn.fetchrow(
                        f"UPDATE {SCHEMA}.knowledge_entries "
                        f"SET content=$1, title=$2, tags=$3, version=$4, updated_at=NOW() "
                        f"WHERE knowledge_id=$5 "
                        f"  AND (owner_agent IS NULL OR owner_agent = $6) "
                        f"RETURNING knowledge_id",
                        p["content"], p["title"], p["tags"],
                        new_version, existing_id, owner_agent or "",
                    )
                    if row is None:
                        # 属主断言未过 → 记 skip 不覆写（不中断其余条目）
                        skipped += 1
                        results.append({"title": p["title"], "action": "skipped",
                                        "reason": f"owner mismatch (owner_agent={owner_agent})"})
                        continue
                    await conn.execute(
                        f"INSERT INTO {SCHEMA}.knowledge_versions "
                        f"(knowledge_id, version, content, changed_by) VALUES ($1,$2,$3,'import')",
                        existing_id, new_version, p["content"],
                    )
                    updated += 1
                    results.append({"title": p["title"], "action": "updated",
                                    "knowledge_id": str(row["knowledge_id"]),
                                    "version": new_version})

                elif action == "conflict":
                    conflicted += 1
                    results.append({"title": p["title"], "action": "conflicted",
                                    "reason": p.get("reason"),
                                    "existing_title": p.get("existing_title")})

                elif action == "create":
                    row = await conn.fetchrow(
                        f"""INSERT INTO {SCHEMA}.knowledge_entries
                            (title, domain, tags, visibility, content, source, quality, status, owner_agent)
                            VALUES ($1,$2,$3,$4,$5,'import',$6,'active',$7)
                            RETURNING knowledge_id""",
                        p["title"], p["domain"], p["tags"], p["visibility"],
                        p["content"], p.get("quality", 3), owner_agent,
                    )
                    await conn.execute(
                        f"INSERT INTO {SCHEMA}.knowledge_versions "
                        f"(knowledge_id, version, content, changed_by) VALUES ($1, 1, $2, 'import')",
                        row["knowledge_id"], p["content"],
                    )
                    created += 1
                    results.append({"title": p["title"], "action": "created",
                                    "knowledge_id": str(row["knowledge_id"])})

            except Exception as e:
                failed += 1
                results.append({"title": p.get("title"), "action": "failed",
                                "reason": str(e)})

        return {
            "status": "completed",
            "total_files": len(files),
            "total_items": len(plan),
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "conflicted": conflicted,
            "failed": failed,
            "results": results,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        }


def build_import_summary(result: dict) -> str:
    """生成人类可读的导入摘要。"""
    if result.get("status") == "plan_ready":
        s = result["summary"]
        return (
            f"导入计划: {s['created']} 新建, {s['updated']} 更新, "
            f"{s['skipped']} 跳过, {s['conflicted']} 冲突, {s['failed']} 失败"
        )
    return (
        f"导入完成: {result['created']} 新建, {result['updated']} 更新, "
        f"{result['skipped']} 跳过, {result['conflicted']} 冲突, {result['failed']} 失败"
    )
