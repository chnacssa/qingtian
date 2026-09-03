"""永恒 — 9-1 修复日 P1×3 回归测试（review 2026-08-28-永恒.md）。

  1. dreem_gate 重置命中计数补 namespace 谓词（跨租户检索破坏）
  2. UpdateProfileRequest learned 入模上限（Levenshtein 事件循环 DoS 入口）
  3. _deduplicate_learned 长度差前置跳过（编辑距离下界）
"""

import pytest
from pydantic import ValidationError
from unittest.mock import patch

from yongheng import config as ycfg
from yongheng.dreem_gate import _reset_hit_counts
from yongheng.models import UpdateProfileRequest
from yongheng.profile_service import _deduplicate_learned, _levenshtein


# ═══════════════════════════════════════════════════════
# 1. dreem_gate — 命中计数重置带 namespace 谓词
# ═══════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reset_hit_counts_scoped_by_namespace(mock_conn):
    """UPDATE SQL 必须含 namespace=$N 谓词（此前全表更新=跨租户命中数归零）。"""
    with patch.object(ycfg, "get_hit_exemption_reset_days", return_value=30):
        await _reset_hit_counts(mock_conn, "agent:a1")
    sql = mock_conn.execute.await_args.args[0]
    assert "namespace = $2" in sql


# ═══════════════════════════════════════════════════════
# 2. models — learned 入模上限
# ═══════════════════════════════════════════════════════


def test_learned_add_items_bounded():
    """条数 >50 → 422 入模拒绝。"""
    items = [{"preference": f"偏好{i}"} for i in range(51)]
    with pytest.raises(ValidationError):
        UpdateProfileRequest(namespace="agent:a1", learned_add=items)


def test_learned_add_preference_length_bounded():
    """单条 preference >200 字 → 拒绝。"""
    with pytest.raises(ValidationError):
        UpdateProfileRequest(namespace="agent:a1",
                             learned_add=[{"preference": "长" * 201}])


def test_learned_override_same_bounds():
    items = [{"preference": "x" * 201}]
    with pytest.raises(ValidationError):
        UpdateProfileRequest(namespace="agent:a1", learned_override=items)


def test_learned_add_valid_passes():
    req = UpdateProfileRequest(
        namespace="agent:a1",
        learned_add=[{"preference": "偏好简洁回复", "confidence": 0.8}],
    )
    assert req.learned_add[0]["preference"] == "偏好简洁回复"


# ═══════════════════════════════════════════════════════
# 3. profile_service — 去重长度差前置跳过
# ═══════════════════════════════════════════════════════


def test_dedup_keeps_short_and_long_separate():
    """长度差超阈值的两条不判重（编辑距离下界=长度差）。"""
    with patch.object(ycfg, "get_learned_duplicate_threshold", return_value=5):
        out = _deduplicate_learned([
            {"preference": "短偏好", "confirmations": 1},
            {"preference": "这是一条非常非常长的偏好描述" * 10, "confirmations": 9},
        ])
    assert len(out) == 2


def test_dedup_near_duplicates_still_merged():
    """长度相近且内容近似 → 仍合并（保留 confirmations 高者）。"""
    with patch.object(ycfg, "get_learned_duplicate_threshold", return_value=5):
        out = _deduplicate_learned([
            {"preference": "用户偏好简洁的回复风格", "confirmations": 1},
            {"preference": "用户偏好简洁的回复形式", "confirmations": 9},
        ])
    assert len(out) == 1
    assert out[0]["confirmations"] == 9


def test_levenshtein_distance_floor_is_length_diff():
    """性质钉死：编辑距离 ≥ 长度差（前置跳过的数学依据）。"""
    assert _levenshtein("abc", "abcdefghijklmnop") == 13
    assert _levenshtein("", "abcde") == 5
