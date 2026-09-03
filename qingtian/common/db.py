"""
统一数据库连接管理
所有板块通过此模块获取 PostgreSQL 连接池
"""

import asyncio
import json
import os
import asyncpg
from . import config as qcfg

_pool = None
_pool_lock = asyncio.Lock()


def _jsonb_encode(v):
    """jsonb 编码（幂等）：dict/list 直接序列化；str 参数若已是 JSON（老代码
    json.dumps 后传 str 的双重编码写法）→ 还原后再序列化，兼容两种调用。
    review(2026-08-15): 原 encoder=json.dumps 对预序列化 str 再 dumps 一次 →
    DB 存 JSON 字符串字面量而非对象/数组（skills/tags/detail 等全部失效）。"""
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


# asyncpg 0.29+ JSONB codec 默认返回字符串，注册自动反序列化（幂等 codec，
# 修复双重编码：预序列化 str 不再被二次 dumps 落成 JSON 字符串字面量）
async def _init_connection(conn):
    await conn.set_type_codec(
        'jsonb', encoder=_jsonb_encode, decoder=_jsonb_decode,
        schema='pg_catalog',
    )


async def get_pool():
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    host=qcfg.get("database.host", "localhost"),
                    port=qcfg.get("database.port", 5432),
                    database=qcfg.get("database.db", "qingtian"),
                    user=qcfg.get("database.user", "qingtian"),
                    # B3: DB 密码优先从环境变量 QINGTIAN_DB_PASSWORD 读取
                    # （禁止明文入库 config.yaml），未设时回退 config 值供本地开发。
                    password=os.environ.get("QINGTIAN_DB_PASSWORD")
                    or qcfg.get("database.password"),
                    min_size=qcfg.get("database.pool_min_size", 5),
                    max_size=qcfg.get("database.pool_max_size", 30),
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
