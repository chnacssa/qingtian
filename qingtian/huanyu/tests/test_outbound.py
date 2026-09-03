"""OutboundPusher 测试（2026-08-14：skill 后台 info 消息出站投递到飞书用户）

覆盖：
- 投递 feishu:ou_xxx / 裸 ou_xxx → 飞书 open_id 正确 + 成功标 read+delivered；
- 非用户目标（岗位 agent 名）→ 不发送；
- SQL 只捞 info 类型 + 用户身份目标（消息过滤在 SQL，非代码层）；
- 发送失败 → 保持 unread + cooldown 生效（下一轮跳过）→ 超限置 failed；
- 单条投递异常 → 不中断整批扫描，该条记冷却+次数，其余消息照常投递；
- payload 无 text → json.dumps 兜底；
- enabled=false / 无凭据 → 不查询不发送；
- token 缓存：连续两轮只请求一次 token 端点。
纯 mock 测试，不依赖数据库。
"""
import json
import os
import re
from unittest.mock import patch

import pytest

import huanyu.outbound as outbound


# ── 假响应 / 假客户端 / 假 DB ──


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeClientCM:
    """模拟 httpx.AsyncClient 的 async context manager；记录每次 post。"""

    def __init__(self, responses):
        self._responses = responses  # url -> data
        self.posts = []  # [(url, kwargs), ...]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeResp(self._responses.get(url, {"code": 0, "msg": "mock"}))


class _FakeConn:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.executed = []  # [(sql, params), ...]
        self.fetch_sql = None
        self.fetch_count = 0

    async def fetch(self, sql, *params):
        self.fetch_count += 1
        self.fetch_sql = sql
        return self._rows

    async def execute(self, sql, *params):
        self.executed.append((sql, params))
        return "OK"


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


def _msg(msg_id="11111111-1111-1111-1111-111111111111",
         to="feishu:ou_userabc", mtype="info", payload=None):
    return {
        "message_id": msg_id,
        "to_agent_id": to,
        "message_type": mtype,
        "payload": payload if payload is not None else {"text": "hello"},
    }


_TOKEN_URL = outbound._FEISHU_TOKEN_URL
_SEND_URL = outbound._FEISHU_SEND_URL + "?receive_id_type=open_id"


def _ok_responses():
    return {_TOKEN_URL: {"code": 0, "tenant_access_token": "tok", "expire": 7200},
            _SEND_URL: {"code": 0}}


@pytest.fixture(autouse=True)
def _reset_globals():
    """清掉模块级 token/告警缓存，防止跨用例污染。"""
    outbound._token = ""
    outbound._token_exp = 0.0
    outbound._token_warned = False
    yield
    outbound._token = ""
    outbound._token_exp = 0.0
    outbound._token_warned = False


def _patch_env(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "appid")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")


# ── 用例 ──


@pytest.mark.asyncio
async def test_deliver_feishu_prefixed(monkeypatch):
    """feishu:ou_xxx → 剥前缀 → receive_id=ou_xxx → 成功标 read+delivered。"""
    conn = _FakeConn(rows=[_msg(to="feishu:ou_userabc")])
    _patch_env(monkeypatch)
    client = _FakeClientCM(_ok_responses())
    pusher = outbound.OutboundPusher()

    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client):
        await pusher._once()

    sent = client.posts[-1][1]["json"]
    assert sent["receive_id"] == "ou_userabc"
    assert sent["msg_type"] == "text"
    upd_sql, upd_params = conn.executed[-1]
    assert "status='read'" in upd_sql and "delivery_status='delivered'" in upd_sql
    assert upd_params[0] == "11111111-1111-1111-1111-111111111111"
    assert pusher._stats["delivered"] == 1


@pytest.mark.asyncio
async def test_deliver_bare_ou(monkeypatch):
    """裸 ou_xxx → receive_id 原样透传。"""
    conn = _FakeConn(rows=[_msg(to="ou_userabc")])
    _patch_env(monkeypatch)
    client = _FakeClientCM(_ok_responses())
    pusher = outbound.OutboundPusher()

    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client):
        await pusher._once()

    sent = client.posts[-1][1]["json"]
    assert sent["receive_id"] == "ou_userabc"


