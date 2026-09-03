"""镇岳 — 守卫规则引擎。

从 DB 加载规则，按优先级匹配请求，支持 allow / deny / audit 三种类型。
"""

import fnmatch
import logging
import re
from typing import Optional

from common.db import get_pool
from . import config as zcfg
from .audit_service import write_audit

logger = logging.getLogger("zhenyue.guard")

_engine: Optional["GuardEngine"] = None


class GuardEngine:
    """守卫规则引擎 — 加载 DB 规则，匹配请求。"""

    def __init__(self):
        self._rules: list[dict] = []

    async def load_rules(self) -> list[dict]:
        """从 DB 加载所有启用规则。"""
        schema = zcfg.get_schema_name()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT rule_id, name, description, rule_type, match_pattern, priority, enabled, created_at "
                f"FROM {schema}.guard_rules "
                f"WHERE enabled = TRUE "
                f"ORDER BY priority DESC"
            )
            self._rules = [dict(r) for r in rows]
            return self._rules

    async def check(self, agent_id: str, action: str, target: str) -> dict:
        """检查请求是否被允许。

        Args:
            agent_id: 请求的 Agent ID
            action: 请求动作（如 'delete_file', 'config_change'）
            target: 请求目标路径或标识

        Returns:
            {"allowed": bool, "rule": "规则名或 None", "reason": "原因"}
        """
        if not self._rules:
            await self.load_rules()

        expression = f"{action}:{target}"

        for rule in self._rules:
            if fnmatch.fnmatch(expression, rule["match_pattern"]):
                rule_type = rule["rule_type"]

                if rule_type == "allow":
                    logger.info("Guard ALLOW: agent=%s, action=%s, target=%s, rule=%s",
                                agent_id, action, target, rule["name"])
                    return {
                        "allowed": True,
                        "rule": rule["name"],
                        "reason": f"Matched allow rule '{rule['name']}'",
                    }

                elif rule_type == "deny":
                    logger.warning("Guard DENY: agent=%s, action=%s, target=%s, rule=%s",
                                   agent_id, action, target, rule["name"])
                    return {
                        "allowed": False,
                        "rule": rule["name"],
                        "reason": f"Denied by rule '{rule['name']}'",
                    }

                elif rule_type == "audit":
                    # audit 规则：写审计日志，不拦截
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        await write_audit(conn, {
                            "agent_id": agent_id,
                            "agent_role": "agent",
                            "action": action,
                            "target_type": "guard_audit",
                            "target_id": target,
                            "severity": "medium",
                            "detail": {
                                "guard_rule": rule["name"],
                                "match_pattern": rule["match_pattern"],
                                "reason": rule.get("description", ""),
                            },
                            "approval_status": "auto",
                        })
                    logger.info("Guard AUDIT: agent=%s, action=%s, target=%s, rule=%s",
                                agent_id, action, target, rule["name"])
                    return {
                        "allowed": True,
                        "rule": rule["name"],
                        "reason": f"Audited by rule '{rule['name']}'",
                    }

        # 无匹配规则时默认允许（但记录）
        logger.info("Guard ALLOW (default): agent=%s, action=%s, target=%s", agent_id, action, target)
        return {
            "allowed": True,
            "rule": None,
            "reason": "No matching rule — allowed by default",
        }

    async def add_rule(self, name: str, rule_type: str, match_pattern: str,
                       priority: int = 0, description: str = "") -> dict:
        """添加守卫规则。"""
        if rule_type not in ("allow", "deny", "audit"):
            raise ValueError(f"Invalid rule_type: {rule_type}. Must be allow/deny/audit")

        schema = zcfg.get_schema_name()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rule_id = await conn.fetchval(
                f"INSERT INTO {schema}.guard_rules (name, description, rule_type, match_pattern, priority) "
                f"VALUES ($1, $2, $3, $4, $5) RETURNING rule_id",
                name, description, rule_type, match_pattern, priority,
            )
        # 重新加载规则
        await self.load_rules()
        return {
            "rule_id": str(rule_id),
            "name": name,
            "rule_type": rule_type,
            "match_pattern": match_pattern,
            "priority": priority,
            "description": description,
            "enabled": True,
        }

    async def delete_rule(self, rule_id: str) -> bool:
        """删除规则。"""
        schema = zcfg.get_schema_name()
        pool = await get_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {schema}.guard_rules WHERE rule_id = $1::uuid",
                rule_id,
            )
        # 重新加载规则
        await self.load_rules()
        return result != "DELETE 0"

    async def list_rules(self) -> list[dict]:
        """列出所有规则（包括禁用的）。"""
        schema = zcfg.get_schema_name()
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT rule_id, name, description, rule_type, match_pattern, priority, enabled, created_at, updated_at "
                f"FROM {schema}.guard_rules "
                f"ORDER BY priority DESC"
            )
            return [dict(r) for r in rows]


def get_engine() -> GuardEngine:
    """获取守卫规则引擎单例。"""
    global _engine
    if _engine is None:
        _engine = GuardEngine()
    return _engine


# ── Fallback 规则匹配（DB 不可用时的硬编码兜底）──


class DangerRule:
    """危险操作规则 — 硬编码兜底，DB 不可用时的最小安全防护"""

    def __init__(self, method: str, path_pattern: str, name: str,
                 severity: str = "critical", required_roles: list[str] | None = None):
        self.method = method
        self.path_pattern = path_pattern
        self.name = name
        self.severity = severity
        self.required_roles = required_roles or ["admin"]


class PathMatcher:
    """路径匹配器 — 支持 {param} 通配符的简单路由匹配"""

    def __init__(self, rules: list[DangerRule]):
        self._rules = rules
        self._compiled = []
        for rule in rules:
            pattern = rule.path_pattern
            pattern = re.escape(pattern)
            pattern = pattern.replace(r"\{agent_id\}", r"[^/]+")
            pattern = pattern.replace(r"\{skill_name\}", r"[^/]+")
            pattern = pattern.replace(r"\{id\}", r"[^/]+")
            self._compiled.append((re.compile("^" + pattern + "$"), rule))

    def match(self, method: str, path: str) -> DangerRule | None:
        for compiled, rule in self._compiled:
            if method == rule.method and compiled.match(path):
                return rule
        return None
