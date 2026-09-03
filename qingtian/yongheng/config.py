"""
永恒 — 配置适配层
从 common.config 读取 yongheng 段
"""

import os
from common.config import get, default_llm_model, default_llm_base_url, default_llm_key_var


def get_schema_name() -> str:
    return get("yongheng.schema_name", "yongheng")


def get_embedding_provider() -> str:
    return get("yongheng.embedding.provider", "fastembed")


def get_embedding_model() -> str:
    return get("yongheng.embedding.model_name", "BAAI/bge-small-zh-v1.5")


def get_embedding_cache_path() -> str:
    return get("yongheng.embedding.cache_path", "/opt/qingtian/models/")


def get_embedding_threads() -> int:
    return get("yongheng.embedding.threads", 2)


def get_embedding_dimension() -> int:
    return get("yongheng.embedding.dimension", 512)


def get_llm_base_url() -> str:
    # 2026-08-27：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序（智谱优先）
    return get("yongheng.llm.base_url", default_llm_base_url())


def get_llm_api_key() -> str:
    # 主 key 随 FIRST_LLM/SECOND_LLM 档案（2026-08-27），另一家兜底
    return os.getenv(default_llm_key_var(), os.getenv(
        "ZHIPU_API_KEY", os.getenv(
            "DEEPSEEK_API_KEY", get("yongheng.llm.api_key", ""))))


def get_llm_high_value_model() -> str:
    return get("yongheng.llm.high_value_model", default_llm_model())


def get_llm_digest_model() -> str:
    return get("yongheng.llm.digest_model", default_llm_model())


def get_llm_agentic_model() -> str:
    return get("yongheng.llm.agentic_model", default_llm_model())


def get_consolidate_token_budget() -> int:
    return get("yongheng.consolidate.token_budget", 20000)


def get_consolidate_max_records() -> int:
    return get("yongheng.consolidate.max_records_per_run", 2000)


def get_consolidate_min_days() -> int:
    return get("yongheng.consolidate.min_days_between", 7)


def get_rate_limit_write() -> int:
    return get("yongheng.rate_limit.write", 60)


def get_rate_limit_search() -> int:
    return get("yongheng.rate_limit.search", 120)


def get_rate_limit_context() -> int:
    return get("yongheng.rate_limit.context", 60)


def get_rate_limit_session_start() -> int:
    return get("yongheng.rate_limit.session_start", 60)


def get_rate_limit_session_end() -> int:
    return get("yongheng.rate_limit.session_end", 30)


def get_batch_max_size() -> int:
    return get("yongheng.batch.max_size", 20)


def get_search_rrf_k() -> int:
    return get("yongheng.search.rrf_k", 60)


def get_search_default_top_k() -> int:
    return get("yongheng.search.default_top_k", 5)


def get_search_context_top_k() -> int:
    return get("yongheng.search.context_default_top_k", 10)


def get_time_decay_recent_days() -> int:
    return get("yongheng.search.time_decay.recent_days", 30)


def get_time_decay_medium_days() -> int:
    return get("yongheng.search.time_decay.medium_days", 90)


def get_time_decay_recent_weight() -> float:
    return get("yongheng.search.time_decay.recent_weight", 1.0)


def get_time_decay_medium_weight() -> float:
    return get("yongheng.search.time_decay.medium_weight", 0.5)


def get_hit_exemption_min_hits() -> int:
    return get("yongheng.search.hit_exemption.min_hits", 5)


def get_hit_exemption_max_bonus() -> float:
    return get("yongheng.search.hit_exemption.max_bonus", 0.1)


def get_hit_exemption_reset_days() -> int:
    return get("yongheng.search.hit_exemption.reset_after_days", 180)


def get_learned_max_items() -> int:
    return get("yongheng.learned.max_items_soft", 50)


def get_learned_min_confidence() -> float:
    return get("yongheng.learned.min_confidence", 0.5)


def get_learned_duplicate_threshold() -> int:
    return get("yongheng.learned.duplicate_threshold", 5)


# ── DashScope API embedding ──────────────────────────────────────────

def get_dashscope_api_key() -> str:
    return os.getenv("DASHSCOPE_API_KEY", get("yongheng.dashscope.api_key", ""))


def get_dashscope_embedding_url() -> str:
    return get(
        "yongheng.dashscope.embedding_url",
        "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
    )


def get_dashscope_timeout() -> int:
    return get("yongheng.dashscope.timeout", 30)


def get_dashscope_max_batch() -> int:
    return get("yongheng.dashscope.max_batch", 10)
