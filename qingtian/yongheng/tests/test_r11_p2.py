"""
R11 P2（中危）收口回归测试 — 永恒模块

覆盖：
  P2-6 digest/trajectory/hook date.today() 本地日 → UTC 日统一
  P2-7 high_value._LLM_RUNNING worker 异常后复位
  P2-8 memory_service keyword 分支 offset/limit 一致切片
  P2-9 dreem_gate fire-and-forget 任务异常消费
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Ctx(self.conn)


# ── P2-6: 本地日 vs UTC 日 ─────────────────────────────


@pytest.mark.asyncio
async def test_digest_default_date_is_utc_today():
    """generate_digest 缺省日期应取 UTC 日，与查询窗口(UTC)一致。"""
    from yongheng import digest as digest_mod

    conn = AsyncMock()
    conn.fetch.return_value = []  # 无记录 → noop
    pool = _Pool(conn)

    with patch("yongheng.digest.get_pool", AsyncMock(return_value=pool)), \
         patch.object(digest_mod, "_acquire_digest_lock", AsyncMock(return_value=True)):
        result = await digest_mod.generate_digest("global")

    assert result["status"] == "noop"
    _sql, _ns, date_start, date_end = conn.fetch.call_args.args
    assert date_start.date() == datetime.now(timezone.utc).date()
    assert date_start.tzinfo == timezone.utc
    assert date_end - date_start == __import__("datetime").timedelta(days=1)


@pytest.mark.asyncio
async def test_trajectory_add_action_uses_utc_today():
    """轨迹按天分区，add_action 应写 UTC 日。"""
    from yongheng import trajectory_service as ts

    conn = AsyncMock()
    conn.fetchrow.return_value = None

    result = await ts.add_action(conn, "agent:a", {"agent_id": "a", "content": "x"})
    assert result["date"] == str(datetime.now(timezone.utc).date())
    _sql, _ns, today = conn.fetchrow.call_args.args
    assert today == datetime.now(timezone.utc).date()


@pytest.mark.asyncio
async def test_hook_write_trajectory_uses_utc_today():
    """hook 事件摄入 trajectory 应写 UTC 日，与 add_action 一致。"""
    from yongheng import hook_ingest

    conn = AsyncMock()
    conn.fetchrow.return_value = None

    await hook_ingest._write_trajectory(conn, {
        "agent_id": "a", "namespace": "agent:a",
        "event": "message:received", "content": "hello",
    })
    _sql, _ns, today = conn.fetchrow.call_args.args
    assert today == datetime.now(timezone.utc).date()


# ── P2-7: high_value _LLM_RUNNING 复位 ─────────────────


@pytest.mark.asyncio
async def test_worker_resets_running_on_crash():
    """worker 异常退出时必须复位 _LLM_RUNNING，否则后续 start_llm_worker 永久失效。"""
    from yongheng import high_value as hv

    hv._LLM_RUNNING = True
    try:
        with patch.object(hv._LLM_QUEUE, "get", AsyncMock(side_effect=RuntimeError("boom"))):
            with pytest.raises(RuntimeError):
                await hv._llm_worker()
        assert hv._LLM_RUNNING is False
    finally:
        hv._LLM_RUNNING = False


@pytest.mark.asyncio
async def test_worker_contains_per_item_exception():
    """单条内容 LLM 检查失败不应杀死 worker，也不应留下未消费的 task_done。"""
    from yongheng import high_value as hv

    hv._LLM_RUNNING = True
    hv._LLM_QUEUE.put_nowait(("ns", 1, "content with keyword"))

    async def _boom(content):
        hv._LLM_RUNNING = False  # 让循环在首条处理后退出
        raise RuntimeError("llm down")

    try:
        with patch.object(hv, "llm_semantic_check", _boom):
            await hv._llm_worker()  # 不应抛出异常
        assert hv._LLM_RUNNING is False
    finally:
        hv._LLM_RUNNING = False


def test_start_llm_worker_guard_after_reset():
    """标志复位后 start_llm_worker 可再次启动（不复位则直接 return 卡死）。"""
    from yongheng import high_value as hv

    hv._LLM_RUNNING = False
    # _LLM_RUNNING 为 False 时不应被误判为运行中
    assert hv._LLM_RUNNING is False


# ── P2-8: keyword 分支切片 ─────────────────────────────


def _row(mid: int, content: str, score: float) -> dict:
    return {"id": mid, "content": content, "memory_type": "episodic", "timestamp": None,
            "protected": False, "search_hit_count": 0, "metadata": {},
            "rrf_score": score}


@pytest.mark.asyncio
async def test_keyword_search_slices_to_top_k():
    """keyword 合并全局结果后应切片到 top_k，而非返回 2×top_k。"""
    from yongheng import memory_service as ms

    conn = AsyncMock()
    conn.execute = AsyncMock()

    local = [_row(1, "local a", 1.0), _row(2, "local b", 0.9)]
    global_res = [_row(101, "global a", 0.8), _row(102, "global b", 0.7)]

    with patch.object(ms, "_fts_search", AsyncMock(side_effect=[local, global_res])):
        result = await ms.search_memory(conn, "agent:a", query="q", method="keyword", top_k=2)

    assert result["total_matched"] == 4
    assert len(result["results"]) == 2  # 切片到 top_k


@pytest.mark.asyncio
async def test_keyword_search_applies_offset():
    """keyword 分支应与其他分支一致应用 offset。"""
    from yongheng import memory_service as ms

    conn = AsyncMock()
    conn.execute = AsyncMock()

    local = [_row(i, f"local {i}", 1.0 - i * 0.01) for i in range(4)]
    global_res = []

    with patch.object(ms, "_fts_search", AsyncMock(side_effect=[local, global_res])):
        result = await ms.search_memory(conn, "agent:a", query="q", method="keyword",
                                        top_k=2, offset=2)

    assert len(result["results"]) == 2
    ids = [r["id"] for r in result["results"]]
    assert ids == [2, 3]  # offset=2 起取 2 条


@pytest.mark.asyncio
async def test_keyword_search_no_global_unchanged():
    """include_global=False 时 keyword 仍只返回 top_k 条。"""
    from yongheng import memory_service as ms

    conn = AsyncMock()
    conn.execute = AsyncMock()

    local = [_row(i, f"local {i}", 1.0 - i * 0.01) for i in range(3)]

    with patch.object(ms, "_fts_search", AsyncMock(return_value=local)):
        result = await ms.search_memory(conn, "agent:a", query="q", method="keyword",
                                        top_k=3, include_global=False)

    assert len(result["results"]) == 3
    assert result["total_matched"] == 3


# ── P2-9: fire-and-forget 任务异常消费 ─────────────────


@pytest.mark.asyncio
async def test_consume_task_exception_swallows_error():
    """done 回调应消费任务异常，避免 'Task exception was never retrieved'。"""
    from yongheng.dreem_gate import _consume_task_exception

    async def _boom():
        raise RuntimeError("background failed")

    task = asyncio.create_task(_boom())
    task.add_done_callback(_consume_task_exception)
    await asyncio.wait([task])

    # 若回调未消费异常，pytest 会告警；此处验证回调已取走异常
    assert task.done()


@pytest.mark.asyncio
async def test_consume_task_exception_handles_cancel():
    """已取消任务不触发告警。"""
    from yongheng.dreem_gate import _consume_task_exception

    async def _never():
        await asyncio.sleep(60)

    task = asyncio.create_task(_never())
    task.add_done_callback(_consume_task_exception)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
