# 执策 Agent 工具使用指南

## 概述

执策是你的任务执行引擎。接到指令后，用执策把自己的工作管起来——引擎告诉你要做什么、检查你做对了没有、持久化你的进度（不怕中断）。

## 完整链路（复杂任务）

```
1. POST /v1/zhice/tasks             创建任务（N 个 Steps）
2. GET  /v1/zhice/tasks/{id}/next   引擎原子分配下一步
3. POST /v1/zhice/steps/{id}/start  确认开始执行
4. POST /v1/zhice/steps/{id}/heartbeat  定期心跳（含进度）
5. POST /v1/zhice/steps/{id}/submit 提交结果 + 检查结果
   ├─ 检查通过 → 回到步骤 2（拿下一步）
   └─ 检查不通过（auto_retry > 0）→ 回到步骤 3
```

## API 参考

### 1. 创建任务

```bash
POST /v1/zhice/tasks
```

简单任务（1 个 Step）：

```json
{
  "title": "检查 Redis 状态",
  "description": "确认 Redis 是否正常响应",
  "steps": [{
    "step_index": 1,
    "title": "PING Redis",
    "instruction": "连接 redis://localhost:6379，执行 PING，返回结果",
    "acceptance_criteria": [
      {"type": "output_contains", "field": "result", "keyword": "PONG"}
    ],
    "timeout_minutes": 2
  }],
  "created_by": "小运"
}
```

复杂任务（N 个 Steps，带依赖）：

```json
{
  "title": "部署智采系统 v2.1",
  "description": "将智采系统 v2.1 部署到生产环境",
  "steps": [
    {
      "step_index": 1,
      "title": "拉取代码",
      "instruction": "从 Gitea 拉取 master 分支到 /opt/zhica/",
      "acceptance_criteria": [
        {"type": "file_exists", "path": "/opt/zhica/main.py", "required": true}
      ],
      "timeout_minutes": 5
    },
    {
      "step_index": 2,
      "title": "安装依赖",
      "instruction": "在 /opt/zhica/ 执行 pip install -r requirements.txt",
      "depends_on": [1],
      "timeout_minutes": 10
    },
    {
      "step_index": 3,
      "title": "重启服务",
      "instruction": "执行 systemctl restart zhica",
      "depends_on": [2],
      "acceptance_criteria": [
        {"type": "api_health", "url": "http://localhost:1996/v1/huanyu/health", "expected_status": 200}
      ],
      "timeout_minutes": 3
    }
  ],
  "acceptance_criteria": [
    {"type": "api_health", "url": "http://localhost:1996/v1/huanyu/health", "expected_status": 200}
  ],
  "timeout_minutes": 60,
  "created_by": "大师"
}
```

### 2. 获取下一步

```bash
GET /v1/zhice/tasks/{task_id}/next?agent_id=你的AgentID
```

返回示例（有待执行 Step）：

```json
{
  "task_id": 42,
  "task_status": "running",
  "current_step": {
    "step_id": 104,
    "step_index": 2,
    "title": "安装依赖",
    "instruction": "在 /opt/zhica/ 执行 pip install -r requirements.txt",
    "acceptance_criteria": null,
    "retries_left": 2,
    "timeout_minutes": 10
  },
  "progress": "1/3 completed",
  "upcoming_steps": [
    {"step_index": 3, "title": "重启服务", "status": "pending"}
  ]
}
```

无待执行 Step 时 `current_step` 为 null：

```json
{
  "task_id": 42,
  "task_status": "running",
  "current_step": null,
  "reason": "dependencies_not_met",
  "blocked_steps": 1,
  "progress": "1/3 completed"
}
```

### 3. 开始执行

```bash
POST /v1/zhice/steps/{step_id}/start
```

```json
{
  "agent_id": "小运"
}
```

`agent_id` 必须与 Step 的 `assigned_agent` 一致，否则返回 403。

### 4. 心跳

```bash
POST /v1/zhice/steps/{step_id}/heartbeat
```

```json
{
  "agent_id": "小运",
  "progress": "50%",
  "status": "正在安装依赖...",
  "status_reason": "executing",
  "outputs": {}
}
```

| status_reason | 含义 | 超时行为 |
|---------------|------|---------|
| `executing` | 正常执行中 | 按 timeout_minutes 正常超时 |
| `waiting_input` | 等待人工输入 | 3x timeout 宽容 |
| `blocked` | 被外部依赖阻塞 | 不按 Step 超时，但 10 分钟无心跳仍 timed_out |

### 5. 提交结果

```bash
POST /v1/zhice/steps/{step_id}/submit
```

**成功提交（含 Agent 本地检查结果）：**

