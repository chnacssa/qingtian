"""
镇岳 — 配置适配层
从 common.config 读取 zhenyue 段
"""

import os
from common.config import get


def get_schema_name() -> str:
    return get("zhenyue.schema_name", "zhenyue")


def get_encryption_key_dir() -> str:
    return get("zhenyue.encryption.key_dir", "/opt/qingtian/keys")


def get_encryption_master_key_file() -> str:
    return get("zhenyue.encryption.master_key_file", "master.key")


def get_bootstrap_admin_token() -> str:
    return os.getenv("ZHENYUE_ADMIN_TOKEN", get("zhenyue.auth.bootstrap_admin_token", ""))


def get_audit_prev_hash_genesis() -> str:
    return get("zhenyue.audit.prev_hash_genesis", "0" * 64)


def get_audit_auto_verify_schedule() -> str:
    return get("zhenyue.audit.auto_verify_schedule", "0 3 * * *")


def get_approval_timeout_high() -> int:
    return get("zhenyue.approval.default_timeout_high", 3600)


def get_approval_timeout_critical() -> int:
    return get("zhenyue.approval.default_timeout_critical", 1800)


def get_approval_escalation_high() -> int:
    return get("zhenyue.approval.escalation_after_high", 600)


def get_approval_escalation_critical() -> int:
    return get("zhenyue.approval.escalation_after_critical", 300)


def get_approver_chains() -> dict:
    return get("zhenyue.approval.approver_chains", {})


def get_capabilities(trust_level: str = "") -> dict:
    caps = get("zhenyue.capabilities", {})
    if trust_level:
        return caps.get(trust_level, caps.get("basic", {}))
    return caps


def get_rate_limit_per_agent() -> int:
    return get("zhenyue.rate_limit.per_agent_rpm", 60)


def get_rate_limit_global() -> int:
    return get("zhenyue.rate_limit.global_rpm", 500)


def get_break_glass_enabled() -> bool:
    return get("zhenyue.break_glass.enabled", True)


def get_break_glass_token_path() -> str:
    return get("zhenyue.break_glass.token_path", "/opt/qingtian/break_glass.token")


def get_break_glass_allowed_actions() -> list:
    return get("zhenyue.break_glass.allowed_actions", ["stop_agent", "isolate_agent", "block_ip"])


def get_break_glass_cooldown() -> int:
    return get("zhenyue.break_glass.cooldown_minutes", 30)


def get_msg_signing_enabled() -> bool:
    return get("zhenyue.message_signing.enabled", True)


def get_msg_signing_time_window() -> int:
    return get("zhenyue.message_signing.time_window_seconds", 300)


def get_msg_signing_key_rotation() -> int:
    return get("zhenyue.message_signing.key_rotation_hours", 24)


def get_msg_signing_grace_period() -> int:
    return get("zhenyue.message_signing.grace_period_seconds", 300)


def get_approval_ttl() -> dict:
    """审批 TTL 统一返回（秒）"""
    return {
        "high": get_approval_timeout_high(),
        "critical": get_approval_timeout_critical(),
        "warning": get("zhenyue.approval.timeout_warning", 600),
    }


def get_execution_delay_seconds() -> int:
    """审批通过后到实际执行的反悔窗口（秒），默认 7 天。
    期间管理员可取消审批。超期后系统自动执行。
    """
    return get("zhenyue.approval.execution_delay_seconds", 604800)  # 7 days


def get_guard_mode() -> str:
    """中间件模式: enforce(拦截) / log_only(仅审计不拦截) / disabled(完全关闭)"""
    return get("zhenyue.guard.mode", "enforce")


def get_builtin_services() -> dict:
    """内置服务身份配置（Hermes 等）"""
    return get("zhenyue.builtin_services", {})


def get_tool_rules_path() -> str:
    """工具规则文件路径（第一层 Plugin 用）"""
    return get("zhenyue.tool_rules.path", "/opt/qingtian/tool-rules.yaml")


def get_alert_dedup_window_seconds() -> int:
    return get("zhenyue.alert.dedup_window_seconds", 300)


def get_alert_throttle_max_per_hour() -> int:
    return get("zhenyue.alert.throttle_max_per_hour", 20)


def get_alert_silent_hours_enabled() -> bool:
    return get("zhenyue.alert.silent_hours_enabled", True)


def get_wireguard_interface() -> str:
    return get("network.wireguard.interface", "wg0")


def get_audit_retention_days() -> int:
    """审计日志保留天数，默认 365 天。"""
    return get("zhenyue.audit.retention_days", 365)


def get_redis_url() -> str:
    """Redis 连接地址（消息签名 nonce 重放检测用，2026-08-26 #4）。

    env REDIS_URL 优先；默认与 huanyu.redis_url 同机默认值对齐。
    """
    return os.getenv("REDIS_URL", get("zhenyue.redis_url", "redis://localhost:6379"))
