"""
吸星 — 经验蒸馏管道（替代 distill.py）

从 yongheng 拉取未蒸馏的 memories → 按 topic 聚类 → LLM 提炼 → 写回 yongheng high_value 记忆
"""

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from common.db import get_pool
from . import config as cfg
from .quality_gate import _jaccard_similarity, gate_content_quality

logger = logging.getLogger("xixing.distiller")

SCHEMA = cfg.get_schema_name()

# LLM 调用最大重试次数
_MAX_LLM_RETRIES = 3
# 重试间隔基数（秒）
_RETRY_BASE_DELAY = 2.0

# 聚类用停用词（中文 + 英文）
_CLUSTER_STOP_WORDS = {
    "的", "是", "在", "和", "了", "有", "不", "这", "也", "就", "都", "与",
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "under", "again",
    "further", "then", "once", "here", "there", "when", "where", "why",
    "how", "all", "both", "each", "few", "more", "most", "other", "some",
    "such", "only", "own", "same", "so", "than", "too", "very", "just",
    "因为", "所以", "但是", "虽然", "如果", "可以", "没有", "什么", "这个",
    "那个", "他们", "我们", "你们", "它们", "自己", "已经", "还是", "或者",
    "并且", "而且", "不是", "就是", "一种", "一个", "一些", "一种", "其中",
}

# ── prompt 注入防护 ──────────────────────────────────

def _sanitize_content_segment(text: str, max_len: int = 500) -> str:
    """截断并转义用户内容，防止 prompt 注入。"""
    truncated = text[:max_len]
    # 闭合代码块标记，避免 LLM 输出被用户内容里的 ``` 干扰
    truncated = truncated.replace("```", "'''")
    return truncated


def _extract_keywords(text: str, top_n: int = 8) -> list[str]:
    """从文本中提取关键词（简单频率法，按字符长度加权）。"""
    # 中文：2+ 字词；英文：3+ 字母单词
    words = re.findall(r"[一-鿿]{2,}|[a-zA-Z]{3,}", text.lower())
    freq: dict[str, int] = defaultdict(int)
    for w in words:
        if w not in _CLUSTER_STOP_WORDS:
            freq[w] += 1
    # 按频率排序，取 top_n
    sorted_words = sorted(freq.items(), key=lambda x: -x[1])
    return [w for w, _ in sorted_words[:top_n]]


def _cluster_by_content(items: list[dict], min_overlap: int = 2) -> list[tuple[str, list[dict]]]:
    """基于关键词重叠的内容聚类。

    每个 item 提取关键词，共享 >= min_overlap 个关键词的 items 归入同一簇。
    返回 [(cluster_label, items), ...]。
    """
    if len(items) <= 1:
        return [("singleton", items)] if items else []

    # 为每个 item 提取关键词
    item_keywords: list[tuple[int, set[str]]] = []
    for i, item in enumerate(items):
        content = item.get("content", "")
        kws = set(_extract_keywords(content))
        item_keywords.append((i, kws))

    # 贪心聚类：从第一个 item 开始，找到所有与其共享 >= min_overlap 关键词的 items
    remaining = set(range(len(items)))
    clusters: list[tuple[str, list[dict]]] = []

    while remaining:
        seed_idx = min(remaining)
        seed_kws = item_keywords[seed_idx][1]
        if not seed_kws:
            remaining.discard(seed_idx)
            continue

        # 找到与 seed 共享 >= min_overlap 关键词的所有 items
        cluster_indices = {seed_idx}
        for idx in list(remaining):
            if idx == seed_idx:
                continue
            overlap = len(seed_kws & item_keywords[idx][1])
            if overlap >= min_overlap:
                cluster_indices.add(idx)

        remaining -= cluster_indices

        cluster_items = [items[i] for i in sorted(cluster_indices)]
        # 用共享最多的关键词作为簇标签
        kw_counts: dict[str, int] = defaultdict(int)
        for idx in cluster_indices:
            for kw in item_keywords[idx][1]:
                kw_counts[kw] += 1
        top_kws = sorted(kw_counts.items(), key=lambda x: -x[1])[:3]
        label = "_".join(kw for kw, _ in top_kws) if top_kws else f"cluster_{len(clusters)}"

        clusters.append((label, cluster_items))

    return clusters


