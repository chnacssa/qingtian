"""
R11 P2（中危）收口回归测试 — 吸星模块

覆盖：
  P2-1 api.run_ingest content_hash 语义统一 + ON CONFLICT 更新 run_id
  P2-2 scheduler buffer seq_id 跨轮复用 → 幂等键改事件指纹
  P2-3 api.run_ingest_to_yongheng 注入成功即标记 injected + 注入幂等
  P2-4 crawler extraction_level 死分支 → requires_js 直达 Playwright
  P2-5 xizhenji LIKE 通配符转义
"""

from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from xixing.crawler import _compute_hash


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


# ── P2-1: content_hash 语义错配 ─────────────────────────


@pytest.mark.asyncio
async def test_run_ingest_hashes_stored_text_not_html():
    """content_hash 应对入库文本求 hash，而非 crawler 的 raw HTML hash。"""
    from xixing import api as xapi

    text = "这是采集到的正文内容。这次足够长以通过质量门。" * 20  # > 120 chars
    row = {
        "run_id": 1, "source_id": "src-1", "content_hash": "html-hash-placeholder",
        "content_size": 500, "raw_path": None,
        "metadata": {"text_length": 120}, "source_name": "Source One",
    }

    conn = AsyncMock()
    conn.fetch.return_value = [row]
    conn.fetchval.return_value = 42

    with patch.object(xapi, "_read_content", return_value=text), \
         patch.object(xapi.quality_gate, "run_blocking_gates",
                      AsyncMock(return_value={"passed": True, "quality_score": 0.9})), \
         patch.object(xapi.classifier, "classify", AsyncMock(return_value="article")), \
         patch.object(xapi.quality_gate, "run_quality_gates",
                      AsyncMock(return_value={"passed": True, "quality_score": 0.8,
                                             "gate_results": {"ok": True}})):
        result = await xapi.run_ingest(conn, run_ids=[1])

    assert result["passed"] == 1
    assert conn.fetchval.await_count == 1
    sql = conn.fetchval.call_args.args[0]
    params = conn.fetchval.call_args.args[1:]
    assert "ON CONFLICT (content_hash)" in sql
    assert "run_id = EXCLUDED.run_id" in sql
    content = params[3]  # 入库文本（截断到 text_length）
    assert content == text[:120]
    # 入库 hash 由文本计算，且 != crawler 存的 raw HTML hash
    assert params[4] == _compute_hash(content)
    assert params[4] != "html-hash-placeholder"


# ── P2-2: buffer seq_id 跨轮复用 → 事件指纹幂等 ────────


def test_event_fingerprint_deterministic():
    from xixing.scheduler import _event_fingerprint
    ev = {"seq_id": 5, "timestamp": "2026-08-16T00:00:00+00:00", "type": "message:sent",
          "content": "hello world", "session_id": "s1", "tool_name": "t"}
    assert _event_fingerprint("agent:a", ev) == _event_fingerprint("agent:a", ev)
    assert _event_fingerprint("agent:a", ev) != _event_fingerprint("agent:b", ev)


def test_event_fingerprint_differs_across_seq_reuse():
    """相同 seq_id 被 buffer_clear 重置复用后，不同事件不得再判同幂等键。"""
    from xixing.scheduler import _event_fingerprint
    ev1 = {"seq_id": 1, "timestamp": "2026-08-16T00:00:00+00:00", "type": "message:sent",
           "content": "first event", "session_id": "s1"}
    ev2 = {"seq_id": 1, "timestamp": "2026-08-16T01:00:00+00:00", "type": "message:sent",
           "content": "second event", "session_id": "s1"}
    assert _event_fingerprint("agent:a", ev1) != _event_fingerprint("agent:a", ev2)


def test_event_fingerprint_stable_across_replay():
    """崩溃重放时同一事件字典 → 指纹稳定，可正确跳过重复注入。"""
    from xixing.scheduler import _event_fingerprint
    ev = {"seq_id": 1, "timestamp": "2026-08-16T00:00:00+00:00", "type": "llm_input",
          "content": "same content", "session_id": "s1", "tool_name": ""}
    assert _event_fingerprint("agent:a", ev) == _event_fingerprint("agent:a", dict(ev))


