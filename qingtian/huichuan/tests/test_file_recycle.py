"""huichuan — 文件 30 天冷静期软删测试（2026-08-13）。

波哥需求：门户/投标文件删除改软删——文件移集中回收区，30 天后 purge 真删，
仅平台可恢复（门户用户不可见、不可自恢复，无回收站 UI）。
范围：delete_agent_file（汇川统一删除入口）+ restore + cron purge。
"""

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("QINGTIAN_ENV", "development")
os.environ.pop("HUICHUAN_FILE_TOKEN", None)  # 空 token = 不限制（内网默认）

import huichuan.api as _huichuan_api  # noqa: E402  # 直改模块对象，避免 dotted-string 解析被 sys.modules 干扰
from huichuan.api import delete_agent_file, restore_agent_file  # noqa: E402
from huichuan.cron import _purge_expired_files_job  # noqa: E402


def _conn(fetchrow_result=None, fetch_result=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    return conn


def _pool(conn):
    pool = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


@pytest.fixture
def fs(tmp_path, monkeypatch):
    """真实临时文件系统：storage + recycle 分离，文件在 storage/agents/portal/2026/08/。"""
    st = tmp_path / "storage"
    rc = tmp_path / "recycle"
    (st / "agents" / "portal" / "2026" / "08").mkdir(parents=True)
    rc.mkdir()
    monkeypatch.setattr(_huichuan_api, "_FILE_STORAGE_BASE", str(st))
    monkeypatch.setattr(_huichuan_api, "_FILE_RECYCLE_BASE", str(rc))
    f = st / "agents" / "portal" / "2026" / "08" / "ab12.txt"
    f.write_text("hello")
    return st, rc, f


def _put_in_recycle(f, rc):
    """把文件从 storage 移到回收区（模拟 delete 后的状态），返回回收区路径。"""
    rel = f.relative_to(f.parents[4])
    recycled = rc / rel
    recycled.parent.mkdir(parents=True, exist_ok=True)
    f.rename(recycled)
    return recycled


def _req(headers=None):
    """P1-6 属主门对齐：delete 需 loopback X-Agent-ID 提供调用方身份。"""
    return SimpleNamespace(query_params={}, headers=headers or {"X-Agent-ID": "portal"},
                           client=SimpleNamespace(host="127.0.0.1", port=40000))


# ── delete：软删（移回收区 + status=deleted）──


@pytest.mark.asyncio
async def test_delete_soft_moves_to_recycle_and_flags_deleted(fs):
    """删除 = 文件移回收区 + UPDATE status='deleted', purge_at，不物理删不 DELETE。"""
    st, rc, f = fs
    conn = _conn(fetchrow_result={"storage_path": str(f), "status": "active",
                                  "metadata": {"owner_agent": "portal"}})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        result = await delete_agent_file(_req(), "ab12")

    assert result["deleted"] is True
    # 原文件消失，回收区出现（mirror 相对结构）
    assert not f.exists()
    rel = f.relative_to(st)
    assert (rc / rel).exists()
    assert (rc / rel).read_text() == "hello"
    # UPDATE 软删字段，非 DELETE
    sql = conn.execute.await_args.args[0]
    assert "status='deleted'" in sql
    assert "purge_at=NOW() + INTERVAL '30 days'" in sql
    assert sql.lstrip().upper().startswith("UPDATE")
    assert "DELETE FROM" not in sql


@pytest.mark.asyncio
async def test_delete_missing_physical_file_still_soft_flags(fs):
    """物理文件缺失时仍软删记录（不因文件已丢而报错）。"""
    st, rc, f = fs
    # 文件实际不存在（仅记录指向一个不存在的路径）
    ghost = st / "agents" / "portal" / "2026" / "08" / "zz.txt"
    conn = _conn(fetchrow_result={"storage_path": str(ghost), "status": "active",
                                  "metadata": {"owner_agent": "portal"}})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        result = await delete_agent_file(_req(), "zz")
    assert result["deleted"] is True
    assert "status='deleted'" in conn.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_delete_already_deleted_idempotent(fs):
    """已软删的文件重复删除 → 幂等跳过（不再 move / 不报错）。"""
    st, rc, f = fs
    conn = _conn(fetchrow_result={"storage_path": str(f), "status": "deleted"})
    req = SimpleNamespace(query_params={}, headers={})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        result = await delete_agent_file(req, "ab12")
    assert result["deleted"] is True
    assert conn.execute.await_count == 0  # 未再执行 UPDATE
    assert f.exists()  # 文件未被移动


# ── restore：平台恢复（移回 storage + status=active）──


@pytest.mark.asyncio
async def test_restore_moves_back_and_activates(fs):
    """restore = 文件从回收区移回原 storage 路径 + status='active', purge_at=NULL。"""
    st, rc, f = fs
    recycled = _put_in_recycle(f, rc)
    conn = _conn(fetchrow_result={"storage_path": str(recycled)})
    req = SimpleNamespace(query_params={}, headers={})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        result = await restore_agent_file(req, "ab12", _admin="admin:console")

    assert result["restored"] is True
    assert f.exists()
    assert f.read_text() == "hello"
    assert not recycled.exists()
    sql = conn.execute.await_args.args[0]
    assert "status='active'" in sql
    assert "deleted_at=NULL" in sql
    assert "purge_at=NULL" in sql


@pytest.mark.asyncio
async def test_restore_missing_recycle_row_404(fs):
    """回收区查无此行（未删除/不存在）→ 404。"""
    conn = _conn(fetchrow_result=None)
    req = SimpleNamespace(query_params={}, headers={})
    from fastapi import HTTPException

    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await restore_agent_file(req, "nope", _admin="admin:console")
    assert exc.value.status_code == 404


# ── purge：30 天到期真删 ──


@pytest.mark.asyncio
async def test_purge_expired_removes_file_and_record(fs):
    """purge_at 到期的软删文件 → 物理删回收区文件 + DELETE 记录。"""
    st, rc, f = fs
    recycled = _put_in_recycle(f, rc)
    conn = _conn(
        fetchrow_result=None,
        fetch_result=[
            {"file_id": "ab12", "storage_path": str(recycled), "original_filename": "a.txt"},
        ],
    )
    with patch("huichuan.cron.get_pool", return_value=_pool(conn)):
        await _purge_expired_files_job()

    assert not recycled.exists()  # 物理文件真删
    assert conn.execute.await_count == 1
    sql = conn.execute.await_args.args[0]
    assert sql.lstrip().upper().startswith("DELETE")


@pytest.mark.asyncio
async def test_purge_expired_missing_file_still_deletes_record(fs):
    """回收区文件已不存在时，仍删除记录（不残留幽灵行）。"""
    st, rc, f = fs
    ghost = rc / "agents" / "portal" / "2026" / "08" / "ghost.txt"  # 文件不存在
    conn = _conn(
        fetchrow_result=None,
        fetch_result=[
            {"file_id": "ghost", "storage_path": str(ghost), "original_filename": "g.txt"},
        ],
    )
    with patch("huichuan.cron.get_pool", return_value=_pool(conn)):
        await _purge_expired_files_job()
    assert conn.execute.await_count == 1  # 记录仍被删
