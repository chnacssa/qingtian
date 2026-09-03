"""
吸星 — 竞品扫描器（替代 xixing-scan.py）

基于 9 维度能力定义，扫描 ClawHub Skills 市场，识别与吸星能力差异化程度高的技能。
结果写入 scan_results 表，高差异度项自动标记 actionable。
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta

import httpx

from common.db import get_pool
from . import config as cfg

logger = logging.getLogger("xixing.scanner")

SCHEMA = cfg.get_schema_name()
TOP_N = cfg.get_scanner_top_n()

# 吸星 9 大维度能力定义
# scope: "shared" = 所有底座通用，从属服务器关注；"management" = 管理服务器专属
CAPABILITY_DIMENSIONS = {
    "memory_distill": {
        "name": "记忆蒸馏/归档",
        "scope": "shared",
        "keywords": ["memory distill", "经验沉淀", "summarize", "digest", "archive", "compress"],
    },
    "competitive_scan": {
        "name": "竞品分析/扫描",
        "scope": "management",
        "keywords": ["competitive", "market scan", "compare", "benchmark", "竞品"],
    },
    "crawl_fetch": {
        "name": "知识采集/爬取",
        "scope": "shared",
        "keywords": ["crawl", "web fetching", "scrape", "feishu", "采集", "爬取"],
    },
    "experience_pack": {
        "name": "经验封装/避坑",
        "scope": "shared",
        "keywords": ["skill", "lesson learn", "trouble shoot", "pitfall", "经验", "踩坑"],
    },
    "longterm_memory": {
        "name": "长期记忆/持久化",
        "scope": "shared",
        "keywords": ["vector", "embedding", "semantic search", "pgvector", "long.?term memory"],
    },
    "session_memory": {
        "name": "短期/会话记忆",
        "scope": "shared",
        "keywords": ["session memory", "transcript", "conversation", "上下文"],
    },
    "memory_orchestration": {
        "name": "记忆编排/管理",
        "scope": "shared",
        "keywords": ["tier", "layer", "hierarchy", "orchestrat", "manage", "organize"],
    },
    "self_evolution": {
        "name": "自进化",
        "scope": "management",
        "keywords": ["self-evolution", "adapt", "learn", "evolve", "进化", "auto.?improve"],
    },
    "multi_agent": {
        "name": "多机/分布式",
        "scope": "management",
        "keywords": ["multi-agent", "sync", "federated", "distributed", "peer", "分布式"],
    },
}


def _score_dimensions(description: str) -> dict:
    """对一段描述文本在 9 个维度上打分。"""
    desc_lower = description.lower()
    scores = {}
    for dim_id, dim in CAPABILITY_DIMENSIONS.items():
        hits = sum(1 for kw in dim["keywords"] if re.search(kw, desc_lower))
        scores[dim_id] = min(hits / max(len(dim["keywords"]) * 0.15, 1), 1.0)
    return scores


def _similarity_to_xixing(dim_scores: dict) -> float:
    """计算与吸星的能力重叠度（高=与吸星重叠多，低=差异化大）。"""
    if not dim_scores:
        return 0.5
    return round(sum(dim_scores.values()) / len(dim_scores), 2)


def _classify_difference(similarity: float) -> str:
    """根据重叠度分类差异度。"""
    if similarity < 0.3:
        return "different"    # ✅ 不同，值得关注
    elif similarity < 0.5:
        return "partial"      # ⚠️ 有重叠但机制更完善
    else:
        return "overlap"      # ⬜ 重叠度高


async def _fetch_clawhub_skills() -> list[dict]:
    """从 ClawHub 获取技能列表（优先使用 CLI）。"""
    # 尝试使用 openclaw CLI（to_thread 避免阻塞事件循环）
    import subprocess
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["openclaw", "skills", "search", "--limit", "100", "--format", "json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            skills = data if isinstance(data, list) else data.get("skills", data.get("results", []))
            return [
                {
                    "name": s.get("name", "unknown"),
                    "description": s.get("description", s.get("summary", "")),
                    "url": s.get("url", s.get("source_url", "")),
                    "function_cluster": s.get("category", s.get("cluster", "")),
                }
                for s in skills
            ]
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # Fallback: HTTP 调用 ClawHub API
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get("https://clawhub.ai/api/skills?limit=100")
            if resp.is_success:
                data = resp.json()
                skills = data if isinstance(data, list) else data.get("skills", [])
                return [
                    {
                        "name": s.get("name", "unknown"),
                        "description": s.get("description", ""),
                        "url": s.get("url", ""),
                        "function_cluster": s.get("category", ""),
                    }
                    for s in skills
                ]
    except Exception:
        pass

    logger.warning("Could not fetch ClawHub skills via CLI or API")
    return []


def _map_cluster_to_dim(function_cluster: str) -> str | None:
    """Map scanner function_cluster name back to dimension ID."""
    for dim_id, dim_info in CAPABILITY_DIMENSIONS.items():
        if dim_info["name"] == function_cluster:
            return dim_id
    # Fuzzy match
    for dim_id, dim_info in CAPABILITY_DIMENSIONS.items():
        if any(kw in function_cluster for kw in dim_info["keywords"]):
            return dim_id
    return None


async def _analyze_gaps(self_scores: dict, scan_results: list[dict]) -> list[dict]:
    """Analyze gaps between self-assessment and external competitive landscape.

    Args:
        self_scores: {dim_id: {"score": float, "name": str, ...}} from self_assess()
        scan_results: list of scan result dicts from run_scan()

    Returns:
        List of gap dicts sorted by gap desc, each with:
        {dimension, dim_name, self_score, external_best, external_name, gap, references}
    """
    # Aggregate external best per dimension
    external_best: dict[str, tuple[float, str, list[dict]]] = {}
    for r in scan_results:
        # Map function_cluster back to dimension ID
        dim_id = _map_cluster_to_dim(r.get("function_cluster", ""))
        if dim_id is None:
            continue
        score = r.get("differentiation_score", 0)
        current_best = external_best.get(dim_id, (0, "", []))
        if dim_id not in external_best or score > current_best[0]:
            # 首次遇到该维度或得分更高：替换。原逻辑在 score==0 且 dim 首次出现时
            # 走 elif 分支 append 到不存在的 key → KeyError
            external_best[dim_id] = (score, r["skill_name"], [r])
        elif score == current_best[0]:
            external_best[dim_id][2].append(r)

    gaps = []
    for dim_id, self_info in self_scores.items():
        ext_info = external_best.get(dim_id, (0, "", []))
        ext_score = ext_info[0]
        gap = round(ext_score - self_info["score"], 2)
        if gap > 0.15:  # meaningful gap threshold
            gaps.append({
                "dimension": dim_id,
                "dim_name": self_info["name"],
                "self_score": self_info["score"],
                "external_best": ext_score,
                "external_name": ext_info[1],
                "gap": gap,
                "references": [
                    {"skill_name": r["skill_name"], "url": r.get("url", ""), "description": r.get("description", "")}
                    for r in ext_info[2][:3]
                ],
            })

    gaps.sort(key=lambda x: x["gap"], reverse=True)
    return gaps


async def _generate_proposals(gaps: list[dict], model: str | None = None) -> list[dict]:
    """Generate concrete improvement proposals from identified gaps using LLM.

    Args:
        gaps: list of gap dicts from _analyze_gaps()
        model: LLM model name, defaults to deepseek-v4-flash

    Returns:
        List of proposal dicts ready for yongheng write_memory
    """
    if not gaps:
        return []

    if model is None:
        model = cfg.get_proposal_model()

    api_key = cfg.get_deepseek_key()
    if not api_key:
        logger.warning("Proposal generation: no LLM API key (ZHIPU_API_KEY/DEEPSEEK_API_KEY), skipping")
        return []

    # Take top 3 gaps to keep LLM cost reasonable
    top_gaps = gaps[:3]

    proposals = []
    for gap in top_gaps:
        ref_text = "\n".join(
            f"- {r['skill_name']}: {r['description'][:200]}\n  URL: {r['url']}"
            for r in gap["references"]
        )

        prompt = (
            f"你是ACSSA 智能体操作系统的架构师。发现以下能力缺口需要改进：\n\n"
            f"维度: {gap['dim_name']} ({gap['dimension']})\n"
            f"当前自评分: {gap['self_score']:.0%}\n"
            f"外部最佳实践: {gap['external_name']} (得分: {gap['external_best']:.0%})\n"
            f"差距: +{gap['gap']:.0%}\n\n"
            f"外部参考实现:\n{ref_text}\n\n"
            f"请生成一条具体的改进提案。返回 JSON（仅 JSON，不要其他内容）：\n"
            f'{{"title": "提案标题", "current_state": "当前状态一句话", '
            f'"target_state": "目标状态一句话", '
            f'"implementation_steps": ["步骤1", "步骤2", "步骤3"], '
            f'"estimated_effort": "small|medium|large", '
            f'"expected_impact": "预期收益一句话", '
            f'"dimension": "{gap["dimension"]}"}}'
        )

        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(
                    f"{cfg.get_deepseek_base_url()}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 3072,
                        "temperature": 0.3,
                        "response_format": {"type": "json_object"},
                    },
                )
                if resp.is_success:
                    raw = resp.json()["choices"][0]["message"]["content"].strip()
                    if raw.startswith("```"):
                        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                    proposal = json.loads(raw)
                    proposal["gap"] = gap["gap"]
                    proposal["references"] = gap["references"]
                    proposals.append(proposal)
                    logger.info(f"Proposal generated: {proposal.get('title', 'unknown')}")
        except Exception as e:
            logger.error(f"Proposal generation failed for {gap['dimension']}: {e}")

    return proposals


async def run_scan(deep: bool = False, since: int = None) -> dict:
    """执行竞品扫描，返回结果摘要。"""
    skills = await _fetch_clawhub_skills()

    if not skills:
        return {"total_scanned": 0, "results": []}

    results = []
    for skill in skills:
        desc = skill.get("description", "")
        dim_scores = _score_dimensions(desc)
        overlap = _similarity_to_xixing(dim_scores)
        diff = _classify_difference(overlap)

        # 差异化得分 = 1.0 - 重叠度（高=与吸星差异大，值得学习）
        differentiation_score = round(1.0 - overlap, 2)

        # 找最高维度
        top_dim = max(dim_scores, key=dim_scores.get) if dim_scores else "unknown"
        top_dim_name = CAPABILITY_DIMENSIONS.get(top_dim, {}).get("name", top_dim)

        results.append({
            "skill_name": skill["name"],
            "function_cluster": top_dim_name,
            "differentiation_score": differentiation_score,  # 高=与吸星差异大，值得学
            "overlap_score": overlap,                       # 高=与吸星重叠多，低优先级
            "difference": diff,
            "description": desc[:300],
            "url": skill.get("url", ""),
            "actionable": diff == "different" and differentiation_score > 0.6,
        })

    # 按差异化得分排序，取 Top N
    results.sort(key=lambda x: x["differentiation_score"], reverse=True)
    top_results = results[:TOP_N]

    # 写入 scan_results 表
    pool = await get_pool()
    async with pool.acquire() as conn:
        today = datetime.now(timezone.utc).date()
        for r in top_results:
            await conn.execute(
                f"""INSERT INTO {SCHEMA}.scan_results (scan_date, skill_name, function_cluster, score, difference, description, url, actionable)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                today, r["skill_name"], r["function_cluster"], r["differentiation_score"],
                r["difference"], r["description"], r["url"], r["actionable"],
            )

    return {"total_scanned": len(skills), "results": top_results}
