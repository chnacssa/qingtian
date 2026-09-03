"""
吸星 — 五道质量门

Gate ① 去重：SHA256 + 模糊去重（Jaccard 相似度）
Gate ② 语义相关性：embedding 余弦相似度（可选）或关键词匹配
Gate ③ 来源质量：sources.reputation 评分
Gate ④ 内容质量：正文率、段落结构、语言检测
Gate ⑤ 时效性：内容新鲜度评分
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone, timedelta

from common.db import get_pool
from . import config as cfg
from .crawler import _compute_hash

logger = logging.getLogger("xixing.quality_gate")

SCHEMA = cfg.get_schema_name()


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Jaccard 相似度（3-gram）。"""
    def ngrams(s, n=3):
        s = re.sub(r"\s+", " ", s).strip().lower()
        return {s[i:i+n] for i in range(max(0, len(s) - n + 1))}

    set_a = ngrams(text_a[:2000])
    set_b = ngrams(text_b[:2000])
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


# ── Gate ①: 去重 ─────────────────────────────────────

async def gate_dedup(conn, content: str) -> tuple[bool, str, float]:
    """SHA256 精确去重 + Jaccard 模糊去重。"""
    content_hash = _compute_hash(content)

    existing_hash = await conn.fetchval(
        f"SELECT id FROM {SCHEMA}.knowledge_items WHERE content_hash = $1", content_hash
    )
    if existing_hash:
        return False, f"精确重复: hash={content_hash[:16]}...", 0.0

    # 模糊去重：拉取同源最近的条目比对
    threshold = cfg.get_fuzzy_dedup_threshold()
    recent = await conn.fetch(
        f"SELECT id, content FROM {SCHEMA}.knowledge_items ORDER BY created_at DESC LIMIT 50"
    )
    for row in recent:
        sim = _jaccard_similarity(content, row["content"])
        if sim > threshold:
            return False, f"模糊重复: 与 #{row['id']} 相似度 {sim:.0%}", 1.0 - sim

    return True, "通过", 1.0


# ── Gate ②: 语义相关性 ────────────────────────────────

_RELEVANCE_KEYWORDS = [
    # AI / 智能体领域
    "agent", "智能体", "AI", "LLM", "大模型", "skill", "技能", "tool",
    "工具", "memory", "记忆", "embedding", "向量", "knowledge", "知识",
    "decision", "决策", "planning", "规划", "OpenClaw", "claude",
    "prompt", "RAG", "蒸馏", "distill", "进化", "evolution",
    # 工程 / 技术
    "工程", "技术", "施工", "建筑", "设计", "规范", "标准", "spec",
    "认证", "验收", "检测", "测试", "试验", "方法", "教程", "指南",
    "手册", "操作", "流程", "工艺", "方案", "参数", "配置", "部署",
    # 材料 / 建材
    "材料", "建材", "钢材", "水泥", "混凝土", "设备", "机械", "电气",
    "自动化", "制造", "生产", "加工", "装配", "安装", "调试",
    # 商业 / 采购
    "价格", "采购", "供应链", "报价", "成本", "预算", "合同", "招标",
    "投标", "商务", "供应", "需求", "市场", "行业", "分析", "报告",
    # 管理 / 运营
    "管理", "运营", "安全", "质量", "环保", "节能", "数据", "研究",
    # 通用知识
    "定义", "原理", "分类", "特点", "优势", "应用", "案例", "实践",
    "经验", "总结", "优化", "改进", "创新", "趋势", "发展",
]