@pytest.mark.asyncio
async def test_non_user_target_skipped():
    """岗位 agent 目标（procurement-feishu）→ 正则不匹配 + _deliver 跳过不发送。"""
    # 该正则作为 SQL `to_agent_id ~ $1` 绑定参数（PG 正则串），测试侧用 re 匹配验证语义
    assert not re.match(outbound._USER_TARGET_RE, "procurement-feishu")
    assert re.match(outbound._USER_TARGET_RE, "ou_abc123")
    assert re.match(outbound._USER_TARGET_RE, "feishu:ou_abc123")

    conn = _FakeConn()
    client = _FakeClientCM({})
    pusher = outbound.OutboundPusher()
    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client), \
         patch.dict(os.environ, {"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b"}):
        await pusher._deliver(_Pool(conn), "huanyu", "m1", "procurement-feishu", {"text": "x"})

    assert client.posts == []
    assert conn.executed == []


@pytest.mark.asyncio
async def test_sql_filters_info_and_user_target(monkeypatch):
    """SQL 只捞 info 类型 + unread + 用户身份目标（消息过滤在 SQL 层）。"""
    conn = _FakeConn(rows=[])
    _patch_env(monkeypatch)
    pusher = outbound.OutboundPusher()

    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)):
        await pusher._once()

    assert conn.fetch_count == 1
    sql = conn.fetch_sql
    assert "message_type='info'" in sql
    # 假delivered 修复（b1c9382b）：AsyncForward 转发 HTTP 200 即抢标 delivered，但转发
    # 成功≠已发飞书——若仍排除 delivered，info→真实飞书用户消息被抢标后 outbound 永不
    # 投递（小智实锤：谈判清单收不到）。故有意豁免 delivery_status，只认 status='unread'，
    # 双发由 _deliver 真实发飞书成功才标 delivered+read 兜底。反向断言钉住该语义，
    # 防止未来"顺手加回"过滤条件复活此 bug。
    assert "delivery_status != 'delivered'" not in sql
    assert "status='unread'" in sql
    assert "to_agent_id ~ $1" in sql  # 用户身份目标正则走绑定参数


@pytest.mark.asyncio
async def test_send_failure_cooldown_retry(monkeypatch):
    """发送失败 → 保持 unread + 记 cooldown/attempts → 下一轮 cooldown 内跳过不再发。"""
    msg_id = "11111111-1111-1111-1111-111111111111"
    conn1 = _FakeConn(rows=[_msg(msg_id=msg_id)])
    _patch_env(monkeypatch)
    client = _FakeClientCM({
        _TOKEN_URL: {"code": 0, "tenant_access_token": "tok", "expire": 7200},
        _SEND_URL: {"code": 999, "msg": "receive_id invalid"},
    })
    pusher = outbound.OutboundPusher()

    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn1)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client):
        await pusher._once()

    # 失败：不标 read，记录 cooldown + attempts
    assert conn1.executed == []
    assert pusher._cooldown.get(msg_id, 0) > 0
    assert pusher._attempts[msg_id] == 1

    # 下一轮（cooldown 内）→ 跳过，不再发
    conn2 = _FakeConn(rows=[_msg(msg_id=msg_id)])
    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn2)):
        await pusher._once()

    send_posts = [p for p in client.posts if p[0] == _SEND_URL]
    assert len(send_posts) == 1  # 只有第一轮的 1 次发送


@pytest.mark.asyncio
async def test_max_attempts_abandon(monkeypatch):
    """attempts 超限 → 置 read + failed（放弃防死循环）。"""
    msg_id = "11111111-1111-1111-1111-111111111111"
    conn = _FakeConn()
    client = _FakeClientCM({
        _TOKEN_URL: {"code": 0, "tenant_access_token": "tok", "expire": 7200},
        _SEND_URL: {"code": 999, "msg": "boom"},
    })
    pusher = outbound.OutboundPusher()
    pusher._attempts[msg_id] = 1  # 已是第 1 次失败，本次第 2 次 → 超 max_attempts=2
    pusher._cooldown[msg_id] = 0

    with patch("huanyu.outbound.httpx.AsyncClient", return_value=client), \
         patch("huanyu.outbound._config",
               return_value={**outbound._config(), "max_attempts": 2}):
        await pusher._deliver(_Pool(conn), "huanyu", msg_id, "feishu:ou_abc", {"text": "x"})

    upd_sql, upd_params = conn.executed[-1]
    assert "delivery_status='failed'" in upd_sql
    assert upd_params[0] == msg_id
    assert pusher._stats["failed"] == 1


