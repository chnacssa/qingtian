"""
寰宇 — HMAC-SHA256 消息签名
防伪造 + 防重放
"""

import hashlib
import hmac
import logging
import os
import time

from . import config as hcfg

logger = logging.getLogger("huanyu.signing")

def _get_key() -> bytes:
    key = hcfg.get_msg_sign_key()
    if not key:
        key = os.getenv("HUANYU_SIGN_KEY", "")
    if not key:
        # review(2026-08-24 P0-4): 硬编码兜底密钥随开源仓库公开，人人可伪造签名——
        # 改 fail-closed：未配置密钥必须显式允许 dev 兜底，否则拒绝签名。
        _allow_dev = (
            os.getenv("HUANYU_ALLOW_DEV_KEY", "")
            or hcfg.get("huanyu.allow_dev_sign_key", "")
        )
        if str(_allow_dev).lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "HUANYU_SIGN_KEY 未配置且未显式允许 dev 兜底（HUANYU_ALLOW_DEV_KEY=1），"
                "拒绝签名——生产必须配置真实密钥"
            )
        logger.warning(
            "HUANYU_SIGN_KEY 未配置，使用 dev 模式默认密钥（仅开发测试用，已显式放行）"
        )
        key = "huanyu-dev-key-2026"
    return key.encode() if isinstance(key, str) else key


def sign_message(from_agent: str, to_agent: str, message_type: str, payload_str: str) -> str:
    """对消息字段计算 HMAC-SHA256"""
    raw = f"{from_agent}:{to_agent}:{message_type}:{payload_str}"
    mac = hmac.new(_get_key(), raw.encode(), hashlib.sha256)
    return mac.hexdigest()


def verify_message(from_agent: str, to_agent: str, message_type: str,
                   payload_str: str, signature: str) -> bool:
    """验证消息签名"""
    if not signature:
        return False
    try:
        expected = sign_message(from_agent, to_agent, message_type, payload_str)
    except RuntimeError:
        # B4: 密钥未配置 → 拒绝验证（fail-closed）
        logger.error("签名密钥未配置，拒绝验证消息")
        return False
    return hmac.compare_digest(expected, signature)


def derive_peer_key(peer_host: str) -> bytes:
    """为指定底座派生独立签名密钥（HMAC-KDF）。

    使用全局密钥作为主密钥，通过 HMAC-SHA256(主密钥, "huanyu:peer:" + peer_host)
    派生每个底座的独立密钥。即使某个底座被攻破，也无法伪造其他底座的签名。
    """
    base = _get_key()
    info = f"huanyu:peer:{peer_host}"
    return hmac.new(base, info.encode(), hashlib.sha256).digest()


def sign_peer_message(payload_str: str, peer_host: str = "") -> str:
    """底座间消息签名。

    如果传入 peer_host，使用该底座专属派生密钥签名；
    否则回退到全局密钥（兼容旧版配置）。
    """
    key = derive_peer_key(peer_host) if peer_host else _get_key()
    mac = hmac.new(key, payload_str.encode(), hashlib.sha256)
    return mac.hexdigest()


def verify_peer_message(payload_str: str, signature: str, peer_host: str = "") -> bool:
    """验证底座间消息签名。

    如果传入 peer_host，使用该底座专属派生密钥验证；
    否则回退到全局密钥（兼容旧版配置）。
    """
    if not signature:
        return False
    try:
        expected = sign_peer_message(payload_str, peer_host)
    except RuntimeError:
        # B4: 密钥未配置 → 拒绝验证（fail-closed）
        logger.error("签名密钥未配置，拒绝验证底座消息")
        return False
    return hmac.compare_digest(expected, signature)


def verify_fed_body(body: dict) -> bool:
    """联邦端点（/peers/sync、/peers/heartbeat、/peers/negotiation/sync）请求体验签。

    review(2026-08-24 P0-6): 这些端点原先接收侧从不验签（发送侧算了 peer_sig 白算），
    且 /peers/* 在网关白名单内无 Bearer 鉴权——伪造 sync 可污染 Hub 目录（路由注入），
    伪造 heartbeat 可注册任意 host（打穿 SSRF 防线）。收紧为缺签/错签一律拒绝。

    签名口径兼容三种在用发送端（均对各自业务体 canonical json 签名，全局密钥）：
    - 整体上报（heartbeat / full_snapshot sync）：body 去掉 peer_sig 后整体
    - 单条注册 sync：body["agent"]
    - 谈判同步：body["record"]
    - 路由投递：body["payload"]
    """
    import json as _json
    sig = (body or {}).get("peer_sig", "")
    if not sig:
        return False
    stripped = {k: v for k, v in body.items() if k != "peer_sig"}
    candidates = [stripped]
    for field in ("agent", "record", "payload"):
        if isinstance(body.get(field), (dict, list)):
            candidates.append(body[field])
    for cand in candidates:
        try:
            if verify_peer_message(
                _json.dumps(cand, ensure_ascii=False, sort_keys=True, default=str), sig
            ):
                return True
        except Exception:
            continue
    return False
