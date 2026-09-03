"""汇川 — R11 P1 安全修复回归测试（无 DB 依赖）。

覆盖:
  - mcp._assert_storage_path   : ingest_file 存储目录 allow-list（防任意文件读）
  - mcp._resolve_mcp_caller    : MCP 调用方身份解析（无 ctx/无 state → fail-closed None）
  - feishu._safe_filename      : 文件名清洗（防 ../ 路径穿越）
  - feishu._os_path_contained  : realpath 包含校验（双保险）
  - feishu.handle_feishu_file_event : 恶意文件名中性化 / 10-50MB 内联入库（不再假排队）/ >50MB 拒绝
  - api.get_abstract           : 越权防护（调用方身份须与目标 agent 一致）
"""

import os
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from huichuan.api import get_abstract
from huichuan.mcp import _assert_storage_path, _resolve_mcp_caller
from huichuan.receiver.feishu import (
    _os_path_contained,
    _safe_filename,
    handle_feishu_file_event,
)


# ═══════════════════════════════════════════════════════
# mcp._assert_storage_path — ingest_file 任意文件读防线
# ═══════════════════════════════════════════════════════


def test_assert_storage_path_inside_allowed(tmp_path):
    root = tmp_path / "storage"
    inner = root / "2026" / "08"
    inner.mkdir(parents=True)
    target = inner / "plan.pdf"
    target.write_bytes(b"x")
    with patch("huichuan.mcp.get_storage_base", return_value=str(root)):
        assert _assert_storage_path(str(target)) == str(target.resolve())


def test_assert_storage_path_outside_denied(tmp_path):
    root = tmp_path / "storage"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"x")
    with patch("huichuan.mcp.get_storage_base", return_value=str(root)):
        assert _assert_storage_path(str(outside)) is None


def test_assert_storage_path_traversal_denied(tmp_path):
    """P1 (R?): ../ 相对路径逃逸存储根 → 拒绝。"""
    root = tmp_path / "storage"
    (root / "sub").mkdir(parents=True)
    escaped = os.path.join(str(root), "sub", "..", "..", "escaped.txt")  # 解析到 storage 之外
    with patch("huichuan.mcp.get_storage_base", return_value=str(root)):
        assert _assert_storage_path(escaped) is None


def test_assert_storage_path_root_itself_denied(tmp_path):
    """存储根目录本身（非其内部文件）也不放行。"""
    root = tmp_path / "storage"
    root.mkdir()
    with patch("huichuan.mcp.get_storage_base", return_value=str(root)):
        assert _assert_storage_path(str(root)) is None


# ═══════════════════════════════════════════════════════
# mcp._resolve_mcp_caller — MCP 调用方身份解析（fail-closed）
# ═══════════════════════════════════════════════════════


class _FakeState:
    def __init__(self, agent_id):
        self.agent_id = agent_id


class _FakeReq:
    def __init__(self, state=None):
        self.state = state


class _FakeCtx:
    def __init__(self, req=None):
        self.request = req


def test_resolve_caller_from_ctx_state():
    """网关中间件注入 agent_id → 解析成功。"""
    ctx = _FakeCtx(req=_FakeReq(state=_FakeState("agent-1")))
    assert _resolve_mcp_caller(ctx) == "agent-1"


def test_resolve_caller_none_ctx_fail_closed():
    """ctx 为 None（stdio/未认证）→ fail-closed None。"""
    assert _resolve_mcp_caller(None) is None


def test_resolve_caller_no_request_fail_closed():
    """ctx 存在但无 request → fail-closed None。"""
    assert _resolve_mcp_caller(_FakeCtx(req=None)) is None


def test_resolve_caller_no_state_fail_closed():
    """request 存在但无 state.agent_id → fail-closed（空身份 = public-only，不泄漏 private）。"""
    assert not _resolve_mcp_caller(_FakeCtx(req=_FakeReq(state=None)))


def test_resolve_caller_empty_agent_id_fail_closed():
    """agent_id 为空串 → 视为无调用方（public-only）。"""
    ctx = _FakeCtx(req=_FakeReq(state=_FakeState("")))
    assert _resolve_mcp_caller(ctx) == ""


# ═══════════════════════════════════════════════════════
# feishu._safe_filename / _os_path_contained
# ═══════════════════════════════════════════════════════


def test_safe_filename_strips_path_traversal():
    """P1 (R?): 恶意 file_name 去掉 ../ 与分隔符。"""
    assert _safe_filename("../../etc/passwd") == "passwd"
    assert _safe_filename("..\\..\\win.txt") == "win.txt"
    assert _safe_filename("/absolute/path/report.pdf") == "report.pdf"


def test_safe_filename_keeps_chinese_and_ext():
    """中文/数字/点/横线保留，空格等非法字符归一为下划线。"""
    assert _safe_filename("技术方案-2026 v2.0.docx") == "技术方案-2026_v2.0.docx"


