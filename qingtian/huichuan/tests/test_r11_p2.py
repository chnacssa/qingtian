"""汇川 — R11 P2（中危）收口回归测试（无 DB 依赖）。

覆盖:
  1. refine.py  : LLM 失败不再无条件重置 pending 无限重刷 —— 失败计数 + 指数退避 + 失败上限转 failed
  2. cron.py    : fire-and-forget 任务统一注册 + add_done_callback 消费异常
  3. ingest.py  : xlsx 只全量解析一次，解析结果复用于 LLM 编译 + 图片注册
  4. api.py     : delete 回收路径 commonpath 包含校验 + 拒绝 `..` 逃逸
  5. api.py     : download 缺省 agent_id 时 fail-closed（从请求上下文解析身份，解析不到拒绝）
  6. import_export.py : 去重键按 owner_agent 范围（IS NOT DISTINCT FROM），防跨企业同标题误判
"""

import asyncio
import json
import os
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("QINGTIAN_ENV", "development")
os.environ.pop("HUICHUAN_FILE_TOKEN", None)  # 空 token = 不限制（内网默认）

import huichuan.api as _huichuan_api  # noqa: E402  # 直改模块对象
import huichuan.cron as _cron  # noqa: E402
from huichuan.api import (create_share_link, delete_agent_file,  # noqa: E402
                          download_agent_file, reprocess_file)
from huichuan.excel_processor import xlsx_to_entries  # noqa: E402
from huichuan.import_export import _check_duplicate, batch_import  # noqa: E402
from huichuan.ingest import ingest_file  # noqa: E402
from huichuan.refine import refine_batch, refine_single  # noqa: E402


def _pool(conn):
    pool = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


