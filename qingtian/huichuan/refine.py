"""汇川 — 精炼管道：LLM 泛化 + 去重 + 入库"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from common.db import get_pool
from huichuan.import_export import validate_entry
from huichuan.sanitizer import sanitize

from . import config as kcfg
from .database import SCHEMA

logger = logging.getLogger("huichuan.refine")

# ── 精炼 System Prompt（对齐设计文档 §7.2）────────────

REFINE_SYSTEM_PROMPT = """你是知识工程师。你的任务是将业务经验转化为通用知识规则。

输出要求：
1. 使用 Markdown 格式，严格按以下章节结构输出：
   ## 标题（概括规则核心，≤20字）
   ## 适用场景
   （1-2 句描述规则适用的行业/地区/条件）
   ## 核心规则
   （具体规律或策略，去除公司名/人名/具体金额，用百分比或区间替代）
   ## 应用建议
   （可操作的建议，≤3 条，每条 1 句）
   ## 限制条件
   （规则的边界和前提条件，如"仅限华东地区"、"Q2-Q3 有效"）

2. 去除：具体供应商名称、真实合同金额、个人姓名
3. 保留：行业术语、百分比趋势、时间规律、地区特征
4. 总字数 ≤ 500 字
5. 如果原始经验不足以提炼规则，输出"INSUFFICIENT_DATA"（纯文本，无其他内容）

示例输入：
{ "observation": "合肥沙供应商A 5月报价比市场均价低8%",
  "context": "采购谈判第3轮" }

