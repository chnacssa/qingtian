"""寰宇 — E2EE 消息封装（企业端 + Hub 端共用）。

封装消息级加解密，含 AAD 拼装（协议细节见技术落地文档 §4.1/4.2）。
底层原语在 crypto.py；本模块只拼装协议字段、不碰底层算法。

- 在线消息：会话密钥（HKDF 派生，有 PFS）+ AAD 绑收发方
- 离线消息：每条独立一次性密钥（贪狼 A3），B 静态公钥加密、o_priv 用完即焚
- 信封：跨企业消息统一信封 + 企业 Ed25519 签名（Hub/接收方验来源，P0-3 接线补）

2026-08-20 贪狼接线：新增信封构造/验签（build_envelope / sign_envelope /
verify_envelope / verify_envelope_schema），对齐技术落地文档 §4.1。
"""

import time
from datetime import datetime, timezone

from . import crypto

# 信封常量（钉死，勿改）
ENVELOPE_LABEL = b"huanyu-e2ee-v1"
ENVELOPE_TYPES = ("msg", "hello", "hello_ack", "error", "end_ack")


def build_aad(from_org: str, to_org: str, from_ain: str, to_ain: str, ts: str) -> bytes:
    """AAD = from_org‖to_org‖from_ain‖to_ain‖ts，防密文重放到别的 (from,to) 组合。"""
    return "‖".join([from_org, to_org, from_ain, to_ain, ts]).encode()


# ── 在线消息（会话密钥，有 PFS）────────────────────────

def encrypt_message(session_key: bytes, plaintext: bytes,
                    from_org: str, to_org: str, from_ain: str, to_ain: str, ts: str) -> dict:
    """加密消息体 → {iv, tag, cipher}。"""
    aad = build_aad(from_org, to_org, from_ain, to_ain, ts)
    return crypto.aes_gcm_encrypt(session_key, plaintext, aad)


def decrypt_message(session_key: bytes, enc: dict,
                    from_org: str, to_org: str, from_ain: str, to_ain: str, ts: str) -> bytes:
    """解密消息体 → 明文 bytes。认证失败（篡改/AAD 不匹配）抛异常。"""
    aad = build_aad(from_org, to_org, from_ain, to_ain, ts)
    return crypto.aes_gcm_decrypt(session_key, enc["iv"], enc["tag"], enc["cipher"], aad)


# ── 离线消息（B 静态公钥，每条独立一次性密钥）───────────

def encrypt_offline_message(x_static_pub: bytes, plaintext: bytes,
                            from_org: str, to_org: str, msg_nonce: str) -> dict:
    """离线消息加密（贪狼 A3）。

    - 每条生成独立 (o_priv, o_pub)，o_priv 用完即焚（不返回、不留存）
    - msg_key = HKDF(X25519(o_priv, B_static_pub), label, from_org‖to_org‖msg_nonce)
    - 返回 {o_pub, iv, tag, cipher}，o_pub 随消息一起给 B（B 用自己静态私钥解）
    """
    o_priv, o_pub = crypto.generate_x25519_keypair()
    shared = crypto.x25519_derive(o_priv, x_static_pub)
    info = f"{from_org}‖{to_org}‖{msg_nonce}".encode()
    msg_key = crypto.hkdf_sha256(shared, crypto.PROTOCOL_LABEL, info)
    aad = f"{from_org}‖{to_org}".encode()
    enc = crypto.aes_gcm_encrypt(msg_key, plaintext, aad)
    # o_priv 此处自然出作用域，由 GC 回收（用完即焚）
    return {
        "o_pub": o_pub.hex(),
        "iv": enc["iv"],
        "tag": enc["tag"],
        "cipher": enc["cipher"],
    }


def decrypt_offline_message(x_static_priv: bytes, o_pub_hex: str, enc: dict,
                            from_org: str, to_org: str, msg_nonce: str) -> bytes:
    """离线消息解密（B 用自己静态私钥 + 消息里的 o_pub）。"""
    o_pub = bytes.fromhex(o_pub_hex)
    shared = crypto.x25519_derive(x_static_priv, o_pub)
    info = f"{from_org}‖{to_org}‖{msg_nonce}".encode()
    msg_key = crypto.hkdf_sha256(shared, crypto.PROTOCOL_LABEL, info)
    aad = f"{from_org}‖{to_org}".encode()
    return crypto.aes_gcm_decrypt(msg_key, enc["iv"], enc["tag"], enc["cipher"], aad)


# ── 信封（跨企业统一信封 + 企业 Ed25519 签名）────────────────