def _conn(fetchrow_result=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchval = AsyncMock(return_value=True)
    return conn


# ═══════════════════════════════════════════════════════
# 1. refine.py — LLM 失败退避 / 失败上限
# ═══════════════════════════════════════════════════════


def _refine_item(meta=None):
    return {
        "id": "u1", "raw_experience": "这是一段足够长的业务经验观察，用于触发 LLM 泛化流程。",
        "submitter": "a1", "domain": "general", "metadata": meta or {},
    }


@pytest.mark.asyncio
async def test_refine_llm_failure_sets_backoff_metadata():
    """首次失败 → 保持 pending + metadata 记 fail_count/next_retry_at（不再无条件重置）。"""
    conn = _conn()
    with ExitStack() as stack:
        stack.enter_context(patch(
            "huichuan.refine._refine_llm_call",
            new=AsyncMock(side_effect=RuntimeError("LLM down")),
        ))
        stack.enter_context(patch("huichuan.refine.kcfg.get_refine_max_failures", return_value=5))
        result = await refine_single(conn, _refine_item())

    assert result["action"] == "rejected"
    assert result["retry_in_hours"] == 1  # 指数退避第 1 档
    sql = conn.execute.await_args.args[0]
    assert "status='pending'" in sql
    payload = json.loads(conn.execute.await_args.args[1])
    assert payload["fail_count"] == 1
    assert payload["next_retry_at"]  # 退避时间戳写入
    assert "last_error" in payload


@pytest.mark.asyncio
async def test_refine_llm_failure_exhausted_turns_failed():
    """失败达上限 → 转 failed，不再自动重试（防无限重刷烧额度）。"""
    conn = _conn()
    with ExitStack() as stack:
        stack.enter_context(patch(
            "huichuan.refine._refine_llm_call",
            new=AsyncMock(side_effect=RuntimeError("LLM down")),
        ))
        stack.enter_context(patch("huichuan.refine.kcfg.get_refine_max_failures", return_value=5))
        # 已失败 4 次 → 本次第 5 次达上限
        result = await refine_single(conn, _refine_item(meta={"fail_count": 4}))

    assert result["action"] == "rejected"
    assert "转 failed" in result["reason"]
    sql = conn.execute.await_args.args[0]
    assert "status='failed'" in sql
    assert "processed_at=NOW()" in sql
    payload = json.loads(conn.execute.await_args.args[1])
    assert payload["fail_count"] == 5


@pytest.mark.asyncio
async def test_refine_batch_skips_backoff_items():
    """batch 只捞 pending 且已到 next_retry_at 的条目（退避过滤进 SQL）。"""
    conn = _conn()
    result = await refine_batch(conn, limit=5)
    assert result["status"] == "empty"
    sql = conn.fetch.await_args.args[0]
    assert "next_retry_at" in sql


# ═══════════════════════════════════════════════════════
# 2. cron.py — 任务注册 / 异常消费
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_spawn_task_consumes_exception():
    """fire-and-forget 任务异常被 done_callback 检索，且完成后从 _tasks 移除。"""
    async def _boom():
        raise RuntimeError("boom")

    task = _cron._spawn_task(_boom(), "test_boom")
    assert task in _cron._tasks
    await asyncio.sleep(0)  # 事件循环推进 → 任务完成
    await asyncio.sleep(0)  # done_callback 调度执行
    assert task.done()
    assert not task.cancelled()
    assert task.exception() is not None  # 异常已被检索（不触发"Task exception was never retrieved"）
    assert task not in _cron._tasks  # 完成后从注册表移除


@pytest.mark.asyncio
async def test_spawn_task_cancel_no_crash():
    """任务取消不触发异常回调（cancel 分支安全返回）。"""
    started = asyncio.Event()

    async def _wait():
        started.set()
        await asyncio.sleep(10)

    task = _cron._spawn_task(_wait(), "test_cancel")
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert task.cancelled()


# ═══════════════════════════════════════════════════════
# 3. ingest.py / excel_processor.py — xlsx 单次解析
# ═══════════════════════════════════════════════════════


def _sheet(markdown="|a|b|"):
    return SimpleNamespace(sheet_name="S1", row_count=2, col_count=2,
                           markdown=markdown, images=[])


def _fake_fc():
    return SimpleNamespace(processable=True, mime="application/octet-stream",
                           category="spreadsheet", fmt="xlsx", future_processable=False)


@pytest.mark.asyncio
async def test_ingest_xlsx_parses_once(tmp_path):
    """ingest_file 对同一 xlsx 只调用 process_xlsx 一次，并复用结果给 xlsx_to_entries。"""
    conn = _conn()
    process_mock = AsyncMock(return_value=[_sheet()])
    xlsx_mock = AsyncMock(return_value=[{"entries": 1, "knowledge_ids": ["k1"], "summary": "s"}])
    with ExitStack() as stack:
        stack.enter_context(patch("huichuan.excel_processor.process_xlsx", new=process_mock))
        stack.enter_context(patch("huichuan.excel_processor.xlsx_to_entries", new=xlsx_mock))
        stack.enter_context(patch("huichuan.ingest.classify", return_value=_fake_fc()))
        stack.enter_context(patch("huichuan.ingest.kcfg.get_excel_sheet_independent", return_value=True))
        result = await ingest_file(conn, b"PK-fake", "test.xlsx", storage_base=str(tmp_path))

    # 关键回归：只解析一次（修复前 ingest 路径会解析两次）
    assert process_mock.await_count == 1
    assert xlsx_mock.await_count == 1
    assert xlsx_mock.await_args.kwargs.get("sheets") == [_sheet()]
    assert result["entries"] == 1


@pytest.mark.asyncio
async def test_xlsx_to_entries_reuses_provided_sheets():
    """xlsx_to_entries 收到 sheets 时不重复解析（process_xlsx 0 次调用）。"""
    conn = _conn()
    process_mock = AsyncMock(return_value=[_sheet()])
    ingest_mock = AsyncMock(return_value={"entries": 1, "knowledge_ids": ["k1"], "summary": "s"})
    with ExitStack() as stack:
        stack.enter_context(patch("huichuan.excel_processor.process_xlsx", new=process_mock))
        stack.enter_context(patch("huichuan.ingest.ingest_text", new=ingest_mock))
        stack.enter_context(patch("huichuan.excel_processor.kcfg.get_excel_sheet_independent", return_value=True))
        results = await xlsx_to_entries(b"x", "api", "t.xlsx", "sp", conn, "huichuan",
                                        storage_base="", sheets=[_sheet()])
    assert process_mock.await_count == 0
    assert len(results) == 1


@pytest.mark.asyncio
async def test_xlsx_to_entries_parses_once_without_sheets():
    """xlsx_to_entries 独立调用（无 sheets）仍自行解析一次（向后兼容）。"""
    conn = _conn()
    process_mock = AsyncMock(return_value=[_sheet()])
    ingest_mock = AsyncMock(return_value={"entries": 0, "knowledge_ids": [], "summary": ""})
    with ExitStack() as stack:
        stack.enter_context(patch("huichuan.excel_processor.process_xlsx", new=process_mock))
        stack.enter_context(patch("huichuan.ingest.ingest_text", new=ingest_mock))
        stack.enter_context(patch("huichuan.excel_processor.kcfg.get_excel_sheet_independent", return_value=True))
        results = await xlsx_to_entries(b"x", "api", "t.xlsx", "sp", conn, "huichuan")
    assert process_mock.await_count == 1
    assert len(results) == 1


# ═══════════════════════════════════════════════════════
# 4. api.py — delete 回收路径逃逸防护
# ═══════════════════════════════════════════════════════


@pytest.fixture
def recycle_fs(tmp_path, monkeypatch):
    st = tmp_path / "storage"
    rc = tmp_path / "recycle"
    st.mkdir()
    rc.mkdir()
    monkeypatch.setattr(_huichuan_api, "_FILE_STORAGE_BASE", str(st))
    monkeypatch.setattr(_huichuan_api, "_FILE_RECYCLE_BASE", str(rc))
    return st, rc


@pytest.mark.asyncio
async def test_delete_refuses_outside_storage(recycle_fs):
    """storage_path 在存储根之外 → 拒绝软删（不 move、不 UPDATE）。"""
    st, rc = recycle_fs
    outside = st.parent / "evil.txt"
    outside.write_text("x")
    conn = _conn(fetchrow_result={"storage_path": str(outside), "status": "active",
                                  "metadata": {"owner_agent": "portal"}})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await delete_agent_file(_req(headers={"X-Agent-ID": "portal"}), "ab12")
    assert exc.value.status_code == 400
    assert conn.execute.await_count == 0  # 未软删记录
    assert outside.exists()  # 文件未被移动


@pytest.mark.asyncio
async def test_delete_refuses_dotdot_traversal(recycle_fs):
    """storage_path 含 ../ 逃逸片段 → 拒绝（commonpath 解析到存储根之外）。"""
    st, rc = recycle_fs
    (st / "sub").mkdir()
    escaped = os.path.join(str(st), "sub", "..", "..", "escape.txt")
    conn = _conn(fetchrow_result={"storage_path": escaped, "status": "active",
                                  "metadata": {"owner_agent": "portal"}})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await delete_agent_file(_req(headers={"X-Agent-ID": "portal"}), "ab12")
    assert exc.value.status_code == 400
    assert conn.execute.await_count == 0


@pytest.mark.asyncio
async def test_delete_normal_path_still_works(recycle_fs):
    """存储根内正常路径仍可软删（回归保障）。"""
    st, rc = recycle_fs
    f = st / "agents" / "portal" / "2026" / "08" / "ab12.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello")
    conn = _conn(fetchrow_result={"storage_path": str(f), "status": "active",
                                  "metadata": {"owner_agent": "portal"}})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        result = await delete_agent_file(_req(headers={"X-Agent-ID": "portal"}), "ab12")
    assert result["deleted"] is True
    assert conn.execute.await_count == 1  # 软删 UPDATE 已执行