@pytest.mark.asyncio
async def test_daily_buffer_job_writes_event_fp_metadata():
    """_daily_buffer_job 以 event_fp 做幂等键，并写入 memory metadata。"""
    from xixing import scheduler

    bus_mock = MagicMock()
    bus_mock.get_buffer_agents.return_value = ["agent:a"]
    bus_mock.buffer_snapshot.return_value = [
        {"seq_id": 1, "timestamp": "2026-08-16T00:00:00+00:00", "type": "message:sent",
         "content": "hello world this is some content", "session_id": "s1", "tool_name": ""}
    ]

    conn = AsyncMock()
    conn.fetchval.return_value = None  # 无重复指纹
    pool = _Pool(conn)

    with patch("common.bus.bus", bus_mock), \
         patch("common.db.get_pool", AsyncMock(return_value=pool)), \
         patch("yongheng.memory_service.write_memory",
               AsyncMock(return_value={"id": 1})) as wm:
        await scheduler._daily_buffer_job()

    assert wm.await_count == 1
    metadata = wm.call_args.kwargs["metadata"]
    assert "event_fp" in metadata
    assert metadata["event_fp"] == scheduler._event_fingerprint(
        "agent:a", bus_mock.buffer_snapshot.return_value[0]
    )
    bus_mock.buffer_clear.assert_called_once_with("agent:a")


# ── P2-3: 注入永恒 — 成功即标记 injected + 幂等 ────────


@pytest.mark.asyncio
async def test_ingest_to_yongheng_marks_injected_on_success():
    from xixing import api as xapi

    row = {"id": 1, "source_id": "src-1", "category": "article", "content": "some content",
           "source_name": "Source One", "source_url": "http://example.com", "title": "T"}
    conn = AsyncMock()
    conn.fetch.return_value = [row]

    with patch.object(xapi, "_find_existing_yongheng_memory", AsyncMock(return_value=None)), \
         patch("yongheng.memory_service.write_memory", AsyncMock(return_value={"id": 999})), \
         patch.object(xapi.xcfg, "get_global_namespace", return_value="global"):
        result = await xapi.run_ingest_to_yongheng(conn, dry_run=False)

    assert len(result["failed"]) == 0
    assert result["stored"][0]["memory_id"] == 999
    sql = conn.execute.call_args.args[0]
    args = conn.execute.call_args.args[1:]
    assert "injected_to_yongheng = TRUE" in sql
    assert args[0] == 999  # memory_id
    assert args[1] == 1    # knowledge id


@pytest.mark.asyncio
async def test_ingest_to_yongheng_skips_existing_memory():
    """注入幂等：memory 已存在（上次标记失败）时跳过 write_memory，直接标记。"""
    from xixing import api as xapi

    row = {"id": 1, "source_id": "src-1", "category": "article", "content": "some content",
           "source_name": "Source One", "source_url": "http://example.com", "title": "T"}
    conn = AsyncMock()
    conn.fetch.return_value = [row]

    with patch.object(xapi, "_find_existing_yongheng_memory", AsyncMock(return_value=777)), \
         patch("yongheng.memory_service.write_memory", AsyncMock()) as wm, \
         patch.object(xapi.xcfg, "get_global_namespace", return_value="global"):
        result = await xapi.run_ingest_to_yongheng(conn, dry_run=False)

    wm.assert_not_awaited()
    assert result["stored"][0]["memory_id"] == 777
    sql = conn.execute.call_args.args[0]
    assert "injected_to_yongheng = TRUE" in sql


@pytest.mark.asyncio
async def test_ingest_to_yongheng_marks_injected_even_if_memoryid_update_fails():
    """write_memory 成功后即使记录 memory_id 的 UPDATE 失败，也不应判定为失败而重复注入。"""
    from xixing import api as xapi

    row = {"id": 1, "source_id": "src-1", "category": "article", "content": "some content",
           "source_name": "Source One", "source_url": "http://example.com", "title": "T"}
    conn = AsyncMock()
    conn.fetch.return_value = [row]
    # 第一次 UPDATE（带 memory_id）抛错，第二次（仅 flag）成功
    conn.execute.side_effect = [Exception("db down"), None]

    with patch.object(xapi, "_find_existing_yongheng_memory", AsyncMock(return_value=None)), \
         patch("yongheng.memory_service.write_memory", AsyncMock(return_value={"id": 999})), \
         patch.object(xapi.xcfg, "get_global_namespace", return_value="global"):
        result = await xapi.run_ingest_to_yongheng(conn, dry_run=False)

    assert len(result["failed"]) == 0
    assert result["stored"][0]["memory_id"] == 999
    assert conn.execute.await_count == 2


# ── P2-4: crawler extraction_level 死分支 ──────────────


