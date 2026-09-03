"""
吸星配置 — 读取 config.yaml 下的 xixing 段
"""

import os

from common.config import get, default_llm_model, default_llm_base_url, default_llm_key_var


def get_schema_name() -> str:
    return get("xixing.schema_name", "xixing")


def get_global_namespace() -> str:
    return get("xixing.global_namespace", "global")


def get_base_dir() -> str:
    return get("xixing.base_dir", "/root/.openclaw/workspace")


def get_sources_path() -> str:
    return get("xixing.sources", "/root/.openclaw/workspace/xixing-sources.json")


def get_timezone() -> str:
    return get("xixing.timezone", "Asia/Shanghai")


# ── 采集 ──────────────────────────────────────────────

def get_collect_mode() -> str:
    return get("xixing.collect.mode", "daily-all")


def get_collect_timeout() -> int:
    return get("xixing.collect.fetch_timeout", 60)


def get_collect_max_size() -> int:
    return get("xixing.collect.max_content_size", 524288)


def get_collect_user_agent() -> str:
    return get("xixing.collect.user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")


def get_collect_proxy() -> str:
    """单代理 URL（http/https/socks5）。为空时不使用代理。"""
    return get("xixing.collect.proxy", "")


def get_collect_proxies() -> list[str]:
    """代理池（列表），随机选取。若只配了 proxy 则返回单元素列表。"""
    proxies = get("xixing.collect.proxies", [])
    if isinstance(proxies, list) and len(proxies) > 0:
        return [p for p in proxies if isinstance(p, str) and p.strip()]
    proxy = get_collect_proxy()
    return [proxy] if proxy else []


def get_collect_request_delay_seconds() -> float:
    """源间请求延迟（秒），防速率限制。"""
    return get("xixing.collect.request_delay_seconds", 2.0)


def get_collect_request_delay_jitter() -> float:
    """源间请求延迟随机抖动（秒），均匀分布。"""
    return get("xixing.collect.request_delay_jitter", 0.5)


def get_collect_tls_impersonate() -> str:
    """curl_cffi TLS 指纹模拟目标。空字符串表示禁用。"""
    return get("xixing.collect.tls_impersonate", "chrome124")


def get_collect_playwright_timeout() -> int:
    """Playwright headless 浏览器页面加载超时（秒）。"""
    return get("xixing.collect.playwright_timeout", 15)


# ── 质量门 ────────────────────────────────────────────

def get_min_content_length() -> int:
    return get("xixing.quality_gate.min_content_length", 200)


def get_min_relevance_score() -> float:
    return get("xixing.quality_gate.min_relevance_score", 0.5)


def get_min_quality_score() -> float:
    return get("xixing.quality_gate.min_quality_score", 0.4)


def get_fuzzy_dedup_threshold() -> float:
    return get("xixing.quality_gate.fuzzy_dedup_threshold", 0.8)


def get_max_freshness_days() -> int:
    return get("xixing.quality_gate.max_freshness_days", 30)


def get_dedup_ttl_days() -> int:
    """采集阶段去重窗口（天）：同源同哈希在此窗口内视为重复，超过后允许重新采集。"""
    return get("xixing.quality_gate.dedup_ttl_days", 7)


# ── 分类 ──────────────────────────────────────────────

def get_classifier_llm_threshold() -> float:
    return get("xixing.classifier.llm_fallback_threshold", 0.6)


def get_classifier_model() -> str:
    # 2026-08-27：默认随 FIRST_LLM/SECOND_LLM 全局厂商顺序（智谱优先）
    return get("xixing.classifier.model", default_llm_model())


# ── 蒸馏 ──────────────────────────────────────────────

def get_distiller_enabled() -> bool:
    return get("xixing.distiller.enabled", True)


def get_distiller_model() -> str:
    return get("xixing.distiller.model", default_llm_model())


def get_distiller_max_source() -> int:
    return get("xixing.distiller.max_source_memories", 500)


def get_distiller_min_cluster() -> int:
    return get("xixing.distiller.min_cluster_size", 3)


# ── 竞品扫描 ──────────────────────────────────────────

def get_scanner_enabled() -> bool:
    return get("xixing.scanner.enabled", True)


def get_scanner_top_n() -> int:
    return get("xixing.scanner.top_n", 10)


def get_proposal_model() -> str:
    """LLM model used for proposal generation."""
    return get("xixing.scanner.proposal_model", default_llm_model())


# ── 踩坑 ──────────────────────────────────────────────

def get_xizhenji_auto_capture() -> bool:
    return get("xixing.xizhenji.auto_capture", True)


def get_xizhenji_llm_severity() -> str:
    return get("xixing.xizhenji.min_severity_for_llm", "high")


# ── 调度 ──────────────────────────────────────────────

