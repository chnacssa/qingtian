# ACSSA 智能体操作系统 10 分钟快速开始

**简体中文** | [English](quickstart.md)
> 从零开始，10 分钟内看到 Agent 在底座上注册、通信、执行任务。

---

## 环境要求

- Docker 24+、Docker Compose v2+
- 一个 LLM API Key（DeepSeek / OpenAI 兼容）

---

## 1. 一键部署（2 分钟）

```bash
# 克隆仓库
git clone https://github.com/chnacssa/qingtian.git
cd qingtian

# 配置 LLM API 密钥
export DEEPSEEK_API_KEY=sk-your-key-here

# 启动（自动拉取 PG + Redis + ACSSA 底座）
docker compose up -d

# 等待就绪（约 30 秒）
sleep 30
curl http://localhost:1996/health
```

**期望输出**：
```json
{"status": "ok", "service": "qingtian", "version": "0.2.0"}
```

---

## 2. 注册一个 Agent（1 分钟）

部署成功后，注册你的第一个 Agent：

```bash
curl -X POST http://localhost:1996/v1/huanyu/agents/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo-agent",
    "category": "biz:buyer"
  }'
```

**期望输出**（你拿到了 `agent_id` 和 `ain`）：
```json
{
  "agent_id": "biz:buyer-01",
  "ain": "acssa.cn:l:demo:localhost:biz:buyer:01",
  "name": "demo-agent",
  "category": "biz:buyer",
  "status": "active",
  "trust_level": "basic"
}
```

> 记下你的 `agent_id`，后面会用。

---

## 3. 查看 Agent 目录（1 分钟）

```bash
# 列出所有 Agent
curl http://localhost:1996/v1/huanyu/agents

# 搜索 Agent
curl "http://localhost:1996/v1/huanyu/agents/search?q=demo"
```

**期望输出**：
```json
{"agents": [{"agent_id": "biz:buyer-01", "name": "demo-agent", ...}]}
```

---

## 4. 发送消息（1 分钟）

Agent 之间最简单的交互是发消息：

```bash
curl -X POST http://localhost:1996/v1/huanyu/messages \
  -H "Content-Type: application/json" \
  -d '{
    "from_agent": "biz:buyer-01",
    "to_agent": "biz:buyer-01",
    "message_type": "info",
    "payload": {"msg": "Hello ACSSA!"}
  }'
```

**期望输出**：
```json
{
  "message_id": "...",
  "from_agent_id": "biz:buyer-01",
  "to_agent_id": "biz:buyer-01",
  "status": "unread"
}
```

检查收件箱：

```bash
curl http://localhost:1996/v1/huanyu/inbox/biz:buyer-01
```

---

## 5. 写入记忆（1 分钟）

Agent 可以通过永恒模块持久化记忆：

```bash
curl -X POST http://localhost:1996/v1/yongheng/memories \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "agent:biz:buyer-01",
    "type": "episodic",
    "content": "今天完成了第一次部署，测试了消息系统和记忆系统。"
  }'
```

**期望输出**：
```json
{"status": "ok", "memory_id": 1}
```

语义搜索记忆（POST，查询走 body）：

```bash
curl -X POST http://localhost:1996/v1/yongheng/memories/search \
  -H "Content-Type: application/json" \
  -d '{"namespace": "agent:biz:buyer-01", "query": "部署"}'
```

---

## 6. 创建一个任务（1 分钟）

执策模块负责任务编排。创建一个简单的质检任务：

```bash
curl -X POST http://localhost:1996/v1/zhice/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "部署验证",
    "description": "确认ACSSA 智能体操作系统各模块正常运行",
    "created_by": "biz:buyer-01",
    "steps": [
      {"step_index": 1, "title": "检查健康端点", "instruction": "检查 /health 端点"},
      {"step_index": 2, "title": "确认注册", "instruction": "确认 Agent 注册成功"},
      {"step_index": 3, "title": "验证消息", "instruction": "验证消息收发功能"}
    ]
  }'
```

**期望输出**：
```json
{"task_id": 1, "status": "created", "steps": 3}
```

---

## 7. 查看总线状态（1 分钟）

总线自动管理 Agent 生命周期，无需手动干预：

```bash
curl http://localhost:1996/v1/bus/buffer/biz:buyer-01
```

---

## 8. 接下来做什么？

| 目标 | 参考 |
|------|------|
| 了解全部 API | [API 参考](../README.zh-CN.md#-api-参考) |
| 部署配置详解 | [配置指南](../README.zh-CN.md#️-配置) |
| 开发自己的 Agent | [OpenClaw SDK](https://github.com/acssa/openclaw) |
| 企业部署 | 商业授权请联系 [acssa.cn](https://acssa.cn) |

---

## 故障排除

| 问题 | 检查 |
|------|------|
| `curl` 无响应 | `docker compose logs qingtian` 查看错误 |
| 注册返回 422 | category 必须是 `biz:buyer` / `biz:seller` / `infra:monitor` 等 |
| 任务创建失败 | 确保 `steps` 包含 `idx` 和 `instruction` |
| 记忆写入无响应 | 检查 `DEEPSEEK_API_KEY` 是否已配置 |
| 端口冲突 | 修改 `docker-compose.yml` 中的端口映射 |
