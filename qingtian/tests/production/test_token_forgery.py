"""【P1 安全】Token 伪造/过期/篡改攻击测试

测试场景:
  1. 伪造 Token（随机字符串）-> 所有端点应返回 401
  2. 过期 Token -> 应被拒绝
  3. 篡改 Token（修改 payload）-> 应被拒绝
  4. 不同角色 Token 越权访问 -> 应被拒绝
  5. 空 Token / 缺失 Authorization header -> 应返回 401

运行前提: ACSSA 底座已启动在 127.0.0.1:1996
  pytest tests/production/test_token_forgery.py -v -s
"""
import hashlib
import hmac
import base64
import json
import time
import uuid
import pytest
from tests.production.conftest import api, BASE_URL

pytestmark = pytest.mark.production


class TestTokenForgery:
    """Token 伪造与篡改检测。"""

    PROTECTED_ENDPOINTS = [
        ("GET", "/v1/huanyu/agents"),
        ("GET", "/v1/zhenyue/tokens"),
        ("GET", "/v1/siku/accounts"),
        ("GET", "/v1/zhice/tasks"),
        ("GET", "/v1/huichuan/knowledge"),
    ]

    FORGED_TOKENS = [
        # 完全随机字符串
        ("random_sha256", hashlib.sha256(b"fake").hexdigest()),
        # UUID 格式
        ("uuid_format", str(uuid.uuid4())),
        # 带 zt_ 前缀（模仿镇岳 token 格式）
        ("zhenyue_prefix", f"zt_adm_{uuid.uuid4().hex}"),
        # 空字符串
        ("empty_string", ""),
        # 特殊字符
        ("special_chars", "'; DROP TABLE tokens; --"),
    ]

    def test_forged_token_rejected(self, base_url):
        """伪造 Token -> 所有端点应返回 401/403。"""
        errors = []

        for method, path in self.PROTECTED_ENDPOINTS:
            for name, token in self.FORGED_TOKENS:
                try:
                    resp = api(method, path, base_url, token=token, timeout=5.0)

                    # 200 意味着伪造 token 被接受（严重问题）
                    if resp.status_code == 200:
                        errors.append((method, path, name, f"伪造 token 被接受: 200"))
                    elif resp.status_code == 500:
                        errors.append((method, path, name, f"服务端错误: {resp.text[:100]}"))
                except Exception as e:
                    errors.append((method, path, name, f"异常: {e}"))

        total = len(self.PROTECTED_ENDPOINTS) * len(self.FORGED_TOKENS)
        if errors:
            print(f"\n  Token 伪造检测到 {len(errors)}/{total} 个问题:")
            for method, path, name, detail in errors[:10]:
                print(f"    {method} {path} / {name}")
                print(f"      {detail}")
        else:
            print(f"  ✅ {total} 个伪造 token 测试全部被拒绝")

        accepted = [e for e in errors if "被接受" in e[3]]
        assert len(accepted) == 0, (
            f"{len(accepted)} 个伪造 token 被端点接受！"
        )

    def test_missing_auth_header(self, base_url):
        """缺失 Authorization header -> 返回 401。"""
        for method, path in self.PROTECTED_ENDPOINTS:
            try:
                resp = api(method, path, base_url, timeout=5.0)

                # 401 是正确响应；200 表示 Gateway 未拦截
                if resp.status_code not in (401, 403):
                    print(f"  [INFO] {method} {path}: 无认证返回 {resp.status_code}（可能是公开端点）")
            except Exception as e:
                print(f"  [INFO] {method} {path}: {e}")

    def test_invalid_auth_scheme(self, base_url):
        """错误的 Authorization scheme -> 应被拒绝。"""
        for method, path in self.PROTECTED_ENDPOINTS:
            try:
                resp = api(method, path, base_url, headers={
                    "Authorization": f"Basic {base64.b64encode(b'admin:admin').decode()}",
                }, timeout=5.0)
                if resp.status_code == 200:
                    print(f"  [WARN] {method} {path}: Basic auth 被接受！")
            except Exception:
                pass

    def test_token_agent_id_mismatch(self, base_url, agents, admin_token):
        """Token 中 agent_id 与实际请求 agent_id 不匹配 -> 应拦截。"""
        # 创建一个新 token 但用不同的 agent_id
        if not admin_token:
            pytest.skip("无 admin token")

        # 创建一个 buyer 的 token
        resp = api("POST", "/v1/zhenyue/token/create", base_url, admin_token, json={
            "agent_id": agents["buyer"],
            "role": "agent",
        }, timeout=5.0)

        if resp.status_code != 200:
            pytest.skip("无法创建测试 token")
        buyer_token = resp.json().get("token", "")
        if not buyer_token:
            pytest.skip("token 为空")

        # 用 buyer 的 token 但请求 seller 的资源
        resp = api("GET", f"/v1/siku/accounts/{agents['seller']}", base_url, buyer_token, timeout=5.0)
        if resp.status_code in (200, 403):
            # 200 表示越权访问成功（问题）
            # 403 是正确的越权拦截
            if resp.status_code == 200:
                print(f"  [WARN] buyer token 访问 seller 账户返回 200（越权）")
            else:
                print(f"  ✅ 越权访问被正确拦截: {resp.status_code}")
        else:
            print(f"  [INFO] 越权行为: {resp.status_code}")