@pytest.mark.asyncio
async def test_delete_other_agent_forbidden(recycle_fs):
    """P1-6: 非 owner/非共享调用方 → 403 拒删（不再仅凭 FILE_TOKEN 放行）。"""
    st, rc = recycle_fs
    f = st / "agents" / "agent-a" / "2026" / "08" / "ab12.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello")
    conn = _conn(fetchrow_result={"storage_path": str(f), "status": "active",
                                  "metadata": {"owner_agent": "agent-a"}})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await delete_agent_file(_req(headers={"X-Agent-ID": "agent-b"}), "ab12")
    assert exc.value.status_code == 403
    assert conn.execute.await_count == 0
    assert f.exists()  # 文件未被动


@pytest.mark.asyncio
async def test_delete_no_identity_fail_closed(recycle_fs):
    """P1-6: 调用方身份解析不到 → 403 fail-closed。"""
    st, rc = recycle_fs
    f = st / "agents" / "agent-a" / "ab12.txt"
    f.parent.mkdir(parents=True)
    f.write_text("hello")
    conn = _conn(fetchrow_result={"storage_path": str(f), "status": "active",
                                  "metadata": {"owner_agent": "agent-a"}})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await delete_agent_file(_req(), "ab12")
    assert exc.value.status_code == 403


