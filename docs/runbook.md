# ACSSA 智能体操作系统 生产 Runbook

> 常见故障恢复流程。线上团队在出现事故时按本文档操作。

---

## 目录

1. [快速诊断](#1-快速诊断)
2. [PostgreSQL 故障](#2-postgresql-故障)
3. [Redis 故障](#3-redis-故障)
4. [底座 OOM](#4-底座-oom)
5. [Agent 死循环](#5-agent-死循环)
6. [Docker 容器异常](#6-docker-容器异常)
7. [数据备份与恢复](#7-数据备份与恢复)

---

## 1. 快速诊断

### 1.1 三秒诊断

```bash
# 1. 底座活着吗？
curl -s -o /dev/null -w "%{http_code}" http://localhost:1996/health

# 2. 容器在跑吗？
docker ps --filter "name=qingtian" --format "{{.Names}}\t{{.Status}}"

# 3. 磁盘还有空间吗？
df -h / | tail -1
```

**正常状态：**
```
底座 → 200
容器 → qingtian-qingtian-1  Up 3 days
磁盘 → /dev/sda1   50G   20G   30G  40% /
```

### 1.2 日志快速定位

```bash
# 查看最近错误
docker compose logs qingtian --tail 100 | grep -E "ERROR|CRITICAL|Traceback"

# 查看总线调度日志
docker compose logs qingtian --tail 50 | grep "\[Bus\]"

# 查看特定 Agent 日志
docker compose logs qingtian --tail 200 | grep "biz:buyer-01"
```

### 1.3 资源占用检查

```bash
# 容器资源占用
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}"

# 主机资源
free -h
top -bn1 | head -5
```

---

## 2. PostgreSQL 故障

### 2.1 症状

| 症状 | 可能原因 |
|------|---------|
| `/health` 返回 `database: disconnected` | PG 容器崩溃 |
| Agent 注册/消息发送报 500 | PG 连接失败 |
| 任务创建失败 | PG 事务异常 |
| `docker logs` 显示 `connection refused` | PG 未启动 |

### 2.2 恢复步骤

#### 检查 PG 状态

```bash
# PG 容器在跑吗？
docker ps --filter "name=postgres" --format "{{.Status}}"

# PG 日志
docker compose logs postgres --tail 50
```

#### 重启 PG

```bash
# 重启 PG 容器
docker compose restart postgres

# 等待就绪（约 5-10 秒）
sleep 10
docker compose logs postgres --tail 5
```

#### 检查数据完整性

```bash
# 进入 PG 容器
docker compose exec postgres psql -U qingtian -d qingtian -c "SELECT count(*) FROM huanyu.agents;"
docker compose exec postgres psql -U qingtian -d qingtian -c "SELECT count(*) FROM yongheng.memories;"

# 检查是否有损坏表
docker compose exec postgres psql -U qingtian -d qingtian -c "SELECT schemaname,tablename,n_dead_tup FROM pg_stat_user_tables WHERE n_dead_tup > 1000;"
```

#### 如果数据损坏

```bash
# 尝试修复
docker compose exec postgres psql -U qingtian -d qingtian -c "VACUUM FULL ANALYZE;"

# 如果修复失败，从备份恢复（见第 7 节）
```

#### 如果 PG 完全无法启动

```bash
# 1. 检查磁盘空间
df -h /var/lib/docker

# 2. 检查 PG 日志
docker compose logs postgres --tail 100 | grep -i "error|fatal"

# 3. 尝试清理并重建
docker compose down
docker volume rm qingtian_postgres_data  # 注意：这会丢失数据！
# 如果有备份，先恢复备份再启动
docker compose up -d
```

**注意：** 生产环境 PG 应用独立实例（非 Docker），由 DBA 团队负责运维。底座团队不要直接操作生产 PG。

---

## 3. Redis 故障

### 3.1 症状

| 症状 | 可能原因 |
|------|---------|
| 消息推送失败 | Redis 不可用 |
| 跨底座消息丢失 | Redis Pub/Sub 断连 |
| Agent 心跳超时 | Redis session 数据丢失 |
| `docker logs` 显示 `connection refused` | Redis 未启动 |

### 3.2 恢复步骤

#### 检查 Redis 状态

```bash
docker ps --filter "name=redis" --format "{{.Status}}"
docker compose logs redis --tail 30
```

#### 重启 Redis

```bash
docker compose restart redis
sleep 5
```

#### 验证 Redis 可用

```bash
docker compose exec redis redis-cli ping
# 应返回 PONG

# 检查 key 数量
docker compose exec redis redis-cli dbsize
```

#### 缓存重建

Redis 重启后，以下数据会自动重建（不丢核心数据）：

| 数据 | 重建方式 | 重建时间 |
|------|---------|---------|
| Agent session | Agent 重连后重建 | 即时 |
| 总线状态 | 底座 reconciliation | 底座启动时 |
| 消息队列 | 持久化在 PG | 无需重建 |

**Redis 是缓存层，重启不丢持久化数据。** 如 Redis 完全不可用，底座功能降级但不崩溃（消息推送降级 inbox）。

---

## 4. 底座 OOM

### 4.1 症状

| 症状 | 说明 |
|------|------|
| 容器被 `docker kill` | Docker 检测到 OOM 自动杀容器 |
| `dmesg` 显示 `Out of memory` | 内核 OOM Killer |
| 底座进程消失 | OOM 后无残留 |
| Agent 全部断连 | 底座重启后自动恢复 |

### 4.2 紧急恢复

```bash
# 1. 确认是 OOM
dmesg | tail -20 | grep -i "oom\|killed"

# 2. Docker 资源限制是否配置
docker inspect qingtian-qingtian-1 | grep -A 5 "Memory"

# 3. 重启底座
docker compose restart qingtian

# 4. 查看日志确认正常启动
docker compose logs qingtian --tail 30
```

### 4.3 根因排查

```bash
# 查看 OOM 前的内存增长
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}" 

# 检查是否 Agent 过多
curl -s http://localhost:1996/v1/xihe/stats | python -m json.tool

# 检查系统内存
free -h
```

### 4.4 预防措施

| 措施 | 方法 | 效果 |
|------|------|------|
| 限制容器内存 | Docker `--memory=4g` | 防止 OOM 影响宿主机 |
| 减少 max_agents | `config.yaml` → `xihe.max_agents: 30` | 限制 Agent 总数 |
| 降低健康检查频率 | `xihe.health_check.interval: 60` | 减少底座开销 |
| 监控告警 | 接入 infra:monitor | 内存 > 80% 告警 |

### 4.5 内存泄漏确认

如果底座正常运行时内存持续增长：

```bash
# 每 30 秒采样一次
for i in $(seq 1 10); do
  echo "$(date +%H:%M:%S)  $(docker stats qingtian-qingtian-1 --no-stream --format "{{.MemUsage}}")"
  sleep 30
done
```

如果 5 分钟内内存增长超过 20%，可能是内存泄漏，需报给开发团队。

---

## 5. Agent 死循环

### 5.1 症状

| 症状 | 说明 |
|------|------|
| Agent CPU 持续 100% | 死循环或无限重试 |
| Agent 连续崩溃重启 | 启动 → 崩溃 → 启动循环 |
| LLM 调用无限循环 | Agent 不断调 LLM 但不产出 |
| 任务进度卡住不变 | 某步骤执行超时 |

### 5.2 紧急恢复

```bash
# 1. 查看 Agent 状态
curl -s http://localhost:1996/v1/xihe/agents | python -m json.tool

# 2. 暂停 Agent（不会杀进程，但停止调度）
curl -X POST http://localhost:1996/v1/xihe/agents/{agent_id}/pause

# 3. 如果暂停无效，强制停止
curl -X POST http://localhost:1996/v1/xihe/agents/{agent_id}/stop
```

### 5.3 根因排查

```bash
# 查看该 Agent 最近的任务
curl -s "http://localhost:1996/v1/zhice/tasks?created_by={agent_id}" | python -m json.tool

# 查看 Agent 资源占用
curl -s http://localhost:1996/v1/xihe/stats | python -m json.tool

# 查看底座日志中该 Agent 的活动
docker compose logs qingtian --tail 200 | grep "{agent_id}"
```

### 5.4 恢复运行

```bash
# 1. 确认问题已修复（如调整了任务参数）
# 2. 重新启动 Agent
curl -X POST http://localhost:1996/v1/xihe/agents/{agent_id}/start

# 3. 监控恢复后行为
curl -s "http://localhost:1996/v1/zhice/tasks?created_by={agent_id}&status=running" | python -m json.tool
```

### 5.5 Agent 崩溃循环保护

底座内置保护机制：

| 机制 | 参数 | 行为 |
|------|------|------|
| 退避重启 | `xihe.restart_backoff: [3,15,60,300]` | 每次重启间隔递增 |
| 重启上限 | `xihe.restart.max_retries: 4` | 超上限进入 fatal |
| 冷却期 | 3 分钟内 3 次重启 | 进入 5 分钟强制冷却 |
| 批量限速 | `xihe.restart.batch_size: 5` | 避免同时重启过多 |

**如果 Agent 进入 fatal 状态：** 人工排查根本原因后再手动重启，不要一键拉回。

---

## 6. Docker 容器异常

### 6.1 容器不断重启

```bash
# 查看重启次数
docker inspect qingtian-qingtian-1 | grep -A 10 "RestartCount"

# 查看最后崩溃日志
docker compose logs qingtian --tail 100 | grep -E "ERROR|CRITICAL|Traceback"
```

**常见原因与修复：**

| 原因 | 修复 |
|------|------|
| 配置错误 | 检查 `config.yaml` 语法 |
| 端口冲突 | 检查 1996 端口占用 |
| PG 未就绪 | 增加 `depends_on` 健康检查 |
| OOM | 增加容器内存限制 |

### 6.2 磁盘空间满

```bash
# 检查磁盘
df -h
du -sh /var/lib/docker/

# 清理 Docker 垃圾
docker system prune -a -f

# 清理日志
docker compose logs qingtian > /tmp/logs-$(date +%Y%m%d).tar.gz
truncate -s 0 $(docker inspect --format='{{.LogPath}}' qingtian-qingtian-1)

# 检查 isolation 区大小
du -sh /opt/qingtian/quarantine/
```

### 6.3 网络问题

```bash
# 检查容器网络
docker network ls
docker network inspect qingtian_default

# 检查 DNS 解析
docker compose exec qingtian ping postgres -c 2
docker compose exec qingtian ping redis -c 2

# 检查对外连接（LLM API）
docker compose exec qingtian curl -s -o /dev/null -w "%{http_code}" https://api.deepseek.com
```

---

## 7. 数据备份与恢复

### 7.1 自动备份

底座内置自动备份：

| 备份类型 | 内容 | 频率 | 保留 |
|---------|------|:----:|:----:|
| 配置备份 | `config.yaml` + `tool-rules.yaml` | 写前备份 | 30 个版本 |
| 删除隔离 | Agent 删除的文件 | 事件触发 | 30 天 |
| 审计日志 | 不可变哈希链 | 实时写入 | 永久 |

### 7.2 PG 手动备份

```bash
# 备份全部数据
docker compose exec postgres pg_dump -U qingtian -d qingtian -F c -f /tmp/qingtian-backup-$(date +%Y%m%d).dump

# 从容器复制出来
docker cp $(docker ps --filter "name=postgres" -q):/tmp/qingtian-backup-*.dump ./

# 仅备份结构（不含数据）
docker compose exec postgres pg_dump -U qingtian -d qingtian --schema-only -f /tmp/qingtian-schema.dump
```

### 7.3 PG 恢复

```bash
# 1. 停止底座
docker compose stop qingtian

# 2. 恢复数据库
docker compose exec -T postgres pg_restore -U qingtian -d qingtian --clean < qingtian-backup-20260705.dump

# 3. 重启底座
docker compose start qingtian

# 4. 验证数据完整性
curl -s http://localhost:1996/health
curl -s http://localhost:1996/v1/huanyu/agents | python -c "import sys,json; print(len(json.load(sys.stdin).get('agents',[])), 'agents')"
```

### 7.4 全量恢复流程

```
故障发生
  │
  ├── ① 确认故障范围（PG / Redis / 底座 / Agent）
  │
  ├── ② 如果数据丢失
  │     ├── 从最新备份恢复 PG
  │     └── Redis 缓存自动重建
  │
  ├── ③ 如果只是进程崩溃
  │     ├── docker compose restart qingtian
  │     └── 底座 reconciliation 自动恢复
  │
  ├── ④ 验证恢复
  │     ├── /health 返回 ok
  │     ├── Agent 列表非空
  │     └── 任务可正常创建
  │
  └── ⑤ 记录事故
        ├── 时间、影响范围、恢复耗时
        └── 根因分析 → 改进措施
```

### 7.5 备份策略建议

| 环境 | PG 备份频率 | 保留周期 | 备份存储 |
|:----:|:----------:|:--------:|---------|
| 生产 | 每日 03:00 | 30 天 | 异地存储 |
| 预发 | 每周 | 2 周 | 本地 |
| 测试 | 不备份 | — | — |

---

## 附录 A：紧急联系方式

| 角色 | 负责 | 响应时间 |
|------|------|:--------:|
| 底座运维 | Docker/PG/Redis 运维 | 30 分钟 |
| 开发团队 | 代码级故障 | 2 小时 |
| 安全管理 | 安全事件 | 15 分钟 |

## 附录 B：故障等级定义

| 等级 | 定义 | 响应 | 升级条件 |
|:----:|------|:----:|---------|
| P0 | 底座完全不可用 | 立即 | 15 分钟无人响应 |
| P1 | 部分模块故障 | 1 小时 | 2 小时未修复 |
| P2 | 单 Agent 故障 | 4 小时 | 影响扩大时升级 |
| P3 | 非功能性缺陷 | 下一迭代 | 不升级 |