@pytest.mark.asyncio
async def test_deliver_exception_isolated(monkeypatch):
    """单条消息投递异常不中断整批扫描：该条记冷却+次数，其余消息照常投递（状态一致性）。"""
    _patch_env(monkeypatch)
    conn = _FakeConn(rows=[_msg(msg_id="11111111-1111-1111-1111-111111111111",
                                to="feishu:ou_a"),
                           _msg(msg_id="22222222-2222-2222-2222-222222222222",
                                to="feishu:ou_b")])
    client = _FakeClientCM(_ok_responses())
    pusher = outbound.OutboundPusher()
    fail_id = "11111111-1111-1111-1111-111111111111"
    orig_deliver = pusher._deliver

    async def _flaky(conn, schema, msg_id, to_agent, payload):
        if msg_id == fail_id:
            raise RuntimeError("db boom")
        return await orig_deliver(conn, schema, msg_id, to_agent, payload)

    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client), \
         patch.object(pusher, "_deliver", side_effect=_flaky):
        await pusher._once()  # 不抛出，整批继续

    # 失败条：冷却 + attempts 记录，防每轮立即重打
    assert pusher._cooldown.get(fail_id, 0) > 0
    assert pusher._attempts[fail_id] == 1
    # 其余消息照常投递
    send_posts = [p for p in client.posts if p[0] == _SEND_URL]
    assert len(send_posts) == 1
    assert pusher._stats["delivered"] == 1


@pytest.mark.asyncio
async def test_payload_without_text_fallback(monkeypatch):
    """payload 无 text 字段 → json.dumps 兜底（_extract_text 单元 + 发送链路）。"""
    # _extract_text 单元
    assert outbound.OutboundPusher._extract_text({"type": "daily_report", "summary": "s"}) == \
        json.dumps({"type": "daily_report", "summary": "s"}, ensure_ascii=False)
    assert outbound.OutboundPusher._extract_text({"text": "hi"}) == "hi"
    assert outbound.OutboundPusher._extract_text(json.dumps({"text": "hi"})) == "hi"
    assert outbound.OutboundPusher._extract_text("not-json") == "{}"

    # 发送链路：payload 无 text 也照发，content 为序列化兜底
    conn = _FakeConn(rows=[_msg(payload={"type": "negotiation_summary", "inquiry_id": "i1"})])
    _patch_env(monkeypatch)
    client = _FakeClientCM(_ok_responses())
    pusher = outbound.OutboundPusher()

    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client):
        await pusher._once()

    sent = client.posts[-1][1]["json"]
    content = json.loads(sent["content"])
    # 飞书 text 消息 content 是 {"text": ...} 包裹，兜底文本为整 payload 序列化
    assert json.loads(content["text"])["inquiry_id"] == "i1"
    assert pusher._stats["delivered"] == 1


@pytest.mark.asyncio
async def test_disabled_or_no_creds_noop():
    """enabled=false 或无凭据 → 不查询不发送。"""
    conn = _FakeConn(rows=[_msg()])
    pusher = outbound.OutboundPusher()

    # enabled=false
    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)), \
         patch("huanyu.outbound._config",
               return_value={**outbound._config(), "enabled": False}):
        await pusher._once()
    assert conn.fetch_count == 0

    # 无凭据（env 未设置）
    assert not os.environ.get("FEISHU_APP_ID")
    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn)):
        await pusher._once()
    assert conn.fetch_count == 0
    assert outbound._token_warned is True  # 只告警一次


@pytest.mark.asyncio
async def test_token_cached_across_rounds(monkeypatch):
    """连续两轮投递 → token 端点只请求一次（缓存生效）。"""
    _patch_env(monkeypatch)
    conn1 = _FakeConn(rows=[_msg(msg_id="11111111-1111-1111-1111-111111111111",
                                 to="feishu:ou_a")])
    conn2 = _FakeConn(rows=[_msg(msg_id="22222222-2222-2222-2222-222222222222",
                                 to="feishu:ou_b")])
    client = _FakeClientCM(_ok_responses())
    pusher = outbound.OutboundPusher()

    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn1)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client):
        await pusher._once()
    with patch("huanyu.outbound.get_pool", return_value=_Pool(conn2)), \
         patch("huanyu.outbound.httpx.AsyncClient", return_value=client):
        await pusher._once()

    token_posts = [p for p in client.posts if p[0] == _TOKEN_URL]
    send_posts = [p for p in client.posts if p[0] == _SEND_URL]
    assert len(token_posts) == 1
    assert len(send_posts) == 2
    assert pusher._stats["delivered"] == 2
