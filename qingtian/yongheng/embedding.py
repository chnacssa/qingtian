"""本地 fastembed / DashScope API 双模嵌入 + 异步队列 + 补偿轮询。"""

import asyncio
import logging
import os
import json
import threading
import httpx

from . import config as cfg

logger = logging.getLogger("yongheng")

_embed_model = None
_model_lock = threading.Lock()


# ── fastembed (本地 ONNX) ────────────────────────────────────────────

def _get_model():
    global _embed_model
    if _embed_model is None:
        with _model_lock:
            if _embed_model is None:
                from fastembed import TextEmbedding
                _embed_model = TextEmbedding(
                    model_name=cfg.get_embedding_model(),
                    cache_dir=cfg.get_embedding_cache_path(),
                    threads=1,  # 单线程避免 ONNX 内部锁竞争
                )
    return _embed_model


def _embed_sync_fastembed(content: str) -> list[float] | None:
    try:
        with _model_lock:
            model = _get_model()
            embeddings = list(model.embed([content]))
        if embeddings and len(embeddings) > 0:
            return embeddings[0].tolist()
    except Exception as e:
        logger.error(f"fastembed inference failed: {e}")
    return None


# ── DashScope API ─────────────────────────────────────────────────────

def _embed_sync_dashscope(content: str) -> list[float] | None:
    """通过 DashScope text-embedding API 获取向量。"""
    api_key = cfg.get_dashscope_api_key()
    if not api_key:
        logger.error("DASHSCOPE_API_KEY not set, DashScope embedding unavailable")
        return None

    url = cfg.get_dashscope_embedding_url()
    model = cfg.get_embedding_model()

    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "input": {"texts": [content]},
            },
            timeout=cfg.get_dashscope_timeout(),
        )
        resp.raise_for_status()
        body = resp.json()

        embeddings = body.get("output", {}).get("embeddings", [])
        if embeddings and len(embeddings) > 0:
            vec = embeddings[0].get("embedding")
            if vec and isinstance(vec, list):
                return vec

        logger.error(
            f"DashScope returned no embedding: "
            f"status={resp.status_code} code={body.get('code')} "
            f"msg={body.get('message')}"
        )
    except httpx.TimeoutException:
        logger.error("DashScope embedding request timed out")
    except Exception as e:
        logger.error(f"DashScope embedding request failed: {e}")
    return None


# ── 统一入口 ──────────────────────────────────────────────────────────

def _embed_sync(content: str) -> list[float] | None:
    """根据 config 中 provider 自动分发。"""
    provider = cfg.get_embedding_provider()
    if provider == "dashscope":
        return _embed_sync_dashscope(content)
    else:
        return _embed_sync_fastembed(content)


def _embed_sync_batch(contents: list[str]) -> list[list[float] | None]:
    """批量嵌入——DashScope 支持单请求多文本，fastembed 逐条。"""
    provider = cfg.get_embedding_provider()
    if provider == "dashscope":
        return _embed_sync_dashscope_batch(contents)
    else:
        return [_embed_sync_fastembed(c) for c in contents]


def _embed_sync_dashscope_batch(contents: list[str]) -> list[list[float] | None]:
    """DashScope 批量嵌入（单请求多文本，减少 API 调用次数）。"""
    api_key = cfg.get_dashscope_api_key()
    if not api_key:
        logger.error("DASHSCOPE_API_KEY not set")
        return [None] * len(contents)

    url = cfg.get_dashscope_embedding_url()
    model = cfg.get_embedding_model()
    max_batch = cfg.get_dashscope_max_batch()

    results: list[list[float] | None] = []

    for i in range(0, len(contents), max_batch):
        batch = contents[i : i + max_batch]
        try:
            resp = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "input": {"texts": batch}},
                timeout=cfg.get_dashscope_timeout(),
            )
            resp.raise_for_status()
            body = resp.json()
            embeddings = body.get("output", {}).get("embeddings", [])
            batch_results = [None] * len(batch)
            for emb in embeddings:
                idx = emb.get("text_index", 0)
                vec = emb.get("embedding")
                if 0 <= idx < len(batch) and isinstance(vec, list):
                    batch_results[idx] = vec
            results.extend(batch_results)
        except Exception as e:
            logger.error(f"DashScope batch embedding failed: {e}")
            results.extend([None] * len(batch))

    return results


