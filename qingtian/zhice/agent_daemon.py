#!/usr/bin/env python3
"""执策 Agent 守护脚本 — 被动轮询 + WS 监听双通道

Agent 启动时运行此脚本，作为后台守护进程。两种模式：
  1. WS 模式（推荐）：连 ws://host/v1/zhice/events → 收到 assigned 通知 → 自动执行
  2. 轮询模式（fallback）：每 N 秒 GET /recover → 有 assigned 的 Step → 自动 start

用法:
  # 轮询模式（默认）
  python3 zhice/agent_daemon.py --agent-id biz:buyer-01 --poll 10

  # WS 模式
  python3 zhice/agent_daemon.py --agent-id biz:buyer-01 --ws

  # 仅打印收到的 Step 而不实际执行（调试）
  python3 zhice/agent_daemon.py --agent-id biz:buyer-01 --dry-run

环境变量:
  ZHICE_ENDPOINT — 执策 API 地址（默认 http://127.0.0.1:1996/v1/zhice）
  AGENT_TOKEN    — 镇岳 Bearer Token（可选）
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import traceback

DEFAULT_ENDPOINT = "http://127.0.0.1:1996/v1/zhice"
DEFAULT_POLL = 10  # 秒

logger = logging.getLogger("zhice.agent_daemon")


# ── HTTP 辅助 ──────────────────────────────────────────

async def http_get(url: str, token: str = "") -> dict | None:
    try:
        import httpx
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


async def http_post(url: str, data: dict, token: str = "") -> dict | None:
    try:
        import httpx
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=data, headers=headers)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


# ── 轮询模式 ──────────────────────────────────────────

async def poll_mode(endpoint: str, agent_id: str, token: str, interval: int, dry_run: bool,
                    allowed_roles: list[str] | None = None):
    print(f"[daemon] Poll mode: {endpoint}, agent={agent_id}, interval={interval}s" +
          (f" roles={allowed_roles}" if allowed_roles else ""))
    while True:
        try:
            # 主动认领 pending 步骤 — 找未完成的 task，调 /next 原子分配
            tasks_data = await http_get(f"{endpoint}/tasks?status=running&limit=10", token)
            if tasks_data:
                for t in tasks_data.get("tasks", [])[:5]:
                    if t.get("status") == "running":
                        next_data = await http_get(
                            f"{endpoint}/tasks/{t['task_id']}/next?agent_id={agent_id}", token
                        )
                        if next_data and next_data.get("current_step"):
                            cs = next_data["current_step"]
                            if allowed_roles and cs.get("assigned_agent","") not in allowed_roles:
                                continue
                            print(f"[daemon] Claimed pending step: task={t['task_id']} step={cs['step_index']}")
                            asyncio.create_task(execute_step(endpoint, agent_id, token, {
                                "task_id": t["task_id"],
                                "step_id": cs["step_id"],
                                "step_index": cs["step_index"],
                                "title": cs.get("title",""),
                                "instruction": cs.get("instruction",""),
                                "acceptance_criteria": cs.get("acceptance_criteria",[]),
                                "assigned_agent": cs.get("assigned_agent",""),
                            }))

            data = await http_get(f"{endpoint}/recover?agent_id={agent_id}", token)
            if data:
                unfinished = data.get("unfinished", [])
                for step in unfinished:
                    if step.get("status") in ("assigned", "in_progress"):
                        # 角色过滤
                        if allowed_roles and step.get("assigned_agent") not in allowed_roles:
                            continue
                        # 获取完整 step 信息（含 acceptance_criteria）
                        detail = None
                        full = await http_get(
                            f"{endpoint}/tasks/{step['task_id']}/next?agent_id={agent_id}", token
                        )
                        if full and full.get("current_step"):
                            detail = full["current_step"]
                            step["assigned_agent"] = detail.get("assigned_agent", step.get("assigned_agent", ""))
                            # acceptance_criteria 可能是 JSON 字符串（旧连接池未注册 codec）
                            ac = detail.get("acceptance_criteria")
                            if isinstance(ac, str):
                                try:
                                    ac = __import__("json").loads(ac)
                                except Exception:
                                    ac = []
                            step["acceptance_criteria"] = ac or []
                        if not _role_match(step, allowed_roles, detail):
                            print(f"[daemon] Skipping step (role mismatch): {step.get('assigned_agent','?')}")
                            continue
                        print(f"[daemon] Found step: task={step['task_id']} step={step['step_index']}: {step.get('title','')}")
                        if not dry_run:
                            await execute_step(endpoint, agent_id, token, step)
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(interval)


# ── WS 模式 ────────────────────────────────────────────

async def ws_mode(endpoint: str, agent_id: str, token: str, dry_run: bool,
                  allowed_roles: list[str] | None = None):
    """WS + /recover 混合模式 — WS 实时推送 + 10s 轮询兜底。"""
    import websockets
    ws_base = endpoint.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_base}/events?agent_id={agent_id}"
    print(f"[daemon] WS+Poll hybrid: {ws_url}")

    # 后台轮询兜底 — 每 10 秒 /recover 补漏
    async def backup_poll():
        while True:
            await asyncio.sleep(10)
            try:
                data = await http_get(f"{endpoint}/recover?agent_id={agent_id}", token)
                if data:
                    for step in data.get("unfinished", []):
                        if step.get("status") in ("assigned", "in_progress"):
                            if allowed_roles and step.get("assigned_agent") not in allowed_roles:
                                continue
                            detail = None
                            full = await http_get(
                                f"{endpoint}/tasks/{step['task_id']}/next?agent_id={agent_id}", token
                            )
                            if full and full.get("current_step"):
                                detail = full["current_step"]
                                step["assigned_agent"] = detail.get("assigned_agent", step.get("assigned_agent", ""))
                                ac = detail.get("acceptance_criteria")
                                if isinstance(ac, str):
                                    try: ac = __import__("json").loads(ac)
                                    except Exception: ac = []
                                step["acceptance_criteria"] = ac or []
                            if not _role_match(step, allowed_roles, detail):
                                continue
                            print(f"[daemon] Poll backup: task={step['task_id']} step={step['step_index']}: {step.get('title','')}")
                            if not dry_run:
                                await execute_step(endpoint, agent_id, token, step)
            except Exception:
                pass
    poll_task = asyncio.create_task(backup_poll())

    try:
        async with websockets.connect(ws_url) as ws:
            print(f"[daemon] Connected to {ws_url}")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "zhice:assigned":
                        step = {
                            "task_id": msg["task_id"],
                            "step_id": msg["step_id"],
                            "step_index": msg["step_index"],
                            "title": msg["title"],
                            "instruction": msg["instruction"],
                        }
                        print(f"[daemon] WS assigned: task={step['task_id']} step={step['step_index']}: {step['title']}")
                        if not dry_run:
                            await execute_step(endpoint, agent_id, token, step)
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        print(f"[daemon] WS connection failed: {e}, falling back to poll only")
    finally:
        poll_task.cancel()
        await poll_mode(endpoint, agent_id, token, DEFAULT_POLL, dry_run, allowed_roles)


def _role_match(step: dict, allowed_roles: list[str], step_detail: dict | None = None) -> bool:
    """检查 step 是否匹配本 daemon 允许的角色。

    优先级:
      1. step.assigned_agent 已指定 → 精确匹配
      2. 未指定 → 看 step 内容是否含角色关键词（如 title=\"运维检测\"）
      3. 都匹配不到 → 允许抢（无人指定=全员可抢）
    """
    if not allowed_roles:
        return True
    # 优先级1: 已指定 assigned_agent
    assigned = step.get("assigned_agent", "")
    if assigned:
        return assigned in allowed_roles
    # 优先级2: 内容关键词
    text = f"{step.get('title','')} {step.get('instruction','')}".lower()
    for role in allowed_roles:
        r = role.lower().replace("-", " ").replace("_", " ")
        if r in text:
            return True
    # 优先级3: 无人指定 → 都可以抢
    return True


# ── 本地检查执行 ──────────────────────────────────────

def _run_http(cmd: str) -> dict:
    """执行 HTTP 调用。格式: 'METHOD /path -d body' 或 'GET /path'。"""
    import subprocess
    try:
        p = subprocess.run(f"curl -s -w '\\n%{{http_code}}' {cmd}", shell=True,
                           capture_output=True, text=True, timeout=30)
        out = p.stdout.strip()
        if "\n" in out:
            *body_lines, code_line = out.rsplit("\n", 1)
            body = "\n".join(body_lines)
            code = int(code_line) if code_line.isdigit() else 0
        else:
            body = out
            code = 0
        passed = 200 <= code < 300
        return {"stdout": body[:5000], "stderr": p.stderr[:500] if not passed else "",
                "exit_code": 0 if passed else 1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": 1}


def _run_shell(command: str, timeout: int = 60) -> dict:
    """执行 shell 指令，返回 {stdout, stderr, exit_code}。

    默认 60s 超时（防止 dmesg/journalctl 等挂死）。
    指令前自动加 timeout 命令兜底：'timeout 55 bash -c \"...\"'
    """
    import subprocess
    # 防止管道命令卡死（如 dmesg 无输出时）用 timeout 命令包一层
    safe = command.replace('"', '\\"')
    wrapped = f'timeout {timeout - 5} bash -c "{safe}"'
    try:
        p = subprocess.run(wrapped, shell=True, capture_output=True, text=True, timeout=timeout)
        return {"stdout": p.stdout.strip(), "stderr": p.stderr.strip(), "exit_code": p.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"timeout after {timeout}s", "exit_code": 124}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": 1}


# ── 只读 SQL 校验（P2 R11 命令注入加固）────────────────

# 写操作关键字黑名单（防御性，配合 shell=False + psql -c 单语句）
_FORBIDDEN_SQL_KW = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"COPY|CALL|DO|EXECUTE|MERGE|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)


def _validate_readonly_sql(sql: str) -> str | None:
    """校验 SQL 为只读单语句查询，返回错误信息或 None。

    P2 (R11) 命令注入加固 — 拒绝可被用于注入 shell/多语句的 SQL:
      - 拒绝注释（-- / /*）: 注释可隐藏分号与换行
      - 拒绝分号: psql -c 会顺次执行多条语句
      - 拒绝 shell 表达式（$( / ${ / `）: 防 shell 展开注入
      - 首语句必须为 SELECT
      - 拒绝写操作关键字（防御性，配合 shell=False）
    """
    s = (sql or "").strip()
    if not s:
        return "SQL 为空"
    if "--" in s or "/*" in s:
        return "SQL 不允许包含注释"
    if ";" in s:
        return "SQL 必须是单条语句（禁止分号多语句）"
    if "$(" in s or "${" in s or "`" in s:
        return "SQL 不允许包含 shell 表达式"
    first = s.split(None, 1)[0].upper()
    if first != "SELECT":
        return "仅允许 SELECT 只读查询"
    m = _FORBIDDEN_SQL_KW.search(s)
    if m:
        return f"禁止写操作关键字: {m.group(0).upper()}"
    return None


def _run_db_query(sql: str) -> int:
    """执行只读 SQL 并返回命中行数；非法/注入 SQL 一律拒绝并返回 0。

    P2 (R11): 原实现 `psql -c "{sql}"` + shell=True，恶意 SQL（如
    `"; rm -rf /; "`）可执行任意 shell 命令。现改为:
      1. 先过 _validate_readonly_sql 白名单（仅 SELECT 单语句）
      2. shell=False + argv 列表传参，SQL 只作为单个参数交给 psql，
        不再经过 shell 解释（$()/反引号/管道/重定向全部失效）
    """
    err = _validate_readonly_sql(sql)
    if err:
        logger.warning("[trace] db_query fail: 非只读 SQL 被拒绝 — %s: %.80s", err, sql)
        return 0
    try:
        import subprocess
        host = os.getenv("ZHICE_DB_HOST", "localhost")
        user = os.getenv("ZHICE_DB_USER", "qingtian")
        db = os.getenv("ZHICE_DB_NAME", "qingtian")
        p = subprocess.run(
            ["psql", "-h", host, "-U", user, "-d", db, "-t", "-A", "-c", sql],
            capture_output=True, text=True, timeout=15,
        )
        out = p.stdout.strip()
        if out.isdigit():
            return int(out)
        if out:
            return len(out.split("\n"))
        return 0
    except Exception:
        return 0


def _run_local_checks(criteria: list[dict]) -> dict:
    """根据 acceptance_criteria 在本地执行检查，返回 check_results dict。"""
    results = {}
    for c in criteria or []:
        ctype = c.get("type", "")
        if ctype == "file_exists":
            path = c.get("path", "")
            import os as _os
            exists = _os.path.exists(path)
            results.setdefault("file_exists", []).append({"path": path, "exists": exists})
        elif ctype == "api_health":
            url = c.get("url", "")
            # 2026-08-28 P1 修复（SSRF）：criteria 由任务创建方提供，URL 先过
            # url_guard（scheme 白名单+私网拦截）再请求，不再裸 urlopen 任意 URL
            try:
                from common.url_guard import check_external_url
                ok, reason = check_external_url(url)
            except Exception:
                ok, reason = False, "url_guard 不可用，拒绝请求"
            if not ok:
                results.setdefault("api_health", []).append(
                    {"url": url, "status_code": 0, "error": f"URL 被安全策略拒绝: {reason}"})
                continue
            try:
                import urllib.request
                req = urllib.request.Request(url, method="GET")
                r = urllib.request.urlopen(req, timeout=10)
                results.setdefault("api_health", []).append({"url": url, "status_code": r.status})
            except Exception as e:
                results.setdefault("api_health", []).append({"url": url, "status_code": 0, "error": str(e)})
        elif ctype == "db_query":
            sql = c.get("sql", "")
            count = _run_db_query(sql)  # P2 (R11): 白名单校验 + 无 shell 执行
            results.setdefault("db_query", []).append({"sql": sql, "count": count})
        elif ctype == "run_script":
            script = c.get("script", "")
            r = _run_shell(f"python3 {script}", timeout=120)
            results.setdefault("run_script", []).append({
                "script": script, "exit_code": r["exit_code"],
                "stdout": r["stdout"][:2000], "stderr": r["stderr"][:500],
            })
    return results


# ── 执行 Step ─────────────────────────────────────────

async def _check_gotchas(endpoint: str, token: str, step: dict) -> str:
    """查 workflow 的踩坑记录，返回警告文本（如有匹配的 gotcha）。"""
    task_id = step.get("task_id", "")
    try:
        task_data = await http_get(f"{endpoint}/tasks/{task_id}", token)
        if not task_data:
            return ""
        wf_id = task_data.get("workflow_id")
        if not wf_id:
            return ""
        wf_data = await http_get(f"{endpoint}/workflows/{wf_id}", token)
        if not wf_data or not wf_data.get("definition"):
            return ""
        definition = wf_data["definition"]
        if isinstance(definition, str):
            import json
            definition = json.loads(definition)
        gotchas = definition.get("_gotchas", [])
        if not gotchas:
            return ""
        # 匹配当前 step 的指数或标题
        idx = step.get("step_index", 0)
        title = step.get("title", "")
        matched = [g for g in gotchas if g.get("step_index") == idx or g.get("title") == title]
        if matched:
            lines = ["\n⚠️ 踩坑预警（Workflow #{}）:".format(wf_id)]
            for g in matched:
                lines.append(f"  - #{g['step_index']} '{g.get('title','')}': {g.get('error','')} → 修复: {g.get('fix','')}")
            return "\n".join(lines)
    except Exception:
        pass
    return ""


async def _call_skill(skill_name: str, action: str, params: dict, token: str = "", agent_id: str = "") -> dict:
    """通过羲和 Runtime 调子进程执行 Skill action。

    #1 修复：必须携带 agent_id（空 agent_id 会拉起 --agent-id="" 的通用子进程、IPC 起不来）。
    """
    import httpx
    url = f"{DEFAULT_ENDPOINT.replace('/v1/zhice', '/api/v1')}/skills/{skill_name}/execute"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"action": action, "params": params}
    if agent_id:
        body["agent_id"] = agent_id
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def execute_step(endpoint: str, agent_id: str, token: str, step: dict):
    """全自动执行 assigned 的 Step: start → 检查 gotcha → 执行 instruction → 本地检查 → 提交结果。"""
    step_id = step["step_id"]
    instruction = step.get("instruction", "")
    criteria = step.get("acceptance_criteria") or []
    print(f"[daemon] Step {step_id}: {instruction[:100]}")

    # 踩坑检查 — 在工作流模板中找匹配的 gotcha
    gotcha_warning = await _check_gotchas(endpoint, token, step)
    if gotcha_warning:
        print(f"[daemon] {gotcha_warning}")
        instruction = gotcha_warning + "\n\n" + instruction

    # 1. start
    r = await http_post(f"{endpoint}/steps/{step_id}/start", {"agent_id": agent_id}, token)
    if not r:
        print(f"[daemon] start failed")
        return

    # 2. heartbeat loop — 执行期间每 5 秒发一次
    hb_stop = False
    async def hb():
        count = 0
        while not hb_stop:
            await asyncio.sleep(5)
            await http_post(f"{endpoint}/steps/{step_id}/heartbeat",
                            {"agent_id": agent_id, "status_reason": "executing",
                             "progress": f"running, hb={count+1}"}, token)
            count += 1
    hb_task = asyncio.create_task(hb())

    # 3. 按 exec_type 执行
    import uuid
    # P0-1 (9-2): 客户端缺省从 shell 改 manual——服务端已归一化（runner._resolve_exec_type），
    # 此处兜底防旧服务端/异常数据缺 exec_type 时 daemon 侧仍拿 shell 执行
    exec_type = step.get("exec_type", "manual")
    # review(2026-08-16): 执行段异常（_call_skill/http_post/本地检查抛错）时原实现
    # 不会走到函数尾部的 hb_stop=True + hb_task.cancel() → 心跳 task 孤儿永续
    # （每 5s 持续 POST）。用 try/finally 兜底：无论成败都停心跳。
    try:
        print(f"[daemon] Executing (type={exec_type}): {instruction[:100]}")

        if exec_type == "manual":
            # manual: 不自动执行，仅提示
            print(f"[daemon] Manual step — agent must handle: {instruction}")
            outputs = {"result": "manual step acknowledged", "exit_code": 0, "note": "需人工处理"}
            passed = True
            shell_result = {"stdout": "manual", "stderr": "", "exit_code": 0}
        elif exec_type == "http":
            # HTTP 调用
            shell_result = _run_http(instruction)
            passed = shell_result["exit_code"] == 0
            outputs = {"result": shell_result["stdout"][:5000], "exit_code": shell_result["exit_code"],
                        "http_response": shell_result["stderr"][:1000]}
        elif exec_type == "skill":
            # skill 调用: instruction = "skill_name:action_name"
            parts = instruction.split(":", 1)
            if len(parts) < 2:
                shell_result = {"stdout": "", "stderr": "skill exec_type requires instruction='skill_name:action_name'", "exit_code": 1}
            else:
                skill_name, action_name = parts[0].strip(), parts[1].strip()
                step_params = step.get("params", {})
                print(f"[daemon] Calling skill: {skill_name}:{action_name}")
                skill_result = await _call_skill(skill_name, action_name, step_params, token, agent_id)
                ok = skill_result.get("ok", False)
                output_text = json.dumps(skill_result.get("data", skill_result), ensure_ascii=False)[:5000]
                shell_result = {"stdout": output_text, "stderr": "", "exit_code": 0 if ok else 1}
            passed = shell_result["exit_code"] == 0
            outputs = {"result": shell_result["stdout"][:5000], "exit_code": shell_result["exit_code"],
                        "skill": parts[0] if len(parts) >= 2 else "", "action": parts[1] if len(parts) >= 2 else ""}
        elif exec_type == "script":
            # 执行脚本文件
            shell_result = _run_shell(f"python3 {instruction}", timeout=600)
            passed = shell_result["exit_code"] == 0
            outputs = {"result": shell_result["stdout"][:5000], "exit_code": shell_result["exit_code"],
                        "stderr": shell_result["stderr"][:500]}
        else:
            # shell: 默认行为
            shell_result = _run_shell(instruction)
            passed = shell_result["exit_code"] == 0
            outputs = {"result": shell_result["stdout"][:5000], "exit_code": shell_result["exit_code"],
                        "stderr": shell_result["stderr"][:500]}

        # 4. 本地检查
        check_results = _run_local_checks(criteria)
        outputs["check_results"] = check_results

        # 5. 提交
        summary = shell_result.get("stdout", "")[:200].replace("\n", " ") if shell_result.get("stdout") else "执行完成"

        submit_body = {
            "agent_id": agent_id,
            "status": "completed" if passed else "failed",
            "summary": summary,
            "outputs": outputs,
            "idempotency_key": str(uuid.uuid4()),
        }
        r = await http_post(f"{endpoint}/steps/{step_id}/submit", submit_body, token)
    finally:
        # 无论执行成败都停心跳，防孤儿 task 每 5s 持续 POST
        hb_stop = True
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass
    if r:
        print(f"[daemon] Submitted step {step_id}: status={r.get('status')} verify={r.get('verification_result')}")
        # 执行完后自动调 /next 推动任务流转
        if r.get("status") == "completed":
            next_data = await http_get(
                f"{endpoint}/tasks/{step.get('task_id','')}/next?agent_id={agent_id}", token
            )
            if next_data and next_data.get("current_step"):
                cs = next_data["current_step"]
                print(f"[daemon] Auto-advance: next step {cs.get('step_index','?')}: {cs.get('title','')}")
    else:
        print(f"[daemon] Submit failed for step {step_id}")


# ── 入口 ──────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="执策 Agent 守护脚本")
    parser.add_argument("--agent-id", required=True, help="Agent ID")
    parser.add_argument("--poll", type=int, default=None, help="轮询间隔（秒）")
    parser.add_argument("--ws", action="store_true", help="使用 WS 模式")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不执行")
    parser.add_argument("--endpoint", default=os.getenv("ZHICE_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--roles", default="", help="限定角色，逗号分隔。如 'sys-eng,ops-agent' 只抢运维类步骤")
    args = parser.parse_args()

    token = os.getenv("AGENT_TOKEN", "")
    allowed_roles = [r.strip() for r in (args.roles or "").split(",") if r.strip()]
    if allowed_roles:
        print(f"[daemon] Role filter: {allowed_roles}")

    if not token:
        print("[daemon] Warning: AGENT_TOKEN not set, API calls may fail")

    if args.ws:
        await ws_mode(args.endpoint, args.agent_id, token, args.dry_run)
    else:
        interval = args.poll or DEFAULT_POLL
        await poll_mode(args.endpoint, args.agent_id, token, interval, args.dry_run, allowed_roles)


if __name__ == "__main__":
    asyncio.run(main())
