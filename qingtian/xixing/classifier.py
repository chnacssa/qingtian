"""
吸星 — 语义分类器

两阶段分类：
  阶段 1（快速）：URL/标题关键词 + 内容结构检测 → 初步分类 + 置信度
  阶段 2（LLM，仅模糊项）：置信度 < 阈值时调用 LLM 分类

输出分类：knowledge | experience | plugin | standard | price | general
"""

import logging
import re

from . import config as cfg

logger = logging.getLogger("xixing.classifier")

# ── 阶段 1：关键词 + 结构规则 ──────────────────────────

# 每个分类的关键词特征
_CATEGORY_KEYWORDS = {
    "plugin": [
        "plugin", "skill", "mcp", "tool", "manifest", "openclaw",
        "prompt", "distiller", "summarizer", "analyzer", "search",
    ],
    "price": [
        "价格", "price", "报价", "元/", "万元", "单价", "市场价",
        "材料价格", "工程材料", "混凝土", "钢材", "水泥", "骨料",
        "信息价", "定额", "cost", "费用",
    ],
    "standard": [
        "标准", "standard", "spec", "规范", "GB/T", "GB ", "ISO",
        "行业标准", "技术规范", "要求", "验收", "检测", "检定",
        "新能源", "变压器", "逆变器", "电力", "设备",
    ],
    "experience": [
        "经验", "教训", "踩坑", "失败", "教训", "经验教训",
        "lesson", "experience", "troubleshoot", "修复", "fix",
        "报错", "错误", "error", "debug", "解决", "方案",
    ],
    "knowledge": [
        "综述", "survey", "总结", "总结", "架构", "设计",
        "论文", "paper", "research", "方法", "method",
        "AI", "LLM", "agent", "智能体", "模型",
    ],
}

# HTML 结构特征检测
_PLUGIN_STRUCTURE_PATTERNS = [
    r"```(?:json|yaml|python|prompt)",
    r'"manifest"\s*:',
    r'"plugin"\s*:',
    r'"skill"\s*:',
    r'"prompt"\s*:',
]

_PRICE_STRUCTURE_PATTERNS = [
    r"\d{2,}\s*元\s*/",
    r"[¥￥]\s*\d[\d,.]*",
    r"\|\s*材料名称\s*\|",
    r"\|\s*规格\s*\|.*\|\s*单价\s*\|",
    r"\|\s*价格\s*\|",
]

_STANDARD_STRUCTURE_PATTERNS = [
    r"GB/T\s*\d",
    r"标准号",
    r"执行标准",
    r"技术要求",
    r"技术参数",
]


def _score_category(content: str, title: str, url: str) -> dict:
    """计算各分类的匹配分数。"""
    text = f"{title} {url} {content[:3000]}".lower()
    scores = {}

    for category, keywords in _CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in text)
        scores[category] = min(hits / max(len(keywords) * 0.2, 1), 1.0)

    return scores


def _check_structure_patterns(content: str) -> dict:
    """结构模式检测。"""
    content_sample = content[:5000]
    results = {}

    results["plugin"] = any(re.search(p, content_sample, re.IGNORECASE) for p in _PLUGIN_STRUCTURE_PATTERNS)
    results["price"] = any(re.search(p, content_sample) for p in _PRICE_STRUCTURE_PATTERNS)
    results["standard"] = any(re.search(p, content_sample) for p in _STANDARD_STRUCTURE_PATTERNS)

    return results


def classify_fast(content: str, title: str = "", url: str = "") -> tuple[str, float]:
    """阶段 1：快速关键词+结构分类。返回 (分类, 置信度)。"""
    kw_scores = _score_category(content, title, url)
    struct_hits = _check_structure_patterns(content)

    # 融合关键词得分和结构得分
    final_scores = {}
    for cat in kw_scores:
        final_scores[cat] = kw_scores[cat] * 0.7
        if struct_hits.get(cat):
            final_scores[cat] += 0.3

    # 找出最高分
    best_cat = max(final_scores, key=final_scores.get)
    best_score = min(final_scores[best_cat], 1.0)

    # 如果最高分很低，归为 general
    if best_score < 0.15:
        return "general", 0.3

    # 计算置信度：最高分与第二高分的差距
    sorted_scores = sorted(final_scores.values(), reverse=True)
    margin = sorted_scores[0] - (sorted_scores[1] if len(sorted_scores) > 1 else 0)
    confidence = round(min(best_score + margin, 1.0), 2)

    return best_cat, confidence


# ── 阶段 2：LLM 分类 ──────────────────────────────────

async def classify_llm(content: str, title: str = "") -> str:
    """阶段 2：调用 LLM 进行语义分类。"""
    import httpx

    model = cfg.get_classifier_model()
    api_key = cfg.get_deepseek_key()

    if not api_key:
        logger.warning("LLM classifier: no API key, falling back to general")
        return "general"

    prompt = (
        "分类以下内容。仅回复一个词：\n"
        "- knowledge: 学术/技术知识、架构设计、综述\n"
        "- experience: 实践经验、教训、故障修复、踩坑\n"
        "- plugin: 工具/技能/插件/MCP 的配置或 manifest\n"
        "- standard: 行业标准、技术规范、检测要求\n"
        "- price: 价格数据、材料报价、市场行情\n"
        "- general: 其他\n\n"
        f"标题: {title}\n"
        f"内容: {content[:2000]}\n"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{cfg.get_deepseek_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,  # glm 思考token计入额度，留思考空间
                    "temperature": 0,
                },
            )
            if resp.is_success:
                result = resp.json()["choices"][0]["message"]["content"].strip().lower()
                valid_categories = {"knowledge", "experience", "plugin", "standard", "price", "general"}
                for cat in valid_categories:
                    if cat in result:
                        return cat
                return "general"
    except Exception as e:
        logger.warning("LLM classifier failed: %s", e)

    return "general"


# ── 统一入口 ──────────────────────────────────────────

async def classify(content: str, title: str = "", url: str = "") -> str:
    """两阶段分类入口。"""
    fast_cat, confidence = classify_fast(content, title, url)

    threshold = cfg.get_classifier_llm_threshold()
    if confidence >= threshold:
        return fast_cat

    logger.info("Fast classification confidence %s < %s, falling back to LLM", confidence, threshold)
    return await classify_llm(content, title)


async def classify_text(text: str) -> dict:
    """供 api.py _process_sync 调用的包装函数

    Returns {"category": str, "confidence": float}
    """
    cat = await classify(text)
    return {"category": cat, "confidence": 1.0}
