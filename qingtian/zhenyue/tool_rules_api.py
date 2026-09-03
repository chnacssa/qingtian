"""
镇岳 — 第一层动态工具规则管理 API
提供运行时工具规则的 CRUD，规则变更后自动写盘触发 fs.watch 热加载。

路由注册在 api.py 中：app.include_router(tool_rules_router)
"""

import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import config as cfg
from .auth import auth_dependency
from .models import ToolRuleRequest, ToolRuleResponse

tool_rules_router = APIRouter(prefix="/v1/zhenyue/tool-rules", tags=["tool-rules"])

RULES_PATH = cfg.get_tool_rules_path()

# 内存缓存
_rules_cache: dict[str, dict] = {}


def _load_rules() -> dict[str, dict]:
    """从磁盘加载规则文件。"""
    global _rules_cache
    if not os.path.exists(RULES_PATH):
        _rules_cache = {}
        return _rules_cache

    try:
        import yaml
        with open(RULES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        raw_rules = data.get("rules", [])
        _rules_cache = {}
        for r in raw_rules:
            rid = r.get("id") or str(uuid.uuid4())[:8]
            _rules_cache[rid] = {
                "id": rid,
                "tool": r.get("tool", ""),
                "match": r.get("match", ""),
                "field": r.get("field", "command"),
                "severity": r.get("severity", "log_only"),
                "approval_severity": r.get("approval_severity", "high"),
                "reason": r.get("reason", ""),
                "enabled": r.get("enabled", True),
            }
    except Exception:
        _rules_cache = {}
    return _rules_cache


def _save_rules() -> None:
    """将内存规则写回磁盘。"""
    import yaml

    # 保留现有文件的其他字段（如 timeout、allowlist）
    existing = {}
    if os.path.exists(RULES_PATH):
        try:
            with open(RULES_PATH, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
        except Exception:
            pass

    existing["rules"] = [
        {
            "id": r["id"],
            "tool": r["tool"],
            "match": r["match"],
            "field": r.get("field", "command"),
            "severity": r["severity"],
            "approval_severity": r["approval_severity"],
            "reason": r.get("reason", ""),
            "enabled": r.get("enabled", True),
        }
        for r in _rules_cache.values()
    ]

    os.makedirs(os.path.dirname(RULES_PATH), exist_ok=True)
    with open(RULES_PATH, "w", encoding="utf-8") as f:
        yaml.dump(existing, f, default_flow_style=False, allow_unicode=True)


# 初始化加载
_load_rules()


# ── 端点 ──────────────────────────────────────────────


@tool_rules_router.get("")
async def list_rules(auth: dict = Depends(auth_dependency)):
    """列出所有工具规则。"""
    return {
        "rules": [
            ToolRuleResponse(**r).model_dump()
            for r in _rules_cache.values()
        ],
        "total": len(_rules_cache),
    }


@tool_rules_router.post("")
async def create_rule(req: ToolRuleRequest, auth: dict = Depends(auth_dependency)):
    """创建新的工具规则（需 admin）。"""
    if auth.get("role") not in ("admin", "ops_admin"):
        return JSONResponse(status_code=403, content={"error": "FORBIDDEN"})

    rid = str(uuid.uuid4())[:8]
    rule = {
        "id": rid,
        "tool": req.tool,
        "match": req.match,
        "field": "command",
        "severity": req.severity,
        "approval_severity": req.approval_severity,
        "reason": req.reason,
        "enabled": True,
    }
    _rules_cache[rid] = rule
    _save_rules()
    return ToolRuleResponse(**rule).model_dump()


@tool_rules_router.delete("/{rule_id}")
async def delete_rule(rule_id: str, auth: dict = Depends(auth_dependency)):
    """删除工具规则（需 admin）。"""
    if auth.get("role") not in ("admin", "ops_admin"):
        return JSONResponse(status_code=403, content={"error": "FORBIDDEN"})

    if rule_id not in _rules_cache:
        return JSONResponse(status_code=404, content={"error": "NOT_FOUND"})

    del _rules_cache[rule_id]
    _save_rules()
    return {"status": "deleted", "rule_id": rule_id}


@tool_rules_router.post("/reload")
async def reload_rules(auth: dict = Depends(auth_dependency)):
    """从磁盘重新加载规则。"""
    _load_rules()
    return {"status": "ok", "total": len(_rules_cache)}
