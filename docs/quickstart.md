# ACSSA Agent OS — 10-Minute Quick Start

**[简体中文](quickstart.zh-CN.md)** | **English**

> From zero to seeing an agent register, communicate, and execute tasks on the base — in 10 minutes.

---

## Requirements

- Docker 24+, Docker Compose v2+
- An LLM API key (DeepSeek / OpenAI-compatible)

---

## 1. One-command deployment (2 min)

```bash
# Clone
git clone https://github.com/chnacssa/qingtian.git
cd qingtian

# Provide your LLM API key
export DEEPSEEK_API_KEY=sk-your-key-here

# Start (pulls PG + Redis + the ACSSA base automatically)
docker compose up -d

# Wait for readiness (~30s)
sleep 30
curl http://localhost:1996/health
```

**Expected output**:
```json
{"status": "ok", "service": "qingtian", "version": "0.2.0"}
```

---

## 2. Register an agent (1 min)

```bash
curl -X POST http://localhost:1996/v1/huanyu/agents/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "demo-agent",
    "category": "biz:buyer"
  }'
```

**Expected output** (you now have an `agent_id` and an `ain`):
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

> Save your `agent_id` — you'll use it below.

---

## 3. Browse the agent directory (1 min)

```bash
# List all agents
curl http://localhost:1996/v1/huanyu/agents

# Search
curl "http://localhost:1996/v1/huanyu/agents/search?q=demo"
```

**Expected output**:
```json
{"agents": [{"agent_id": "biz:buyer-01", "name": "demo-agent", ...}]}
```

---

## 4. Send a message (1 min)

The simplest inter-agent interaction is messaging:

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

**Expected output**:
```json
{
  "message_id": "...",
  "from_agent_id": "biz:buyer-01",
  "to_agent_id": "biz:buyer-01",
  "status": "unread"
}
```

Check the inbox:

```bash
curl http://localhost:1996/v1/huanyu/inbox/biz:buyer-01
```

---

## 5. Write a memory (1 min)

Agents persist memory via the Yongheng (永恒) module:

```bash
curl -X POST http://localhost:1996/v1/yongheng/memories \
  -H "Content-Type: application/json" \
  -d '{
    "namespace": "agent:biz:buyer-01",
    "type": "episodic",
    "content": "First deployment complete. Messaging and memory systems tested."
  }'
```

**Expected output**:
```json
{"status": "ok", "memory_id": 1}
```

Semantic search (POST — the query goes in the body):

```bash
curl -X POST http://localhost:1996/v1/yongheng/memories/search \
  -H "Content-Type: application/json" \
  -d '{"namespace": "agent:biz:buyer-01", "query": "deployment"}'
```

---

## 6. Create a task (1 min)

The Zhice (执策) module orchestrates tasks. Create a simple acceptance-check task:

```bash
curl -X POST http://localhost:1996/v1/zhice/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Deployment verification",
    "description": "Confirm all ACSSA modules are running normally",
    "created_by": "biz:buyer-01",
    "steps": [
      {"step_index": 1, "title": "Check health endpoint", "instruction": "Check the /health endpoint"},
      {"step_index": 2, "title": "Confirm registration", "instruction": "Confirm the agent registered successfully"},
      {"step_index": 3, "title": "Verify messaging", "instruction": "Verify message send/receive"}
    ]
  }'
```

**Expected output**:
```json
{"task_id": 1, "status": "created", "steps": 3}
```

---

## 7. Check the bus (1 min)

The bus manages agent lifecycles automatically — no manual steps needed:

```bash
curl http://localhost:1996/v1/bus/buffer/biz:buyer-01
```

---

## 8. Where to go next

| Goal | Reference |
|------|------|
| Full API surface | [API reference](../README.md#-api-reference) |
| Deployment configuration | [Configuration](../README.md#️-configuration) |
| Build your own agents | [OpenClaw SDK](https://github.com/acssa/openclaw) |
| Enterprise deployment | Contact [acssa.cn](https://acssa.cn) for commercial licensing |

---

## Troubleshooting

| Problem | Check |
|------|------|
| `curl` hangs | `docker compose logs qingtian` for errors |
| Registration returns 422 | `category` must be one of `biz:buyer` / `biz:seller` / `infra:monitor` etc. |
| Task creation fails | Make sure `steps` entries include `idx` and `instruction` |
| Memory write hangs | Check that `DEEPSEEK_API_KEY` is set |
| Port conflict | Adjust port mappings in `docker-compose.yml` |