# ═══════════════════════════════════════════════════════
# 5. api.py — download 缺省 agent_id fail-closed
# ═══════════════════════════════════════════════════════


def _download_row(tmp_path, owner="agent-a", authorized=None):
    f = tmp_path / "storage" / owner / "f.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("data")
    meta = {"owner_agent": owner}
    if authorized:
        meta["authorized_agents"] = authorized
    return {"storage_path": str(f), "original_filename": "f.txt",
            "file_size": 4, "metadata": meta}


def _req(headers=None, query=None):
    # client=127.0.0.1 对齐真实链路拓扑：X-Agent-ID 直传仅存在于 loopback 调用方
    # （羲和 IPC 代理 / 网关插件 → 本机 1996）。9-1 修复日 _resolve_caller_agent
    # 收紧后，非 loopback 的 X-Agent-ID 不再被信任（fail-closed）。
    return SimpleNamespace(headers=headers or {}, query_params=query or {},
                           client=SimpleNamespace(host="127.0.0.1", port=40000))


@pytest.mark.asyncio
async def test_download_no_identity_fail_closed(tmp_path):
    """缺省 agent_id 且无法解析调用方 → 403（不再向后兼容跳过校验）。"""
    conn = _conn(fetchrow_result=_download_row(tmp_path))
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await download_agent_file(_req(), "f1", agent_id="")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_download_other_agent_forbidden(tmp_path):
    """缺省 agent_id + X-Agent-ID 非 owner/非共享 → 403。"""
    conn = _conn(fetchrow_result=_download_row(tmp_path, owner="agent-a"))
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await download_agent_file(_req(headers={"X-Agent-ID": "agent-b"}), "f1", agent_id="")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_download_resolved_owner_allowed(tmp_path):
    """缺省 agent_id + X-Agent-ID 命中 owner → 200 下载成功。"""
    conn = _conn(fetchrow_result=_download_row(tmp_path, owner="agent-a"))
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        resp = await download_agent_file(_req(headers={"X-Agent-ID": "agent-a"}), "f1", agent_id="")
    assert resp.status_code == 200
    assert resp.body == b"data"


@pytest.mark.asyncio
async def test_download_shared_agent_allowed(tmp_path):
    """缺省 agent_id + X-Agent-ID 命中 authorized_agents → 200。"""
    conn = _conn(fetchrow_result=_download_row(tmp_path, owner="agent-a", authorized=["agent-b"]))
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        resp = await download_agent_file(_req(headers={"X-Agent-ID": "agent-b"}), "f1", agent_id="")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_download_explicit_agent_id_kept(tmp_path):
    """显式传 agent_id 的既有路径保留（owner 匹配仍可下载）。"""
    conn = _conn(fetchrow_result=_download_row(tmp_path, owner="agent-a"))
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        resp = await download_agent_file(_req(), "f1", agent_id="agent-a")
    assert resp.status_code == 200


