"""执策行为规范引擎 — 策略匹配 + CRUD（v1.10）

create_task() 内部调用 policy_check()，在 LLM 分解前检查：
  1. 黑名单（keyword + pattern）→ 任一命中即拒绝
  2. 白名单（scope）→ 不在范围内即拒绝

scope 的子串未命中时调 LLM 语义判断。
"""
import re
import json
import logging
import httpx
from common.db import get_pool
from huanyu.config import get_schema_name as _huanyu_schema
from . import config as cfg

logger = logging.getLogger("zhice.policy")
SCHEMA = cfg.get_schema_name()


# ── 策略匹配引擎 ──────────────────────────────────────

def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    """编译正则列表，编译失败跳过并记录错误。"""
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            logger.error("behavior_policy: invalid pattern '%s': %s", p, e)
    return compiled


def _check_keyword(text: str, rule: dict) -> bool:
    keywords = rule.get("keywords", [])
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _check_pattern(text: str, rule: dict) -> bool:
    patterns = _compile_patterns(rule.get("patterns", []))
    return any(p.search(text) for p in patterns)


def _check_scope_substring(text: str, rule: dict) -> tuple[bool, bool]:
    """检查 scope 规则，返回 (allowed_by_substring, denied_by_substring)。
    deny 命中返回 True,True。
    """
    text_lower = text.lower()
    allow = rule.get("allow", [])
    deny = rule.get("deny", [])

    # deny 优先检查
    deny_hit = any(d.lower() in text_lower for d in deny)
    if deny_hit:
        return False, True

    # allow 子串匹配
    allow_hit = any(a.lower() in text_lower for a in allow)
    if allow_hit:
        return True, False

    return False, False


async def _llm_scope_check(text: str, allow_list: list[str]) -> bool | None:
    """调 LLM 判断任务文本是否属于指定的 allow 领域。

    P2 (R11) 返回值区分三种情形:
      True  — LLM 判定属于
      False — LLM 判定不属于，或 LLM 调用失败（fail-closed 拦截，防御瞬时异常）
      None  — LLM key 未配置（配置缺失，调用方应降级放行并记录告警）
    """
    api_key = cfg.get_llm_api_key()
    if not api_key:
        logger.warning("LLM API key not configured, scope check falls back to substring only")
        return None

    base_url = cfg.get_llm_base_url()
    model = cfg.get_llm_decompose_model()
    domains = "、".join(allow_list)

    prompt = (
        f"以下任务是否属于以下领域之一？仅回复 YES 或 NO。\n\n"
        f"领域: {domains}\n"
        f"任务: {text[:500]}\n\n"
        f"判断标准：如果任务的核心意图与任一领域相关，回复 YES。"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 512,  # 2026-08-27: glm思考强制开启计入max_tokens,5必空
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
            return answer.startswith("YES")
    except Exception as e:
        logger.warning("LLM scope check failed: %s", e)
        return False


def _collect_text(task_data: dict) -> str:
    """收集 title + description + steps[*].instruction 等所有需检查的文本。"""
    parts = [task_data.get("title", ""), task_data.get("description", "")]
    for s in task_data.get("steps", []) or []:
        parts.append(s.get("instruction", ""))
    return " ".join(parts)


async def policy_check(agent_id: str, task_data: dict) -> dict:
    """检查任务是否符合该 Agent 的行为规范。

    Args:
        agent_id: Agent ID（如 "biz:seller-01"）
        task_data: {"title": ..., "description": ..., "steps": [...]}

    Returns:
        {"allowed": True}
        或 {"allowed": False, "action": "block", "message": "...", "matched_policy": "..."}
    """
    text = _collect_text(task_data)
    if not text.strip():
        return {"allowed": True}

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 查 Agent 的 category
        agent_category = None
        try:
            agent_category = await conn.fetchval(
                f"SELECT category FROM {_huanyu_schema()}.agents WHERE agent_id = $1", agent_id,
            )
        except Exception:
            pass

        # 查所有匹配的策略：agent_id 精确匹配 + category 匹配 + 全局
        conditions = ["enabled = true"]
        params = []
        idx = 1

        conditions.append(
            f"(agent_id = ${idx} OR agent_id IS NULL)"
        )
        params.append(agent_id)
        idx += 1

        if agent_category:
            conditions.append(
                f"(category = ${idx} OR category IS NULL)"
            )
            params.append(agent_category)
            idx += 1
        else:
            conditions.append("category IS NULL")

        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.behavior_policies "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY priority DESC",
            *params,
        )

    if not rows:
        return {"allowed": True}

    block_message = None
    block_policy = None
    # 先按优先级处理 keyword/pattern（不需要 LLM）
    scope_rules = []

    for row in rows:
        rule = row["rule"] if isinstance(row["rule"], dict) else json.loads(row["rule"])
        ptype = row["policy_type"]
        paction = row["action"]

        if ptype == "keyword":
            if _check_keyword(text, rule):
                if paction == "block":
                    block_message = row["reject_message"] or "行为规范限制：任务包含禁止的关键词"
                    block_policy = row["name"]
                    break  # 黑名单命中 → 立即拒绝
                elif paction == "warn":
                    logger.warning("behavior_policy warn: agent=%s policy=%s keyword matched", agent_id, row["name"])
                # log_only: 不干预

        elif ptype == "pattern":
            if _check_pattern(text, rule):
                if paction == "block":
                    block_message = row["reject_message"] or "行为规范限制：任务匹配禁止模式"
                    block_policy = row["name"]
                    break
                elif paction == "warn":
                    logger.warning("behavior_policy warn: agent=%s policy=%s pattern matched", agent_id, row["name"])

        elif ptype == "scope":
            scope_rules.append((row["name"], rule, paction, row["reject_message"]))

    # 黑名单命中
    if block_message:
        return {"allowed": False, "action": "block", "message": block_message, "matched_policy": block_policy or ""}

    # scope 检查
    for sname, srule, saction, smsg in scope_rules:
        allowed_by_sub, denied_by_sub = _check_scope_substring(text, srule)

        # deny 命中
        if denied_by_sub:
            if saction == "block":
                return {"allowed": False, "action": "block",
                        "message": smsg or "行为规范限制：任务不在服务范围内",
                        "matched_policy": sname}
            elif saction == "warn":
                logger.warning("behavior_policy warn: agent=%s policy=%s scope denied", agent_id, sname)
                continue  # 仅记录警告，继续检查后续 scope 规则

        # allow 子串命中 → 通过
        if allowed_by_sub:
            continue  # 这条 scope 通过，检查下一个

        # 子串未命中 → LLM 判断
        allow_list = srule.get("allow", [])
        if allow_list:
            llm_ok = await _llm_scope_check(text, allow_list)
            if llm_ok is None:
                # P2 (R11): LLM key 未配置（配置缺失）→ 降级放行并记录告警，
                # 避免 scope allow 策略因无法调用 LLM 而全拦（fail-closed 过度，
                # 什么任务都建不了）。仅当"有 key 但 LLM 判定拒绝"才拦截。
                logger.warning("behavior_policy: scope '%s' 无法调用 LLM（key 未配置），降级放行", sname)
                continue
            if not llm_ok:
                # LLM 判定不在范围内（或 LLM 调用失败，fail-closed）→ 拦截
                if saction == "block":
                    return {"allowed": False, "action": "block",
                            "message": smsg or "行为规范限制：任务不在服务范围内",
                            "matched_policy": sname}
            # LLM 判断在范围内 → 继续检查下一个 scope

    return {"allowed": True}