async def run_distillation(
    namespace: str = "global",
    max_source_memories: int = 500,
    model: str | None = None,
) -> dict:
    """执行经验蒸馏：聚类 → LLM 提炼 → 写回。"""
    if model is None:
        model = cfg.get_distiller_model()

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 记录蒸馏运行
        run_id = await conn.fetchval(
            f"INSERT INTO {SCHEMA}.distillation_runs (namespace, llm_model, status) VALUES ($1, $2, 'running') RETURNING id",
            namespace, model,
        )

        # 获取 yongheng schema 名
        from yongheng import config as ycfg
        yh_schema = ycfg.get_schema_name()

        # 拉取未蒸馏的记忆
        rows = await conn.fetch(
            f"""SELECT id, content, memory_type, source, created_at
                FROM {yh_schema}.memories
                WHERE namespace = $1 AND memory_type != 'high_value'
                ORDER BY created_at DESC LIMIT $2""",
            namespace, max_source_memories,
        )

        source_count = len(rows)
        if source_count < cfg.get_distiller_min_cluster():
            await conn.execute(
                f"UPDATE {SCHEMA}.distillation_runs SET finished_at=NOW(), source_count=$1, produced_count=0, status='skipped' WHERE id=$2",
                source_count, run_id,
            )
            return {"source_count": source_count, "produced_count": 0, "llm_model": model, "status": "skipped"}

        # 聚类：先按 memory_type 分大类，再在每个大类内做内容关键词聚类
        raw_by_type: dict[str, list[dict]] = {}
        for row in rows:
            key = row['memory_type']
            if key not in raw_by_type:
                raw_by_type[key] = []
            raw_by_type[key].append({
                "id": row["id"],
                "content": row["content"],
                "memory_type": row["memory_type"],
                "created_at": row["created_at"].isoformat(),
            })

        # 每个 memory_type 内做内容聚类
        clusters: dict[str, list[dict]] = {}
        for mem_type, items in raw_by_type.items():
            if len(items) <= 1:
                continue
            content_clusters = _cluster_by_content(items, min_overlap=2)
            for label, cluster_items in content_clusters:
                if len(cluster_items) < 2:
                    continue
                cluster_key = f"{mem_type}:{label}"
                clusters[cluster_key] = cluster_items

        produced_count = 0
        total_tokens = 0

        for cluster_key, items in clusters.items():

            # 构建蒸馏 prompt
            items_text = "\n\n---\n\n".join(
                f"[#{i+1}] {_sanitize_content_segment(item['content'])}" for i, item in enumerate(items[:10])
            )

            prompt = (
                "从以下多条记忆记录中提取关键经验，提炼为一条结构化的经验条目。\n"
                "输出 JSON 格式：\n"
                '{"title": "经验标题", "background": "背景", "decision": "决策", "outcome": "结果", "lesson": "教训", '
                '"target_audience": {"categories": ["biz:buyer" 等，若无特定受众则空数组], '
                '"capabilities": ["钢材" 等关键能力标签，若无则空数组], '
                '"scope": "targeted|global"}}\n\n'
                "target_audience 说明：如果知识具有通用性（如安全规范），scope 填 global；"
                "如果只对特定领域/角色有用，scope 填 targeted 并列出 categories 和 capabilities。\n\n"
                f"{items_text}"
            )

            api_key = cfg.get_deepseek_key()
            if not api_key:
                logger.warning("Distillation skipped: no LLM API key (ZHIPU_API_KEY/DEEPSEEK_API_KEY) available")
                continue

            # LLM 调用含重试
            result_text = None
            resp_data = None
            last_error = None
            for attempt in range(1, _MAX_LLM_RETRIES + 1):
                try:
                    async with httpx.AsyncClient(timeout=60) as client:
                        resp = await client.post(
                            f"{cfg.get_deepseek_base_url()}/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                            json={
                                "model": model,
                                "messages": [{"role": "user", "content": prompt}],
                                "max_tokens": 4096,
                                "temperature": 0.3,
                                "response_format": {"type": "json_object"},
                            },
                        )
                        if resp.is_success:
                            resp_data = resp.json()
                            result_text = resp_data["choices"][0]["message"]["content"]
                            total_tokens += resp_data.get("usage", {}).get("total_tokens", 0)
                            break
                        else:
                            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                            logger.warning(f"Distillation LLM attempt {attempt}/{_MAX_LLM_RETRIES}: {last_error}")
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"Distillation LLM attempt {attempt}/{_MAX_LLM_RETRIES}: {e}")
                if attempt < _MAX_LLM_RETRIES:
                    await asyncio.sleep(_RETRY_BASE_DELAY * attempt)

            if result_text is None:
                logger.error(f"Distillation LLM failed after {_MAX_LLM_RETRIES} retries: {last_error}")
                continue

            try:
                try:
                    extracted = json.loads(result_text)
                except json.JSONDecodeError:
                    extracted = {"title": f"经验蒸馏: {cluster_key}", "lesson": result_text[:500]}

                # ── 质量门 ①：内容校验 ──────────────────────
                title = extracted.get("title", "").strip()
                lesson = extracted.get("lesson", "").strip()
                if not title or len(title) < 3:
                    logger.warning("蒸馏跳过空标题: cluster=%s title=%r", cluster_key, title)
                    continue
                if not lesson or len(lesson) < 30:
                    logger.warning("蒸馏跳过无实质教训: cluster=%s lesson=%r",
                                   cluster_key, lesson[:50])
                    continue
                # 防灌水：全是废话的产出
                boilerplate = {"暂无经验", "无", "无相关经验", "暂无", "待补充", "N/A", "none",
                               "没有经验", "暂无相关经验", "无经验", "暂无可用经验"}
                if lesson.strip() in boilerplate or title.strip() in boilerplate:
                    logger.warning("蒸馏跳过空话产出: cluster=%s", cluster_key)
                    continue

                # ── 质量门 ②：去重（精确 + 模糊）─────────────
                # 将 extracted dict 序列化为文本用于去重比较
                distilled_text = json.dumps(extracted, ensure_ascii=False)

                # 精确去重：memories 表无 content_hash 列（原查询引用不存在列，
                # 导致每个 cluster 都抛错、蒸馏产出恒为 0），改用 content 精确比较
                dup = await conn.fetchval(
                    f"SELECT id FROM {yh_schema}.memories "
                    f"WHERE content = $1 AND memory_type = 'high_value' LIMIT 1",
                    distilled_text,
                )
                if dup:
                    logger.info("蒸馏跳过精确重复: cluster=%s", cluster_key)
                    continue

                # 模糊去重
                recent_hv = await conn.fetch(
                    f"SELECT content FROM {yh_schema}.memories "
                    f"WHERE namespace = $1 AND memory_type = 'high_value' "
                    f"ORDER BY created_at DESC LIMIT 50",
                    namespace,
                )
                skip_fuzzy = False
                for hv in recent_hv:
                    hv_content = hv["content"]
                    if isinstance(hv_content, str):
                        try:
                            hv_content = json.loads(hv_content)
                        except json.JSONDecodeError:
                            pass
                    if isinstance(hv_content, dict):
                        hv_text = json.dumps(hv_content, ensure_ascii=False)
                    else:
                        hv_text = str(hv_content) if hv_content else ""
                    sim = _jaccard_similarity(distilled_text, hv_text)
                    if sim > 0.7:
                        logger.info("蒸馏跳过模糊重复: cluster=%s 相似度 %.0f%%",
                                    cluster_key, sim * 100)
                        skip_fuzzy = True
                        break
                if skip_fuzzy:
                    continue

                # ── 质量门 ③：内容质量评分 ──────────────────
                # 蒸馏产出是 JSON，天生不长。长度不达标时仅警告，正文率/HTML 异常则拦截。
                qc_passed, qc_detail, qc_score = gate_content_quality(distilled_text)
                if not qc_passed:
                    if "过短" in qc_detail and len(distilled_text) > 80:
                        # 蒸馏 JSON 天生紧凑，80 字符以上视为正常，仅警告
                        logger.info("蒸馏内容偏短但可接受: cluster=%s len=%d score=%.2f",
                                    cluster_key, len(distilled_text), qc_score)
                    else:
                        logger.warning("蒸馏内容质量不过关: cluster=%s detail=%s score=%.2f",
                                       cluster_key, qc_detail, qc_score)
                        continue

                # ── 从 LLM 输出中提取 target_audience，默认 global ──
                audience = extracted.pop("target_audience", None)
                if not isinstance(audience, dict):
                    audience = {"scope": "global", "categories": [], "capabilities": []}
                audience.setdefault("scope", "global")
                audience.setdefault("categories", [])
                audience.setdefault("capabilities", [])

                # 写回 yongheng 为 high_value 记忆
                from yongheng.memory_service import write_memory
                # distilled_text 已在质量门②中序列化（见上方 json.dumps(extracted)）
                await write_memory(
                    conn,
                    namespace=namespace,
                    content=distilled_text,
                    mem_type="high_value",
                    source=f"xixing-distiller:{cluster_key}",
                    metadata={
                        "distilled_from": [item["id"] for item in items],
                        "distillation_run": run_id,
                        "source_count": len(items),
                        "cluster_key": cluster_key,
                        "target_audience": audience,
                    },
                )
                produced_count += 1

                # 同步写入汇川 knowledge_entries
                try:
                    from huichuan import config as kl_cfg
                    kl_schema = kl_cfg.get_schema_name()
                    title = extracted.get("title", f"经验蒸馏: {cluster_key}")
                    domain = extracted.get("domain", cluster_key)
                    tags = audience.get("capabilities", []) + audience.get("categories", [])
                    await conn.execute(
                        f"INSERT INTO {kl_schema}.knowledge_entries "
                        f"(title, domain, content, tags, source, visibility, quality, status, metadata) "
                        f"VALUES ($1, $2, $3, $4, 'xixing-distiller', 'enterprise', 3, 'active', $5) "
                        f"ON CONFLICT DO NOTHING",
                        title, domain, distilled_text, tags,
                        # review(2026-08-15): metadata 是 JSONB 列，直接传 dict
                        {"distilled_from": [item["id"] for item in items],
                         "distillation_run": str(run_id),
                         "target_audience": audience},
                    )
                except Exception:
                    logger.exception("huichuan sync failed for cluster %s", cluster_key)
            except Exception as e:
                logger.error(f"Distillation failed for cluster {cluster_key}: {e}")

        # 完成运行记录
        status = "completed" if produced_count > 0 else "empty"
        await conn.execute(
            f"UPDATE {SCHEMA}.distillation_runs SET finished_at=NOW(), source_count=$1, produced_count=$2, token_used=$3, status=$4 WHERE id=$5",
            source_count, produced_count, total_tokens, status, run_id,
        )

    # ★ 吸星扩增：Skill 提案生成（作为蒸馏的副产品）
    skill_result = []
    try:
        pool = await get_pool()
        skill_result = await _generate_skill_proposals(pool)
        if skill_result:
            logger.info(f"Distill: {len(skill_result)} skill proposal(s) generated")
    except Exception as e:
        logger.error(f"Distill skill generation failed: {e}")
        # 不抛出异常，不影响主流程

    return {
        "source_count": source_count,
        "produced_count": produced_count,
        "llm_model": model,
        "token_used": total_tokens,
        "status": status,
        "skill_proposals": skill_result if skill_result else [],
    }