@pytest.mark.asyncio
async def test_fetch_source_uses_playwright_for_requires_js():
    """requires_js 源应走 Playwright（JS 渲染），而非不可达的 HTTP 路径。"""
    from xixing import crawler

    conn = AsyncMock()
    conn.fetchval.return_value = 101   # INSERT collection_runs 返回 run_id
    conn.fetchrow.return_value = None  # _get_previous_fingerprint 无历史

    source = {"id": "s1", "url": "http://example.com", "name": "S1",
              "requires_js": True, "headers": None}
    raw_html = "<html><body><div>hello world</div></body></html>"
    text_content = "hello world " * 50  # > 100 chars

    with patch.object(crawler, "_playwright_fetch", AsyncMock(return_value=(raw_html, text_content))) as pw, \
         patch.object(crawler, "_http_fetch", AsyncMock(return_value=(200, raw_html))) as http, \
         patch.object(crawler, "_llm_quality_judge",
                      AsyncMock(return_value={"is_valid": True, "content_type": "article",
                                             "quality_level": "B", "should_retry": False,
                                             "retry_suggestion": ""})), \
         patch.object(crawler, "_structure_fingerprint", return_value="fp123"), \
         patch.object(crawler, "_is_duplicate", AsyncMock(return_value=False)), \
         patch.object(crawler, "_finish_run", AsyncMock()), \
         patch.object(crawler, "_update_source_status", AsyncMock()), \
         patch("os.makedirs", return_value=None), \
         patch("builtins.open", mock_open()):
        result = await crawler._fetch_source(conn, source, dry_run=False)

    pw.assert_awaited_once()
    http.assert_not_awaited()
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_fetch_source_uses_http_for_non_js():
    """非 requires_js 源仍走 HTTP + 本地提取，不影响原路径。"""
    from xixing import crawler

    conn = AsyncMock()
    conn.fetchval.return_value = 202
    conn.fetchrow.return_value = None

    source = {"id": "s2", "url": "http://example.com", "name": "S2",
              "requires_js": False, "headers": None}
    raw_html = "<html><body>" + "<p>sentence one two three</p>" * 60 + "</body></html>"
    text_content = "sentence one two three " * 60

    with patch.object(crawler, "_playwright_fetch", AsyncMock(return_value=(raw_html, text_content))) as pw, \
         patch.object(crawler, "_http_fetch", AsyncMock(return_value=(200, raw_html))) as http, \
         patch.object(crawler, "_extract_text", return_value=text_content), \
         patch.object(crawler, "_llm_quality_judge",
                      AsyncMock(return_value={"is_valid": True, "content_type": "article",
                                             "quality_level": "B", "should_retry": False,
                                             "retry_suggestion": ""})), \
         patch.object(crawler, "_structure_fingerprint", return_value="fp123"), \
         patch.object(crawler, "_is_duplicate", AsyncMock(return_value=False)), \
         patch.object(crawler, "_finish_run", AsyncMock()), \
         patch.object(crawler, "_update_source_status", AsyncMock()), \
         patch("os.makedirs", return_value=None), \
         patch("builtins.open", mock_open()):
        result = await crawler._fetch_source(conn, source, dry_run=False)

    http.assert_awaited_once()
    pw.assert_not_awaited()
    assert result["status"] == "success"


# ── P2-5: xizhenji LIKE 通配符转义 ────────────────────


def test_escape_like():
    from xixing.xizhenji import _escape_like
    assert _escape_like("abc") == "abc"
    assert _escape_like("a%b_c") == "a\\%b\\_c"
    assert _escape_like("100%") == "100\\%"
    assert _escape_like("a\\b") == "a\\\\b"


@pytest.mark.asyncio
async def test_detect_from_audit_log_escapes_wildcards():
    """audit_uid 含 %/_ 时应转义，避免 LIKE 误匹配其他记录。"""
    from xixing import xizhenji

    conn = AsyncMock()
    conn.fetch.return_value = [
        {"audit_uid": "abc%def", "agent_id": "agent-1", "action": "block",
         "severity": "critical", "created_at": None}
    ]
    conn.fetchval.return_value = None  # 未录入过
    pool = _Pool(conn)

    with patch("xixing.xizhenji.get_pool", AsyncMock(return_value=pool)), \
         patch("zhenyue.config.get_schema_name", return_value="zhenyue"):
        captured = await xizhenji.detect_from_audit_log(days=1)

    assert captured == 1
    sql = conn.fetchval.call_args.args[0]
    pattern = conn.fetchval.call_args.args[1]
    assert "ESCAPE" in sql.upper()
    # 通配符 % 被转义为 \%，不再作为 LIKE 通配符
    assert "abc\\%def" in pattern
    assert pattern == "%abc\\%def%"
