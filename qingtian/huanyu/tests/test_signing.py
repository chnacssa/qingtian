"""
signing.py 单元测试
HMAC-SHA256 签名 / 验证 — 纯逻辑，无外部依赖
"""

import hashlib
import hmac
import os

import pytest

from huanyu.signing import (
    sign_message,
    sign_peer_message,
    verify_message,
    verify_peer_message,
)


@pytest.fixture(autouse=True)
def _test_sign_key():
    """B4: 移除 dev 回退密钥后，签名测试显式配置测试密钥"""
    os.environ["HUANYU_SIGN_KEY"] = "test-signing-key-for-b4"


class TestSignMessage:
    def test_deterministic(self):
        """相同输入产生相同签名"""
        sig1 = sign_message("a1", "a2", "info", '{"k":"v"}')
        sig2 = sign_message("a1", "a2", "info", '{"k":"v"}')
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA256 hex = 64 chars

    def test_different_inputs_different_signatures(self):
        """不同字段变化产生不同签名"""
        sig1 = sign_message("a1", "a2", "info", "payload")
        sig2 = sign_message("a1", "a3", "info", "payload")  # to_agent 不同
        sig3 = sign_message("a1", "a2", "quote", "payload")  # type 不同
        sig4 = sign_message("a1", "a2", "info", "payload2")  # payload 不同
        assert len({sig1, sig2, sig3, sig4}) == 4

    def test_hex_format(self):
        """签名是 64 位十六进制字符串"""
        sig = sign_message("a", "b", "info", "{}")
        assert all(c in "0123456789abcdef" for c in sig)

    def test_empty_payload(self):
        """空 payload 仍能生成签名"""
        sig = sign_message("a", "b", "info", "")
        assert len(sig) == 64

    def test_chinese_payload(self):
        """中文 payload 正常签名"""
        sig = sign_message("采购Agent-1", "销售Agent-1", "inquiry",
                           '{"产品":"螺纹钢","数量":"200吨"}')
        assert len(sig) == 64


class TestVerifyMessage:
    def test_valid_signature(self):
        """正确签名验证通过"""
        sig = sign_message("a1", "a2", "info", "payload")
        assert verify_message("a1", "a2", "info", "payload", sig) is True

    def test_tampered_payload(self):
        """篡改 payload 后验证失败"""
        sig = sign_message("a1", "a2", "info", "payload")
        assert verify_message("a1", "a2", "info", "tampered", sig) is False

    def test_tampered_from_agent(self):
        """篡改 from_agent 后验证失败"""
        sig = sign_message("a1", "a2", "info", "payload")
        assert verify_message("hacker", "a2", "info", "payload", sig) is False

    def test_tampered_type(self):
        """篡改 type 后验证失败"""
        sig = sign_message("a1", "a2", "info", "payload")
        assert verify_message("a1", "a2", "quote", "payload", sig) is False

    def test_empty_signature(self):
        """空签名返回 False"""
        assert verify_message("a1", "a2", "info", "payload", "") is False
        assert verify_message("a1", "a2", "info", "payload", None) is False  # type: ignore

    def test_wrong_format_signature(self):
        """错误格式签名验证失败"""
        assert verify_message("a1", "a2", "info", "payload", "not_a_hex_sig") is False

    def test_timing_safe_comparison(self):
        """使用 hmac.compare_digest 防止时序攻击"""
        sig = sign_message("a1", "a2", "info", "p" * 1000)
        wrong = "f" * 64
        # 验证不会因长度不匹配而抛异常
        result = verify_message("a1", "a2", "info", "p" * 1000, wrong)
        assert result is False


class TestFailClosed:
    """B4 (R11): 移除硬编码回退密钥后，未配置密钥时 fail-closed"""

    @pytest.fixture(autouse=True)
    def _no_key(self, monkeypatch):
        monkeypatch.delenv("HUANYU_SIGN_KEY", raising=False)
        import huanyu.config as hcfg
        monkeypatch.setattr(hcfg, "get_msg_sign_key", lambda: "")

    def test_sign_raises_without_key(self):
        """密钥未配置 → 签名抛异常（拒绝降级到公开默认密钥）"""
        import pytest as _pytest
        with _pytest.raises(RuntimeError):
            sign_message("a1", "a2", "info", "payload")

    def test_verify_rejects_without_key(self):
        """密钥未配置 → 验证返回 False（fail-closed）"""
        assert verify_message("a1", "a2", "info", "payload", "fake-sig") is False
        assert verify_peer_message('{"p":1}', "fake-sig") is False

    def test_peer_sign_raises_without_key(self):
        """密钥未配置 → 底座间签名抛异常"""
        with pytest.raises(RuntimeError):
            sign_peer_message('{"payload":"data"}')


class TestPeerMessage:
    def test_peer_sign_and_verify(self):
        """底座间签名和验证"""
        sig = sign_peer_message('{"payload":"data"}')
        assert len(sig) == 64
        assert verify_peer_message('{"payload":"data"}', sig) is True

    def test_peer_tampered(self):
        """篡改后验证失败"""
        sig = sign_peer_message('{"payload":"data"}')
        assert verify_peer_message('{"payload":"tampered"}', sig) is False

    def test_peer_empty_signature(self):
        """空签名返回 False"""
        assert verify_peer_message("data", "") is False

    def test_peer_different_from_agent_signing(self):
        """底座签名和消息签名互不相同"""
        peer_sig = sign_peer_message("test")
        msg_sig = sign_message("a", "b", "info", "test")
        assert peer_sig != msg_sig  # 不同算法路径