# ═══════════════════════════════════════════════════════
# 6. import_export.py — 去重按 owner_agent 范围
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_check_duplicate_scoped_by_owner():
    """同标题不同 owner → 互不误判（都走 create），SQL 带 owner 范围。"""
    conn = _conn()
    conn.fetchrow = AsyncMock(return_value=None)
    r1 = await _check_duplicate(conn, "变压器选型", "内容A" * 10, owner_agent="agent-a")
    r2 = await _check_duplicate(conn, "变压器选型", "内容A" * 10, owner_agent="agent-b")
    assert r1["action"] == "create"
    assert r2["action"] == "create"
    sql = conn.fetchrow.await_args.args[0]
    assert "owner_agent IS NOT DISTINCT FROM" in sql


@pytest.mark.asyncio
async def test_check_duplicate_same_owner_matches():
    """同 owner 同标题同内容 → 命中精确去重（skip）。"""
    conn = _conn()
    conn.fetchrow = AsyncMock(return_value={
        "knowledge_id": "k1", "title": "T", "version": 1,
        "content": "内容A" * 10, "updated_at": "2026-01-01",
    })
    r = await _check_duplicate(conn, "T", "内容A" * 10, owner_agent="agent-a")
    assert r["action"] == "skip"
    assert r["existing_id"] == "k1"


@pytest.mark.asyncio
async def test_batch_import_create_records_owner():
    """batch_import 落库 owner_agent，且去重查询按 owner 范围。"""
    conn = _conn()
    # _check_duplicate 两次 fetchrow(None) → INSERT fetchrow 返回新行
    conn.fetchrow = AsyncMock(side_effect=[
        None, None,
        {"knowledge_id": "k-new"},
    ])
    with patch("huichuan.import_export.get_pool", return_value=_pool(conn)):
        result = await batch_import(
            [("a.txt", "这是足够长的导入内容用于验证归属范围去重与落库")],
            auto_confirm=True, owner_agent="ent-1",
        )
    assert result["created"] == 1
    sql = conn.fetchrow.await_args.args[0]  # 最后一次 = INSERT
    assert "owner_agent" in sql
    # INSERT 参数含 owner_agent 值
    assert "ent-1" in conn.fetchrow.await_args.args


# ═══════════════════════════════════════════════════════
# 7. import_export.py — update 写路径属主断言（P1-4，9-1 修复日）
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_batch_import_update_owner_mismatch_refused():
    """UPDATE 属主断言：existing_id 属他人 → fetchrow 无返回行 → skip 不覆写。"""
    conn = _conn()
    # 第一次 fetchrow：_check_duplicate 精确命中（同 owner 范围，内容不同 → update）
    # 第二次 fetchrow：UPDATE ... RETURNING 无匹配行（owner 不符被 WHERE 拦截）
    conn.fetchrow = AsyncMock(side_effect=[
        {"knowledge_id": "k1", "title": "T", "version": 1,
         "content": "旧内容" * 20, "updated_at": "2026-01-01"},
        None,
    ])
    with patch("huichuan.import_export.get_pool", return_value=_pool(conn)):
        result = await batch_import(
            [("t.txt", "T\n\n新内容足够长以通过准入校验门槛的要求" * 5)],
            auto_confirm=True, owner_agent="ent-1",
        )
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert "owner mismatch" in result["results"][0]["reason"]
    # 第二次 fetchrow 的 SQL（UPDATE）带属主断言
    update_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "owner_agent IS NULL OR owner_agent = $6" in update_sql


@pytest.mark.asyncio
async def test_batch_import_update_owner_match_proceeds():
    """UPDATE 属主断言：属主匹配（或旧条目 owner 为 NULL）→ 正常覆写。"""
    conn = _conn()
    conn.fetchrow = AsyncMock(side_effect=[
        {"knowledge_id": "k1", "title": "T", "version": 1,
         "content": "旧内容" * 20, "updated_at": "2026-01-01"},
        {"knowledge_id": "k1"},  # UPDATE RETURNING 命中
    ])
    with patch("huichuan.import_export.get_pool", return_value=_pool(conn)):
        result = await batch_import(
            [("t.txt", "T\n\n新内容足够长以通过准入校验门槛的要求" * 5)],
            auto_confirm=True, owner_agent="ent-1",
        )
    assert result["updated"] == 1