def _envelope_sig_bytes(env: dict) -> bytes:
    """钉死验签原文：label ‖ v ‖ type ‖ from_org ‖ to_org ‖ from_ain ‖ to_ain ‖ nonce ‖ ts ‖ body ‖ msg_id。

    覆盖信封全部字段（含路由目标 from_ain/to_ain，防 Hub/中间人篡改投递目标；
    含 msg_id，可靠投递幂等标识，防篡改去重键）。
    任何字段顺序/分隔符改动都会破坏签名一致性——此处即协议，勿改。
    """
    return ENVELOPE_LABEL + "‖".join([
        env.get("v", ""), env.get("type", ""),
        env.get("from_org", ""), env.get("to_org", ""),
        env.get("from_ain", ""), env.get("to_ain", ""),
        env.get("nonce", ""), env.get("ts", ""), env.get("body", ""),
        env.get("msg_id", ""), env.get("seq", ""),
    ]).encode()


def sign_envelope(org_sign_priv_hex: str, env: dict) -> str:
    """企业 Ed25519 签名信封 → 写 env["sig"] 并返回。私钥 hex（不落 git）。"""
    env["sig"] = crypto.sign(org_sign_priv_hex, _envelope_sig_bytes(env))
    return env["sig"]


def verify_envelope(org_pub_hex: str, env: dict) -> bool:
    """验企业信封签名。签名缺失/字段非法/不匹配 → False。

    Hub 与接收方都靠它认证消息来源（篡改 from_org/body 一律被拒）。
    """
    sig = env.get("sig", "")
    if not sig or not isinstance(sig, str):
        return False
    try:
        return crypto.verify(org_pub_hex, _envelope_sig_bytes(env), sig)
    except Exception:
        return False


def verify_envelope_schema(env: dict) -> bool:
    """信封结构校验：必填字段 + 类型 + 长度守卫，防畸形信封进处理链。"""
    if not isinstance(env, dict):
        return False
    if env.get("v") != "1":
        return False
    if env.get("type") not in ENVELOPE_TYPES:
        return False
    for f in ("from_org", "to_org", "from_ain", "to_ain", "nonce", "ts", "body", "sig"):
        if not isinstance(env.get(f), str) or not env.get(f):
            return False
    # 长度守卫：org/ain 限长，body 限长，防超长畸形信封占满内存
    for f in ("from_org", "to_org", "from_ain", "to_ain"):
        if len(env[f]) > 128:
            return False
    if len(env["nonce"]) > 64 or len(env["ts"]) > 64:
        return False
    if len(env.get("msg_id", "")) > 128 or len(env.get("seq", "")) > 20:
        return False
    if len(env["body"]) > 1024 * 1024:   # 1MB 封顶
        return False
    return True


def build_envelope(from_org: str, to_org: str, from_ain: str, to_ain: str,
                   body_b64: str, org_sign_priv_hex: str,
                   mtype: str = "msg", nonce: str | None = None,
                   msg_id: str = "", seq: int = 0) -> dict:
    """构造跨企业消息信封 + 企业签名。body 已加密（base64），加密在调用方。

    nonce 可显式传入（发送方需用它同时作为离线 KDF info 时，保证两边一致）。
    msg_id 为原始业务消息 id（可靠投递幂等标识，重试保持不变，接收方据此去重）。
    seq 为 org 内自增序号（resync 对齐点，连续窗口补投）。
    """
    env = {
        "v": "1", "type": mtype,
        "from_org": from_org, "to_org": to_org,
        "from_ain": from_ain, "to_ain": to_ain,
        "nonce": nonce or crypto.generate_msg_nonce(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "body": body_b64,
        "msg_id": msg_id,
        "seq": str(seq),
    }
    sign_envelope(org_sign_priv_hex, env)
    return env


def envelope_ts_valid(ts: str, skew_seconds: int = 300) -> bool:
    """信封时间戳 ±5min 窗口校验。格式非法/超窗 → False（防旧消息重放）。"""
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return abs(age) <= skew_seconds


class NonceLRU:
    """进程内 nonce 去重 LRU（TTL 默认 10min，容量上限 20000）。

    企业端用（本底座只收一次）；Hub 端用 Redis SET NX EX（跨 worker 权威）。
    信封 nonce 是 hex（非 QACP 时间戳格式），不能复用 replay_guard 的
    Bloom 滑动窗口（其 _validate_timestamp 会把 hex nonce 判为无效）。
    """

    def __init__(self, ttl_seconds: int = 600, maxlen: int = 20000):
        self._ttl = ttl_seconds
        self._maxlen = maxlen
        self._d: dict[str, float] = {}

    def seen(self, nonce: str) -> bool:
        """记录并判断是否已见。返回 True=重放拒绝，False=首次放行。"""
        if not nonce:
            return True
        now = time.monotonic()
        prev = self._d.get(nonce)
        if prev is not None and now - prev < self._ttl:
            return True
        # 容量守卫：超过上限先清过期，仍超则整体丢弃最旧一半（有序字典语义）
        if len(self._d) >= self._maxlen:
            stale = [k for k, t in self._d.items() if now - t >= self._ttl]
            for k in stale:
                del self._d[k]
            if len(self._d) >= self._maxlen:
                self._d = dict(list(self._d.items())[-self._maxlen // 2:])
        self._d[nonce] = now
        return False
