"""汇川企业系统连接器 — YAML 配置驱动的通用对接引擎

Phase 4 核心模块。不做特定厂商适配，提供通用连接协议。

边界约束:
  - 配置文件不存在 → 返回 error
  - auth token_env 未设置 → 跳过 (不崩溃)
  - HTTP 请求超时 30s → 返回 partial
  - 单次拉取最多 500 条 → 返回 count
  - 蒸馏失败 → 记录日志 + 返回 error_count
  - 并发 LLM 调用 ≤ 5（Semaphore 限流）
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import httpx
import yaml

from huichuan.ingest import _now_str, ingest_text

logger = logging.getLogger("huichuan.connector")

CONNECTOR_DIR = Path("/opt/qingtian/huichuan/connectors")

# 模板渲染：{{last_run}} → 上次运行时间
_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

# LLM 并发限制
_INGEST_SEMAPHORE = asyncio.Semaphore(5)


async def run_connector(
    conn,
    connector_name: str,
    schema: str = "huichuan",
) -> dict:
    """运行单个连接器，拉取数据→标准化→ingest 入库。"""
    config_path = CONNECTOR_DIR / f"{connector_name}.yaml"
    if not config_path.exists():
        return {"error": f"connector '{connector_name}' not found", "count": 0}

    # 文件 I/O + YAML 解析在线程池执行
    raw = await asyncio.to_thread(config_path.read_text, encoding="utf-8")
    try:
        config = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        logger.exception("Connector %s: YAML parse error", connector_name)
        return {"error": f"YAML parse error: {e}", "count": 0}

    if not isinstance(config, dict):
        return {"error": "invalid YAML: not a dict", "count": 0}

    ctype = config.get("type", "http_poll")
    if ctype != "http_poll":
        return {"error": f"unsupported type: {ctype}", "count": 0}

    # Auth
    token_env = config.get("source", {}).get("auth", {}).get("token_env", "")
    token = os.getenv(token_env, "")
    if not token:
        logger.warning(
            "Connector %s: token env %s not set, skipping", connector_name, token_env
        )
        return {"error": "auth not configured", "count": 0}

    # Fetch — 安全取值防止 KeyError
    source = config.get("source", {})
    endpoint = source.get("endpoint", "")
    resource_template = source.get("resource", "")

    if not endpoint:
        return {"error": "source.endpoint not configured", "count": 0}

    resource = await _render_template(resource_template, connector_name)

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            url = _url_join(endpoint, resource)
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return {"error": "timeout", "count": 0}
    except Exception as e:
        logger.exception("Connector %s fetch failed", connector_name)
        return {"error": str(e), "count": 0}

    # Map & Ingest
    items = data if isinstance(data, list) else data.get("data", [])
    items = items[:500]  # max 500

    mapping = config.get("mapping", {})
    domain = mapping.get("domain", "erp_import")
    title_tpl = mapping.get("title_template", "{}")
    fields = mapping.get("fields", {})

    count = 0
    error_count = 0

    for item in items:
        try:
            title = title_tpl
            for fname, fpath in fields.items():
                val = _extract_jsonpath(item, fpath)
                title = title.replace(f"{{{{{fname}}}}}", str(val))

            text = json.dumps(item, ensure_ascii=False)
            async with _INGEST_SEMAPHORE:
                result = await ingest_text(
                    conn,
                    text,
                    source=f"connector:{connector_name}",
                    original_filename=title,
                    schema=schema,
                )
            if result.get("entries", 0) > 0:
                count += result["entries"]
            else:
                error_count += 1
        except Exception:
            error_count += 1

    logger.info(
        "Connector %s: %d ingested, %d errors",
        connector_name,
        count,
        error_count,
    )
    return {"count": count, "error_count": error_count}


async def _render_template(template: str, connector_name: str) -> str:
    """渲染 {{last_run}} → 上次运行时间 ISO。

    每个连接器使用独立的状态文件，避免不同连接器间 last_run 互相覆盖。
    """
    state_file = CONNECTOR_DIR / f".last_run_{connector_name}"
    last_run = ""
    if state_file.exists():
        last_run = (await asyncio.to_thread(state_file.read_text)).strip()
    result = _TEMPLATE_RE.sub(
        lambda m: last_run if m.group(1) == "last_run" else "", template
    )
    # Update state
    await asyncio.to_thread(state_file.write_text, _now_str())
    return result


def _url_join(base: str, path: str) -> str:
    """安全拼接 URL（处理尾部/头部斜杠不匹配）。"""
    if not path:
        return base
    if path.startswith("http"):
        return path
    if base.endswith("/") and path.startswith("/"):
        return base + path[1:]
    if not base.endswith("/") and not path.startswith("/"):
        return base + "/" + path
    return base + path


def _extract_jsonpath(obj, path: str) -> str:
    """简易 JSONPath: $.foo.bar → obj['foo']['bar']"""
    parts = path.lstrip("$.").split(".")
    val = obj
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p, "")
        elif isinstance(val, list) and p.isdigit():
            val = val[int(p)] if int(p) < len(val) else ""
        else:
            return ""
    return str(val) if val else ""
