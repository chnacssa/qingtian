"""执策检查规则引擎 — engine-auto 比对 + agent-report 比对

检查规则分两类：
  engine-auto：引擎直接比对 submit.outputs（output_contains、manual_review）
  agent-report：Agent 本地执行后上报 check_results，引擎比对预期值

check_single 返回:
  (passed: bool, actual: str, error: str, schema_error: bool)
  - manual_review 返回 passed=False, error="needs_review" — 表示挂起等人审
  - schema 校验失败返回 schema_error=True — 不消耗 auto_retry
"""
import re
import json as _json
import logging
from .models import ENGINE_AUTO_TYPES, AGENT_REPORT_TYPES, VALID_CHECK_TYPES

logger = logging.getLogger("zhice.checker")

# ── schema 校验（§3.4.4）──────────────────────────────────

SCHEMA_RULES = {
    "file_exists": {
        "required": ["path", "exists"],
        "types": {"path": str, "exists": bool},
    },
    "api_health": {
        "required": ["url", "status_code"],
        "types": {"url": str, "status_code": int},
    },
    "db_query": {
        "required": ["sql", "count"],
        "types": {"sql": str, "count": int},
    },
    "run_script": {
        "required": ["script", "exit_code"],
        "types": {"script": str, "exit_code": int},
    },
}


def validate_check_results_schema(criteria_type: str, item: dict) -> str | None:
    """校验单条 check_result 的结构合法性，返回错误信息或 None"""
    rules = SCHEMA_RULES.get(criteria_type)
    if rules is None:
        return None  # 非 agent-report 类型，跳过

    for field in rules["required"]:
        if field not in item:
            return f"缺少必填字段: {field}"

    for field, expected_type in rules["types"].items():
        if field in item and not isinstance(item[field], expected_type):
            return f"字段 {field} 类型错误: 期望 {expected_type.__name__}, 实际 {type(item[field]).__name__}"

    # 字符串长度校验
    for str_field in ("path", "url", "sql", "script"):
        if str_field in item and isinstance(item[str_field], str):
            if len(item[str_field]) > 10000:
                return f"字段 {str_field} 超过最大长度 10000"
            if str_field == "sql" and len(item[str_field]) > 500:
                return f"字段 sql 超过最大长度 500"
            if str_field == "script" and len(item[str_field]) > 500:
                return f"字段 script 超过最大长度 500"

    # api_health status_code 范围
    if criteria_type == "api_health" and "status_code" in item:
        sc = item["status_code"]
        if not (100 <= sc <= 599):
            return f"status_code 超出有效范围 (100-599): {sc}"

    # run_script exit_code 范围
    if criteria_type == "run_script" and "exit_code" in item:
        ec = item["exit_code"]
        if not (0 <= ec <= 255):
            return f"exit_code 超出有效范围 (0-255): {ec}"

    return None


# ── 引擎比对 ──────────────────────────────────────────────

def check_single(criterion: dict, check_results: dict) -> tuple[bool, str, str, bool]:
    """对单条 acceptance_criteria 执行比对

    Returns:
        (passed, actual_value, error_message, schema_error)
        - schema_error=True: 协议错误，不消耗 auto_retry
    """
    ctype = criterion.get("type", "")

    # ── engine-auto ──
    if ctype == "output_contains":
        field = criterion.get("field", "")
        keyword = criterion.get("keyword", "")
        try:
            actual = _nested_get(check_results, field)
            actual_str = str(actual) if actual is not None else ""
            if keyword in actual_str:
                return True, actual_str, "", False
            else:
                return False, actual_str, f"output_contains: '{keyword}' not found in {field}", False
        except (KeyError, TypeError):
            return False, "", f"output_contains: field '{field}' not found in outputs", False

    if ctype == "manual_review":
        return False, "", "needs_review", False

    if ctype == "reasonableness":
        field = criterion.get("field", "result")
        min_val = criterion.get("min")
        max_val = criterion.get("max")
        unit = criterion.get("unit", "")
        actual_str = str(_nested_get(check_results, field) or "")
        # 尝试提取数字
        nums = re.findall(r"[\d.]+", actual_str)
        if not nums:
            return False, actual_str, f"reasonableness: 无法从 '{field}' 提取数值", False
        val = float(nums[0])
        if min_val is not None and val < min_val:
            return False, str(val), f"reasonableness: {val}{unit} < 最小值 {min_val}{unit}（可疑）", False
        if max_val is not None and val > max_val:
            return False, str(val), f"reasonableness: {val}{unit} > 最大值 {max_val}{unit}（数据溢出？）", False
        return True, f"{val}{unit}", "", False

    # ── agent-report ──
    agent_items = check_results.get(ctype)
    if agent_items is None:
        # P2 (R11): 只有引擎自动类型（output_contains/manual_review）未上报才视为通过；
        # api_health 已移出 ENGINE_AUTO_TYPES（属 agent-report），未上报 = 未验证，
        # 必须 fail-closed，避免 API 健康检查从未真正执行却判定通过。
        # 用明确的"未上报/未验证"错误信息区分 unknown 状态，方便运维定位。
        if ctype in ENGINE_AUTO_TYPES:
            return True, "(引擎自动)", "", False
        return False, "", f"{ctype}: 未上报检查结果，视为未验证（fail-closed）", False

    # 统一为列表处理
    if not isinstance(agent_items, list):
        agent_items = [agent_items]

    errors = []
    for item in agent_items:
        # 先做 schema 校验 — schema 错误不消耗 auto_retry
        schema_err = validate_check_results_schema(ctype, item)
        if schema_err:
            return False, str(item), f"schema 校验失败: {schema_err}", True

        if ctype == "file_exists":
            path = item.get("path", "")
            exists = item.get("exists", False)
            required = criterion.get("required", True)
            if exists == required:
                return True, f"path={path}, exists={exists}", "", False
            else:
                errors.append(f"file_exists: {path} exists={exists}, required={required}")

        elif ctype == "api_health":
            url = item.get("url", "")
            status_code = item.get("status_code", 0)
            expected = criterion.get("expected_status", 200)
            if url != criterion.get("url", ""):
                continue  # 不匹配的 URL，跳过
            if status_code == expected:
                return True, f"url={url}, status={status_code}", "", False
            else:
                errors.append(f"api_health: {url} status={status_code}, expected={expected}")

        elif ctype == "db_query":
            sql = item.get("sql", "")
            count = item.get("count", 0)
            expected_min = criterion.get("expected_min", 0)
            if sql != criterion.get("sql", ""):
                continue
            if count >= expected_min:
                return True, f"sql={sql[:50]}..., count={count}", "", False
            else:
                errors.append(f"db_query: count={count}, expected_min={expected_min}")

        elif ctype == "run_script":
            script = item.get("script", "")
            exit_code = item.get("exit_code", -1)
            expected_code = criterion.get("expected_exit_code", 0)
            if script != criterion.get("script", ""):
                continue
            if exit_code == expected_code:
                return True, f"script={script[:50]}..., exit_code={exit_code}", "", False
            else:
                errors.append(f"run_script: exit_code={exit_code}, expected={expected_code}")

    if errors:
        return False, "; ".join(errors[:3]), "; ".join(errors[:3]), False
    return False, "", f"{ctype}: 无匹配的 check_result 项", False