# ═══════════════════════════════════════════════════════════
# 吸星扩增：Skill 提案生成
# ═══════════════════════════════════════════════════════════


async def _generate_skill_proposals(pool, full_scan: bool = False) -> list[dict]:
    """从 Agent 对话和纠正数据中生成轻量 Skill 提案。

    7 步流程：
    1. 拉取数据源（当前仅 baishitong，可配置扩展）
    2. 获取已有 Skill 列表（去重用）
    3. LLM 聚类分析
    4. 去重（与已有 Skill + 提案间去重）
    5. 频次阈值过滤（>= min_frequency）
    6. 提交轻量提案至管理服（HTTP POST，不含 schema/实现代码）
    7. 标记纠正已同步
    """
    from .config import (
        get_skill_proposals_enabled,
        get_skill_proposals_min_frequency,
        get_skill_proposals_max_per_round,
    )

    if not get_skill_proposals_enabled():
        return []

    # Step 1a: 拉取对话
    time_window = "" if full_scan else "AND created_at >= NOW() - INTERVAL '7 days'"
    conversations = await pool.fetch(f"""
        SELECT content, skill_used, confidence, feedback
        FROM baishitong.conversations
        WHERE role = 'user' AND skill_used != ''
          {time_window}
        ORDER BY created_at DESC LIMIT 500
    """)

    # Step 1b: 拉取未同步纠正 + 近期纠正
    time_window_corr = "" if full_scan else "OR c.created_at >= NOW() - INTERVAL '7 days'"
    corrections = await pool.fetch(f"""
        SELECT c.*, conv.content as original_query
        FROM baishitong.corrections c
        JOIN baishitong.conversations conv ON conv.id = c.conversation_id
        WHERE c.synced_to_xixing = FALSE
           {time_window_corr}
        ORDER BY c.created_at DESC LIMIT 200
    """)

    if not conversations and not corrections:
        return []

    # Step 2: 已有 Skill 列表（管理服接管后需改为调用管理服 API）
    existing = []
    try:
        existing = await pool.fetch(
            "SELECT name, description FROM skills.skill_definitions WHERE status IN ('active', 'draft')"
        )
    except Exception:
        logger.warning("skills.skill_definitions table not accessible (management server not deployed yet)")

    # Step 3: LLM 聚类分析
    proposals, rejected_skill_names = await _llm_analyze_skills(
        conversations=list(conversations),
        corrections=list(corrections),
        existing_skills=[{"name": r["name"], "description": r["description"]} for r in existing],
        pool=pool,
    )
    if not proposals:
        return []

    # Step 4a: 与已有 Skill 去重
    existing_names = {r["name"] for r in existing}
    proposals = [p for p in proposals if p["name"] not in existing_names]

    # Step 4b: 提案间去重（字符集 Jaccard 相似度 > 85%）
    seen_names = set()
    deduped = []
    for p in proposals:
        base = p["name"].lower().replace("_", "").replace("-", "")
        if base in seen_names:
            continue
        too_similar = False
        for kept in deduped:
            k = kept["name"].lower().replace("_", "").replace("-", "")
            if len(set(base) & set(k)) / max(len(set(base)), len(set(k)), 1) > 0.85:
                too_similar = True
                break
        if too_similar:
            continue
        seen_names.add(base)
        deduped.append(p)
    proposals = deduped
    if not proposals:
        return []

    # Step 5: 频次阈值过滤
    min_freq = get_skill_proposals_min_frequency()
    proposals = [p for p in proposals if p.get("frequency", 0) >= min_freq]
    if not proposals:
        return []

    # Step 6: 提交轻量提案至管理服（同进程 DB 写入）
    # 吸星只输出方向（name / display_name / description / frequency / sample_queries），
    # 不含完整 schema 和实现代码。管理服收到后自动完成代码生成和测试。
    from osskill.database import insert_proposal

    max_per = get_skill_proposals_max_per_round()
    saved = []
    for prop in proposals[:max_per]:
        proposal = {
            "name": prop["name"],
            "display_name": prop["display_name"],
            "description": prop["description"],
            "category": prop.get("category", "cost"),
            "reason": prop.get("reason", ""),
            "frequency": prop.get("frequency", 0),
            "sample_queries": prop.get("sample_queries", []),
            "knowledge_categories": prop.get("knowledge_categories", []),
            "evidence": {
                "source": "evolved",
                "analysis_date": datetime.now(timezone.utc).isoformat(),
                "data_sources_checked": ["baishitong.conversations", "baishitong.corrections"],
                "conversation_count": len(conversations),
                "correction_count": len(corrections),
                "rejected_skills_checked": rejected_skill_names,
            },
        }
        try:
            result = await insert_proposal(proposal)
            saved.append(result)
            logger.info("Proposal saved: %s (id=%s)", proposal["name"], result["id"])
        except Exception as e:
            logger.warning("Failed to save proposal %s: %s", proposal["name"], e)

    # Step 7: 标记本次已处理的纠正为已同步
    if saved:
        correction_ids = [c["id"] for c in corrections if not c.get("synced_to_xixing", False)]
        if correction_ids:
            await pool.execute(
                "UPDATE baishitong.corrections SET synced_to_xixing = TRUE WHERE id = ANY($1::bigint[])",
                correction_ids
            )

    return saved


