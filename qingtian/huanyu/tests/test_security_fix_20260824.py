# -*- coding: utf-8 -*-
"""2026-08-24 寰宇上线前安全评审 P0 修复的回归测试。

覆盖：
- P0-4 HUANYU_SIGN_KEY fail-closed（无密钥拒绝签名，显式放行走 dev 兜底）
- P0-6 联邦端点验签 verify_fed_body（整体/agent/record 三口径 + 缺签/篡改拒绝）
- P0-2 凭证签发绑定申请者公钥 + 挑战签名验证 + 签发方签名证书验签
- P0-1 网关 /v1/huanyu/ 白名单方法感知收窄
"""

import json

import pytest


# ── P0-4：签名密钥 fail-closed ──────────────────────────

class TestSignKeyFailClosed:
    def test_missing_key_raises(self, monkeypatch):
        monkeypatch.delenv("HUANYU_SIGN_KEY", raising=False)
        monkeypatch.delenv("HUANYU_ALLOW_DEV_KEY", raising=False)
        monkeypatch.delenv("QINGTIAN_CONFIG", raising=False)
        from huanyu import signing
        monkeypatch.setattr(signing.hcfg, "get", lambda k, d=None: "")
        with pytest.raises(RuntimeError):
            signing._get_key()

    def test_explicit_dev_allow_falls_back(self, monkeypatch):
        monkeypatch.delenv("HUANYU_SIGN_KEY", raising=False)
        monkeypatch.setenv("HUANYU_ALLOW_DEV_KEY", "1")
        monkeypatch.delenv("QINGTIAN_CONFIG", raising=False)
        from huanyu import signing
        monkeypatch.setattr(signing.hcfg, "get", lambda k, d=None: "")
        assert signing._get_key() == b"huanyu-dev-key-2026"


# ── P0-6：联邦端点验签 ──────────────────────────────────

class TestVerifyFedBody:
    def test_whole_body_signature_accepted(self):
        from huanyu.signing import sign_peer_message, verify_fed_body
        body = {"peer_id": "p1", "host": "h1", "port": 1996}
        body["peer_sig"] = sign_peer_message(
            json.dumps(body, ensure_ascii=False, sort_keys=True))
        assert verify_fed_body(body) is True

    def test_agent_scope_signature_accepted(self):
        from huanyu.signing import sign_peer_message, verify_fed_body
        agent = {"agent_id": "a1", "name": "x"}
        body = {"agent": agent, "peer_sig": sign_peer_message(
            json.dumps(agent, ensure_ascii=False, sort_keys=True))}
        assert verify_fed_body(body) is True

    def test_missing_signature_rejected(self):
        from huanyu.signing import verify_fed_body
        assert verify_fed_body({"peer_id": "p1"}) is False
        assert verify_fed_body({}) is False

    def test_tampered_body_rejected(self):
        from huanyu.signing import sign_peer_message, verify_fed_body
        body = {"peer_id": "p1", "host": "h1"}
        body["peer_sig"] = sign_peer_message(
            json.dumps(body, ensure_ascii=False, sort_keys=True))
        body["host"] = "evil-attacker"  # 签名后篡改
        assert verify_fed_body(body) is False


# ── P0-2：凭证与鉴别 ────────────────────────────────────

class TestIdentityP0:
    @pytest.mark.asyncio
    async def test_issue_binds_applicant_public_key(self):
        from common.identity import DefaultCredentialProvider
        from huanyu import ed25519_utils as ed
        _, pk = ed.generate_keypair()
        result = await DefaultCredentialProvider().issue("ain:cn:hf:test:base:1", pk, "free")
        cert = result.certificate
        assert ed.public_key_from_pem(cert["public_key"]) == pk  # 公钥绑定申请者
        assert cert.get("issuer_public_key")  # 签发方公钥随证书携带

        from huanyu.certificate import verify_cert
        assert verify_cert(cert) is True
        # 篡改 ain 后验签失败
        cert["ain"] = "ain:cn:hf:test:attacker:9"
        assert verify_cert(cert) is False

    @pytest.mark.asyncio
    async def test_issue_requires_public_key(self):
        from common.identity import DefaultCredentialProvider
        with pytest.raises(ValueError):
            await DefaultCredentialProvider().issue("ain:x", b"", "free")

    @pytest.mark.asyncio
    async def test_verify_checks_challenge_signature(self):
        from common.identity import (DefaultAuthProvider, AuthChallenge)
        from huanyu import ed25519_utils as ed
        provider = DefaultAuthProvider()
        challenge = await provider.create_challenge("ain:cn:hf:test:base:1")

        sk, pk = ed.generate_keypair()
        cert_result = await (await _get_provider()).issue(
            "ain:cn:hf:test:base:1", pk, "free")
        cert = cert_result.certificate

        # 正确签名 → success
        good_sig = ed.sign_message(sk, challenge.random_nonce)
        assertion = await provider.verify(
            challenge, {"certificate": cert, "signature": good_sig})
        assert assertion.result == "success"
        assert assertion.agent_ain == "ain:cn:hf:test:base:1"

        # 错误私钥签名 → failed（原实现不验签恒 success，此为 P0-2 回归锚点）
        sk2, _ = ed.generate_keypair()
        bad_sig = ed.sign_message(sk2, challenge.random_nonce)
        assertion = await provider.verify(
            challenge, {"certificate": cert, "signature": bad_sig})
        assert assertion.result == "failed"
        assert "signature" in assertion.detail

        # 缺签名 → failed
        assertion = await provider.verify(challenge, {"certificate": cert})
        assert assertion.result == "failed"


async def _get_provider():
    from common.identity import DefaultCredentialProvider
    return DefaultCredentialProvider()


# ── P0-1：网关白名单收窄 ────────────────────────────────

class TestGatewayWhitelistNarrowed:
    def _call(self, path, method):
        import sys
        import os
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        if root not in sys.path:
            sys.path.insert(0, root)
        from gateway.middleware import _is_path_public
        return _is_path_public(path, method)

    def test_skill_subprocess_paths_stay_public(self):
        assert self._call("/v1/huanyu/messages", "POST") is True
        assert self._call("/v1/huanyu/inbox/agent-1", "GET") is True
        assert self._call("/v1/huanyu/agents/agent-1/heartbeat", "POST") is True
        assert self._call("/v1/huanyu/agents/register", "POST") is True
        assert self._call("/v1/huanyu/agents", "GET") is True
        assert self._call("/v1/huanyu/agents/identity/resolve", "GET") is True
        assert self._call("/v1/huanyu/orgs/org-1/keys", "GET") is True

    def test_sensitive_write_paths_require_auth(self):
        # RCE 面：runtime 进程控制
        assert self._call("/v1/huanyu/runtime/agents/agent-1/start", "POST") is False
        # C 级认证伪造
        assert self._call("/v1/huanyu/verification/upgrade", "POST") is False
        # agent 删除
        assert self._call("/v1/huanyu/agents/agent-1", "DELETE") is False
        # 谈判/协议签署
        assert self._call("/v1/huanyu/negotiations/1/counter", "POST") is False
        assert self._call("/v1/huanyu/agreements/1/sign", "POST") is False

    def test_agents_read_paths_public_write_gated(self):
        assert self._call("/v1/huanyu/agents/agent-1", "GET") is True
        assert self._call("/v1/huanyu/agents/agent-1", "PUT") is False