```json
{
  "agent_id": "小运",
  "status": "completed",
  "summary": "代码已拉到 /opt/zhica/，main.py 存在",
  "outputs": {
    "repo_path": "/opt/zhica/",
    "branch": "master",
    "check_results": {
      "file_exists": [
        {"path": "/opt/zhica/main.py", "exists": true}
      ]
    }
  },
  "idempotency_key": "550e8400-e29b-41d4-a716-446655440000"
}
```

**报告执行失败（Agent 自己判定失败）：**

```json
{
  "agent_id": "小运",
  "status": "failed",
  "summary": "pip install 失败：无法访问 pypi.org",
  "idempotency_key": "550e8400-e29b-41d4-a716-446655440001"
}
```

**关于 check_results：**

Agent 上报检查的规则类型，Agent 需本地执行检查后将结果写入 `outputs.check_results`：

| 规则类型 | Agent 本地执行 | check_results 格式 |
|---------|---------------|-------------------|
| `file_exists` | `test -f <path>` | `{"file_exists": [{"path": "...", "exists": true}]}` |
| `api_health` | `curl -s -o /dev/null -w "%{http_code}" <url>` | `{"api_health": [{"url": "...", "status_code": 200}]}` |
| `db_query` | 执行 SQL 获取 count | `{"db_query": [{"sql": "...", "count": 5}]}` |
| `run_script` | `python3 <script>` | `{"run_script": [{"script": "...", "exit_code": 0}]}` |

**关于 idempotency_key：**

每次 submit 必须生成新的 UUID v4。`python3 -c "import uuid; print(uuid.uuid4())"`

**关于 signature（Ed25519 签名，§3.4.3）：**

submit 含 `check_results` 时应附带 Ed25519 签名，供引擎验签。签名流程：

1. 从镇岳获取私钥：
```bash
PRIVATE_KEY=$(curl -s -H "Authorization: Bearer <你的token>" \
  http://127.0.0.1:1996/v1/zhenyue/agents/<你的agent_id>/private-key | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['private_key'])")
```

2. 用私钥对 check_results 签名：
```bash
SIGNATURE=$(python3 -c "
import hashlib, json
from nacl.signing import SigningKey

check_results = $(cat /tmp/check_results.json)  # 你的 check_results
sk = SigningKey(bytes.fromhex('$PRIVATE_KEY'))
message = json.dumps(check_results, sort_keys=True, separators=(',',':')).encode()
print(sk.sign(message).signature.hex())
")
```

3. 提交时带上 signature 字段：
```json
{
  "agent_id": "小运",
  "status": "completed",
  "summary": "...",
  "outputs": {"check_results": {...}},
  "idempotency_key": "550e8400-...",
  "signature": "<第2步得到的hex签名>"
}
```

> 签名是可选的：未提供时引擎不验签（向后兼容Phase 1）。高价值任务应签。

### 6. 报告问题

```bash
POST /v1/zhice/steps/{step_id}/issue
```

```json
{
  "agent_id": "小运",
  "issue_type": "blocked_by_dependency",
  "description": "pip install 失败，服务器无法访问 pypi.org",
  "severity": "blocking"
}
```

`issue_type`：`blocked_by_dependency` | `need_clarification` | `resource_insufficient`

### 7. 取消任务

```bash
POST /v1/zhice/tasks/{task_id}/cancel
```

### 8. 中断恢复

```bash
GET /v1/zhice/recover?agent_id=你的AgentID
```

## 检查规则速查

| 规则 | 类型 | 谁执行 | 必填字段 |
|------|------|--------|---------|
| `output_contains` | engine | 引擎 | `field`, `keyword` |
| `manual_review` | engine | 人 | `reviewer` |
| `file_exists` | agent_report | Agent | `path`, `required` (bool) |
| `api_health` | agent_report | Agent | `url`, `expected_status` |
| `db_query` | agent_report | Agent | `sql`, `expected_min` |
| `run_script` | agent_report | Agent | `script`, `expected_exit_code` |

## 常见错误码

| HTTP | 含义 | 处理 |
|------|------|------|
| 400 | 参数校验失败 | 检查请求体格式 |
| 403 | 无权操作此 Step | 确认 agent_id 与 assigned_agent 一致 |
| 404 | Task/Step 不存在 | 检查 ID 是否正确 |
| 409 | 状态冲突 | Step 状态不满足操作要求 |
| 422 | 依赖未满足 | 等前置 Step 完成后再试 |

## Agent 注意事项

1. **每次 submit 生成新的 idempotency_key**（UUID v4），不要复用
2. **先 /next 拿锁，再 start，再执行** — 不能跳过引擎直接做
3. **check_results 字段名必须与 acceptance_criteria type 一致**
4. **不要滥发 issue** — 自己能解决的走 submit→rejected→retry 通道
5. **auto_retry=0 时 Step 已终态失败**，需重做联系创建者调用 `POST /steps/{step_id}/reject`
6. heartbeat 的 `status_reason` 影响看门狗超时策略，请如实填写