async def gate_relevance(content: str) -> tuple[bool, str, float]:
    """关键词相关性评分。包含内容长度奖励：>2000 字符且结构良好 +0.1。"""
    content_lower = content.lower()
    hits = sum(1 for kw in _RELEVANCE_KEYWORDS if kw.lower() in content_lower)
    # 使用 0.1 因子（而非 0.15），使 2-3 次命中即可通过
    base_score = min(hits / max(len(_RELEVANCE_KEYWORDS) * 0.1, 1), 1.0)

    # 内容长度奖励：>2000 字符且包含换行分段 → 结构化长文，+0.1
    length_bonus = 0.0
    if len(content) > 2000 and content.count("\n") >= 3:
        length_bonus = 0.1

    score = round(min(base_score + length_bonus, 1.0), 2)

    threshold = cfg.get_min_relevance_score()
    if score >= threshold:
        return True, f"相关性 {score:.0%} (hits={hits})", score
    return False, f"相关性不足: {score:.0%} < {threshold:.0%} (hits={hits})", score


# ── Gate ③: 来源质量 ──────────────────────────────────

async def gate_source_quality(conn, source_id: str) -> tuple[bool, str, float]:
    """基于 sources.reputation 评估来源质量。"""
    row = await conn.fetchrow(
        f"SELECT reputation, consecutive_errors FROM {SCHEMA}.sources WHERE id = $1", source_id
    )
    if row is None:
        return True, "未知来源，默认通过", 0.5

    rep = row["reputation"]
    errors = row["consecutive_errors"]

    if rep < 0.2:
        return False, f"来源信誉过低: {rep:.0%}", rep
    if rep < 0.4:
        return True, f"来源信誉偏低: {rep:.0%}（降级标记）", rep
    return True, f"信誉 {rep:.0%}", rep


# ── Gate ④: 内容质量 ──────────────────────────────────

def gate_content_quality(content: str) -> tuple[bool, str, float]:
    """正文率、段落结构、语言检测。"""
    stripped = content.strip()

    if len(stripped) < cfg.get_min_content_length():
        return False, f"内容过短: {len(stripped)}B", 0.0

    # 正文率：非 HTML 标签的文本占比
    html_tag_chars = len(re.findall(r"<[^>]+>", stripped))
    total_chars = max(len(stripped), 1)
    text_ratio = 1.0 - (html_tag_chars / total_chars)
    if text_ratio < 0.4:
        return False, f"正文率过低: {text_ratio:.0%}", text_ratio

    # 段落结构：至少 2 个有效段落（>50 chars each）
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", stripped) if len(p.strip()) > 50]
    para_score = min(len(paragraphs) / 3, 1.0)

    # 综合
    quality_score = round(text_ratio * 0.6 + para_score * 0.4, 2)
    threshold = cfg.get_min_quality_score()
    if quality_score >= threshold:
        return True, f"质量 {quality_score:.0%}", quality_score
    return False, f"质量不足: {quality_score:.0%} < {threshold:.0%}", quality_score


# ── Gate ⑤: 时效性 ────────────────────────────────────

