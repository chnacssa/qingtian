"""LLM-as-a-Judge 评估原语（设计文档 v0.2 §11.1/11.2）

目标：让 skill 行为"可度量、可回溯"——按 rubric 自动评估输出质量，并在 evalset
上批量跑出通过率，直接回答"模型质量到底行不行"。

rubric 规范:
    {"dimensions": [{"name", "weight", "criteria"}], "pass_threshold": 0.8}
evalset case 规范:
    {"input", "expected"(参考，可选), "rubric"}

链路: judge() 走 common.llm.llm_call_json（precise 档）+ 结构化评分 prompt；
parse 失败降级 fail 并标注 reason。指标: eval_runs/eval_score。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import random
from typing import Any, Awaitable, Callable

from common.llm import llm_call_json
from common import metrics

logger = logging.getLogger("common.eval")

_JUDGE_SYSTEM_TEMPLATE = (
    "你是输出质量评审员。请严格按评分标准对给定输出打分。\n"
    "评分标准（各维度 0-10 整数分）：\n"
    "{criteria_lines}\n"
    "请只输出 JSON，不要 markdown 代码块："
    '{{"dimensions": [{{"name": "维度名", "score": 0-10, "reason": "简短理由"}}]}}'
)

_JUDGE_PROMPT_TEMPLATE = (
    "待评估的输出内容：\n{response}\n\n"
    "请按评分标准逐维度打分并给出理由。"
)


def validate_rubric(rubric: dict) -> list[str]:
    """校验 rubric 规范，返回错误列表（空 = 合法）。"""
    errors: list[str] = []
    if not isinstance(rubric, dict):
        return ["rubric 必须是 dict"]
    dims = rubric.get("dimensions")
    if not isinstance(dims, list) or not dims:
        return ["dimensions 必须是非空列表"]
    for i, d in enumerate(dims):
        if not isinstance(d, dict):
            errors.append(f"dimensions[{i}] 必须是 dict")
            continue
        if not d.get("name"):
            errors.append(f"dimensions[{i}] 缺 name")
        if not isinstance(d.get("weight"), (int, float)):
            errors.append(f"dimensions[{i}] 缺数值 weight")
        if not d.get("criteria"):
            errors.append(f"dimensions[{i}] 缺 criteria")
    pt = rubric.get("pass_threshold")
    if not isinstance(pt, (int, float)):
        errors.append("缺 pass_threshold（0-1）")
    elif not (0 <= pt <= 1):
        errors.append(f"pass_threshold 须在 0-1 之间（当前 {pt}）")
    return errors


async def judge(response: str, rubric: dict, model: str | None = None) -> dict:
    """按 rubric 评估输出质量。

    Returns:
        {"score": 加权总分(0-10), "verdict": "pass"|"fail",
         "dimensions": {name: {"score", "reason"}}, "error": ""}
    """
    errors = validate_rubric(rubric)
    if errors:
        return {"score": 0, "verdict": "fail", "dimensions": {},
                "error": f"rubric 非法: {errors}"}

    dims = rubric["dimensions"]
    criteria_lines = "\n".join(
        f"- {d['name']}（权重 {d['weight']}）：{d['criteria']}" for d in dims
    )
    system_prompt = _JUDGE_SYSTEM_TEMPLATE.format(criteria_lines=criteria_lines)
    prompt = _JUDGE_PROMPT_TEMPLATE.format(response=response)

    result = await llm_call_json(
        prompt=prompt,
        caller="eval.judge",
        default=None,
        max_tokens=800,
        temperature=0,
        model=model or "",
        system_prompt=system_prompt,
    )
    if not isinstance(result, dict):
        return {"score": 0, "verdict": "fail", "dimensions": {},
                "error": "judge 解析失败（LLM 未返回有效 JSON）"}

    raw_dims = result.get("dimensions")
    if not isinstance(raw_dims, list):
        return {"score": 0, "verdict": "fail", "dimensions": {},
                "error": "judge 返回缺少 dimensions 列表"}

    scored: dict[str, dict] = {}
    total_w = 0.0
    total_s = 0.0
    missing: list[str] = []
    # 按 rubric 维度逐个匹配：LLM 缺失/多出的维度不影响加权（缺失按 0 分计入，
    # 防"只回了高分维度"的虚高）；分数夹取 0-10，防模型返回越界值。
    for d in dims:
        name = d["name"]
        w = float(d.get("weight", 0) or 0)
        total_w += w
        matched = next((x for x in raw_dims
                        if isinstance(x, dict) and x.get("name") == name), None)
        if matched is None:
            missing.append(name)
            continue
        score = matched.get("score")
        if not isinstance(score, (int, float)):
            missing.append(name)
            continue
        score = max(0.0, min(float(score), 10.0))
        scored[name] = {"score": score, "reason": str(matched.get("reason", ""))}
        total_s += w * score

    score = total_s / total_w if total_w else 0.0
    threshold = rubric.get("pass_threshold", 0.8)
    verdict = "pass" if (score / 10.0) >= threshold else "fail"
    note = f"缺失维度按 0 分计入: {missing}" if missing else ""

    metrics.counter("eval_runs", {"verdict": verdict})
    metrics.histogram("eval_score", value=round(score, 2))
    return {"score": round(score, 2), "verdict": verdict,
            "dimensions": scored, "error": note}


async def run_evalset(skill_exec: Callable[[Any], Awaitable[str]],
                      evalset_dir: str,
                      sample_limit: int = 0) -> dict:
    """批量评估：对 evalset_dir 下每个 <case>.json 执行 skill_exec(input) → judge。

    Args:
        skill_exec: async (input) -> str，skill 对单个输入的文本响应
        evalset_dir: 存放 <case>.json 的目录
        sample_limit: >0 时随机抽 N 条（防耗时/表膨胀）；0 = 全量

    Returns:
        {"total", "passed", "pass_rate", "cases": [{case,score,verdict,error}]}
    """
    files = sorted(glob.glob(os.path.join(evalset_dir, "*.json")))
    if not files:
        return {"total": 0, "passed": 0, "pass_rate": 0.0, "cases": [],
                "error": f"evalset 目录无 case 文件: {evalset_dir}"}
    if sample_limit > 0 and sample_limit < len(files):
        files = random.sample(files, sample_limit)

    cases: list[dict] = []
    passed = 0
    for path in files:
        base = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as fh:
                case = json.load(fh)
            response = await skill_exec(case.get("input"))
            if not isinstance(response, str):
                response = str(response)
        except Exception as e:
            logger.warning("evalset case %s 执行失败: %s", base, e)
            cases.append({"case": base, "score": 0, "verdict": "fail",
                          "error": f"skill_exec 失败: {e}"[:200]})
            continue

        j = await judge(response, case.get("rubric", {}))
        ok = j.get("verdict") == "pass"
        if ok:
            passed += 1
        cases.append({"case": base, "score": j.get("score", 0),
                      "verdict": j.get("verdict", "fail"),
                      "error": j.get("error", "")})

    total = len(cases)
    return {"total": total, "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "cases": cases}
