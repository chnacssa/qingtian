"""【P1 安全】API 层 SQL 注入扫描

测试场景:
  1. 对所有公共 API 端点注入标准 SQL 注入 payload
  2. 验证注入 payload 不会导致 500 或 SQL 语法错误（即被正确转义/参数化）
  3. 对 POST body、URL query params、URL path params 分别测试

关键原则:
  - 注入 payload 不应导致 500 Internal Server Error
  - 注入 payload 不应泄露 SQL 错误信息到响应 body
  - 即使返回 4xx（有效拦截）也比 500 好

运行前提: ACSSA 底座已启动在 127.0.0.1:1996
  pytest tests/production/test_sql_injection.py -v -s
"""
import pytest
import urllib.parse
from tests.production.conftest import api, BASE_URL

pytestmark = pytest.mark.production

# 标准 SQL 注入测试向量
INJECTION_PAYLOADS = [
    "' OR '1'='1",
    "'; DROP TABLE memories; --",
    "' UNION SELECT * FROM pg_catalog.pg_tables --",
    "1; SELECT pg_sleep(5) --",
    "' OR 1=1 --",
    "\" OR 1=1 --",
    "admin' --",
    "1' AND 1=1; --",
    "' OR '1'='1' /*",
    "'; EXEC xp_cmdshell('dir') --",  # SQL Server 专用，无害化
    "1' ORDER BY 100 --",
    "1' GROUP BY 1 HAVING 1=1 --",
    "'/**/OR/**/1=1",
    "' OR ''='",
    "' UNION SELECT NULL,NULL,NULL,NULL --",
    "\\'; DROP TABLE users; --",
    "' WAITFOR DELAY '0:0:5' --",
]


def _is_sql_error(body: str) -> bool:
    """检查响应 body 是否包含 SQL 错误特征。"""
    sql_indicators = [
        "syntax error", "unterminated", "pg_catalog", "psycopg",
        "ProgrammingError", "DatabaseError", "SQL syntax",
        "column does not exist", "relation", "does not exist",
        "pg_sleep", "ORA-", "SQLSTATE", "mysql",
        "truncated", "incorrect syntax", "unclosed",
    ]
    body_lower = body.lower()
    return any(indicator.lower() in body_lower for indicator in sql_indicators)


class TestSqlInjectionQueryParams:
    """URL query parameter SQL 注入扫描。"""

    ENDPOINTS = [
        "/v1/huanyu/agents/search?q={payload}",
        "/v1/huanyu/agents?category={payload}",
        "/v1/huanyu/messages/inbox?agent_id={payload}",
        "/v1/yongheng/search?q={payload}",
        "/v1/huichuan/search?q={payload}",
        "/v1/huichuan/knowledge/search?q={payload}",
        "/v1/huanyu/inbox/{payload}",
        "/v1/zhice/tasks?created_by={payload}",
        "/v1/zhenyue/audit/logs?agent_id={payload}",
        "/v1/siku/accounts/{payload}",
    ]

    def test_query_param_injection(self, base_url, agents):
        """对 URL query 参数注入 -> 不应返回 500 或 SQL 错误。"""
        errors = []
        total = 0

        for template in self.ENDPOINTS:
            for payload in INJECTION_PAYLOADS[:5]:  # 每个端点测前 5 种
                total += 1
                path = template.format(payload=urllib.parse.quote(payload))
                try:
                    resp = api("GET", path, base_url, timeout=10.0)
                    body = resp.text

                    if resp.status_code == 500:
                        errors.append((template, payload, f"500: {body[:200]}"))
                    elif _is_sql_error(body):
                        errors.append((template, payload, f"SQL 泄漏: {body[:200]}"))
                except Exception as e:
                    errors.append((template, payload, f"异常: {e}"))

        if errors:
            print(f"\n  SQL 注入检测到 {len(errors)}/{total} 个问题:")
            for path, payload, detail in errors[:10]:
                print(f"    {path}")
                print(f"      payload: {payload}")
                print(f"      detail: {detail[:100]}")
        else:
            print(f"  ✅ {total} 个 query param 注入测试全部通过")

        # 不允许任何 500 或 SQL 错误泄漏
        severe = [e for e in errors if "500" in e[2] or "SQL" in e[2]]
        assert len(severe) == 0, f"{len(severe)} 个严重 SQL 注入问题"
        if errors:
            print(f"  [INFO] {len(errors)} 个非严重问题（非 500）")