async def _llm_analyze_skills(conversations, corrections, existing_skills, pool=None) -> tuple[list[dict], list[str]]:
    """LLM 分析对话和纠正数据，识别 Skill 需求

    返回值：(proposals, rejected_skill_names)
    """
    from common.llm import llm_call_json

    # 获取近期被驳回的提案（反馈闭环）
    rejected_context = ""
    rejected_skill_names = []
    if pool:
        try:
            rows = await pool.fetch("""
                SELECT sd.name, sr.reason
                FROM skills.skill_reviews sr
                JOIN skills.skill_definitions sd ON sd.id = sr.skill_id
                WHERE sr.action = 'reject'
                  AND sr.created_at >= NOW() - INTERVAL '30 days'
                ORDER BY sr.created_at DESC LIMIT 10
            """)
            if rows:
                rejected_skill_names = [r["name"] for r in rows]
                rejected_context = (
                    "\n近期被驳回的提案（请避免生成相似方向）：\n" +
                    "\n".join(f"- {r['name']}: {r['reason']}" for r in rows)
                )
        except Exception:
            logger.warning("Failed to fetch rejected skills (management server not deployed yet)")

    existing_desc = "\n".join(
        f"- {s['name']}: {s['description']}" for s in existing_skills
    )

    recent = "\n".join(
        f"- {c['content'][:200]} [skill: {c['skill_used']}] [conf: {c['confidence']}]"
        for c in conversations[:80]
    )

    correction_text = "\n".join(
        f"- 用户问: {c['original_query'][:100]} → 纠正: {c['correction'][:100]}"
        for c in corrections[:30]
    )

    prompt = f"""你是一个 Skill 需求分析师。分析以下对话和纠正数据。

已有 Skill：
{existing_desc or '(暂无)'}
{rejected_context}

过去 7 天对话（{len(conversations)} 条）：
{recent}

纠正反馈（{len(corrections)} 条）：
{correction_text}

请找出高频、输入输出明确、现有 Skill 未覆盖的能力需求。
对每个潜在 Skill 输出 JSON 数组：
[
  {{
    "name": "英文标识",
    "display_name": "中文名",
    "description": "功能描述",
    "category": "分类(cost/bidding/procurement/general)",
    "reason": "为什么需要",
    "frequency": 次数估算,
    "sample_queries": ["样例1", "样例2"],
    "knowledge_categories": ["依赖知识类别"],
    "existing_skill_overlap": true/false
  }}
]
注：提案是轻量的方向识别，不需要输出 input_schema/output_schema ——
管理服收到后会据此自动生成完整实现。
没有则返回 []。"""

    result = await llm_call_json(
        prompt=prompt,
        caller="xixing.skill_proposal",
        system_prompt="你是一个准确的分析师，只输出 JSON。",
        temperature=0.1,
    )

    if result is None:
        return [], rejected_skill_names

    # Handle both direct array and wrapped responses
    if isinstance(result, dict):
        proposals = result.get("proposals", result.get("skills", []))
    elif isinstance(result, list):
        proposals = result
    else:
        return [], rejected_skill_names

    proposals = proposals if isinstance(proposals, list) else []
    return proposals, rejected_skill_names


async def extract_entities(text: str) -> list[dict]:
    """供 api.py _process_sync 调用的简易实体提取包装函数

    使用关键词提取和聚类，返回实体列表 [{entity, type, mentions}]
    """
    keywords = _extract_keywords(text, top_n=15)
    return [{"entity": kw, "type": "keyword", "mentions": 1} for kw in keywords]