示例输出：
## 合肥沙料谈判让步窗口
## 适用场景
合肥地区沙料采购谈判，适用于中小型供应商。
## 核心规则
第3-4轮谈判通常是供应商让步窗口，此时报价可比首轮低8-15%。
## 应用建议
- 在第2轮结束时暂缓回应，为第3轮创造施压空间
- 参考同期市场均价作为锚定价格
- 第3轮明确要求供应商给出"最终报价"
## 限制条件
- 仅限合肥及周边地区沙料市场
- 大宗采购（>100吨）让步空间更大
- 需结合市场供需波动验证"""

ADVISORY_LOCK_ID = 12345


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _safe_metadata(val) -> dict:
    """queue_item.metadata 兼容提取（asyncpg jsonb 可能为 dict / 字符串 / None）。"""
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


# ── LLM 调用 ──────────────────────────────────────────


async def _refine_llm_call(raw_experience: str, context: str = "") -> str:
    """调用 DeepSeek LLM 进行经验泛化。复用 xixing/distiller.py 的 httpx 模式。"""
    api_key = kcfg.get_deepseek_api_key()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not configured")

    user_message = json.dumps(
        {"observation": raw_experience, "context": context},
        ensure_ascii=False,
    )

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{kcfg.get_deepseek_base_url()}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": kcfg.get_refine_llm_model(),
                "messages": [
                    {"role": "system", "content": REFINE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                "max_tokens": 4096,
                "temperature": 0.2,
            },
        )
        if resp.is_success:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        else:
            raise RuntimeError(f"LLM API error: {resp.status_code} {resp.text[:200]}")


# ── 输出解析 ──────────────────────────────────────────


def _parse_llm_output(llm_output: str) -> dict:
    """解析 LLM 输出。

    返回:
        {status: "ok", title: str, content: str, confidence: int}
        {status: "insufficient", confidence: 1}
        {status: "invalid", raw: str, confidence: 2}
    """
    text = llm_output.strip()

    if text.upper().startswith("INSUFFICIENT_DATA"):
        return {"status": "insufficient", "confidence": 1}

    if text.startswith("## "):
        lines = text.split("\n")
        title = lines[0].replace("## ", "").strip()[:50]
        return {
            "status": "ok",
            "title": title,
            "content": text,
            "confidence": 4,  # LLM 成功泛化 → confidence 4
        }

    if text.startswith("##"):
        # "##标题" 无空格 → 修正
        lines = text.split("\n")
        title = lines[0].replace("##", "").strip()[:50]
        return {
            "status": "ok",
            "title": title,
            "content": text,
            "confidence": 3,
        }

    # 其他异常格式 → 保留原文，降级 confidence
    return {"status": "invalid", "raw": text, "confidence": 2}


def _confidence_to_quality(confidence: int) -> int:
    """confidence → quality 映射（设计文档 §5.1）"""
    if confidence >= 5:
        return 4
    if confidence >= 3:
        return 3
    return 2  # 挂起不入库时用 quality=2 标记


# ── 单条精炼 ──────────────────────────────────────────


async def refine_single(conn, queue_item: dict) -> dict:
    """精炼单条经验。返回处理结果。

    Args:
        conn: asyncpg connection
        queue_item: refinement_queue 行

    Returns:
        {"action": "accepted"|"rejected"|"held", "knowledge_id": ..., "confidence": ...}
    """
    item_id = queue_item["id"]
    raw_experience = queue_item["raw_experience"]
    submitter = queue_item["submitter"]
    domain = queue_item.get("domain")

    # Step 1: LLM 泛化
    try:
        llm_output = await _refine_llm_call(raw_experience)
    except Exception as e:
        # P2 (R11): LLM 失败不再无条件重置 pending 无限重刷烧额度 —— 记失败计数 +
        # 指数退避 + 失败上限，超限转 failed 不再自动重试。
        logger.error("LLM call failed for refine item %s: %s", item_id, e)
        meta = _safe_metadata(queue_item.get("metadata"))
        fail_count = int(meta.get("fail_count", 0)) + 1
        max_failures = kcfg.get_refine_max_failures()
        last_error = str(e)[:500]

        if fail_count >= max_failures:
            await conn.execute(
                f"UPDATE {SCHEMA}.refinement_queue "
                f"SET status='failed', confidence=1, processed_at=NOW(), "
                f"metadata = metadata || $1::jsonb WHERE id=$2",
                json.dumps({"fail_count": fail_count, "last_error": last_error},
                           ensure_ascii=False),
                item_id,
            )
            logger.error("Refine item %s exhausted after %d LLM failures → failed",
                         item_id, fail_count)
            return {"action": "rejected", "refine_id": str(item_id),
                    "reason": f"LLM 连续失败 {fail_count} 次，转 failed 不再自动重试"}

        delays = kcfg.get_refine_backoff_hours() or [1]
        delay_h = delays[min(fail_count - 1, len(delays) - 1)]
        next_retry = (datetime.now(timezone.utc) + timedelta(hours=delay_h)).isoformat()
        await conn.execute(
            f"UPDATE {SCHEMA}.refinement_queue "
            f"SET status='pending', confidence=1, "
            f"metadata = metadata || $1::jsonb WHERE id=$2",
            json.dumps({"fail_count": fail_count, "next_retry_at": next_retry,
                        "last_error": last_error}, ensure_ascii=False),
            item_id,
        )
        return {"action": "rejected", "refine_id": str(item_id), "reason": str(e),
                "retry_in_hours": delay_h}

    # Step 2: 解析输出
    parsed = _parse_llm_output(llm_output)

    # Step 3: 更新队列状态
    confidence = parsed["confidence"]
    if parsed["status"] == "insufficient":
        await conn.execute(
            f"UPDATE {SCHEMA}.refinement_queue "
            f"SET status='rejected', confidence=$1, processed_at=NOW() WHERE id=$2",
            confidence, item_id,
        )
        return {"action": "held", "refine_id": str(item_id), "confidence": confidence,
                "reason": "insufficient_data"}

    if parsed["status"] == "invalid":
        await conn.execute(
            f"UPDATE {SCHEMA}.refinement_queue "
            f"SET status='rejected', confidence=$1, refined_content=$2, processed_at=NOW() WHERE id=$3",
            confidence, parsed.get("raw", ""), item_id,
        )
        return {"action": "held", "refine_id": str(item_id), "confidence": confidence,
                "reason": "invalid_format"}

    # Step 4: confidence→quality 映射
    quality = _confidence_to_quality(confidence)
    if confidence <= 2:
        await conn.execute(
            f"UPDATE {SCHEMA}.refinement_queue "
            f"SET status='rejected', confidence=$1, refined_content=$2, processed_at=NOW() WHERE id=$3",
            confidence, parsed["content"], item_id,
        )
        return {"action": "held", "refine_id": str(item_id), "confidence": confidence,
                "reason": "low_confidence"}

    # ── 质量门（P1-5，9-1 修复日）：LLM 产出必须过自检才入库 ──
    # 此前 Step 5 将 LLM 原文未校验直接 INSERT（visibility='public'）。
    # 口径对齐 ingest.py：先 sanitize(PII) 再 validate_entry(准入)；
    # 不过门 → 转 rejected 不入库。visibility 同步收紧 'public'→'enterprise'
    # （泛化产物未经人审，不应默认公开面）。
    content = sanitize(parsed["content"], level="erp_to_ingest")
    title = sanitize(parsed["title"], level="erp_to_ingest")[:256]
    violation = validate_entry(title, content, domain or "general")
    if violation:
        await conn.execute(
            f"UPDATE {SCHEMA}.refinement_queue "
            f"SET status='rejected', confidence=$1, refined_content=$2, processed_at=NOW() WHERE id=$3",
            confidence, content, item_id,
        )
        logger.warning("Refine item %s rejected by validation gate: %s", item_id, violation)
        return {"action": "held", "refine_id": str(item_id), "confidence": confidence,
                "reason": f"validation_failed: {violation}"}

    # Step 5: 入库 (status=draft, 冷启动 48h)
    row = await conn.fetchrow(
        f"""INSERT INTO {SCHEMA}.knowledge_entries
            (title, domain, tags, visibility, owner_agent, content, source, quality, status, refined_at)
            VALUES ($1,$2,$3,'enterprise',$4,$5,'refinement',$6,'draft',NOW())
            RETURNING knowledge_id""",
        title,
        domain or "general",
        [],
        submitter,
        content,
        quality,
    )

    knowledge_id = row["knowledge_id"]

    # 版本快照（sanitize 后内容，与主表一致）
    await conn.execute(
        f"INSERT INTO {SCHEMA}.knowledge_versions (knowledge_id, version, content, changed_by) "
        f"VALUES ($1, 1, $2, $3)",
        knowledge_id, content, submitter,
    )

    # 更新队列
    await conn.execute(
        f"UPDATE {SCHEMA}.refinement_queue "
        f"SET status='approved', confidence=$1, refined_content=$2, knowledge_id=$3, processed_at=NOW() "
        f"WHERE id=$4",
        confidence, content, knowledge_id, item_id,
    )

    return {"action": "accepted", "knowledge_id": str(knowledge_id),
            "refine_id": str(item_id), "confidence": confidence}


# ── 批量精炼 ──────────────────────────────────────────


async def refine_batch(conn=None, limit: Optional[int] = None) -> dict:
    """处理精炼队列中的 pending 条目。

    使用 PostgreSQL advisory lock 保证幂等性（设计文档 §7.2）。
    """
    if limit is None:
        limit = kcfg.get_refine_batch_size()

    t0 = time.time()
    close_conn = False

    if conn is None:
        pool = await get_pool()
        conn = await pool.acquire().__aenter__()
        close_conn = True

    try:
        # 幂等锁
        locked = await conn.fetchval("SELECT pg_try_advisory_lock($1)", ADVISORY_LOCK_ID)
        if not locked:
            logger.warning("Refinement skipped: advisory lock held by another process")
            return {"processed": 0, "accepted": 0, "rejected": 0,
                    "status": "locked", "reason": "another refinement in progress"}

        try:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.refinement_queue "
                f"WHERE status = 'pending' "
                f"AND (metadata->>'next_retry_at' IS NULL "
                f"     OR (metadata->>'next_retry_at')::timestamptz <= NOW()) "
                f"ORDER BY created_at ASC LIMIT $1",
                limit,
            )

            if not rows:
                return {"processed": 0, "accepted": 0, "rejected": 0, "status": "empty"}

            accepted = 0
            rejected = 0

            for row in rows:
                result = await refine_single(conn, dict(row))
                if result["action"] == "accepted":
                    accepted += 1
                else:
                    rejected += 1

            duration_ms = (time.time() - t0) * 1000
            logger.info(
                f"Refinement batch done: {len(rows)} processed, "
                f"{accepted} accepted, {rejected} rejected in {duration_ms:.0f}ms"
            )

            return {
                "processed": len(rows),
                "accepted": accepted,
                "rejected": rejected,
                "duration_ms": duration_ms,
            }

        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_ID)

    finally:
        if close_conn:
            await conn.close()
