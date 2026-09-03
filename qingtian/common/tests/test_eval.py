"""G1 评估原语单测（实施文档 §九 test_eval）

judge 用 mock llm_call_json 注入，不依赖真实 LLM；run_evalset 走假 skill_exec。
"""

import json

import pytest

import common.eval as eval_mod
from common.eval import judge, run_evalset, validate_rubric


GOOD_RUBRIC = {
    "dimensions": [
        {"name": "准确性", "weight": 2, "criteria": "答案无事实错误"},
        {"name": "完整度", "weight": 1, "criteria": "覆盖用户全部诉求"},
    ],
    "pass_threshold": 0.8,
}


# ── E1: rubric 校验 ──


def test_rubric_valid():
    assert validate_rubric(GOOD_RUBRIC) == []


def test_rubric_invalid():
    assert validate_rubric("not-dict")
    assert validate_rubric({"dimensions": []})
    assert validate_rubric({"dimensions": [{"name": "x"}]})          # 缺 weight/criteria
    assert validate_rubric({"dimensions": [{"name": "x", "weight": 1, "criteria": "c"}]})  # 缺 threshold


# ── E2: judge 解析成功 → 加权评分 + 判定 ──


async def _fake_llm_json_pass(prompt, caller, default=None, **kwargs):
    return {
        "dimensions": [
            {"name": "准确性", "score": 9, "reason": "无错误"},
            {"name": "完整度", "score": 7, "reason": "部分遗漏"},
        ]
    }


@pytest.mark.asyncio
async def test_judge_weighted_score(monkeypatch):
    monkeypatch.setattr(eval_mod, "llm_call_json", _fake_llm_json_pass)
    r = await judge("某回答", GOOD_RUBRIC)
    assert r["error"] == ""
    # (2*9 + 1*7) / 3 = 25/3 ≈ 8.33 → pass
    assert r["score"] == 8.33
    assert r["verdict"] == "pass"
    assert r["dimensions"]["准确性"]["score"] == 9


@pytest.mark.asyncio
async def test_judge_fail_below_threshold(monkeypatch):
    async def _low(prompt, caller, default=None, **kwargs):
        return {"dimensions": [{"name": "准确性", "score": 4, "reason": "错"},
                               {"name": "完整度", "score": 3, "reason": "漏"}]}
    monkeypatch.setattr(eval_mod, "llm_call_json", _low)
    r = await judge("某回答", GOOD_RUBRIC)
    assert r["verdict"] == "fail"


@pytest.mark.asyncio
async def test_judge_score_clamped_to_0_10(monkeypatch):
    # 模型返回越界分（15 / -3）→ 夹取到 [0,10]，防虚高
    async def _wild(prompt, caller, default=None, **kwargs):
        return {"dimensions": [{"name": "准确性", "score": 15, "reason": "x"},
                               {"name": "完整度", "score": -3, "reason": "y"}]}
    monkeypatch.setattr(eval_mod, "llm_call_json", _wild)
    r = await judge("某回答", GOOD_RUBRIC)
    assert r["dimensions"]["准确性"]["score"] == 10
    assert r["dimensions"]["完整度"]["score"] == 0
    assert r["score"] == 6.67   # (2*10 + 1*0)/3
    assert r["verdict"] == "fail"


@pytest.mark.asyncio
async def test_judge_missing_dimension_penalized(monkeypatch):
    # 只回高分维度 → 缺失维度按 0 计入，防"只报喜"虚高
    async def _partial(prompt, caller, default=None, **kwargs):
        return {"dimensions": [{"name": "准确性", "score": 10, "reason": "ok"}]}
    monkeypatch.setattr(eval_mod, "llm_call_json", _partial)
    r = await judge("某回答", GOOD_RUBRIC)
    assert r["verdict"] == "fail"          # (2*10 + 1*0)/3 ≈ 6.67 < 8
    assert "缺失维度" in r["error"]
    assert "完整度" in r["error"]


# ── E3: judge 解析失败 → 降级 fail + error 标注 ──


@pytest.mark.asyncio
async def test_judge_parse_failure_falls_to_fail(monkeypatch):
    async def _bad(prompt, caller, default=None, **kwargs):
        return None
    monkeypatch.setattr(eval_mod, "llm_call_json", _bad)
    r = await judge("某回答", GOOD_RUBRIC)
    assert r["verdict"] == "fail"
    assert r["error"] != ""


@pytest.mark.asyncio
async def test_judge_missing_dimensions_fail(monkeypatch):
    async def _no_dims(prompt, caller, default=None, **kwargs):
        return {"foo": 1}
    monkeypatch.setattr(eval_mod, "llm_call_json", _no_dims)
    r = await judge("某回答", GOOD_RUBRIC)
    assert r["verdict"] == "fail"
    assert "dimensions" in r["error"]


@pytest.mark.asyncio
async def test_judge_illegal_rubric(monkeypatch):
    r = await judge("某回答", {"dimensions": []})
    assert r["verdict"] == "fail"
    assert "rubric 非法" in r["error"]


# ── E4: run_evalset 通过率统计 ──


async def _skill_exec_ok(input_text):
    return f"好的，已处理：{input_text}"


_CASE_RUBRIC = json.dumps({
    "dimensions": [{"name": "准确性", "weight": 1, "criteria": "答案无事实错误"}],
    "pass_threshold": 0.8,
}, ensure_ascii=False)


@pytest.mark.asyncio
async def test_run_evalset_all_pass(monkeypatch, tmp_path):
    # 2 个 case，judge 全部给 pass
    for i in (1, 2):
        (tmp_path / f"c{i}.json").write_text(
            f'{{"input": "hi", "rubric": {_CASE_RUBRIC}}}', encoding="utf-8")
    async def _good(prompt, caller, default=None, **kwargs):
        return {"dimensions": [{"name": "准确性", "score": 9, "reason": "ok"}]}
    monkeypatch.setattr(eval_mod, "llm_call_json", _good)
    r = await run_evalset(_skill_exec_ok, str(tmp_path))
    assert r["total"] == 2
    assert r["passed"] == 2
    assert r["pass_rate"] == 1.0
    assert all(c["verdict"] == "pass" for c in r["cases"])


@pytest.mark.asyncio
async def test_run_evalset_empty_dir(monkeypatch, tmp_path):
    r = await run_evalset(_skill_exec_ok, str(tmp_path))
    assert r["total"] == 0
    assert "无 case" in r["error"]


@pytest.mark.asyncio
async def test_run_evalset_skill_exec_failure_counts_as_fail(monkeypatch, tmp_path):
    async def _boom(input_text):
        raise RuntimeError("tool down")
    (tmp_path / "bad.json").write_text(
        '{"input": "x", "rubric": {"dimensions": [{"name": "d", "weight": 1, "criteria": "c"}], "pass_threshold": 0.8}}',
        encoding="utf-8",
    )
    r = await run_evalset(_boom, str(tmp_path))
    assert r["total"] == 1
    assert r["passed"] == 0
    assert "skill_exec 失败" in r["cases"][0]["error"]