def check_all(criteria_list: list[dict], check_results: dict) -> dict:
    """遍历所有 acceptance_criteria，逐条比对

    Returns:
        {"passed": bool, "failed_rules": [...], "results": [...],
         "has_manual_review": bool, "has_schema_error": bool}
    """
    passed = True
    failed_rules = []
    results = []
    has_manual_review = False
    has_schema_error = False

    # 兼容 jsonb codec 未注册时 DB 返回字符串的情况
    if isinstance(criteria_list, str):
        criteria_list = _json.loads(criteria_list)
    for criterion in (criteria_list or []):
        if isinstance(criterion, str):
            criterion = _json.loads(criterion)
        ok, actual, error, schema_error = check_single(criterion, check_results)

        if error == "needs_review":
            has_manual_review = True

        if schema_error:
            has_schema_error = True

        results.append({
            "type": criterion.get("type", ""),
            "passed": ok,
            "actual": actual,
            "error": error,
            "schema_error": schema_error,
        })
        if not ok:
            passed = False
            failed_rules.append({
                "type": criterion.get("type", ""),
                "expected": criterion,
                "actual": actual,
                "error": error,
                "schema_error": schema_error,
            })

    return {
        "passed": passed,
        "failed_rules": failed_rules,
        "results": results,
        "has_manual_review": has_manual_review,
        "has_schema_error": has_schema_error,
    }


def is_engine_auto(criteria_type: str) -> bool:
    return criteria_type in ENGINE_AUTO_TYPES


def is_agent_report(criteria_type: str) -> bool:
    return criteria_type in AGENT_REPORT_TYPES


def _nested_get(d: dict, dotted_key: str):
    """按点分隔路径取值，如 'result.summary' → d['result']['summary']"""
    keys = dotted_key.split(".")
    val = d
    for k in keys:
        if isinstance(val, dict):
            val = val.get(k)
        else:
            return None
    return val


def check_quality_criteria(criteria_list: list[dict], comparison_data: dict) -> list[dict]:
    """v2 引擎重检 quality_criteria（不阻塞，仅返回结果）

    Args:
        criteria_list: 步骤的 quality_criteria 列表
        comparison_data: 参考数据（如历史报价、市场价等）

    Returns:
        [{passed, type, criterion, actual}, ...]
    """
    results = []
    for c in criteria_list:
        ctype = c.get("type", "")
        field = c.get("field", "")
        expected = c.get("expected")
        actual = _nested_get(comparison_data, field) if field else comparison_data

        passed = True
        if ctype == "range" and isinstance(expected, dict):
            mn, mx = expected.get("min"), expected.get("max")
            if isinstance(actual, (int, float)) and (mn is not None or mx is not None):
                passed = (mn is None or actual >= mn) and (mx is None or actual <= mx)
        elif ctype == "match" and expected is not None:
            passed = str(actual) == str(expected)
        elif ctype == "exists":
            passed = actual is not None

        results.append({
            "passed": passed,
            "type": ctype,
            "criterion": c,
            "actual": actual,
        })
    return results
