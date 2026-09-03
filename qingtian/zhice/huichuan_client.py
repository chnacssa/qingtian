"""
执策 → 汇川知识搜索客户端
在任务创建时自动搜索企业知识库和同岗经验。
"""

import logging
from common.config import get
from common.db import get_pool
from huanyu.config import get_schema_name as _huanyu_schema

logger = logging.getLogger("zhice.huichuan_client")

SEARCH_TIMEOUT = get("zhice.knowledge_search.search_timeout_seconds", 5)
MAX_RESULTS = get("zhice.knowledge_search.max_results", 3)
MIN_SCORE = get("zhice.knowledge_search.min_score", 0.5)
MODE = get("zhice.knowledge_search.mode", "hybrid")


async def search_knowledge(
    query: str,
    agent_category: str = "",
    max_results: int = MAX_RESULTS,
    min_score: float = MIN_SCORE,
) -> list[dict]:
    """
    搜索汇川知识库。
    agent_category 为 "" 时只搜企业知识库（全局）；
    非空时同时搜企业知识库 + 同 category Agent 经验。
    """
    if not query.strip():
        return []

    try:
        from huichuan.service import search as huichuan_search

        results = await huichuan_search(
            query=query,
            top_k=max_results * 2,
            mode=MODE,
            agent_category=agent_category,
        )
        filtered = [r for r in results if r.get("score", 0) >= min_score]
        return filtered[:max_results]

    except ImportError:
        logger.warning("huichuan.service 不可用，知识搜索跳过")
        return []
    except Exception as e:
        logger.error(f"知识搜索失败: {e}")
        return []


async def search_same_category_experience(agent_id: str, query: str) -> list[dict]:
    """搜索同类 Agent 积累的操作经验（同 category 传帮带）"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT category FROM {_huanyu_schema()}.agents WHERE agent_id = $1",
                agent_id,
            )
            if not row or not row["category"]:
                return []

            category = row["category"]
            return await search_knowledge(
                query=query,
                agent_category=category,
                max_results=MAX_RESULTS,
                min_score=MIN_SCORE,
            )
    except Exception as e:
        logger.error(f"同岗经验搜索失败: {e}")
        return []
