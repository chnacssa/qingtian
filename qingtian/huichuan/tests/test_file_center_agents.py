"""汇川文件中心 — Agent 下拉数据源测试。

要求: /v1/huichuan/agents 从镇岳（zhenyue）agents 登记表取**本地活跃** agent
（status='active'），而非 huanyu.agents（跨底座通信目录，可能含其他底座的 Agent）。
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("QINGTIAN_ENV", "development")

from huichuan.api import list_agents_for_file_center

_REPO = Path(__file__).resolve().parents[1]


def _pool(rows):
    pool = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield pool

    pool.acquire = _acquire
    pool.fetch = AsyncMock(return_value=rows)
    return pool


# ── 数据源正确性 ──


@pytest.mark.asyncio
async def test_list_agents_queries_zhenyue_active_only():
    """下拉数据来自镇岳表，且只取本地活跃 agent。"""
    pool = _pool([
        {"agent_id": "sales-01", "name": "销售岗"},
        {"agent_id": "proc-01", "name": "采购岗"},
    ])
    with patch("huichuan.api.get_pool", return_value=pool):
        result = await list_agents_for_file_center()

    assert result["agents"] == [
        {"agent_id": "sales-01", "name": "销售岗"},
        {"agent_id": "proc-01", "name": "采购岗"},
    ]
    sql = pool.fetch.await_args.args[0]
    assert "huanyu.agents" not in sql       # 不再用跨底座目录
    assert "zhenyue.agents" in sql          # 用镇岳本地登记表
    assert "status = 'active'" in sql       # 只取本地活跃 agent
    assert "agent_id::text" not in sql      # 镇岳 agent_id 本就是 TEXT，无需 cast


# ── 端点契约不变（source-inspection）──


def test_agent_select_contract_unchanged():
    """内嵌页面下拉仍读 /v1/huichuan/agents 的 {agent_id, name}。"""
    src = (_REPO / "api.py").read_text(encoding="utf-8")
    assert "fetch('/v1/huichuan/agents')" in src
    assert "option value=" in src  # 下拉渲染契约保留