# ── CRUD ──────────────────────────────────────────────

async def list_policies() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.behavior_policies ORDER BY priority DESC, policy_id"
        )
    results = []
    for r in rows:
        d = dict(r)
        d["rule"] = d["rule"] if isinstance(d["rule"], dict) else json.loads(d["rule"])
        d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
        d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
        results.append(d)
    return results


async def get_policy(policy_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.behavior_policies WHERE policy_id = $1", policy_id,
        )
    if not row:
        return None
    d = dict(row)
    d["rule"] = d["rule"] if isinstance(d["rule"], dict) else json.loads(d["rule"])
    d["created_at"] = d["created_at"].isoformat() if d.get("created_at") else None
    d["updated_at"] = d["updated_at"].isoformat() if d.get("updated_at") else None
    return d


async def create_policy(
    name: str, policy_type: str, rule: dict, action: str,
    created_by: str, agent_id: str = "", category: str = "",
    reject_message: str = "", priority: int = 0,
) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"INSERT INTO {SCHEMA}.behavior_policies "
            f"(name, agent_id, category, policy_type, rule, action, reject_message, priority, created_by) "
            f"VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING *",
            name, agent_id or None, category or None, policy_type,
            json.dumps(rule, ensure_ascii=False), action,
            reject_message, priority, created_by,
        )
    d = dict(row)
    d["rule"] = d["rule"] if isinstance(d["rule"], dict) else json.loads(d["rule"])
    logger.info("Policy '%s' created by %s: type=%s action=%s", name, created_by, policy_type, action)
    return d


async def update_policy(policy_id: int, updates: dict) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await get_policy(policy_id)
        if not existing:
            return None

        name = updates.get("name", existing["name"])
        ptype = updates.get("policy_type", existing["policy_type"])
        rule = updates.get("rule", existing["rule"])
        action = updates.get("action", existing["action"])
        reject_message = updates.get("reject_message", existing.get("reject_message") or "")
        priority = updates.get("priority", existing["priority"])
        enabled = updates.get("enabled", existing["enabled"])
        agent_id = updates.get("agent_id", existing.get("agent_id"))
        category = updates.get("category", existing.get("category"))

        row = await conn.fetchrow(
            f"UPDATE {SCHEMA}.behavior_policies SET name=$1, agent_id=$2, category=$3, "
            f"policy_type=$4, rule=$5, action=$6, reject_message=$7, priority=$8, "
            f"enabled=$9, updated_at=NOW() "
            f"WHERE policy_id=$10 RETURNING *",
            name, agent_id, category, ptype, json.dumps(rule, ensure_ascii=False),
            action, reject_message, priority, enabled, policy_id,
        )
        if not row:
            return None
    d = dict(row)
    d["rule"] = d["rule"] if isinstance(d["rule"], dict) else json.loads(d["rule"])
    return d


async def delete_policy(policy_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            f"DELETE FROM {SCHEMA}.behavior_policies WHERE policy_id = $1", policy_id,
        )
        return result != "DELETE 0"
