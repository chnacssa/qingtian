#!/usr/bin/env python3
"""
ACSSA 智能体操作系统 Agent 适配器 v2.1
用法: qingtian.py <action> [参数...]

安全设计：
  - 所有 JSON 通过 json.dumps 构造，不接受裸字符串拼接
  - 多行内容通过 stdin 或 --file 传入
  - 所有 HTTP 响应做状态码检查
  - 瞬态错误自动重试（429/5xx），最多 3 次
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

HOST = os.getenv("QINGTIAN_HOST", "http://localhost:1996")
AGENT_ID = os.getenv("AGENT_ID", "")
AGENT_NAME = os.getenv("AGENT_NAME", AGENT_ID)
AGENT_CATEGORY = os.getenv("AGENT_CATEGORY", "internal")
try:
    AGENT_CAPABILITIES = json.loads(os.getenv("AGENT_CAPABILITIES", "[]"))
except json.JSONDecodeError:
    AGENT_CAPABILITIES = []
AGENT_SERVER = os.getenv("AGENT_SERVER", "")
TOKEN = os.getenv("QINGTIAN_TOKEN", "")
try:
    TIMEOUT = int(os.getenv("QINGTIAN_TIMEOUT", "30"))
except ValueError:
    TIMEOUT = 30
try:
    MAX_RETRIES = int(os.getenv("QINGTIAN_MAX_RETRIES", "3"))
except ValueError:
    MAX_RETRIES = 3
try:
    RETRY_BACKOFF = float(os.getenv("QINGTIAN_RETRY_BACKOFF", "1.5"))
except ValueError:
    RETRY_BACKOFF = 1.5


def _request(method: str, path: str, data: dict | None = None) -> dict:
    """统一 HTTP 请求，带重试逻辑。

    重试策略：
      - 429 (限流) → 等 Retry-After 或指数退避后重试
      - 5xx (服务端错误) → 指数退避后重试
      - 4xx (客户端错误) → 不重试，直接返回
      - 连接错误 → 退避后重试
    """
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                f"{HOST}{path}", data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {TOKEN}",
                } if body else {
                    "Authorization": f"Bearer {TOKEN}",
                },
                method=method,
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if not raw:
                    return {"status": resp.status}
                return json.loads(raw)

        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            status = e.code

            if status == 429:
                # 限流 — 等 Retry-After 或退避
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF ** (attempt + 1)
                    print(f"[warn] 429 rate limited, retry in {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
                    time.sleep(wait)
                    continue
                return {"error": True, "status": 429, "code": "RATE_LIMITED", "detail": err_body}

            if status >= 500:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF ** (attempt + 1)
                    print(f"[warn] {status} server error, retry in {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
                    time.sleep(wait)
                    continue
                return {"error": True, "status": status, "code": "SERVER_ERROR", "detail": err_body}

            # 4xx — 不重试
            return {"error": True, "status": status, "detail": err_body}

        except (urllib.error.URLError, OSError) as e:
            last_error = str(e.reason) if hasattr(e, "reason") else str(e)
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF ** (attempt + 1)
                print(f"[warn] connection failed: {last_error}, retry in {wait:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
                time.sleep(wait)
                continue
            return {"error": True, "status": 0, "code": "CONNECTION_FAILED", "detail": f"连接失败: {last_error}"}

        except Exception as e:
            return {"error": True, "status": 0, "code": "UNKNOWN", "detail": str(e)}

    return {"error": True, "status": 0, "code": "MAX_RETRIES", "detail": f"重试 {MAX_RETRIES} 次后仍失败: {last_error}"}


def _get(path: str) -> dict:
    return _request("GET", path)


def _post(path: str, data: dict) -> dict:
    return _request("POST", path, data)


def _read_content(args: list[str]) -> str:
    """读取内容：优先 --file <path>，其次 stdin，最后命令行参数拼接。"""
    if "--file" in args:
        idx = args.index("--file")
        if idx + 1 < len(args):
            try:
                with open(args[idx + 1], "r", encoding="utf-8") as f:
                    return f.read()
            except (FileNotFoundError, PermissionError, OSError) as e:
                return f"[ERROR] 无法读取文件 {args[idx + 1]!r}: {e}"
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return " ".join(args) if args else ""


# ── 命令实现 ────────────────────────────────────────

def cmd_health():
    """健康检查 — 测试底座连通性"""
    return _get("/health")


def cmd_register():
    return _post("/v1/huanyu/agents/register", {
        "name": AGENT_NAME,
        "category": AGENT_CATEGORY,
        "capabilities": AGENT_CAPABILITIES,
        "server_host": AGENT_SERVER,
    })


def cmd_session_start(args: list[str]):
    context = _read_content(args)
    return _post("/v1/yongheng/session/start", {
        "namespace": AGENT_ID,
        "context": context,
        "top_k": int(os.getenv("QINGTIAN_TOP_K", "5")),
    })


def cmd_recall(args: list[str]):
    query = " ".join(args) if args else ""
    return _post("/v1/yongheng/memories/search", {
        "namespace": AGENT_ID,
        "query": query,
        "method": "hybrid",
        "top_k": int(os.getenv("QINGTIAN_TOP_K", "5")),
    })


def cmd_remember(args: list[str]):
    content = _read_content(args)
    mem_type = os.getenv("QINGTIAN_MEM_TYPE", "episodic")
    return _post("/v1/yongheng/memories", {
        "namespace": AGENT_ID,
        "content": content,
        "type": mem_type,
    })


def cmd_learn(args: list[str]):
    content = _read_content(args)
    source = os.getenv("QINGTIAN_LEARN_SOURCE", "agent-self-report")
    mem_type = os.getenv("QINGTIAN_MEM_TYPE", "episodic")
    # 走吸星：Agent 主动提交经验 → 质量门 → 分类 → 蒸馏 → 永恒
    return _post(f"/v1/xixing/agent/{AGENT_ID}/learn", {
        "content": content,
        "memory_type": mem_type,
        "source": source,
    })


def cmd_insights(_args: list[str]):
    return _get(f"/v1/xixing/agent/{AGENT_ID}/insights")


def cmd_pitfall(args: list[str]):
    if len(args) < 2:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py pitfall <标题> <描述> [severity]"}
    title = args[0]
    desc = args[1]
    severity = args[2] if len(args) > 2 else "medium"
    return _post(f"/v1/xixing/agent/{AGENT_ID}/report-pitfall", {
        "title": title,
        "description": desc,
        "severity": severity,
    })


def cmd_session_end(args: list[str]):
    summary = _read_content(args)

    # 组装 state：优先 QINGTIAN_STATE 环境变量，其次从 summary 文件解析 ## State: 块
    state = {}
    state_str = os.getenv("QINGTIAN_STATE", "")
    if state_str:
        try:
            state = json.loads(state_str)
        except json.JSONDecodeError:
            pass

    # 从 summary 解析 ## State: 块（YAML-like key: value 行，合并到 state）
    if "## State:" in summary:
        try:
            state_block = summary.split("## State:")[1].split("##")[0].strip()
            for line in state_block.split("\n"):
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    k, v = line.split(":", 1)
                    state[k.strip()] = v.strip()
            # 去掉 ## State: 块，保持 summary 干净
            summary = summary.split("## State:")[0].strip()
        except Exception:
            pass

    # decisions 合并到 state
    decisions_str = os.getenv("QINGTIAN_DECISIONS", "[]")
    try:
        decisions = json.loads(decisions_str)
        if decisions:
            state["decisions"] = decisions
    except json.JSONDecodeError:
        pass

    return _post("/v1/yongheng/session/end", {
        "namespace": AGENT_ID,
        "summary": summary,
        "state": state,
    })


def cmd_transfer(args: list[str]):
    if len(args) < 1:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py transfer <source_ns> [target_ns] [mode]"}
    source = args[0]
    target = args[1] if len(args) > 1 else AGENT_ID
    mode = args[2] if len(args) > 2 else "copy"
    return _post("/v1/yongheng/memories/transfer", {
        "source_namespace": source,
        "target_namespace": target,
        "mode": mode,
    })


def cmd_recover(args: list[str]):
    ns = args[0] if args else AGENT_ID
    since = os.getenv("QINGTIAN_RECOVER_SINCE")
    body = {"namespace": ns}
    if since:
        body["since"] = since
    return _post("/v1/yongheng/session/recover", body)


def cmd_heartbeat():
    return _post(f"/v1/huanyu/agents/{AGENT_ID}/heartbeat", {})


def cmd_profile():
    """获取当前 Agent 画像 + 偏好 + 状态"""
    return _get(f"/v1/yongheng/profile?namespace={AGENT_ID}")


# ── 执策（Zhice）命令 ────────────────────────────────

def cmd_zhice_task(args: list[str]):
    """创建执策任务 — JSON 内容通过 --file 或 stdin 传入"""
    content = _read_content(args)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return {"error": True, "code": "PARSE_ERROR", "detail": f"JSON 解析失败: {e}"}
    if "created_by" not in data:
        data["created_by"] = AGENT_NAME
    return _post("/v1/zhice/tasks", data)


def cmd_zhice_list(args: list[str]):
    """查询任务列表 [status] [created_by]"""
    params = []
    if args:
        params.append(f"status={args[0]}")
    if len(args) > 1:
        params.append(f"created_by={args[1]}")
    qs = "?" + "&".join(params) if params else ""
    return _get(f"/v1/zhice/tasks{qs}")


def cmd_zhice_detail(args: list[str]):
    """查看任务详情 <task_id>"""
    if not args:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py zhice-detail <task_id>"}
    return _get(f"/v1/zhice/tasks/{args[0]}")


def cmd_zhice_next(args: list[str]):
    """获取下一步 <task_id>"""
    if not args:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py zhice-next <task_id>"}
    return _get(f"/v1/zhice/tasks/{args[0]}/next?agent_id={AGENT_NAME}")


def cmd_zhice_start(args: list[str]):
    """开始执行步骤 <step_id>"""
    if not args:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py zhice-start <step_id>"}
    return _post(f"/v1/zhice/steps/{args[0]}/start", {"agent_id": AGENT_NAME})


def cmd_zhice_heartbeat(args: list[str]):
    """发送心跳 <step_id> [status_reason]"""
    if not args:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py zhice-heartbeat <step_id> [status_reason]"}
    status_reason = args[1] if len(args) > 1 else "executing"
    return _post(f"/v1/zhice/steps/{args[0]}/heartbeat", {
        "agent_id": AGENT_NAME,
        "status_reason": status_reason,
    })


def cmd_zhice_submit(args: list[str]):
    """提交步骤结果 <step_id> — 结果 JSON 通过 --file 或 stdin 传入"""
    if not args:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py zhice-submit <step_id> [--file <path>]"}
    step_id = args[0]
    # 从剩余参数或 stdin 读取内容
    content_args = args[1:] if len(args) > 1 else []
    content = _read_content(content_args)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return {"error": True, "code": "PARSE_ERROR", "detail": f"JSON 解析失败: {e}"}
    if "agent_id" not in data:
        data["agent_id"] = AGENT_NAME
    return _post(f"/v1/zhice/steps/{step_id}/submit", data)


def cmd_zhice_issue(args: list[str]):
    """报告问题 <step_id> <issue_type> <description>"""
    if len(args) < 3:
        return {"error": True, "code": "USAGE",
                "detail": "用法: qingtian.py zhice-issue <step_id> <issue_type> <description>\n"
                          "issue_type: blocked_by_dependency | need_clarification | resource_insufficient"}
    return _post(f"/v1/zhice/steps/{args[0]}/issue", {
        "agent_id": AGENT_NAME,
        "issue_type": args[1],
        "description": args[2],
    })


def cmd_zhice_cancel(args: list[str]):
    """取消任务 <task_id>"""
    if not args:
        return {"error": True, "code": "USAGE", "detail": "用法: qingtian.py zhice-cancel <task_id>"}
    return _post(f"/v1/zhice/tasks/{args[0]}/cancel", {})


# ── 命令路由 ─────────────────────────────────────────

COMMANDS = {
    "health":         (cmd_health,        0, "健康检查"),
    "register":       (cmd_register,      0, "注册到寰宇"),
    "session-start":  (cmd_session_start, -1, "启动会话 [--file <path>]"),
    "recall":         (cmd_recall,        -1, "搜索历史记忆 <query>"),
    "remember":       (cmd_remember,      -1, "写入长期记忆 [--file <path>]"),
    "learn":          (cmd_learn,         -1, "Agent学习/提交经验 [--file <path>]"),
    "insights":       (cmd_insights,       0, "查看进化洞察"),
    "pitfall":        (cmd_pitfall,       -1, "上报踩坑 <title> <desc> [severity]"),
    "session-end":    (cmd_session_end,   -1, "结束会话 [--file <path>]"),
    "transfer":       (cmd_transfer,      -1, "迁移记忆 <source_ns> [target_ns] [mode]"),
    "recover":        (cmd_recover,       -1, "崩溃恢复 [namespace]"),
    "heartbeat":      (cmd_heartbeat,      0, "心跳"),
    "profile":        (cmd_profile,        0, "查看画像与状态"),
    "zhice-task":     (cmd_zhice_task,    -1, "创建执策任务 [--file <path>]"),
    "zhice-list":     (cmd_zhice_list,    -1, "查询任务列表 [status] [created_by]"),
    "zhice-detail":   (cmd_zhice_detail,   1, "查看任务详情 <task_id>"),
    "zhice-next":     (cmd_zhice_next,     1, "获取下一步 <task_id>"),
    "zhice-start":    (cmd_zhice_start,    1, "开始执行步骤 <step_id>"),
    "zhice-heartbeat":(cmd_zhice_heartbeat, 1, "发送心跳 <step_id> [status_reason]"),
    "zhice-submit":   (cmd_zhice_submit,   1, "提交步骤结果 <step_id> [--file <path>]"),
    "zhice-issue":    (cmd_zhice_issue,    3, "报告问题 <step_id> <issue_type> <description>"),
    "zhice-cancel":   (cmd_zhice_cancel,   1, "取消任务 <task_id>"),
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("ACSSA 智能体操作系统 Agent 适配器 v2.1")
        print(f"  AGENT_ID={AGENT_ID}  HOST={HOST}")
        print()
        for name, (_, _, desc) in COMMANDS.items():
            print(f"  {name:20s}  {desc}")
        print()
        print("环境变量:")
        print("  QINGTIAN_HOST          底座地址 (default: http://localhost:1996)")
        print("  AGENT_ID               Agent 命名空间 (required)")
        print("  AGENT_NAME             Agent 显示名称")
        print("  QINGTIAN_TOKEN         API Token (required)")
        print("  QINGTIAN_MEM_TYPE      记忆类型 (default: episodic)")
        print("  QINGTIAN_RECOVER_SINCE 恢复起始时间 ISO (optional)")
        print("  QINGTIAN_DECISIONS     会话决策 JSON 数组 (session-end 用)")
        print("  QINGTIAN_TIMEOUT       HTTP 超时秒数 (default: 30)")
        print("  QINGTIAN_MAX_RETRIES   最大重试次数 (default: 3)")
        print()
        print("内容输入方式（优先级从高到低）:")
        print("  1. --file <path>       从文件读取")
        print("  2. stdin 管道           echo '...' | qingtian.py learn")
        print("  3. 命令行参数            qingtian.py recall 关键词")
        sys.exit(1)

    cmd_name = sys.argv[1]
    if cmd_name not in COMMANDS:
        print(f"未知命令: {cmd_name}", file=sys.stderr)
        sys.exit(1)

    handler, _, _ = COMMANDS[cmd_name]
    result = handler(sys.argv[2:])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
