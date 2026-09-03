"""execute_api 失败冷却窗测试 — 防失败重发再起新执行（2026-08-29 任务 202→203 实锤）

线上事故：execute 失败（LLM 推理预算爆 → HTTP 500）后调用方**同秒重发同一消息**
（同 message_id）——原幂等层只有 cached-success / in-flight 两种命中，失败后
_INFLIGHT 已清 → 重发被视为新请求 → 新 bid_records 又起一单（202→203 连环失败）。

修复：失败出口记 `_IDEM_FAILURES[key]=(ts, err)`；同 key 冷却窗（120s）内再来
直接 429 RECENTLY_FAILED，不再起新执行；成功即清除；过期可重跑。

纯逻辑测试（假 runtime/handle 直调端点函数），不依赖数据库。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException

import osskill.execute_api as ea
from osskill.execute_api import ExecuteRequest, api_execute_skill


class _State:
    def __init__(self, agent_id="user-1"):
        self.agent_id = agent_id


class _FakeRequest:
    def __init__(self):
        self.headers = {}
        self.state = _State()


class _FakeHandle:
    def __init__(self, exc=None, result=None):
        self.exc = exc
        self.result = result or {"ok": True, "reply": "done"}
        self.calls = 0

    async def execute(self, params):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


class _FakeRuntime:
    def __init__(self, handle):
        self._h = handle

    async def check_skill_access(self, skill_name, agent_id):
        return True

    async def get_handle(self, skill_name, agent_id):
        return self._h


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ea._IDEM_FAILURES.clear()
    ea._IDEM_RESULTS.clear()
    ea._IDEM_INFLIGHT.clear()
    handle = _FakeHandle(exc=ValueError("mock llm 500: reasoning budget exhausted"))
    monkeypatch.setattr(api_execute_skill, "_runtime", _FakeRuntime(handle), raising=False)
    yield handle
    ea._IDEM_FAILURES.clear()
    ea._IDEM_RESULTS.clear()
    ea._IDEM_INFLIGHT.clear()


def _req(message_id):
    return ExecuteRequest(
        params={"payload": {"_raw_text": "生成投标文件"}},
        agent_id="agent-1",
        user_id="user-1",
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_fail_then_same_key_429(_reset):
    """第一次执行失败 500；同 key 同秒重发 → 429 RECENTLY_FAILED，不再执行。"""
    handle = _reset
    with pytest.raises(HTTPException) as ei:
        await api_execute_skill("sales", _req("m-1"), _FakeRequest())
    assert ei.value.status_code == 500
    assert ei.value.detail["code"] == "EXECUTE_FAILED"
    assert handle.calls == 1
    assert ea._IDEM_FAILURES, "失败出口应记录 _IDEM_FAILURES"

    # 同秒重发（202→203 实锤场景）→ 429 拦截，handle 不再被调
    with pytest.raises(HTTPException) as ei2:
        await api_execute_skill("sales", _req("m-1"), _FakeRequest())
    assert ei2.value.status_code == 429
    assert ei2.value.detail["code"] == "RECENTLY_FAILED"
    assert "刚执行失败" in ei2.value.detail["message"]
    assert handle.calls == 1, "冷却窗内重发不得再起新执行"
    assert "m-1" not in ea._IDEM_INFLIGHT, "429 路径不留脏在途标记"


@pytest.mark.asyncio
async def test_cooldown_expiry_allows_new_execution(_reset, monkeypatch):
    """冷却窗过期 → 同 key 可重跑（用户有意重试不受影响），成功后失败记录清除。"""
    handle = _reset
    with pytest.raises(HTTPException):
        await api_execute_skill("sales", _req("m-2"), _FakeRequest())
    assert handle.calls == 1
    # 时间快进越过冷却窗；handle 切成功
    key = next(iter(ea._IDEM_FAILURES))
    ea._IDEM_FAILURES[key] = (time.time() - ea._IDEM_FAIL_COOLDOWN_S - 1,
                              ea._IDEM_FAILURES[key][1])
    handle.exc = None
    result = await api_execute_skill("sales", _req("m-2"), _FakeRequest())
    assert result["ok"] is True
    assert handle.calls == 2
    assert not ea._IDEM_FAILURES, "成功后失败记录应清除"
    # 成功结果进缓存：再发同 key 直接回缓存
    result2 = await api_execute_skill("sales", _req("m-2"), _FakeRequest())
    assert result2["ok"] is True
    assert handle.calls == 2


@pytest.mark.asyncio
async def test_different_key_not_blocked(_reset):
    """新消息（新 message_id=新 key）不受冷却窗影响——用户稍后有意重发可正常执行。"""
    handle = _reset
    with pytest.raises(HTTPException):
        await api_execute_skill("sales", _req("m-3"), _FakeRequest())
    handle.exc = None
    result = await api_execute_skill("sales", _req("m-4"), _FakeRequest())
    assert result["ok"] is True
    assert handle.calls == 2


def test_idem_cleanup_purges_expired_failures(_reset):
    """_idem_cleanup 清掉过期失败记录（字典不无限增长）。"""
    ea._IDEM_FAILURES["k-old"] = (time.time() - ea._IDEM_FAIL_COOLDOWN_S - 1, "err")
    ea._IDEM_FAILURES["k-new"] = (time.time(), "err")
    ea._idem_cleanup()
    assert "k-old" not in ea._IDEM_FAILURES
    assert "k-new" in ea._IDEM_FAILURES