# ═══════════════════════════════════════════════════════
# 8. refine.py — LLM 产出质量门（P1-5，9-1 修复日）
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_refine_validation_gate_rejects_short_content():
    """LLM 输出内容过短 → validation_failed 转 rejected，不入库。"""
    conn = _conn()
    with patch("huichuan.refine._refine_llm_call",
               new=AsyncMock(return_value="## 短\n短")):
        result = await refine_single(conn, _refine_item())
    assert result["action"] == "held"
    assert result["reason"].startswith("validation_failed")
    sql = conn.execute.await_args.args[0]
    assert "status='rejected'" in sql
    assert conn.fetchrow.await_count == 0  # 未 INSERT knowledge_entries


@pytest.mark.asyncio
async def test_refine_accepted_uses_enterprise_visibility():
    """通过质量门的 LLM 泛化 → 入库 visibility='enterprise'（不再 'public'）。"""
    conn = _conn()
    conn.fetchrow = AsyncMock(return_value={"knowledge_id": "k-new"})
    long_content = "## 规则标题\n## 核心规则\n" + "第三四轮谈判是供应商让步窗口期的具体规律描述。" * 5
    with patch("huichuan.refine._refine_llm_call",
               new=AsyncMock(return_value=long_content)):
        result = await refine_single(conn, _refine_item())
    assert result["action"] == "accepted"
    insert_sql = conn.fetchrow.await_args.args[0]
    assert "'enterprise'" in insert_sql
    assert "'public'" not in insert_sql


# ═══════════════════════════════════════════════════════
# 9. api.py — share 属主校验（P1-6）+ reprocess 路径遏制（P1-7）
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_share_other_agent_forbidden():
    """P1-6: 非 owner 调用方 → 403，不签发外链。"""
    conn = _conn(fetchrow_result={
        "original_filename": "f.txt",
        "metadata": {"owner_agent": "agent-a"},
    })
    req = _req(headers={"X-Agent-ID": "agent-b"})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await create_share_link(req, "f1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_share_owner_gets_token(monkeypatch):
    """P1-6: owner 调用方 → 正常签发签名 token。"""
    monkeypatch.setenv("HUICHUAN_FILE_TOKEN", "test-share-secret")
    conn = _conn(fetchrow_result={
        "original_filename": "f.txt",
        "metadata": {"owner_agent": "agent-a"},
    })
    # 注：_check_file_token 读小写 "authorization"（代码原样），_resolve_caller_agent
    # 读 "X-Agent-ID"（大写原样）——裸 dict 大小写敏感，两者各给各的 key。
    req = _req(headers={"authorization": "Bearer test-share-secret",
                        "X-Agent-ID": "agent-a"})
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        resp = await create_share_link(req, "f1", ttl_minutes=30)
    assert resp["ok"] is True
    assert resp["token"].startswith("f1.")
    monkeypatch.delenv("HUICHUAN_FILE_TOKEN")


@pytest.mark.asyncio
async def test_share_no_identity_fail_closed():
    """P1-6: 身份解析不到 → 403 fail-closed（签发外链是数据外泄通道）。"""
    conn = _conn(fetchrow_result={
        "original_filename": "f.txt",
        "metadata": {"owner_agent": "agent-a"},
    })
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await create_share_link(_req(), "f1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_reprocess_refuses_path_outside_storage(tmp_path, monkeypatch):
    """P1-7: registry 命中但路径在存储/回收根之外 → 400 拒绝（防任意读）。"""
    monkeypatch.setattr(_huichuan_api, "_FILE_STORAGE_BASE", str(tmp_path / "storage"))
    monkeypatch.setattr(_huichuan_api, "_FILE_RECYCLE_BASE", str(tmp_path / "recycle"))
    conn = _conn(fetchrow_result={"original_filename": "f.txt", "file_id": "ab12"})
    evil = os.path.join(str(tmp_path), "..", "etc", "passwd")
    with patch("huichuan.api.get_pool", return_value=_pool(conn)):
        with pytest.raises(HTTPException) as exc:
            await reprocess_file(evil, _admin="x")
    assert exc.value.status_code == 400
    assert conn.execute.await_count == 0
