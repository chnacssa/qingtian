"""执策 → 吸星 集成客户端

通过吸星 HTTP API 写入知识（替代直接写 yongheng/xixing 表）。
文档 §4.4:
  - Task 完成 → POST /v1/xixing/agent/{agent_id}/learn
  - 检查失败 → POST /v1/xixing/agent/{agent_id}/report-pitfall
"""
import json
import logging
import httpx
from . import config as cfg

logger = logging.getLogger("zhice.xixing_client")

# ACSSA 底座地址（同机部署）
_BASE_URL = cfg.get_xixing_base_url()


async def _post(path: str, body: dict, timeout: int = 10) -> dict | None:
    """向吸星 API 发送 POST 请求，失败返回 None"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{_BASE_URL}{path}",
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        logger.warning(f"[xixing] POST {path} timeout after {timeout}s")
    except Exception:
        logger.exception(f"[xixing] POST {path} failed")
    return None


async def learn_workflow(agent_id: str, task_title: str, steps: list[dict],
                         task_id: int, acceptance_criteria: list[dict] | None = None) -> bool:
    """将 Task 完成后的 Workflow 骨架提交到吸星知识进化管道

    调用 POST /v1/xixing/agent/{agent_id}/learn
    """
    steps_skeleton = [
        {"step_index": s["step_index"], "title": s["title"],
         "instruction": s["instruction"],
         "depends_on": s.get("depends_on")}
        for s in steps
    ]
    content = json.dumps({
        "type": "workflow_skeleton",
        "task_id": task_id,
        "title": task_title,
        "steps": steps_skeleton,
        "acceptance_criteria": acceptance_criteria,
    }, ensure_ascii=False)

    body = {
        "content": content,
        "memory_type": "episodic",
        "source": "zhice",
        "metadata": {
            "task_id": task_id,
            "type": "workflow_skeleton",
            "step_count": len(steps),
        },
    }
    # 2026-08-28 P2 修复：agent_id 来自 task.created_by（请求方自报文本），
    # 直接拼 URL 可路径穿越打本机任意端点——quote 后再拼
    from urllib.parse import quote as _quote
    result = await _post(f"/v1/xixing/agent/{_quote(agent_id, safe='')}/learn", body)
    if result and result.get("status") == "stored":
        logger.info(f"[xixing] Workflow skeleton learned for task {task_id} → agent:{agent_id}")
        return True
    return False


async def report_pitfall(agent_id: str, step_index: int, step_title: str,
                         task_id: int, failed_rules: list[dict]) -> bool:
    """将检查失败的踩坑记录提交到吸星

    调用 POST /v1/xixing/agent/{agent_id}/report-pitfall
    """
    body = {
        "title": f"[执策] Step {step_index}: {step_title} 检查失败",
        "description": (
            f"Task #{task_id} Step {step_index} 检查不通过，"
            f"失败规则: {json.dumps(failed_rules, ensure_ascii=False)[:500]}"
        ),
        "severity": "medium",
        "tags": ["zhice", "check_failure"],
    }
    from urllib.parse import quote as _quote
    result = await _post(f"/v1/xixing/agent/{_quote(agent_id, safe='')}/report-pitfall", body)
    if result and result.get("status") == "recorded":
        logger.info(f"[xixing] Pitfall recorded: task={task_id}, step={step_index}, agent={agent_id}")
        return True
    return False
