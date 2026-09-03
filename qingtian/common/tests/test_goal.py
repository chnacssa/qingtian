"""G2 目标监控单测（实施文档 §九 test_goal）

Goal 状态转换 / progress 单调 / fail；GoalTracker 的 persist_hook 自动触发。
"""

import asyncio

import pytest

from common.goal import Goal, GoalTracker


# ── G2-1: 状态转换 ──


def test_goal_state_transitions():
    g = Goal(goal_id="g1", objective="写报告")
    assert g.status == "pending"
    g.start()
    assert g.status == "running"
    g.complete()
    assert g.status == "done"
    assert g.progress == 1.0


def test_goal_fail_records_error():
    g = Goal(goal_id="g2", objective="x")
    g.start()
    g.fail("步数耗尽")
    assert g.status == "failed"
    assert g.error == "步数耗尽"


def test_goal_progress_monotonic_and_clamped():
    g = Goal(goal_id="g3", objective="x")
    g.update_progress(0.5)
    g.update_progress(0.2)   # 不倒退
    assert g.progress == 0.5
    g.update_progress(1.5)   # 夹取上限
    assert g.progress == 1.0
    g.update_progress(-1.0)  # 夹取下限、不倒退
    assert g.progress == 1.0


def test_goal_to_dict():
    g = Goal(goal_id="g4", objective="x", subgoals=[{"id": "s1", "done": False}])
    d = g.to_dict()
    assert d["goal_id"] == "g4"
    assert d["status"] == "pending"
    assert d["subgoals"][0]["id"] == "s1"


# ── G2-2: tracker ──


def test_tracker_create_get_list():
    t = GoalTracker()
    g1 = t.create("A")
    g2 = t.create("B")
    g1.start()
    assert t.get(g1.goal_id) is g1
    assert len(t.list()) == 2
    assert len(t.list(status="running")) == 1


@pytest.mark.asyncio
async def test_tracker_persist_hook_fires_on_change():
    seen = []

    async def hook(goal):
        seen.append((goal.goal_id, goal.status, goal.progress))

    t = GoalTracker(persist_hook=hook)
    g = t.create("A")
    g.start()
    g.update_progress(0.5)
    g.complete()
    await asyncio.sleep(0)  # 让 fire-and-forget 任务跑完
    assert len(seen) >= 3
    assert seen[0][1] == "running"     # start 触发
    assert seen[-1][1] == "done"       # complete 触发
    assert seen[-1][2] == 1.0
