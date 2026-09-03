"""执策提取器 — Task → Workflow 骨架提取

从已完成的 Task 中提取可复用的 Workflow 模板骨架，识别变量占位符。
"""
import re
import json
import logging
from common.db import get_pool
from . import config as cfg

logger = logging.getLogger("zhice.extractor")
SCHEMA = cfg.get_schema_name()

# 常见变量模式 regex — 用于识别指令中的硬编码值
_VARIABLE_PATTERNS = [
    (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "{ip_address}"),
    (r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b", "{mac_address}"),
    (r"(?:/[a-zA-Z0-9._-]+)+/[a-zA-Z0-9._-]+", "{file_path}"),
    (r"\bhttps?://[^\s,;]+", "{url}"),
    (r"\b\d{1,5}\b", "{port}"),
    (r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", "{email}"),
    (r"\bv\d+\.\d+(?:\.\d+)?\b", "{version}"),
]


def _detect_hints(text: str) -> list[str]:
    """识别文本中的硬编码值，返回变量替换提示"""
    hints = []
    for pattern, placeholder in _VARIABLE_PATTERNS:
        matches = re.findall(pattern, text)
        for m in matches[:2]:  # 每种最多 2 条
            hints.append(f"指令含 '{m}'，建议替换为变量 {placeholder}")
    return hints


async def extract_workflow(task_id: int) -> dict:
    """从 Task 提取 Workflow 骨架

    Returns:
        {
            "source_task_id": int,
            "name": str,
            "description": str,
            "definition": {steps: [...], acceptance_criteria: [...], timeout_minutes: int},
            "hints": [...],
        }
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        task = await conn.fetchrow(
            f"SELECT * FROM {SCHEMA}.tasks WHERE task_id = $1", task_id,
        )
        if not task:
            return {"success": False, "error": f"Task {task_id} 不存在"}

        steps = await conn.fetch(
            f"SELECT * FROM {SCHEMA}.steps "
            f"WHERE task_id = $1 ORDER BY step_index",
            task_id,
        )

        task = dict(task)
        steps = [dict(s) for s in steps]

        definition_steps = []
        all_hints = []

        for s in steps:
            instruction = s["instruction"]
            hints = _detect_hints(instruction)
            all_hints.extend(hints)

            step_def = {
                "step_index": s["step_index"],
                "title": s["title"],
                "instruction": instruction,
            }
            if s.get("depends_on"):
                step_def["depends_on"] = s["depends_on"]
            if s.get("acceptance_criteria"):
                step_def["acceptance_criteria"] = s["acceptance_criteria"]
            if s.get("timeout_minutes"):
                step_def["timeout_minutes"] = s["timeout_minutes"]

            definition_steps.append(step_def)

        draft_name = f"{task['title']}（草案）"

        definition = {"steps": definition_steps}
        if task.get("acceptance_criteria"):
            definition["acceptance_criteria"] = task["acceptance_criteria"]
        if task.get("timeout_minutes"):
            definition["timeout_minutes"] = task["timeout_minutes"]

        logger.info(f"Workflow extracted from task {task_id}: {len(definition_steps)} steps, "
                    f"{len(all_hints)} hints")

        return {
            "success": True,
            "source_task_id": task_id,
            "name": draft_name,
            "description": task.get("description", ""),
            "definition": definition,
            "hints": all_hints,
        }
