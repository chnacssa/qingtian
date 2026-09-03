"""能力查询服务 —— 根据 trust_level 返回 Agent 能力集合。"""

from . import config as cfg

# trust_level → 允许的工具列表映射（兜底，config.yaml 优先）
DEFAULT_CAPABILITIES = {
    "basic": {"allowed_tools": ["read", "search"], "trust_weight": 0.3, "max_message_rpm": 60},
    "verified": {"allowed_tools": ["read", "search", "write", "inquire"], "trust_weight": 0.6, "max_message_rpm": 120},
    "trusted": {"allowed_tools": ["read", "search", "write", "inquire", "negotiate", "agree"], "trust_weight": 0.8, "max_message_rpm": 300},
    "admin": {"allowed_tools": ["*"], "trust_weight": 1.0, "max_message_rpm": 0},
}


def _get_caps(trust_level: str) -> dict:
    """优先 config.yaml，回退内置默认。"""
    caps = cfg.get_capabilities(trust_level)
    if caps:
        return caps
    return DEFAULT_CAPABILITIES.get(trust_level, DEFAULT_CAPABILITIES["basic"])


def get_allowed_tools(trust_level: str) -> list[str]:
    return _get_caps(trust_level).get("allowed_tools", [])


def get_trust_weight(trust_level: str) -> float:
    return _get_caps(trust_level).get("trust_weight", 0.3)


def get_max_message_rpm(trust_level: str) -> int:
    return _get_caps(trust_level).get("max_message_rpm", 120)


def has_capability(trust_level: str, tool: str) -> bool:
    tools = get_allowed_tools(trust_level)
    if "*" in tools:
        return True
    return tool in tools


def check_action_allowed(trust_level: str, required_capabilities: list[str]) -> tuple[bool, list[str]]:
    """检查当前信任等级是否拥有执行某操作所需的全部能力。

    返回 (allowed, missing_caps)，missing_caps 为缺失的能力列表。
    admin 通配符 * 自动通过所有检查。
    """
    tools = get_allowed_tools(trust_level)
    if "*" in tools:
        return True, []
    missing = [c for c in required_capabilities if c not in tools]
    return len(missing) == 0, missing


def get_trust_upgrade_requirements(current_level: str) -> dict:
    levels = {
        "basic": {"next": "verified", "min_transactions": 5, "min_rating": 3.5},
        "verified": {"next": "trusted", "min_transactions": 20, "min_rating": 4.0},
        "trusted": {"next": None},
        "admin": {"next": None},
    }
    return levels.get(current_level, {})
