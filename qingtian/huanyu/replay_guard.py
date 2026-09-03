"""
Nonce 滑动窗口防重放 — Bloom Filter 实现
QACP v0.4 标准：300s 窗口，每秒清洗过期切片，内存 ~3.75 MB
"""

import base64
import hashlib
import os
import struct
import time
from typing import Optional


# ── Nonce 生成 ────────────────────────────────────────

def generate_nonce(peer_id: str = "") -> str:
    """生成 QACP nonce：base64url(timestamp_ms || random_8bytes || peer_hash_4bytes)"""
    ts_ms = int(time.time() * 1000)
    rand = os.urandom(8)
    ts_bytes = struct.pack(">Q", ts_ms)
    if peer_id:
        peer_hash = hashlib.sha256(peer_id.encode()).digest()[:4]
    else:
        peer_hash = os.urandom(4)
    raw = ts_bytes + rand + peer_hash
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_nonce(nonce: str) -> Optional[dict]:
    """解析 nonce，返回 {timestamp_ms, random_hex, peer_hash_hex}；格式错误返回 None"""
    try:
        # 补齐 base64 padding
        pad = 4 - len(nonce) % 4
        if pad != 4:
            nonce += "=" * pad
        raw = base64.urlsafe_b64decode(nonce)
        if len(raw) < 20:
            return None
        ts_ms = struct.unpack(">Q", raw[:8])[0]
        return {
            "timestamp_ms": ts_ms,
            "random_hex": raw[8:16].hex(),
            "peer_hash_hex": raw[16:20].hex(),
        }
    except Exception:
        return None


# ── Bloom Filter（单时间切片）─────────────────────────

class _BloomSlice:
    """单秒 Bloom Filter — 预期 10k 条，误报率 ~0.1%"""

    def __init__(self):
        # m = 100,000 bits = 12,500 bytes，7 个哈希函数
        self._bits = bytearray(12500)
        self._m = 100000
        self._k = 7

    def _hashes(self, data: bytes):
        """返回 k 个哈希位置"""
        h = hashlib.sha256(data)
        digest = h.digest()
        positions = []
        for i in range(self._k):
            seed = digest + struct.pack(">H", i)
            val = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
            positions.append(val % self._m)
        return positions

    def add(self, nonce: str) -> None:
        """将 nonce 写入 Bloom Filter"""
        for pos in self._hashes(nonce.encode()):
            byte_idx = pos >> 3
            bit_idx = pos & 7
            self._bits[byte_idx] |= (1 << bit_idx)

    def contains(self, nonce: str) -> bool:
        """检查 nonce 是否可能存在（可能误报，不会漏报）"""
        for pos in self._hashes(nonce.encode()):
            byte_idx = pos >> 3
            bit_idx = pos & 7
            if not (self._bits[byte_idx] & (1 << bit_idx)):
                return False
        return True

    def clear(self) -> None:
        """清空切片"""
        self._bits = bytearray(12500)


# ── 滑动窗口 ──────────────────────────────────────────

class ReplayGuard:
    """Nonce 滑动窗口防重放 — 300 个 1 秒切片，环形缓冲区

    用法:
        guard = ReplayGuard()

        # 检查 nonce 是否重放
        if guard.is_replay(nonce):
            raise qacp_err.nonce_reused()

        # 记录 nonce
        guard.record(nonce)
    """

    WINDOW_SECONDS = 300  # 5 分钟窗口
    MAX_AGE_MS = WINDOW_SECONDS * 1000  # 允许的最大时钟偏差

    def __init__(self):
        self._slices: list[_BloomSlice] = [_BloomSlice() for _ in range(self.WINDOW_SECONDS)]
        self._slice_ts: list[int] = [0] * self.WINDOW_SECONDS  # 每片的起始秒级时间戳
        self._cursor = 0
        self._last_cleanup = int(time.time())

    def _current_slot(self) -> int:
        """返回当前秒对应的槽位（0-299）"""
        now = int(time.time())
        slot = now % self.WINDOW_SECONDS
        # 如果槽位对应的时间戳变了，清空该切片
        if self._slice_ts[slot] != now:
            self._slices[slot].clear()
            self._slice_ts[slot] = now
        return slot

    def _cleanup_expired(self) -> None:
        """清理过期切片（当前时间往前 WINDOW_SECONDS 之前的）"""
        now = int(time.time())
        if now == self._last_cleanup:
            return
        self._last_cleanup = now
        expire_boundary = now - self.WINDOW_SECONDS
        for i in range(self.WINDOW_SECONDS):
            if self._slice_ts[i] != 0 and self._slice_ts[i] < expire_boundary:
                self._slices[i].clear()
                self._slice_ts[i] = 0

    def _validate_timestamp(self, nonce: str) -> bool:
        """校验 nonce 中的时间戳是否在允许窗口内（±300s）"""
        parsed = parse_nonce(nonce)
        if not parsed:
            return False
        ts_ms = parsed["timestamp_ms"]
        now_ms = int(time.time() * 1000)
        age_ms = now_ms - ts_ms
        # nonce 太老（超过窗口）或来自未来（超过 5s 时钟偏差）
        if age_ms > self.MAX_AGE_MS:
            return False
        if age_ms < -5000:
            return False
        return True

    def is_replay(self, nonce: str) -> bool:
        """检查 nonce 是否已被使用（重放检测）

        Returns True 如果是重放（拒绝），False 如果是新 nonce（放行）
        """
        if not nonce or not self._validate_timestamp(nonce):
            return True  # 无效或过期的 nonce 视为重放

        self._cleanup_expired()

        # 检查所有有效切片
        now_sec = int(time.time())
        for i in range(self.WINDOW_SECONDS):
            ts = self._slice_ts[i]
            if ts == 0:
                continue
            if now_sec - ts > self.WINDOW_SECONDS:
                continue
            if self._slices[i].contains(nonce):
                return True  # 重放
        return False

    def record(self, nonce: str) -> None:
        """记录 nonce 到当前秒切片"""
        if not nonce:
            return
        slot = self._current_slot()
        self._slices[slot].add(nonce)

    def check_and_record(self, nonce: str) -> bool:
        """检查 + 记录，一步完成。返回 True 表示通过（新 nonce），False 表示重放"""
        if self.is_replay(nonce):
            return False
        self.record(nonce)
        return True

    @property
    def memory_estimate(self) -> int:
        """估算内存占用（字节）"""
        return sum(len(s._bits) for s in self._slices)


# ── 全局单例 ──────────────────────────────────────────

_guard: Optional[ReplayGuard] = None


def get_replay_guard() -> ReplayGuard:
    """获取全局 ReplayGuard 单例"""
    global _guard
    if _guard is None:
        _guard = ReplayGuard()
    return _guard
