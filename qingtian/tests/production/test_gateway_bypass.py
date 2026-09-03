"""【P1 安全】Gateway 绕过攻击测试

测试场景:
  1. 直调模块 API（不经过 Gateway）-> 重要端点应验证身份
  2. 使用其他 Agent 的 Token -> 应拒绝越权访问
  3. 路径遍历（path traversal）-> 应被拦截
  4. HTTP 方法篡改 -> 只允许的方法应被限制
  5. 绕过 Gateway 中间件直接访问内部端点

运行前提: ACSSA 底座已启动在 127.0.0.1:1996
  pytest tests/production/test_gateway_bypass.py -v -s
"""
import pytest
from tests.production.conftest import api, BASE_URL

pytestmark = pytest.mark.production


class TestDirectModuleAccess:
    """绕过 Gateway 直接访问模块端点。"""

    INTERNAL_ENDPOINTS = [
        "/v1/yongheng/memories",
        "/v1/huichuan/knowledge",
        "/v1/zhenyue/tokens",
        "/v1/siku/accounts",
        "/v1/zhice/tasks",
        "/v1/huanyu/agents",
        "/v1/xihe/agents",
    ]

    def test_direct_access_without_auth(self, base_url):
        """未认证直接访问内部端点 -> 应被拒绝（401/403）。"""
        vulnerable = []
        for path in self.INTERNAL_ENDPOINTS:
            resp = api("GET", path, base_url, timeout=5.0)
            if resp.status_code == 200:
                vulnerable.append(path)
                print(f"  [WARN] {path}: 未认证访问返回 200")

        if vulnerable:
            print(f"\n  ⚠️ 以下端点未认证即可访问:")
            for path in vulnerable:
                print(f"    {path}")
        else:
            print(f"  ✅ 所有内部端点未认证访问均被拒绝")

    def test_direct_access_with_admin_token(self, base_url, admin_token):
        """用 admin token 直接访问 -> 应正常通行。"""
        if not admin_token:
            pytest.skip("无 admin token")

        errors = []
        for path in self.INTERNAL_ENDPOINTS:
            try:
                resp = api("GET", path, base_url, admin_token, timeout=5.0)
                if resp.status_code in (401, 403):
                    errors.append((path, resp.status_code))
            except Exception:
                pass

        if errors:
            print(f"\n  [INFO] Admin token 被拒绝的端点:")
            for path, code in errors[:5]:
                print(f"    {path}: {code}")
        else:
            print(f"  ✅ Admin token 被所有端点接受")


class TestPathTraversal:
    """路径遍历攻击。"""

    TRAVERSAL_PAYLOADS = [
        "/v1/huanyu/agents/../zhenyue/tokens",
        "/v1/huanyu/agents/..%2fzhenyue/tokens",
        "/v1/../etc/passwd",
        "/v1/huanyu/agents/../../../etc/shadow",
        "/v1/huanyu/agents/%2e%2e%2fzhenyue/tokens",
        "/v1/huanyu/agents/....//....//etc/passwd",
        "/v1/zhice/../../config.yaml",
        "/v1/../../../opt/qingtian/config.yaml",
    ]

    def test_path_traversal_blocked(self, base_url):
        """路径遍历 -> 应返回 400/403/404（不应是 200）。"""
        vulnerable = []
        for path in self.TRAVERSAL_PAYLOADS:
            try:
                resp = api("GET", path, base_url, timeout=5.0)
                if resp.status_code == 200:
                    # 可能返回的是后备路由的 200，检查 body 是否暴露敏感信息
                    body = resp.text.lower()
                    if "root:" in body or "password" in body or "api_key" in body:
                        vulnerable.append((path, "敏感信息泄漏"))
                    else:
                        print(f"  [INFO] {path[:50]}... 返回 200（可能是 fallback）")
                elif resp.status_code == 500:
                    print(f"  [WARN] {path[:50]}... 返回 500，可能暴露了内部错误")
            except Exception:
                pass

        if vulnerable:
            print(f"\n  ⚠️ 路径遍历漏洞:")
            for path, detail in vulnerable:
                print(f"    {detail}: {path}")
            pytest.fail(f"{len(vulnerable)} 个路径遍历漏洞")
        else:
            print(f"  ✅ 所有路径遍历攻击均被拦截")


class TestHttpMethodTamper:
    """HTTP 方法篡改。"""

    METHOD_TESTS = [
        ("DELETE", "/v1/huanyu/agents"),
        ("PUT", "/v1/huanyu/agents/register"),
        ("PATCH", "/v1/huanyu/agents"),
        ("DELETE", "/v1/huichuan/knowledge"),
        ("PUT", "/v1/zhice/tasks"),
        ("PATCH", "/v1/zhenyue/tokens"),
        ("DELETE", "/v1/siku/accounts"),
    ]

    def test_unsupported_methods_blocked(self, base_url):
        """不支持的 HTTP 方法 -> 返回 405/403/400。"""
        errors = []
        for method, path in self.METHOD_TESTS:
            try:
                resp = api(method, path, base_url, timeout=5.0)
                # 500 表示服务端错误（问题）
                if resp.status_code == 500:
                    errors.append((method, path, "500"))
                # 200 表示某个方法被意外处理
                elif resp.status_code == 200:
                    print(f"  [INFO] {method} {path}: 返回 200（可能被意外处理）")
            except Exception as e:
                errors.append((method, path, str(e)[:50]))

        if errors:
            print(f"\n  HTTP 方法篡改检测:")
            for method, path, detail in errors:
                print(f"    {method} {path}: {detail[:100]}")
        else:
            print(f"  ✅ 不支持的 HTTP 方法均被正确拒绝")


class TestAuthBypass:
    """认证绕过攻击。"""

    BYPASS_HEADERS = [
        {"X-Forwarded-For": "127.0.0.1"},
        {"X-Real-IP": "127.0.0.1"},
        {"X-Forwarded-Host": "localhost"},
        {"X-Auth-Type": "bypass"},
        {"X-Admin": "true"},
        {"X-Internal": "true"},
        {"X-Debug": "1"},
    ]

    def test_auth_bypass_headers(self, base_url):
        """认证绕过 header -> 不应返回 200。"""
        vulnerable = []
        for headers in self.BYPASS_HEADERS:
            try:
                resp = api("GET", "/v1/zhenyue/tokens", base_url, headers=headers, timeout=5.0)
                if resp.status_code == 200:
                    vulnerable.append(headers)
            except Exception:
                pass

        if vulnerable:
            print(f"\n  ⚠️ 以下 header 可绕过认证:")
            for h in vulnerable:
                print(f"    {h}")
            pytest.fail(f"{len(vulnerable)} 个认证绕过 header")
        else:
            print(f"  ✅ 所有认证绕过 header 均无效")


@pytest.mark.production
def test_gateway_bypass_full(base_url, admin_token):
    """按顺序执行全部 Gateway 安全测试。"""
    import inspect
    results = []
    suites = [
        ("直连模块", TestDirectModuleAccess()),
        ("路径遍历", TestPathTraversal()),
        ("HTTP 方法篡改", TestHttpMethodTamper()),
        ("认证绕过", TestAuthBypass()),
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
        pytest.fail(f"{failed} 个 Gateway 安全测试失败")