class TestSqlInjectionPostBody:
    """POST body SQL 注入扫描。"""

    def test_post_body_injection(self, base_url, agents):
        """对 POST JSON body 注入 -> 不应返回 500 或 SQL 错误。"""
        errors = []
        agent_id = agents["buyer"]

        test_cases = [
            ("POST", "/v1/huanyu/agents/register", {
                "name": f"test' OR '1'='1",
                "category": f"biz:buyer'; DROP TABLE --",
            }),
            ("POST", "/v1/huanyu/messages", {
                "from_agent": agent_id,
                "to_agent": agent_id,
                "message_type": "test' OR '1'='1",
                "payload": {"msg": f"'; DROP TABLE memories; --"},
            }),
            ("POST", "/v1/yongheng/memories", {
                "namespace": f"'; DROP SCHEMA yongheng; --",
                "memory_type": "episodic",
                "content": f"1' OR 1=1; --",
            }),
            ("POST", "/v1/huichuan", {
                "title": f"test' OR '1'='1",
                "domain": "test",
                "content": f"'; DROP TABLE huichuan; --",
            }),
            ("POST", "/v1/zhice/tasks", {
                "title": f"test' OR '1'='1",
                "description": f"'; DROP TABLE zhice; --",
                "created_by": agent_id,
            }),
            ("POST", "/v1/zhenyue/token/create", {
                "agent_id": f"admin'; --",
                "role": "admin",
            }),
        ]

        for method, path, body in test_cases:
            for key, value in body.items():
                # 对每个字段注入
                injected = dict(body)
                payload = INJECTION_PAYLOADS[0]  # "' OR '1'='1"
                injected[key] = f"{value} {payload}"

                try:
                    resp = api(method, path, base_url, json=injected, timeout=10.0)
                    body_text = resp.text

                    if resp.status_code == 500:
                        errors.append((path, key, payload, f"500: {body_text[:200]}"))
                    elif _is_sql_error(body_text):
                        errors.append((path, key, payload, f"SQL 泄漏: {body_text[:200]}"))
                except Exception as e:
                    errors.append((path, key, payload, f"异常: {e}"))

        if errors:
            print(f"\n  POST body 注入检测到 {len(errors)} 个问题:")
            for path, key, payload, detail in errors[:5]:
                print(f"    {path} / {key}")
                print(f"      detail: {detail[:100]}")
        else:
            print(f"  ✅ POST body 注入测试全部通过")

        severe = [e for e in errors if "500" in e[3] or "SQL" in e[3]]
        assert len(severe) == 0, f"{len(severe)} 个严重 SQL 注入问题"


class TestSqlInjectionPathParams:
    """URL path parameter SQL 注入扫描（路径中的参数化 ID）。"""

    def test_path_param_injection(self, base_url):
        """路径参数注入 -> 不应 500。"""
        errors = []
        payloads = ["1' OR '1'='1", "1; DROP TABLE", "admin' --"]

        templates = [
            "/v1/huanyu/agents/{p}",
            "/v1/huanyu/messages/{p}",
            "/v1/huanyu/negotiations/{p}",
            "/v1/huanyu/agreements/{p}",
            "/v1/zhice/tasks/{p}",
            "/v1/zhice/steps/{p}",
            "/v1/siku/accounts/{p}",
        ]

        for template in templates:
            for payload in payloads:
                path = template.format(p=urllib.parse.quote(payload))
                try:
                    resp = api("GET", path, base_url, timeout=5.0)
                    body_text = resp.text

                    if resp.status_code == 500:
                        errors.append((template, payload, f"500: {body_text[:200]}"))
                    elif _is_sql_error(body_text):
                        errors.append((template, payload, f"SQL 泄漏: {body_text[:200]}"))
                except Exception as e:
                    errors.append((template, payload, f"异常: {e}"))

        if errors:
            print(f"\n  Path param 注入检测到 {len(errors)} 个问题:")
            for path, payload, detail in errors[:5]:
                print(f"    {path}")
                print(f"      payload: {payload}")
                print(f"      detail: {detail[:100]}")
        else:
            print(f"  ✅ Path param 注入测试全部通过")

        severe = [e for e in errors if "500" in e[2] or "SQL" in e[2]]
        assert len(severe) == 0, f"{len(severe)} 个严重 SQL 注入问题"


@pytest.mark.production
def test_sql_injection_full(base_url, agents):
    """按顺序执行全部 SQL 注入扫描。"""
    import inspect
    results = []
    suites = [
        ("Query Params", TestSqlInjectionQueryParams()),
        ("POST Body", TestSqlInjectionPostBody()),
        ("Path Params", TestSqlInjectionPathParams()),
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
                try:
                    method(**kwargs)
                    results.append((suite_name, attr, "PASS"))
                    print(f"  ✅ {attr}")
                except Exception as e:
                    results.append((suite_name, attr, "FAIL"))
                    print(f"  ❌ {attr}: {e}")

    failed = sum(1 for r in results if r[2] == "FAIL")
    if failed:
        pytest.fail(f"{failed} 个 SQL 注入测试失败")
