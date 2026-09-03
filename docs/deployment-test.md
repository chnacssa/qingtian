# 开源版部署测试文档 v2.0（终版）

> 2026-07-14 | 破军 | R1-R7 全量代码审核完成，供小智收底验收

---

## 一、测试环境要求

| 项 | 要求 |
|------|------|
| OS | Ubuntu 22.04+ / Debian 12+ / Rocky 9+ |
| Docker | 24+ |
| Docker Compose | v2+ |
| 端口 | 1996（ACSSA）、5432（PG）、6379（Redis） |
| LLM Key | DEEPSEEK_API_KEY |

---

## 二、测试流程

### 测试 1：Docker 一键部署

```bash
# 1. 克隆（仓库根含 docker-compose.yml，代码在 qingtian/ 子目录）
git clone https://github.com/chnacssa/qingtian.git qingtian-test
cd qingtian-test

# 2. 配置密钥
export DEEPSEEK_API_KEY=sk-your-key-here

# 3. 启动
docker compose up -d

# 4. 等待就绪（约 30 秒）
sleep 30

# 5. 健康检查
curl http://localhost:1996/health
```

**期望**：`{"status": "ok"}`

**失败处理**：`docker compose logs qingtian | tail -50`

---

### 测试 2：手动安装（裸机部署）

```bash
# 全新机器（Python 3.12+ / PostgreSQL 16+ / Redis 7+ 已装）
git clone https://github.com/chnacssa/qingtian.git qingtian-test
cd qingtian-test/qingtian
pip install -r requirements.txt
psql -U postgres -c "CREATE USER qingtian WITH PASSWORD 'qingtian-2026';"
psql -U postgres -c "CREATE DATABASE qingtian OWNER qingtian;"
psql -U qingtian -d qingtian -f scripts/init.sql
cp config.yaml.example config.yaml
export DEEPSEEK_API_KEY=sk-your-key-here
python3 main.py
```

**期望**：服务监听 1996 端口，`curl http://localhost:1996/health` 返回 ok

---

### 测试 3：工作秘书自动绑定

```bash
# 1. 确认工作秘书已安装
ls /opt/qingtian/skills/packages/work_secretary/
# 期望: __init__.py skill.json work_secretary.py ... 共 17 个文件

# 2. 注册 Agent（agent_id 由系统自动分配，不在 body 中传）
curl -s -X POST http://localhost:1996/v1/huanyu/agents/register \
  -H "Content-Type: application/json" \
  -d '{"category": "biz:assistant", "display_name": "测试助手"}'

# 3. 等待启动
sleep 5

# 4. 检查 Agent 状态
curl -s http://localhost:1996/v1/xihe/agents | python3 -m json.tool
```

**期望**：Agent 注册成功，返回 agent_id
```

**期望**：Agent 状态 running，绑定 work_secretary Skill

---

### 测试 4：冷启动欢迎消息

```bash
# 查询管理消息（需替换 {agent_id} 为实际 ID）
curl -s "http://localhost:1996/api/v1/skills/admin/messages"
```

**期望**：返回消息列表，包含工作秘书冷启动欢迎消息

---

### 测试 5：NL 问答

```bash
# 通过 Skill API 执行工作秘书（需替换 {agent_id} 为实际 ID）
curl -s -X POST "http://localhost:1996/api/v1/skills/work_secretary/execute" \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: {agent_id}" \
  -d '{"action": "帮我查一下今天的会议"}'
```

**期望**：返回 ok 或 passthrough，不返回 500

---

### 测试 6：轨迹记录

```bash
# 发送 3 条操作
for i in 1 2 3; do
  curl -s -X POST "http://localhost:1996/api/v1/skills/work_secretary/execute" \
    -H "Content-Type: application/json" \
    -H "X-Agent-ID: {agent_id}" \
    -d "{\"action\": \"测试操作 $i\"}"
done

# 检查轨迹（需要 admin token）
curl -s -H "Authorization: Bearer $ACSSA_ADMIN_API_KEY" \
  "http://localhost:1996/v1/yongheng/trajectory?namespace=agent:{agent_id}&date=$(date +%Y-%m-%d)"
```

**期望**：返回 actions 列表包含 3 条操作

---

### 测试 7：日报生成

```bash
curl -s -X POST "http://localhost:1996/api/v1/skills/work_secretary/execute" \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: {agent_id}" \
  -d '{"action": "brief"}'
```

**期望**：`{"ok": true, "brief": "..."}`

---

### 测试 8：DND 免打扰

```bash
# 设置
curl -s -X POST "http://localhost:1996/api/v1/skills/work_secretary/execute" \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: {agent_id}" \
  -d '{"action": "dnd:set", "start": "12:00", "end": "14:00", "days": ["mon","tue","wed","thu","fri"]}'

# 查看
curl -s -X POST "http://localhost:1996/api/v1/skills/work_secretary/execute" \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: {agent_id}" \
  -d '{"action": "dnd:list"}'
```

**期望**：返回规则列表

---

### 测试 9：监管适配器

```bash
ls qingtian/osskill/implementations/regulatory_adapter/
# 期望: __init__.py adapter.py skill.json
```

---

### 测试 10：测试套件

```bash
cd qingtian-test/qingtian

# 工作秘书测试
python3 -m pytest osskill/tests/work_secretary/ -v

# Ed25519 验签测试
python3 -m pytest huanyu/tests/test_resolver.py -v
```

**期望**：全部 PASSED

---

## 三、测试结果记录

| 测试 | 结果 | 备注 |
|------|:--:|------|
| 1 Docker 部署 | ⬜ | |
| 2 手动安装 | ⬜ | |
| 3 秘书绑定 | ⬜ | |
| 4 冷启动 | ⬜ | |
| 5 NL 问答 | ⬜ | |
| 6 轨迹记录 | ⬜ | |
| 7 日报 | ⬜ | |
| 8 DND | ⬜ | |
| 9 监管适配器 | ⬜ | |
| 10 测试套件 | ⬜ | |

---

## 四、问题反馈

测试发现问题请回复此文档或发到 `.comm/board.md`。附：
- 测试编号
- 完整错误信息
- `docker compose logs qingtian | tail -50` 或 `journalctl -u qingtian -n 50`
