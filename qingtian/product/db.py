"""
产品目录模块 — 本地数据库连接池，不依赖 common.db
"""

import asyncio
import json
import os

import asyncpg

_pool = None
_pool_lock = asyncio.Lock()


def _env(key: str, default=None):
    return os.environ.get(f"QINGTIAN_{key}", default)


def _jsonb_encode(v):
    """jsonb 编码（幂等）：dict/list 直接序列化；str 参数若已是 JSON（老代码
    json.dumps 后传 str 的双重编码写法）→ 还原后再序列化，兼容两种调用。
    review(2026-08-16): 原 encoder=json.dumps 对预序列化 str 再 dumps 一次 →
    DB 存 JSON 字符串字面量而非对象/数组（technical_params/product_spec 等全部失效）。"""
    if isinstance(v, str) and v.lstrip().startswith(("{", "[")):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            pass
    return json.dumps(v, ensure_ascii=False)


def _jsonb_decode(v):
    """jsonb 解码（幂等）：正常 jsonb 对象/数组 → 原样；双重编码存量数据
    （库里存 JSON 字符串字面量）→ 再解一层还原 dict/list，读取自愈无需回填。"""
    v = json.loads(v)
    if isinstance(v, str) and v.lstrip().startswith(("{", "[")):
        try:
            v = json.loads(v)
        except (ValueError, TypeError):
            pass
    return v


async def _init_connection(conn):
    await conn.set_type_codec(
        "jsonb", encoder=_jsonb_encode, decoder=_jsonb_decode,
        schema="pg_catalog",
    )


async def get_pool():
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    host=_env("DB_HOST", "localhost"),
                    port=int(_env("DB_PORT", "5432")),
                    database=_env("DB_NAME", "qingtian"),
                    user=_env("DB_USER", "qingtian"),
                    password=_env("DB_PASSWORD"),
                    min_size=1,
                    max_size=3,
                    max_inactive_connection_lifetime=300,
                    timeout=10,
                    init=_init_connection,
                )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
