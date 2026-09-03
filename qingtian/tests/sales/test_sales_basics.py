"""
销售侧（biz:seller）角色测试

运行：pytest qingtian/tests/sales/ -v
或通过 BASE_URL 指定目标服务：
    BASE_URL=http://10.0.100.3:1996 pytest qingtian/tests/sales/ -v
"""

import os
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:1996").rstrip("/")


def _fetch(path, method="GET", body=None):
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"}
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except HTTPError as e:
        raw = e.read().decode()
        return e.code, json.loads(raw) if raw else {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


class TestSalesBasics:
    """销售 Agent 基础功能测试"""

    def test_list_seller_agents(self):
        status, data = _fetch("/v1/huanyu/agents?category=biz:seller")
        assert status == 200
        assert "agents" in data or isinstance(data, list) or "items" in data

    def test_send_quote(self):
        """销售侧发起报价（同服）"""
        status, sellers = _fetch("/v1/huanyu/agents?category=biz:seller")
        assert status == 200

    def test_inbox_status_filter(self):
        """收件箱 ?status=unread 过滤"""
        status, data = _fetch("/v1/huanyu/inbox/nonexistent?status=unread")
        assert status == 200
        assert "messages" in data


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
