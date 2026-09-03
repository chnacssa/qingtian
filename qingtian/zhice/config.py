"""执策配置 — 从 qingtian/config.yaml 读取 zhice 相关配置"""
import os
from common.config import get, default_llm_model, default_llm_base_url, default_llm_key_var


def get_schema_name() -> str:
    return get("zhice.schema_name", "zhice")


def get_timeout_check_interval() -> int:
    """看门狗扫描间隔（秒）"""
    return get("zhice.timeout_check_interval_seconds", 30)


def get_default_step_timeout() -> int:
    """Step 默认超时（分钟）"""
    return get("zhice.default_step_timeout_minutes", 30)


def get_default_task_timeout() -> int:
    """Task 默认超时（分钟）"""
    return get("zhice.default_task_timeout_minutes", 120)


def get_heartbeat_loss_minutes() -> int:
    """心跳丢失判定阈值（分钟）"""
    return get("zhice.heartbeat_loss_minutes", 5)


def get_assignment_timeout_minutes() -> int:
    """分配未响应回收阈值（分钟）— 超时后回收为 pending 供其他 Agent 认领"""
    return get("zhice.assignment_timeout_minutes", 3)


# ── LLM 自动分解 ───────────────────────────────────────────

def get_llm_base_url() -> str:
    """LLM API 地址（2026-08-27：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序，智谱优先）"""
    return get("zhice.llm.base_url",
               get("yongheng.llm.base_url", default_llm_base_url()))


def get_llm_api_key() -> str:
    """LLM API Key（主 key 随 FIRST_LLM/SECOND_LLM 档案，另一家兜底）"""
    return os.getenv(default_llm_key_var(),
                     os.getenv("ZHIPU_API_KEY",
                               os.getenv("DEEPSEEK_API_KEY", get("zhice.llm.api_key", ""))))


def get_llm_decompose_model() -> str:
    """自动分解用的模型（2026-08-27：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序）"""
    return get("zhice.llm.decompose_model", default_llm_model())


def get_llm_decompose_timeout() -> int:
    """LLM 调用超时（秒）"""
    return get("zhice.llm.decompose_timeout", 120)


def get_llm_decompose_max_tokens() -> int:
    return get("zhice.llm.decompose_max_tokens", 4000)


def get_llm_decompose_temperature() -> float:
    return get("zhice.llm.decompose_temperature", 0.1)


def get_llm_max_tokens() -> int:
    """LLM 调用 max_tokens 默认值（候选列表长时需要更多 tokens）"""
    return get("zhice.llm.max_tokens", 600)


def get_xixing_base_url() -> str:
    """吸星 API 地址（同机部署，默认 127.0.0.1:1996）"""
    return get("zhice.xixing_base_url", "http://127.0.0.1:1996")


def get_zhenyue_base_url() -> str:
    """镇岳 API 地址（同机部署，默认 127.0.0.1:1996）"""
    return get("zhice.zhenyue_base_url", "http://127.0.0.1:1996")


# ── 知识搜索配置 ──

def get_knowledge_search_enabled() -> bool:
    return get("zhice.knowledge_search.enabled", True)

def get_knowledge_search_max_results() -> int:
    return get("zhice.knowledge_search.max_results", 3)

def get_knowledge_search_min_score() -> float:
    return get("zhice.knowledge_search.min_score", 0.5)

def get_knowledge_search_timeout() -> int:
    return get("zhice.knowledge_search.search_timeout_seconds", 5)

def get_knowledge_search_mode() -> str:
    return get("zhice.knowledge_search.mode", "hybrid")
