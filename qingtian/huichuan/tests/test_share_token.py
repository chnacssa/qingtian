"""汇川分享链接 — 签名 token 生成/校验测试（纯函数，免 DB）。"""

import os
import time

import pytest

os.environ.setdefault("QINGTIAN_ENV", "development")

from huichuan.api import _parse_share_token, _share_token


@pytest.fixture(autouse=True)
def _share_secret(monkeypatch):
    """默认注入分享密钥，测禁用场景时单独清除。"""
    monkeypatch.setenv("HUICHUAN_SHARE_SECRET", "test-share-secret")
    yield
    monkeypatch.delenv("HUICHUAN_SHARE_SECRET", raising=False)
    monkeypatch.delenv("HUICHUAN_FILE_TOKEN", raising=False)


class TestShareToken:
    """签名 token 生命周期"""

    def test_valid_token_roundtrip(self):
        """合法 token → 校验通过，返回 file_id"""
        fid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        expires = int(time.time()) + 3600
        token = _share_token(fid, expires)
        assert _parse_share_token(token) == fid

    def test_expired_token_rejected(self):
        """过期 token → 返回 None"""
        fid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        expires = int(time.time()) - 10  # 已过期
        token = _share_token(fid, expires)
        assert _parse_share_token(token) is None

    def test_tampered_token_rejected(self):
        """篡改 file_id → 签名不匹配 → None"""
        fid = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
        expires = int(time.time()) + 3600
        token = _share_token(fid, expires)
        forged = token.replace(fid, "0" * 36, 1) if fid in token else token + "x"
        assert _parse_share_token(forged) is None

    def test_malformed_token_rejected(self):
        """格式错误 → None"""
        assert _parse_share_token("not-a-token") is None
        assert _parse_share_token("") is None

    def test_disabled_without_secret(self, monkeypatch):
        """未配置分享密钥 → 生成抛 503，校验返回 None（不启用默认密钥）"""
        monkeypatch.delenv("HUICHUAN_SHARE_SECRET", raising=False)
        monkeypatch.delenv("HUICHUAN_FILE_TOKEN", raising=False)
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            _share_token("3fa85f64-5717-4562-b3fc-2c963f66afa6", int(time.time()) + 3600)
        assert ei.value.status_code == 503
        assert _parse_share_token("3fa85f64-5717-4562-b3fc-2c963f66afa6.1.2") is None
