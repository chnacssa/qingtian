"""汇川 — 配置获取器"""

import os

from common.config import get, default_llm_model


def get_schema_name() -> str:
    return get("huichuan.schema_name", "huichuan")


def get_storage_base() -> str:
    """汇川 Layer 1 文件存储根目录。

    MCP ingest_file 等工具只允许摄入此目录内的文件（防任意文件读），
    与 huichuan/api.py 的 _FILE_STORAGE_BASE 保持一致的环境变量。
    """
    return os.environ.get("QINGTIAN_HUICHUAN_STORAGE", "/opt/qingtian/huichuan/storage")


def get_deploy_env() -> str:
    return get("huichuan.deploy_env", "prod")


def get_refine_cron() -> str:
    return get("huichuan.refine_cron", "0 2 * * *")


def get_refine_llm_model() -> str:
    # 2026-08-27：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序（智谱优先）
    return get("huichuan.refine_llm_model", default_llm_model())


def get_refine_batch_size() -> int:
    return get("huichuan.refine_batch_size", 20)


def get_refine_max_failures() -> int:
    """精炼 LLM 失败上限（超过转 failed，不再自动重试）。P2 (R11)"""
    return max(1, get("huichuan.refine.max_failures", 5))


def get_refine_backoff_hours() -> list[int]:
    """精炼 LLM 失败指数退避（小时）。P2 (R11)"""
    return list(get("huichuan.refine.backoff_hours", [1, 2, 4, 8]))


def get_dedup_threshold() -> float:
    return get("huichuan.dedup_threshold", 0.92)


def get_max_knowledge_size() -> int:
    return get("huichuan.max_knowledge_size", 50000)


def get_abstract_max_tokens() -> int:
    return get("huichuan.abstract_max_tokens", 3000)


def get_redis_url() -> str:
    # QINGTIAN_REDIS_URL 环境变量优先（与 huanyu/config.py 同源；Docker compose 场景注入）
    return os.getenv("QINGTIAN_REDIS_URL", get("huanyu.redis_url", "redis://localhost:6379"))


def get_deepseek_api_key() -> str:
    # 2026-08-26 起 LLM 主模型为 zhipu：主 ZHIPU_API_KEY，备 DEEPSEEK_API_KEY 兜底。
    # 旧名保留（refine 链路及测试在用），语义 = 当前生效的主 LLM key。
    return os.environ.get("ZHIPU_API_KEY", os.environ.get("DEEPSEEK_API_KEY", ""))


def get_deepseek_base_url() -> str:
    # 默认智谱端点（2026-08-26 全量切）；旧名保留（refine 链路及测试在用）
    return get("yongheng.llm.base_url", "https://open.bigmodel.cn/api/paas/v4")


# ── Phase 1+ 文件管道配置 ────────────────────────────


def get_image_extraction_enabled() -> bool:
    return get("huichuan.image_extraction.enabled", True)


def get_max_images_per_file() -> int:
    return max(1, min(get("huichuan.image_extraction.max_images_per_file", 50), 500))


def get_max_image_size_mb() -> int:
    return get("huichuan.image_extraction.max_image_size_mb", 10)


def get_image_storage_subdir() -> str:
    return get("huichuan.image_extraction.storage_subdir", "images")


def get_excel_sheet_independent() -> bool:
    return get("huichuan.excel_processor.sheet_independent", True)


def get_excel_max_sheets() -> int:
    return get("huichuan.excel_processor.max_sheets", 20)


def get_excel_chart_as_image() -> bool:
    return get("huichuan.excel_processor.chart_as_image", True)


def get_mime_detection_mode() -> str:
    return get("huichuan.file_classifier.mime_detection", "magic")
