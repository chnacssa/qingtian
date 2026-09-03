"""
吸星 — 知识采集引擎

功能：
1. 从 DB sources 表读取 enabled 源，按 schedule 筛选当日应采集的源
2. HTTP GET + 自适应重试（7 种失败类型分类处理 + 指数退避）
3. LLM 内容质量裁判（deepseek-v4-flash 判定 content_type + quality_level）
4. 结构指纹检测（DOM 骨架哈希 → 页面改版感知）
5. 提取级别降级：BS4 → 换 UA → Playwright → LLM 辅助
6. SHA256 去重 + 纯文本 .txt 保存
7. 源级自适应：连续失败自动禁用 / 恢复自动启用
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from enum import Enum

import httpx

from common.db import get_pool
from . import config as cfg

logger = logging.getLogger("xixing.crawler")

_CONFIG_UA = cfg.get_collect_user_agent()
TIMEOUT = cfg.get_collect_timeout()
MAX_SIZE = cfg.get_collect_max_size()
SCHEMA = cfg.get_schema_name()
BASE_DIR = cfg.get_base_dir()

# ── 反爬配置 ──────────────────────────────────────────
PROXY = cfg.get_collect_proxy()
PROXIES = cfg.get_collect_proxies()
REQUEST_DELAY = cfg.get_collect_request_delay_seconds()
REQUEST_DELAY_JITTER = cfg.get_collect_request_delay_jitter()
TLS_IMPERSONATE = cfg.get_collect_tls_impersonate()
PLAYWRIGHT_TIMEOUT = cfg.get_collect_playwright_timeout()

# curl_cffi 可用性检测
_HAS_CURL_CFFI: bool = False
try:
    import curl_cffi  # noqa: F401
    _HAS_CURL_CFFI = True
except ImportError:
    pass

# ── 失败分类 ──────────────────────────────────────────

class FailType(str, Enum):
    TIMEOUT = "timeout"
    HTTP_5XX = "http_5xx"
    HTTP_429 = "http_429"
    HTTP_403_ANTI_BOT = "http_403_anti_bot"
    HTTP_404_410 = "http_404_410"
    EMPTY_RESPONSE = "empty_response"
    DNS_ERROR = "dns_error"
    UNKNOWN = "unknown"


# 每种失败类型的重试策略: (max_retries, base_delay_seconds, backoff_multiplier)
_RETRY_POLICY = {
    FailType.TIMEOUT:         (3, 30, 2),   # 30s→60s→120s
    FailType.HTTP_5XX:        (2, 60, 1),   # 60s→60s
    FailType.HTTP_429:        (1, 300, 1),  # 5min, 读 Retry-After
    FailType.HTTP_403_ANTI_BOT: (2, 60, 2),  # 重试 2 次，切换 proxy/UA
    FailType.HTTP_404_410:    (0, 0, 0),    # 不重试，标记失效
    FailType.EMPTY_RESPONSE:  (1, 60, 1),   # 换 UA 重试一次
    FailType.DNS_ERROR:       (0, 0, 0),    # 不重试
    FailType.UNKNOWN:         (1, 60, 1),
}


# User-Agent 池（反爬轮换）— 现代桌面 + 移动端，共 19 个
UA_POOL = [
    # ── Chrome 桌面子集 ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # ── Edge ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    # ── Firefox ──
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:134.0) Gecko/20100101 Firefox/134.0",
    # ── Safari 桌面 ──
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # ── 移动端 ──
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


# ── UA / 代理 / Header 选择 ────────────────────────

# 将 config 中的 UA 也纳入池（去重）
if _CONFIG_UA and _CONFIG_UA not in UA_POOL and _CONFIG_UA != "Qingtian-Xixing/3.0":
    UA_POOL.append(_CONFIG_UA)

def _pick_ua() -> str:
    """随机选取一个 UA。"""
    return random.choice(UA_POOL)


def _pick_proxy(exclude: str | None = None) -> str | None:
    """从代理池随机选一个代理（可选排除某个代理）。

    返回 None 表示不使用代理。
    """
    candidates = [p for p in PROXIES if p and p != exclude]
    if not candidates:
        return None
    return random.choice(candidates)


def _get_source_headers(source: dict) -> dict:
    """提取源配置中的自定义 headers（JSONB 字段）。

    支持 dict 或 JSON 字符串格式。
    """
    raw = source.get("headers")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(v, str)}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {k: v for k, v in parsed.items() if isinstance(v, str)}
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


async def _http_fetch(url: str, headers: dict, timeout: int,
                      proxy: str | None = None, max_size: int = MAX_SIZE) -> tuple[int, str]:
    """HTTP GET 请求，优先使用 curl_cffi（TLS 指纹模拟），不可用时退回 httpx。

    Returns:
        (http_status, response_text)
    """
    # 2026-08-28 P0 修复（SSRF）：请求前过 url_guard 同步快校验
    # （scheme 白名单 + 显式 IP 私网拦截；入库侧已做 DNS 解析复验）
    from common.url_guard import check_external_url
    ok, reason = check_external_url(url)
    if not ok:
        logger.warning(f"[xixing] 采集 URL 被安全策略拒绝: {url} ({reason})")
        return 0, f"[blocked] URL 被安全策略拒绝: {reason}"

    if TLS_IMPERSONATE and _HAS_CURL_CFFI:
        try:
            from curl_cffi import requests as curl_requests
            session_kwargs = {
                "impersonate": TLS_IMPERSONATE,
                "timeout": timeout,
            }
            if proxy:
                session_kwargs["proxies"] = {"http": proxy, "https": proxy}
            async with curl_requests.AsyncSession(**session_kwargs) as session:
                resp = await session.get(url, headers=headers)
                return resp.status_code, resp.text[:max_size]
        except Exception as e:
            logger.debug(f"curl_cffi fetch failed, falling back to httpx: {e}")

    # httpx 回退
    client_kwargs: dict = {"timeout": timeout, "headers": headers}
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        resp = await client.get(url, follow_redirects=True)
        return resp.status_code, resp.text[:max_size]


async def _playwright_fetch(url: str, timeout: int = PLAYWRIGHT_TIMEOUT) -> tuple[str, str]:
    """用 Playwright headless Chromium 加载 JS 渲染页面，返回 (html, text)。

    Returns:
        (raw_html, extracted_text) — 任一失败时对应元素为空字符串。
    """
    # 2026-08-28 P0 修复（SSRF）：请求前过 url_guard 同步快校验，同 _http_fetch
    from common.url_guard import check_external_url
    ok, reason = check_external_url(url)
    if not ok:
        logger.warning(f"[xixing] Playwright 采集 URL 被安全策略拒绝: {url} ({reason})")
        return "", ""

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("playwright not installed; cannot render JS pages")
        return "", ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
                html = await page.content()
                text = await page.inner_text("body")
                return html[:MAX_SIZE], text
            finally:
                await browser.close()
    except Exception as e:
        logger.error(f"Playwright fetch failed for {url}: {e}")
        return "", ""


def _classify_failure(http_status: int | None, error: Exception | None) -> FailType:
    """将 HTTP 状态码 / 异常映射为失败类型。"""
    if isinstance(error, httpx.TimeoutException):
        return FailType.TIMEOUT

    if http_status:
        if 500 <= http_status <= 599:
            return FailType.HTTP_5XX
        if http_status == 429:
            return FailType.HTTP_429
        if http_status == 403:
            return FailType.HTTP_403_ANTI_BOT
        if http_status in (404, 410):
            return FailType.HTTP_404_410

    if error is not None:
        error_str = str(error).lower()
        if "name or service not known" in error_str or "getaddrinfo" in error_str:
            return FailType.DNS_ERROR

    return FailType.UNKNOWN


# ── 公共工具 ──────────────────────────────────────────

def _compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _extract_text(html: str, level: int = 0) -> str:
    """从 HTML 中提取正文文本。

    level: 0/1 — BeautifulSoup 去标签（默认，零成本；level 仅保留兼容上游换 UA 语义）。
    JS 渲染路径由 _playwright_fetch()（异步）直接处理，此处不涉及 level>=2 分支。
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separators="\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)
    except ImportError:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


