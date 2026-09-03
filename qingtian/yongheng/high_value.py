"""高价值检测 —— 关键词快速扫描 + LLM 语义兜底。"""

import asyncio
import logging
import time

logger = logging.getLogger("yongheng")

_HIGH_VALUE_KEYWORDS = [
    "决定", "决策", "选定", "确认采用", "批准", "否决", "暂缓",
    "报价", "价格", "报价单", "合同金额",
    "配置变更", "修改配置", "端口变更", "部署", "迁移",
    "修复", "回滚", "恢复", "紧急",
    "风险", "产能不足", "缺货", "延迟", "质量问题",
]

_LLM_QUEUE: asyncio.Queue = asyncio.Queue()
_LLM_RUNNING = False
_LLM_LAST_CALL: dict[str, float] = {}


def keyword_scan(content: str) -> bool:
    return any(kw in content for kw in _HIGH_VALUE_KEYWORDS)


async def llm_semantic_check(content: str) -> bool:
    import httpx
    from . import config as cfg

    prompt = (
        "判断以下内容是否是高价值事件（决策、报价、配置变更、部署、修复、风险发现）。"
        "仅回复 YES 或 NO。\n\n"
        f"内容：{content}"
    )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{cfg.get_llm_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.get_llm_api_key()}"},
                json={
                    "model": cfg.get_llm_high_value_model(),
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 5,
                    "temperature": 0,
                },
                timeout=15,
            )
            resp.raise_for_status()
            answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
            return answer.startswith("YES")
    except Exception as e:
        logger.warning(f"LLM semantic check failed: {e}")
        return False


def _consume_worker_exception(task: asyncio.Task) -> None:
    """消费 worker 任务的异常，防止 "Task exception was never retrieved" 告警。"""
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        return
    if exc is not None:
        logger.warning("LLM high-value worker exited with error: %s", exc)


def start_llm_worker():
    global _LLM_RUNNING
    if _LLM_RUNNING:
        return
    _LLM_RUNNING = True
    task = asyncio.create_task(_llm_worker())
    task.add_done_callback(_consume_worker_exception)


async def _llm_worker():
    global _LLM_RUNNING
    try:
        while _LLM_RUNNING:
            try:
                namespace, memory_id, content = await asyncio.wait_for(_LLM_QUEUE.get(), timeout=10.0)
            except asyncio.TimeoutError:
                continue
            try:
                _LLM_LAST_CALL[namespace] = time.time()
                is_hv = await llm_semantic_check(content)
                if is_hv:
                    from common.db import get_pool
                    from . import config as cfg
                    pool = await get_pool()
                    async with pool.acquire() as conn:
                        await conn.execute(
                            f"UPDATE {cfg.get_schema_name()}.memories SET memory_type = 'high_value', protected = TRUE WHERE id = $1",
                            memory_id,
                        )
            except Exception as e:
                logger.warning(f"LLM high-value check failed for memory {memory_id}: {e}")
            finally:
                _LLM_QUEUE.task_done()
    finally:
        # P2 (R11): worker 异常/退出时复位标志，否则 _LLM_RUNNING 恒 True 且无 worker 在跑，
        # 后续 start_llm_worker() 直接 return，高价值检测永久卡死。
        _LLM_RUNNING = False


def enqueue_llm_check(namespace: str, memory_id: int, content: str):
    # 节流：每 namespace 30s 内只做一次 LLM 语义检查。窗口内直接跳过排队，
    # 避免 worker 取出后丢弃（原实现对已入队的检查静默丢弃）。
    if time.time() - _LLM_LAST_CALL.get(namespace, 0) < 30:
        return
    _LLM_QUEUE.put_nowait((namespace, memory_id, content))