def gate_freshness(source_date: str | None = None, category: str = "general") -> tuple[bool, str, float]:
    """内容新鲜度评分。不同分类对时效的容忍度不同。

    - standard / knowledge：长期有效，无日期默认 0.6
    - price / plugin：时效敏感，无日期默认 0.2
    - general / experience：中等，无日期默认 0.4
    """
    no_date_scores = {
        "standard": 0.6,
        "knowledge": 0.6,
        "general": 0.4,
        "experience": 0.4,
        "price": 0.2,
        "plugin": 0.2,
    }
    default_no_date = no_date_scores.get(category, 0.4)

    if source_date is None:
        return True, f"无日期信息（{category} 类默认 {default_no_date:.0%}）", default_no_date

    try:
        # 尝试多种日期格式
        for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"]:
            try:
                dt = datetime.strptime(source_date, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return True, "日期格式无法解析", 0.5

        now = datetime.now(timezone.utc)
        days = (now - dt).days
        max_days = cfg.get_max_freshness_days()

        if days <= 1:
            return True, "24h 内", 1.0
        elif days <= 7:
            return True, f"{days} 天前", 0.8
        elif days <= 30:
            return True, f"{days} 天前", 0.4
        elif days > max_days:
            return False, f"已过期: {days} 天", 0.1
        return True, f"{days} 天前", 0.3
    except Exception:
        return True, "时效检查异常，默认通过", 0.5


# ── 阻塞质量门（①-④）────────────────────────────────

async def run_blocking_gates(
    conn,
    content: str,
    source_id: str,
) -> dict:
    """运行前四道阻塞质量门。任一失败则短路拒绝。"""
    gate_results = {}

    # Gate ① 去重
    passed, detail, score = await gate_dedup(conn, content)
    gate_results["dedup"] = {"passed": passed, "score": score, "detail": detail}
    if not passed:
        return {"passed": False, "gate_results": gate_results, "quality_score": 0, "reject_reason": f"去重: {detail}"}

    # Gate ② 语义相关性
    passed, detail, score = await gate_relevance(content)
    gate_results["relevance"] = {"passed": passed, "score": score, "detail": detail}
    if not passed:
        return {"passed": False, "gate_results": gate_results, "quality_score": score, "reject_reason": f"相关性: {detail}"}

    # Gate ③ 来源质量
    passed, detail, score = await gate_source_quality(conn, source_id)
    gate_results["source_quality"] = {"passed": passed, "score": score, "detail": detail}
    if not passed:
        return {"passed": False, "gate_results": gate_results, "quality_score": score, "reject_reason": f"来源: {detail}"}

    # Gate ④ 内容质量
    passed, detail, score = gate_content_quality(content)
    gate_results["content_quality"] = {"passed": passed, "score": score, "detail": detail}
    if not passed:
        return {"passed": False, "gate_results": gate_results, "quality_score": score, "reject_reason": f"内容: {detail}"}

    return {
        "passed": True,
        "gate_results": gate_results,
        "quality_score": 0,
        "reject_reason": None,
    }


# ── 综合质量门（含 Gate ⑤）───────────────────────────

async def run_quality_gates(
    conn,
    content: str,
    source_id: str,
    source_date: str | None = None,
    category: str = "general",
) -> dict:
    """运行全部五道质量门，返回综合结果。

    Gate ⑤（时效性）在分类后调用，可使用 category 做差异化评分。
    对 price/plugin 等时效敏感分类，Gate ⑤ 失败会直接阻塞（reject）。
    调用方应先通过 run_blocking_gates，分类后再调用此函数。
    """
    # 先跑阻塞门
    result = await run_blocking_gates(conn, content, source_id)
    if not result["passed"]:
        return result

    gate_results = result["gate_results"]

    # Gate ⑤ 时效性（分类感知）
    passed, detail, score = gate_freshness(source_date, category)
    gate_results["freshness"] = {"passed": passed, "score": score, "detail": detail}

    # 时效敏感分类（price/plugin）：Gate ⑤ 失败直接阻塞
    FRESHNESS_BLOCKING_CATEGORIES = {"price", "plugin"}
    if not passed and category in FRESHNESS_BLOCKING_CATEGORIES:
        return {
            "passed": False,
            "gate_results": gate_results,
            "quality_score": score,
            "reject_reason": f"时效性: {detail}（{category} 类内容要求近期数据）",
        }

    # 综合质量分（各门加权平均）
    weights = {"dedup": 0.3, "relevance": 0.25, "source_quality": 0.15, "content_quality": 0.2, "freshness": 0.1}
    overall = round(sum(
        weights.get(gate, 0.2) * gate_results[gate]["score"]
        for gate in gate_results
    ), 2)

    return {
        "passed": True,
        "gate_results": gate_results,
        "quality_score": overall,
        "reject_reason": None,
    }


async def evaluate_quality(text: str) -> dict:
    """供 api.py _process_sync 调用的内容质量评估包装函数"""
    passed, detail, score = gate_content_quality(text)
    return {"score": score, "issues": [] if passed else [detail], "passed": passed}
