"""
token_service 单元测试
R11 A6 回归：expires_at 必须传 aware datetime 对象给 asyncpg timestamptz 列，
不能传 .isoformat() 字符串（asyncpg 拒收 str → DataError → admin/ops_admin 登录 500）。
"""

from datetime import datetime

from zhenyue import token_service


async def test_ops_admin_expires_at_is_datetime_not_str(mock_conn):
    """ops_admin 触发 24h 过期 → 传给 asyncpg 的 expires_at 是 datetime，不是 str"""
    await token_service.create_token(mock_conn, "agent-001", role="ops_admin")
    args, _ = mock_conn.execute.call_args
    # INSERT VALUES ($1,$2,$3,$4,$5,...) → expires_at 是第 5 个位置参数（args[5]）
    expires_at = args[5]
    assert isinstance(expires_at, datetime)
    assert not isinstance(expires_at, str)
    # aware datetime（带 UTC 时区），asyncpg 原生接受
    assert expires_at.tzinfo is not None


async def test_agent_expires_at_is_none(mock_conn):
    """普通 agent 令牌不过期 → expires_at 为 None（不触发 500）"""
    await token_service.create_token(mock_conn, "agent-002", role="agent")
    args, _ = mock_conn.execute.call_args
    assert args[5] is None


async def test_returns_token_and_role(mock_conn):
    """返回 token/agent_id/role/issued_at，issued_at 是 iso 字符串（返回给调用方，非 asyncpg）"""
    result = await token_service.create_token(mock_conn, "agent-003", role="ops_admin")
    assert result["token"].startswith("zt_ns_")
    assert result["agent_id"] == "agent-003"
    assert result["role"] == "ops_admin"
    assert isinstance(result["issued_at"], str)