def test_safe_filename_fallback():
    """清洗后为空 → 返回 'file'。"""
    assert _safe_filename("") == "file"
    assert _safe_filename("///") == "file"


def test_os_path_contained(tmp_path):
    parent = tmp_path / "storage"
    inner = parent / "2026" / "08"
    inner.mkdir(parents=True)
    inside = inner / "a.txt"
    inside.write_text("x")
    assert _os_path_contained(str(inside), str(parent))

    outside = tmp_path / "b.txt"
    outside.write_text("x")
    assert not _os_path_contained(str(outside), str(parent))

    # 含 ../ 的相对路径逃逸
    assert not _os_path_contained(str(parent / ".." / "b.txt"), str(parent))


# ═══════════════════════════════════════════════════════
# feishu.handle_feishu_file_event
# ═══════════════════════════════════════════════════════


class _FakeFeishuClient:
    def __init__(self, data: bytes = b"hello world"):
        self._data = data

    async def download_resource(self, file_key, file_type):
        return self._data


def _run_feishu_event(tmp_path, event, client=None):
    """组装 mock：download_resource 返回 bytes + ingest_file 打桩。"""
    stack = ExitStack()
    stack.enter_context(
        patch("huichuan.receiver.feishu.ingest_file",
              new=AsyncMock(return_value={"storage_path": "mock/path.pdf", "entries": 1}))
    )
    return stack, handle_feishu_file_event(
        None, event, client or _FakeFeishuClient(), storage_base=str(tmp_path)
    )


@pytest.mark.asyncio
async def test_feishu_traversal_name_neutralized(tmp_path):
    """P1 (R?): file_name 含 ../ 穿越 → 清洗后入库，不产生逃逸文件。"""
    event = {
        "file_key": "k1",
        "file_type": "txt",
        "file_name": "../../evil.txt",
        "file_size": 500,
    }
    stack, coro = _run_feishu_event(tmp_path, event)
    with stack:
        result = await coro

    assert result["action"] == "ingested"
    assert result["file_name"] == "../../evil.txt"  # 原样回显给飞书
    # 落盘文件名无路径分隔符、无 .. 逃逸片段
    stored = os.path.basename(result["storage_path"])
    assert "/" not in stored and "\\" not in stored and ".." not in stored


@pytest.mark.asyncio
async def test_feishu_10_to_50mb_ingests_inline(tmp_path):
    """P1 (R?): 10-50MB 区间此前假排队永不入库 → 现内联摄入。"""
    event = {
        "file_key": "k2",
        "file_type": "pdf",
        "file_name": "big.pdf",
        "file_size": 20 * 1024 * 1024,  # 20MB
    }
    stack, coro = _run_feishu_event(tmp_path, event)
    with stack:
        result = await coro

    assert result["action"] == "ingested"  # 不再是死分支 "queued"


@pytest.mark.asyncio
async def test_feishu_over_50mb_rejected(tmp_path):
    """> 50MB 超飞书 download_resource 上限 → 拒绝。"""
    event = {
        "file_key": "k3",
        "file_type": "pdf",
        "file_name": "huge.pdf",
        "file_size": 51 * 1024 * 1024,
    }
    stack, coro = _run_feishu_event(tmp_path, event)
    with stack:
        result = await coro

    assert result["action"] == "skipped"
    assert "too large" in result["reason"]


@pytest.mark.asyncio
async def test_feishu_unsupported_type_skipped(tmp_path):
    event = {
        "file_key": "k4",
        "file_type": "exe",
        "file_name": "evil.exe",
        "file_size": 100,
    }
    stack, coro = _run_feishu_event(tmp_path, event)
    with stack:
        result = await coro

    assert result["action"] == "skipped"
    assert "unsupported" in result["reason"]


# ═══════════════════════════════════════════════════════
# api.get_abstract — 越权防护
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_abstract_other_agent_forbidden():
    """P1 (R?): 调用方 agent 与目标不一致 → 403。"""
    with pytest.raises(HTTPException) as ei:
        await get_abstract("agent-b", caller="agent-a")
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_get_abstract_no_caller_forbidden():
    """无调用方身份（未认证）→ fail-closed 403。"""
    with pytest.raises(HTTPException) as ei:
        await get_abstract("agent-a", caller=None)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_get_abstract_own_agent_allowed():
    """调用方与目标一致 → 正常返回（无订阅 → 空摘要）。"""
    pool = MagicMock()
    acquire_cm = AsyncMock()
    acquire_cm.fetch.return_value = []
    pool.acquire.return_value = acquire_cm

    with patch("huichuan.api.get_pool", new=AsyncMock(return_value=pool)):
        result = await get_abstract("agent-a", caller="agent-a")

    assert result["agent_id"] == "agent-a"
    assert result["abstract"] == []
    assert result["count"] == 0