# ── warmup ────────────────────────────────────────────────────────────

def warmup():
    try:
        provider = cfg.get_embedding_provider()
        if provider == "dashscope":
            result = _embed_sync_dashscope("warmup")
            if result:
                logger.info(
                    f"DashScope embedding warmed up (dim={len(result)})"
                )
            else:
                logger.warning("DashScope warmup returned None")
        else:
            _get_model()
            _embed_sync_fastembed("warmup")
            logger.info("fastembed model warmed up")
    except Exception as e:
        logger.warning(f"embedding warmup failed: {e}")


# ── 异步队列（不变）───────────────────────────────────────────────────

class EmbeddingQueue:
    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def start(self):
        self._running = True
        asyncio.create_task(self._worker())
        asyncio.create_task(self._compensate())
        asyncio.create_task(self._warmup_async())

    async def _warmup_async(self):
        loop = asyncio.get_running_loop()
        await asyncio.to_thread(warmup)

    async def enqueue(self, memory_id: int, content: str):
        await self._queue.put((memory_id, content))

    async def _worker(self):
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                memory_id, content = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                continue

            for attempt in range(3):
                try:
                    vector = await asyncio.to_thread(_embed_sync, content)
                    if vector is not None:
                        await self._update_vector(memory_id, vector, "done")
                        break
                    raise RuntimeError("embedding returned None")
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep((1, 5, 30)[attempt])
            else:
                # 3 次尝试全部失败：标记 failed（原实现 else 分支不可达，failed 状态从未写入）
                await self._update_vector(memory_id, None, "failed")
                logger.error(
                    f"Embedding failed for memory_id={memory_id} after 3 attempts"
                )
            self._queue.task_done()

    async def _update_vector(
        self, memory_id: int, vector: list[float] | None, status: str
    ):
        from common.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            schema = cfg.get_schema_name()
            if vector:
                expected = cfg.get_embedding_dimension()
                actual = len(vector)
                if actual != expected:
                    # C17 (R11): 维度不匹配必须拦截——memories.embedding 为 vector(512)，
                    # 超维向量写入 DB 直接报错（如 DashScope 1536 vs 列 512 → 全部更新失败）。
                    # 不写向量，标记 failed 供排查，避免状态悬置 pending/假 done。
                    logger.error(
                        f"Vector dimension mismatch for memory_id={memory_id}: "
                        f"expected={expected} actual={actual} "
                        f"(provider={cfg.get_embedding_provider()}) → 标记 failed，不写入"
                    )
                    await conn.execute(
                        f"UPDATE {schema}.memories SET embedding_status = $1 "
                        f"WHERE id = $2",
                        "failed", memory_id,
                    )
                    return
                vec_str = "[" + ",".join(str(v) for v in vector) + "]"
                await conn.execute(
                    f"UPDATE {schema}.memories SET embedding = $1, "
                    f"embedding_status = $2 WHERE id = $3",
                    vec_str, status, memory_id,
                )
            else:
                await conn.execute(
                    f"UPDATE {schema}.memories SET embedding_status = $1 "
                    f"WHERE id = $2",
                    status, memory_id,
                )

    async def _compensate(self):
        while self._running:
            await asyncio.sleep(300)
            try:
                from common.db import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    schema = cfg.get_schema_name()
                    rows = await conn.fetch(
                        f"SELECT id, content FROM {schema}.memories "
                        "WHERE embedding_status = 'pending' "
                        "AND created_at < NOW() - INTERVAL '2 minutes' "
                        "LIMIT 100"
                    )
                    for row in rows:
                        await self._queue.put((row["id"], row["content"]))
                    if rows:
                        logger.info(f"Compensated {len(rows)} pending embeddings")
            except Exception as e:
                logger.error(f"Compensation poll failed: {e}")

    async def stop(self):
        self._running = False


embedding_queue = EmbeddingQueue()


async def embed_text(content: str, timeout: float = 30.0) -> list[float] | None:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_embed_sync, content),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error(f"embed_text timed out after {timeout}s")
        return None
