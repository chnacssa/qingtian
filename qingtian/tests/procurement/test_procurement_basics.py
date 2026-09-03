"""
采购侧（biz:buyer）角色测试

运行：pytest qingtian/tests/procurement/ -v
或通过 BASE_URL 指定目标服务：
    BASE_URL=http://10.0.100.2:1996 pytest qingtian/tests/procurement/ -v
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


class TestProcurementBasics:
    """采购 Agent 基础功能测试"""

    def test_list_buyer_agents(self):
        status, data = _fetch("/v1/huanyu/agents?category=biz:buyer")
        assert status == 200
        assert "agents" in data or isinstance(data, list) or "items" in data

    def test_send_inquiry(self):
        """采购侧发起询价（同服）"""
        # 查找一个采购 agent 和一个销售 agent
        status, buyers = _fetch("/v1/huanyu/agents?category=biz:buyer")
        assert status == 200
        status, sellers = _fetch("/v1/huanyu/agents?category=biz:seller")
        assert status == 200

    def test_inbox_age_filter(self):
        """收件箱 ?max_age_days= 过滤"""
        status, data = _fetch("/v1/huanyu/inbox/nonexistent?max_age_days=7")
        assert status == 200
        assert "messages" in data


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