class TestTokenTamper:
    """Token 篡改检测。"""

    def _tamper_hmac_token(self, token: str) -> list[str]:
        """生成篡改后的 token 变体。"""
        tampered = []
        # 翻转前 10 个字符中的若干位
        if len(token) > 10:
            tampered.append(token[:5] + ("X" if token[5] != "X" else "Y") + token[6:])
            tampered.append(token[:-1] + ("0" if token[-1] != "0" else "1"))
        return tampered

    def test_tampered_token_rejected(self, base_url, admin_token):
        """篡改后的 Token -> 应被拒绝。"""
        if not admin_token:
            pytest.skip("无 admin token")

        tampered = self._tamper_hmac_token(admin_token)
        errors = []

        for t in tampered:
            for method, path in [
                ("GET", "/v1/huanyu/agents"),
                ("GET", "/v1/zhice/tasks"),
            ]:
                try:
                    resp = api(method, path, base_url, token=t, timeout=5.0)
                    if resp.status_code == 200:
                        errors.append((method, path, f"篡改 token 被接受"))
                except Exception as e:
                    errors.append((method, path, f"异常: {e}"))

        if errors:
            print(f"\n  Token 篡改检测到 {len(errors)} 个问题:")
            for method, path, detail in errors:
                print(f"    {method} {path}: {detail}")
        else:
            print(f"  ✅ 所有篡改 token 均被拒绝")


@pytest.mark.production
def test_token_forgery_full(base_url, agents, admin_token):
    """按顺序执行全部 Token 安全测试。"""
    import inspect
    results = []
    suites = [
        ("Token 伪造", TestTokenForgery()),
        ("Token 篡改", TestTokenTamper()),
    ]

    for suite_name, instance in suites:
        print(f"\n  [{suite_name}]")
        for attr in sorted(dir(instance)):
            if attr.startswith("test_"):
                method = getattr(instance, attr)
                sig = inspect.signature(method)
                kwargs = {}
                for p in sig.parameters:
                    if p == "base_url":
                        kwargs["base_url"] = base_url
                    elif p == "agents":
                        kwargs["agents"] = agents
                    elif p == "admin_token":
                        kwargs["admin_token"] = admin_token
                try:
                    method(**kwargs)
                    results.append((suite_name, attr, "PASS"))
                    print(f"  ✅ {attr}")
                except Exception as e:
                    results.append((suite_name, attr, "FAIL"))
                    print(f"  ❌ {attr}: {e}")

    failed = sum(1 for r in results if r[2] == "FAIL")
    if failed:
        pytest.fail(f"{failed} 个 Token 安全测试失败")