def get_scheduler_enabled() -> bool:
    return get("xixing.scheduler.enabled", True)


# ── 从属服务器知识同步 ─────────────────────────────

def get_sync_enabled() -> bool:
    """从属服务器是否启用知识同步（从管理服务器定时拉取）。"""
    return get("xixing.sync.enabled", False)


def get_sync_management_url() -> str:
    """管理服务器地址，从属服务器从此拉取知识。"""
    return get("xixing.sync.management_url", "http://localhost:1996")


def get_sync_interval_minutes() -> int:
    """知识同步间隔（分钟），默认 360（6 小时）。"""
    return get("xixing.sync.interval_minutes", 360)


def get_capability_sync_enabled() -> bool:
    """是否启用能力差距检测同步（从属服务器从管理端拉取自评分数对比）。"""
    return get("xixing.sync.capability_sync", True)


def get_capability_gap_threshold() -> float:
    """能力差距阈值（管理端分数 - 本地分数 > 此值才告警），默认 0.05。"""
    return get("xixing.sync.capability_gap_threshold", 0.05)


def get_capability_dedup_days() -> int:
    """同维度能力差距去重天数，默认 7 天。"""
    return get("xixing.sync.capability_dedup_days", 7)


def get_capability_shared_dimensions() -> list[str]:
    """从属服务器关心的通用能力维度（管理专属维度不触发差距告警）。

    默认从 CAPABILITY_DIMENSIONS 读取 scope="shared" 的维度，
    可通过 config 覆盖为自定义列表。
    """
    from .scanner import CAPABILITY_DIMENSIONS
    defaults = [dim_id for dim_id, dim_info in CAPABILITY_DIMENSIONS.items()
                if dim_info.get("scope") == "shared"]
    return get("xixing.sync.capability_shared_dimensions", defaults)


# ── 自动部署 ─────────────────────────────────────────────

def get_auto_deploy_enabled() -> bool:
    """从属服务器是否启用自动部署（检测到能力差距后自动 git pull + 重启）。"""
    return get("xixing.sync.auto_deploy", False)


def get_deploy_cooldown_minutes() -> int:
    """两次自动部署之间的最小冷却时间（分钟），默认 30。"""
    return get("xixing.sync.deploy_cooldown_minutes", 30)


def get_deploy_max_failures() -> int:
    """连续自动部署失败上限，超过后暂停自动部署，默认 3。"""
    return get("xixing.sync.deploy_max_failures", 3)


def get_deploy_health_timeout_seconds() -> int:
    """部署后健康检查轮询超时（秒），默认 30。"""
    return get("xixing.sync.deploy_health_timeout_seconds", 30)


def get_deploy_restart_command() -> str:
    """部署后重启服务的 shell 命令，默认 systemctl restart qingtian。"""
    return get("xixing.sync.deploy_restart_command", "systemctl restart qingtian")


def get_sync_time_window_start_hour() -> int:
    """从属同步时间窗口起始小时（含），默认 0 点（午夜）。"""
    return get("xixing.sync.time_window_start_hour", 0)


def get_sync_time_window_end_hour() -> int:
    """从属同步时间窗口结束小时（不含），默认 6 点（凌晨 6 点前）。"""
    return get("xixing.sync.time_window_end_hour", 6)


# ── LLM ──────────────────────────────────────────────────
# 2026-08-27 波哥定调：厂商顺序统一读全局 FIRST_LLM/SECOND_LLM（智谱优先），
# 不硬编码绑死一家。旧函数名 get_deepseek_key/get_deepseek_base_url 保留
# （classifier/crawler/distiller/scanner 在用），语义 = 当前生效的主 LLM 配置。

def get_deepseek_key() -> str:
    return os.getenv(default_llm_key_var(),
                     os.getenv("ZHIPU_API_KEY", os.getenv("DEEPSEEK_API_KEY", "")))


def get_deepseek_base_url() -> str:
    return get("xixing.llm.base_url", default_llm_base_url())


def get_quality_judge_model() -> str:
    return get("xixing.quality_judge.model", default_llm_model())


# ── Skill 提案 ───────────────────────────────────────────


def get_skill_proposals_enabled() -> bool:
    """Skill 提案生成总开关"""
    return get("skill_proposals.enabled", True)


def get_skill_proposals_min_frequency() -> int:
    """频次阈值（需 >= 1，配置错误时返回默认值兜底）"""
    val = get("skill_proposals.min_frequency", 10)
    return val if isinstance(val, int) and val >= 1 else 10


def get_skill_proposals_max_per_round() -> int:
    """每轮最多提案数（需 >= 1，配置错误时返回默认值兜底）"""
    val = get("skill_proposals.max_proposals_per_round", 5)
    return val if isinstance(val, int) and val >= 1 else 5
