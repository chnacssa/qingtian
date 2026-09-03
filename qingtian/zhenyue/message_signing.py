"""消息签名 —— HMAC-SHA256 防伪造 + 重放检测。"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time

from . import config as cfg

logger = logging.getLogger("zhenyue.message_signing")

_nonce_set: set[str] = set()
_nonce_last_clean: float = time.time()

# P1 (2026-08-26 review #4): nonce 原为进程内存 set——uvicorn 多 worker 部署时每进程
# 各一套，攻击者在时间窗内把同一签名消息重放给另一 worker 即绕过重放检测。现优先走
# Redis SETNX+TTL（全局一份，多 worker/多实例一致生效）；Redis 不可用时回退内存 set
#（单 worker 等价旧行为，并记 warning 提示部署形态限制）。
_redis_client = None
_redis_tried: bool = False


def _get_redis():
    """懒加载同步 Redis 客户端；不可用返回 None（每次尝试后缓存结果，避免逐条重连）。"""
    global _redis_client, _redis_tried
    if _redis_tried:
        return _redis_client
    _redis_tried = True
    try:
        import redis  # redis[hiredis]>=5.0 已在 requirements
        _redis_client = redis.Redis.from_url(
            cfg.get_redis_url(), socket_timeout=2, socket_connect_timeout=2,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as e:
        logger.warning("nonce Redis 不可用（%s），回退进程内存 set——多 worker 部署下重放保护弱化", e)
        _redis_client = None
        return None


def check_and_record_nonce(message_id: str) -> bool:
    global _nonce_last_clean
    # 首选：Redis SETNX + TTL（TTL ≥ 时间窗，窗口过后 key 自过期，重放窗关闭）
    client = _get_redis()
    if client is not None:
        try:
            ttl = max(cfg.get_msg_signing_time_window(), 60)
            ok = client.set(f"zhenyue:msg_nonce:{message_id}", 1, nx=True, ex=ttl)
            return bool(ok)
        except Exception as e:
            logger.warning("nonce Redis 写入失败（%s），本条回退内存判定", e)
            # 落到下方内存路径（不缓存失败——Redis 可能瞬时抖动后恢复）

    # 回退：进程内存 set（仅单 worker 语义正确）
    now = time.time()
    # P2 (#6 顺手修): 清空阈值原固定 600s，若 time_window 配 >600s 则重放窗打开；
    # 改为 ≥ 2×time_window，保证窗内 nonce 绝不被清。
    if now - _nonce_last_clean > max(600, 2 * cfg.get_msg_signing_time_window()):
        _nonce_set.clear()
        _nonce_last_clean = now
    if message_id in _nonce_set:
        return False
    _nonce_set.add(message_id)
    return True


def _get_signing_key(key_version: int = 1) -> bytes:
    key_dir = f"{cfg.get_encryption_key_dir()}/signing"
    key_path = f"{key_dir}/v{key_version}.key"
    try:
        with open(key_path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        os.makedirs(key_dir, exist_ok=True)
        key = secrets.token_bytes(32)
        with open(key_path, "wb") as f:
            f.write(key)
        os.chmod(key_path, 0o600)
        return key


def sign_message(from_id: str, to_id: str, message_id: str,
                 timestamp: int, payload: dict, key_version: int = 1) -> str:
    key = _get_signing_key(key_version)
    raw = f"{from_id}:{to_id}:{message_id}:{timestamp}:{json.dumps(payload, sort_keys=True)}"
    return hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()


def verify_message(from_id: str, to_id: str, message_id: str,
                   timestamp: int, payload: dict, signature: str,
                   key_version: int = 1) -> bool:
    if not cfg.get_msg_signing_enabled():
        return True

    now = int(time.time())
    if abs(now - timestamp) > cfg.get_msg_signing_time_window():
        return False

    if not check_and_record_nonce(message_id):
        return False

    expected = sign_message(from_id, to_id, message_id, timestamp, payload, key_version)
    return secrets.compare_digest(expected, signature)