# ── 结构指纹 ──────────────────────────────────────────

def _structure_fingerprint(html: str) -> str:
    """提取 DOM 骨架指纹（仅标签名 + class + id，去文本内容）。

    指纹变化 + 内容长度骤降 → 页面改版，提取规则可能失效。
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        # 清空所有文本节点，只保留标签骨架
        for text_node in soup.find_all(string=True):
            text_node.replace_with("")

        skeleton = str(soup)
        return hashlib.sha256(skeleton.encode("utf-8")).hexdigest()
    except Exception:
        return ""


async def _get_previous_fingerprint(conn, source_id: str) -> str | None:
    """获取该源最近一次成功采集的结构指纹。"""
    row = await conn.fetchrow(
        f"""SELECT metadata->>'structure_fingerprint' as fp
            FROM {SCHEMA}.collection_runs
            WHERE source_id = $1 AND status = 'success'
            ORDER BY id DESC LIMIT 1""",
        source_id,
    )
    return row["fp"] if row and row["fp"] else None


# ── LLM 内容质量裁判 ──────────────────────────────────

async def _llm_quality_judge(url: str, title: str, text: str) -> dict:
    """调用 deepseek-v4-flash 判定内容质量。

    输入：URL + 标题 + 提取文本前 2000 字符
    输出：{is_valid, content_type, quality_level, readable_summary, should_retry, retry_suggestion}
    """
    # 内容太短时跳过 LLM 判定，直接用规则
    if len(text) < 100:
        return {
            "is_valid": False,
            "content_type": "empty",
            "quality_level": "D",
            "readable_summary": "内容过短",
            "should_retry": True,
            "retry_suggestion": "换 UA",
        }

    api_key = cfg.get_deepseek_key()
    model = cfg.get_quality_judge_model()

    if not api_key:
        logger.warning("LLM quality judge: no API key, default accept")
        return {
            "is_valid": True,
            "content_type": "article",
            "quality_level": "B",
            "readable_summary": "未判定（无 API key）",
            "should_retry": False,
            "retry_suggestion": "",
        }

    prompt = (
        "判定以下网页采集内容的质量。\n\n"
        f"URL: {url}\n"
        f"标题: {title}\n"
        f"正文（前 2000 字符）:\n{text[:2000]}\n\n"
        "返回 JSON（仅 JSON，不要其他内容）：\n"
        '{"is_valid": true/false, '
        '"content_type": "article|login_page|js_shell|empty|anti_bot|error_page|low_quality", '
        '"quality_level": "A|B|C|D", '
        '"readable_summary": "一句话摘要", '
        '"should_retry": true/false, '
        '"retry_suggestion": "换 UA|加 Referer|换时段|不需要"}'
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{cfg.get_deepseek_base_url()}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2048,
                    "temperature": 0,
                },
            )
            if resp.is_success:
                raw = resp.json()["choices"][0]["message"]["content"].strip()
                # 提取 JSON（处理 markdown code block 包裹）
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                result = json.loads(raw)
                return {
                    "is_valid": result.get("is_valid", True),
                    "content_type": result.get("content_type", "article"),
                    "quality_level": result.get("quality_level", "B"),
                    "readable_summary": result.get("readable_summary", ""),
                    "should_retry": result.get("should_retry", False),
                    "retry_suggestion": result.get("retry_suggestion", ""),
                }
    except Exception as e:
        logger.warning(f"LLM quality judge failed: {e}")

    # LLM 不可达时默认放行
    return {
        "is_valid": True,
        "content_type": "article",
        "quality_level": "C",
        "readable_summary": "LLM 判定不可达，默认放行",
        "should_retry": False,
        "retry_suggestion": "",
    }


# ── 采集核心 ──────────────────────────────────────────

async def get_sources_for_today(conn, source_ids: list[str] | None = None) -> list[dict]:
    """获取当日应采集的知识源列表。"""
    today_dow = datetime.now().weekday()  # 0=Mon ... 6=Sun

    if source_ids:
        placeholders = ", ".join(f"${i+1}" for i in range(len(source_ids)))
        rows = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.sources WHERE enabled = TRUE AND id IN ({placeholders})",
            *source_ids,
        )
    else:
        mode = cfg.get_collect_mode()
        if mode == "daily-all":
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.sources WHERE enabled = TRUE"
            )
        else:
            rows = await conn.fetch(
                f"SELECT * FROM {SCHEMA}.sources WHERE enabled = TRUE AND day_of_week = $1",
                today_dow,
            )

    return [dict(r) for r in rows]


async def _is_duplicate(conn, content_hash: str, source_id: str | None = None) -> bool:
    """时间窗口去重：检查同一源在 dedup_ttl_days 天内是否有相同哈希的成功采集。

    注意：不再查 knowledge_items 表做永久去重 —— 永久去重由质量门的 gate_dedup 负责。
    这里只做采集阶段的短期去重，允许同一页面在窗口过期后重新采集。
    """
    ttl_days = cfg.get_dedup_ttl_days()
    if source_id:
        existing = await conn.fetchval(
            f"""SELECT id FROM {SCHEMA}.collection_runs
                WHERE source_id = $1 AND content_hash = $2 AND status IN ('success', 'duplicate')
                AND finished_at > NOW() - make_interval(days => $3)
                ORDER BY id DESC LIMIT 1""",
            source_id, content_hash, ttl_days,
        )
    else:
        existing = await conn.fetchval(
            f"""SELECT id FROM {SCHEMA}.collection_runs
                WHERE content_hash = $1 AND status IN ('success', 'duplicate')
                AND finished_at > NOW() - make_interval(days => $2)
                ORDER BY id DESC LIMIT 1""",
            content_hash, ttl_days,
        )
    return existing is not None


async def _fetch_source(conn, source: dict, dry_run: bool) -> dict:
    """采集单个知识源，带自适应重试、LLM 质量裁判和结构指纹检测。"""
    source_id = source["id"]
    url = source["url"]

    if dry_run:
        return {
            "source_id": source_id,
            "status": "dry_run",
            "fail_type": None,
            "retry_count": 0,
            "quality": None,
            "fingerprint_changed": False,
            "content_hash": None,
            "content_size": None,
            "error": None,
        }

    run_id = await conn.fetchval(
        f"INSERT INTO {SCHEMA}.collection_runs (source_id, started_at, status) VALUES ($1, NOW(), 'running') RETURNING id",
        source_id,
    )

    fail_type = None
    retry_count = 0
    last_error = None
    extraction_level = 0

    # 解析源级配置（requires_js + 自定义 headers）
    source_requires_js = source.get("requires_js", False)
    source_custom_headers = _get_source_headers(source)

    prev_fp = await _get_previous_fingerprint(conn, source_id)
    last_proxy: str | None = None

    for attempt in range(5):  # 安全上限，实际由 _RETRY_POLICY 控制
        # 每次请求轮换 UA
        ua = _pick_ua()
        # 403 重试时强制切换代理
        proxy = _pick_proxy(exclude=last_proxy if fail_type == FailType.HTTP_403_ANTI_BOT else None)

        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        if retry_count > 0:
            headers["Referer"] = "/".join(url.rstrip("/").split("/")[:-1]) or url

        # 合并源自定义 headers（覆盖默认值）
        if source_custom_headers:
            headers.update(source_custom_headers)

        try:
            # P2 (R11): Playwright 分支原以 extraction_level>=2 触发，但 extraction_level 只能升到 1，
            # 分支永不可达 → requires_js 源永远走 HTTP、JS 内容缺失。改用源声明的 requires_js 直达。
            use_playwright = source_requires_js
            if use_playwright:
                raw, text_content = await _playwright_fetch(url, PLAYWRIGHT_TIMEOUT)
                http_status = 200 if raw else 503
            else:
                http_status, raw = await _http_fetch(url, headers, TIMEOUT, proxy, MAX_SIZE)

            if http_status == 200:
                # HTTP 路径需要提取文本，Playwright 已在内置提取
                if not use_playwright:
                    text_content = _extract_text(raw, level=extraction_level)

                # 空响应检测
                if len(text_content) < 100:
                    fail_type = FailType.EMPTY_RESPONSE
                    policy = _RETRY_POLICY[fail_type]
                    if retry_count < policy[0]:
                        retry_count += 1
                        await asyncio.sleep(policy[1])
                        continue
                    last_error = "empty response (< 100 chars)"
                    break

                # LLM 内容质量裁判
                quality = await _llm_quality_judge(url, source.get("name", ""), text_content)

                # 内容无效时根据 LLM 建议是否重试
                if not quality["is_valid"] and quality["should_retry"] and retry_count < 2:
                    logger.info(
                        f"LLM judge rejected {source_id} ({quality['content_type']}), "
                        f"retrying: {quality['retry_suggestion']}"
                    )
                    retry_count += 1
                    if quality["retry_suggestion"] == "换 UA":
                        extraction_level = max(extraction_level, 1)
                    await asyncio.sleep(10)
                    continue

                # 结构指纹
                curr_fp = _structure_fingerprint(raw)
                fp_changed = bool(prev_fp and curr_fp and curr_fp != prev_fp)

                if fp_changed:
                    prev_len = await _get_previous_content_length(conn, source_id)
                    if prev_len and len(text_content) < prev_len * 0.5:
                        logger.warning(
                            f"Structure fingerprint changed + content length dropped "
                            f"({prev_len} → {len(text_content)}) for {source_id} — possible page redesign"
                        )

                # 去重（时间窗口内，同一源同哈希跳过）
                content_hash = _compute_hash(raw)
                if await _is_duplicate(conn, content_hash, source_id):
                    await _finish_run(conn, run_id, "duplicate", http_status,
                        content_hash=content_hash, content_size=len(raw),
                        fail_type="duplicate",
                        metadata={"text_length": len(text_content), "text_content": text_content[:20000],
                                  "quality": quality,
                                  "structure_fingerprint": curr_fp, "fingerprint_changed": fp_changed})
                    await _update_source_status(conn, source_id, "success")
                    return {
                        "source_id": source_id, "status": "duplicate",
                        "fail_type": "duplicate", "retry_count": retry_count,
                        "quality": quality, "fingerprint_changed": fp_changed,
                        "content_hash": content_hash, "content_size": len(raw), "error": None,
                    }

                # 保存文件
                reports_dir = os.path.join(BASE_DIR, "reports", datetime.now().strftime("%Y-%m-%d"))
                os.makedirs(reports_dir, exist_ok=True)
                safe_id = source_id.replace("/", "_")
                raw_path = os.path.join(reports_dir, f"{safe_id}.html")
                with open(raw_path, "w", encoding="utf-8") as f:
                    f.write(raw)
                text_path = os.path.join(reports_dir, f"{safe_id}.txt")
                with open(text_path, "w", encoding="utf-8") as f:
                    f.write(text_content)

                await _finish_run(conn, run_id, "success", http_status,
                    content_hash=content_hash, content_size=len(raw), raw_path=raw_path,
                    fail_type=None,
                    metadata={
                        "text_length": len(text_content),
                        "text_content": text_content[:20000],  # 截断存储作为文件缺失时的回退
                        "retry_count": retry_count,
                        "extraction_level": extraction_level,
                        "quality": quality,
                        "structure_fingerprint": curr_fp,
                        "fingerprint_changed": fp_changed,
                    })
                await _update_source_status(conn, source_id, "success")

                return {
                    "source_id": source_id, "status": "success",
                    "fail_type": None, "retry_count": retry_count,
                    "quality": quality, "fingerprint_changed": fp_changed,
                    "content_hash": content_hash, "content_size": len(raw), "error": None,
                }

            # ── HTTP 非 200：分类 + 重试 ──────────────────
            fail_type = _classify_failure(http_status, None)
            policy = _RETRY_POLICY[fail_type]
            last_error = f"HTTP {http_status}"

            if retry_count >= policy[0]:
                break

            # 403 反爬：记录当前代理以便下次重试时切换
            if fail_type == FailType.HTTP_403_ANTI_BOT:
                last_proxy = proxy
                logger.info(
                    f"403 anti-bot for {source_id}, "
                    f"will retry with different proxy/UA"
                )

            delay = policy[1] * (policy[2] ** retry_count) if policy[2] > 1 else policy[1]
            logger.warning(
                f"Retry {retry_count + 1}/{policy[0]} for {source_id} "
                f"({fail_type.value}): waiting {delay}s"
            )
            await asyncio.sleep(delay)
            retry_count += 1

        except httpx.TimeoutException:
            fail_type = FailType.TIMEOUT
            policy = _RETRY_POLICY[fail_type]
            last_error = "timeout"
            if retry_count < policy[0]:
                delay = policy[1] * (policy[2] ** retry_count)
                logger.warning(f"Timeout for {source_id}, retry {retry_count + 1}/{policy[0]} in {delay}s")
                await asyncio.sleep(delay)
                retry_count += 1
            else:
                break

        except Exception as e:
            fail_type = _classify_failure(None, e)
            policy = _RETRY_POLICY[fail_type]
            last_error = str(e)
            if retry_count < policy[0]:
                await asyncio.sleep(policy[1])
                retry_count += 1
            else:
                break

    # ── 所有重试耗尽 ──────────────────────────────────
    fail_type = fail_type or FailType.UNKNOWN
    error_msg = last_error or str(fail_type.value)

    await _finish_run(conn, run_id, "error",
        error_message=error_msg,
        fail_type=fail_type.value,
        metadata={"retry_count": retry_count})
    await _update_source_status(conn, source_id, "error", fail_type=fail_type.value)

    return {
        "source_id": source_id, "status": "error",
        "fail_type": fail_type.value, "retry_count": retry_count,
        "quality": None, "fingerprint_changed": False,
        "content_hash": None, "content_size": None, "error": error_msg,
    }


# ── 数据库辅助 ────────────────────────────────────────

async def _finish_run(conn, run_id: int, status: str,
                      http_status: int = None, content_hash: str = None,
                      content_size: int = None, raw_path: str = None,
                      error_message: str = None, fail_type: str = None,
                      metadata: dict = None):
    meta = dict(metadata or {})
    if fail_type:
        meta["fail_type"] = fail_type
    await conn.execute(
        f"UPDATE {SCHEMA}.collection_runs SET finished_at=NOW(), status=$1, http_status=$2, "
        f"content_hash=$3, content_size=$4, raw_path=$5, error_message=$6, metadata=$7 WHERE id=$8",
        status, http_status, content_hash, content_size, raw_path, error_message,
        json.dumps(meta, ensure_ascii=False), run_id,
    )


async def _update_source_status(conn, source_id: str, status: str, fail_type: str = None):
    """更新源状态，处理连续失败自适应逻辑。"""
    if status == "error":
        await conn.execute(
            f"UPDATE {SCHEMA}.sources SET last_status=$1, last_fetched_at=NOW(), "
            f"consecutive_errors=consecutive_errors+1, updated_at=NOW() WHERE id=$2",
            status, source_id,
        )
    else:
        # 恢复：如果之前被自动禁用，则重新启用
        row = await conn.fetchrow(
            f"SELECT enabled, consecutive_errors FROM {SCHEMA}.sources WHERE id = $1",
            source_id,
        )
        if row and not row["enabled"] and (row["consecutive_errors"] or 0) > 0:
            await conn.execute(
                f"UPDATE {SCHEMA}.sources SET enabled=TRUE, last_status=$1, "
                f"last_fetched_at=NOW(), consecutive_errors=0, updated_at=NOW() WHERE id=$2",
                status, source_id,
            )
            logger.info(f"Source {source_id} auto re-enabled after recovery")
        else:
            await conn.execute(
                f"UPDATE {SCHEMA}.sources SET last_status=$1, last_fetched_at=NOW(), "
                f"consecutive_errors=0, updated_at=NOW() WHERE id=$2",
                status, source_id,
            )

    # 连续 3 天失败 → 日报标红（日志告警）
    if status == "error":
        errors = await conn.fetchval(
            f"SELECT consecutive_errors FROM {SCHEMA}.sources WHERE id = $1", source_id
        )
        if errors and errors >= 3:
            await conn.execute(
                f"UPDATE {SCHEMA}.sources SET reputation = GREATEST(0.1, reputation - 0.1) WHERE id = $1",
                source_id,
            )
            logger.warning(
                f"Source {source_id}: {errors} consecutive errors (fail_type={fail_type}) — red flag"
            )
        # 连续 7 天失败 → 自动禁用
        if errors and errors >= 7:
            await conn.execute(
                f"UPDATE {SCHEMA}.sources SET enabled = FALSE WHERE id = $1",
                source_id,
            )
            logger.error(
                f"Source {source_id}: auto-disabled after {errors} consecutive errors"
            )


async def _get_previous_content_length(conn, source_id: str) -> int | None:
    row = await conn.fetchrow(
        f"""SELECT content_size, metadata->>'text_length' as text_length
            FROM {SCHEMA}.collection_runs
            WHERE source_id = $1 AND status = 'success'
            ORDER BY id DESC LIMIT 1""",
        source_id,
    )
    if not row:
        return None
    tl = row["text_length"]
    return int(tl) if tl else (row["content_size"] or None)


# ── 入口 ──────────────────────────────────────────────

async def run_collect(dry_run: bool = False, source_ids: list[str] | None = None) -> dict:
    """执行知识采集，返回采集汇总（含失败类型分布和质量评级）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        sources = await get_sources_for_today(conn, source_ids)
        results = []

        for i, source in enumerate(sources):
            # 源间延迟（防速率限制），首个源不延迟
            if i > 0 and not dry_run:
                delay = REQUEST_DELAY + random.uniform(0, REQUEST_DELAY_JITTER)
                await asyncio.sleep(delay)
            result = await _fetch_source(conn, source, dry_run)
            results.append(result)

        collected = sum(1 for r in results if r["status"] == "success")
        failed = sum(1 for r in results if r["status"] == "error")
        duplicates = sum(1 for r in results if r["status"] == "duplicate")

        # 失败类型分布
        fail_dist: dict[str, int] = {}
        for r in results:
            ft = r.get("fail_type")
            if ft:
                fail_dist[ft] = fail_dist.get(ft, 0) + 1

        # 质量评级分布
        quality_dist: dict[str, int] = {}
        for r in results:
            q = r.get("quality") or {}
            ql = q.get("quality_level", "?")
            quality_dist[ql] = quality_dist.get(ql, 0) + 1

        return {
            "sources_total": len(sources),
            "sources_collected": collected,
            "sources_failed": failed,
            "sources_duplicate": duplicates,
            "fail_distribution": fail_dist,
            "quality_distribution": quality_dist,
            "results": results,
        }
