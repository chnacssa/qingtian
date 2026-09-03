# ACSSA 智能体操作系统 — 企业生产级多智能体底座

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/badge/release-v0.1.0-2ea44f.svg)](https://github.com/chnacssa/qingtian/releases/tag/v0.1.0)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB.svg)](qingtian/requirements.txt)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%2B-336791.svg)](docker-compose.yml)
[![GitHub stars](https://img.shields.io/github/stars/chnacssa/qingtian?style=social)](https://github.com/chnacssa/qingtian/stargazers)

**简体中文** | [English](README.md)
> **企业落地 AI Agent 的完整操作系统**——不是框架、不是 Demo、不是聊天机器人。

ACSSA（擎天）是面向企业生产环境的多智能体操作系统，提供 Agent 落地所需的完整基础设施：记忆检索、知识进化、安全审计、跨底座通信、财务核算、采购询价、Skill 执行运行时。已在电力工程企业真实生产环境运行。

**一句话定位** — 数据主权归你，不经过任何第三方 SaaS；八模块开箱即用，私有化部署任意环境。

---

## 🔒 为什么选择 ACSSA — 数据主权

企业上 AI Agent 最大的顾虑不是"有没有能力"，而是"数据敢不敢交出去"。标书、报价、客户信息、财务数据是企业命脉，绝不能上传到别人的云。

**ACSSA 的核心承诺：数据主权完全归你。**

| 部署方式 | 数据主权 | 适用场景 |
|---------|:--:|------|
| 内网物理机 | ✅ 完全离线内网 | 数据合规要求极严的企业 |
| 云主机（VPC/专线接入内网） | ✅ 数据在你自己的云主机 | 中小企业快速上线 |
| 私有云 / 政务云 | ✅ 专有云自控 | 政企、等保合规 |
| 单机 Docker | ✅ 一台服务器跑全部 | 快速体验 / 小团队 |

**三层保障**：
- **私有化部署**：系统装在你自己的服务器，数据不出你的网络边界
- **权限隔离**：按岗位分级授权，跨企业数据物理隔离（enterprise_id 分区）
- **审计留痕**：哈希链审计日志不可篡改，谁在什么时间做了什么可追溯

> 分场景话术：给企业决策者说"数据不出你的内网"；给技术团队说"数据不出你的网络边界，数据主权归你"。

---

## 🚀 10 分钟快速启动

> 完整步骤指南见 [docs/quickstart.zh-CN.md](docs/quickstart.zh-CN.md) — 从零开始到 Agent 注册、通信、执行任务的全流程。

### Docker 一键部署（推荐）

```bash
# 1. 克隆（仓库根含 docker-compose.yml，代码在 qingtian/ 子目录）
git clone https://github.com/chnacssa/qingtian.git
cd qingtian

# 2. 配置 LLM API 密钥
export DEEPSEEK_API_KEY=sk-your-key-here

# 3. 启动（PostgreSQL + Redis + ACSSA 智能体操作系统）
docker compose up -d

# 4. 验证
curl http://localhost:1996/health
# → {"status": "ok"}

# 5. 查看日志
docker compose logs -f qingtian
```

**首次启动**时自动完成以下流程：

| 步骤 | 自动完成 | 说明 |
|------|---------|------|
| 1. 数据库初始化 | ✅ | `init.sql` 自动执行，创建全部 30+ 张表 |
| 2. 配置生成 | ✅ | 从 `config.yaml.example` 复制默认配置 |
| 3. 健康检查 | ✅ | 等待 PG、Redis、ACSSA全部就绪 |
| 4. 底座就绪 | ✅ | 服务监听 1996 端口，/health 返回 ok |

> **环境要求**：Docker 24+、Docker Compose v2+。无需手动安装 Python、PostgreSQL、Redis。

### 手动安装（裸机部署）

```bash
# 1. 环境要求
# - Python 3.12+
# - PostgreSQL 16+
# - Redis 7+

# 2. 克隆并进入代码目录（代码在仓库根的 qingtian/ 子目录）
git clone https://github.com/chnacssa/qingtian.git
cd qingtian/qingtian

# 3. 安装依赖
pip install -r requirements.txt

# 4. 创建数据库
psql -U postgres -c "CREATE USER qingtian WITH PASSWORD 'qingtian-2026';"
psql -U postgres -c "CREATE DATABASE qingtian OWNER qingtian;"
psql -U qingtian -d qingtian -f scripts/init.sql

# 5. 配置
cp config.yaml.example config.yaml
# 编辑 config.yaml，至少配置 DEEPSEEK_API_KEY

# 6. 启动
python3 main.py
# → 服务运行在 http://localhost:1996
```

---

## 🖥️ 部署环境与平台支持

> **生产环境请用 Linux 裸机 / VM + systemd** —— cgroups 资源隔离、OOM 检测、出站检测、自动部署等生产级能力在此环境下完整可用。

| 部署方式 | 定位 | 资源隔离等生产级能力 |
|---------|------|:--:|
| Linux 裸机 / VM + systemd | **生产（推荐）** | ✅ 完整 |
| Docker（`docker compose up -d`） | 快速体验 / 开发联调 | ⚠️ 部分降级 |
| Windows / macOS | 本地开发调试 | ❌ 基本降级 |

完整能力对照表与降级原因见 [docs/platform-support.md](docs/platform-support.md)。

---

## 🧩 架构概览

### 模块

| 模块 | 名称 | 职责 |
|------|------|------|
| `common/` | 公共基础设施 | 配置、数据库连接、LLM调用、密码学工具 |
| `huanyu/` | **寰宇** — Agent 通信目录 | 注册发现、消息路由、会话管理、GBZ185 合规 |
| `yongheng/` | **永恒** — 记忆检索 | 语义记忆写入/检索、DREEM 门控、embedding |
| `xixing/` | **吸星** — 知识进化 | 知识采集、质量门、分类、蒸馏、自知自评 |
| `huichuan/` | **汇川** — 数据资产中枢 | 全渠道文件接入、智能分类、元数据/图片提取、统一检索 |
| `zhenyue/` | **镇岳** — 安全审计 | 哈希链审计、Ed25519 签名、Token 管理、AES-256-GCM |
| `zhice/` | **执策** — 任务编排 | 任务分解/分派、FSM 状态机、多签验证 |
| `siku/` | **司库** — 企业间结算 | 财务 Agent 跨底座资金交割、账户、哈希链财务审计 |
| `gateway/` | **网关** | 身份解析、角色检查、中间件编排 |
| `xihe/` | **羲和** — Agent 运行时管理 | 进程管理、健康检查、退避重启、资源监控 |
| `bus/` | **总线** — 统一调度 | Agent 生命周期编排、上下文注入、自动注册/接管 |

### 作为操作系统：子系统与企业价值

买 ACSSA 不是买任何一个 AI 功能，而是买一台**能持续雇佣数字员工的计算机**——员工（Skill/岗位智能体）可以随时换，机器永远是你的，所有员工的经验、数据与审计记录都沉淀在这台机器上。每个子系统对应操作系统的经典职责，也对应企业的一项确定性价值：

| OS 子系统 | 擎天模块 | 企业价值 |
|----------|----------|---------|
| 进程管理 | **羲和**（子进程隔离 / cgroups CPU 配额 / OOM 检测 / trust 三级） | N 个数字员工共享一台"计算机"：失控的 Skill 被资源配额和沙箱隔离，一个 Agent 出错不拖垮全局 |
| 进程协调 | **执策**（SOP 任务分解 / FSM 状态机 / 多签审批） | 复杂任务自动拆解、崩溃可恢复；关键动作多签审批——LLM 出错有状态机兜底，不是裸奔 |
| 内存管理 | **永恒**（语义记忆 / 记忆固化 / Agent 画像） | 组织记忆不随人员流失：老员工的做法和经验沉淀为所有 Agent 可检索的记忆 |
| 知识编译器 | **吸星**（采集 → 质量门 → 蒸馏 → 注入） | 组织知识持续"编译"进系统，越用越精——违反一般软件的折旧逻辑，是资产不是成本 |
| 文件系统 / 数据湖 | **汇川**（飞书等多渠道接入 / 智能分类 / 元数据与图片提取 / 统一检索） | **企业数据资产集中**：散落各处的文档、表格、图纸、证书统一入湖、自动分类打标，所有 Agent 共用同一份资产底座——这是企业知识库的升级 |
| 网络栈 | **寰宇**（消息总线 / GB/Z 185 国标 / 跨底座联邦 + AIN 寻址） | 数字员工跨部门、跨企业协作的标准通信协议；企业间对话有统一身份寻址与端到端加密 |
| 安全子系统 | **镇岳**（哈希链审计 / 审批 / 隔离 / 令牌） | 治理在内核层强制执行而非依赖应用自觉——权限、审计、隔离一处配置，全公司所有数字员工服从 |
| 结算子系统 | **司库**（财务 Agent 持 AIN 身份跨底座交割 / 哈希链财务审计） | **面向企业之间的资金交割**：每个底座运行一个财务 Agent，付款通知自动按 AIN 寻址对方企业会计、挑战验证、查账确认到账，全程不可篡改审计——未来 Skill 市场生态里企业间可自动结算的基础设施 |

**OS 级的独特价值，应用层工具给不了的三件事**：

1. **不被锁定**：Skill 不耦合底座，换供应商不换底座——记忆、审计、流程全保留；
2. **治理一处生效**：权限、审计、数据隔离在 OS 层配置一次，全公司所有数字员工服从；
3. **替换成本随时间递增**：部署第一天价值最低，之后每个岗位上岗、每份记忆与数据资产沉淀都在加深粘性——这是企业软件里最抗流失的形态。

### 数据流

```
采集 → 吸星(质量门) → 蒸馏 → 永恒(记忆) → Agent
                                                ↓
                         寰宇(通信) ← Agent间协作
                                                ↓
                         执策(任务) ← LLM分解 + 原子分派
```

### 谁在用这个系统

| 角色 | 使用方式 |
|------|---------|
| **Agent 开发者** | 基于 OpenClaw SDK 开发智能体，接入底座获得记忆/通信/知识能力 |
| **企业管理员** | 部署底座，管理 Agent 注册、权限、审计 |
| **业务用户** | 通过 IM/Web 与 Agent 交互，Agent 自动完成采购、销售、分析等任务 |

> **真实案例**：[某电力工程企业的多 Agent 落地](docs/case-study.md) —— 投标 3-5 个工作日压到 2-3 小时、采购⇄销售自动谈判成交。

---

## ⚙️ 配置

### 环境变量速查

| 变量 | 必填 | 默认值 | 说明 |
|------|:----:|--------|------|
| `DEEPSEEK_API_KEY` | ✅ | — | LLM 调用密钥（DeepSeek / OpenAI 兼容） |
| `QINGTIAN_CONFIG` | ❌ | `config.yaml` | 配置文件路径 |
| `DASHSCOPE_API_KEY` | ❌ | — | embedding API（使用 fastembed 时可省略） |
| `ZHENYUE_ADMIN_TOKEN` | ❌ | — | 镇岳管理接口令牌 |
| `YONGHENG_BOOTSTRAP_TOKEN` | ❌ | — | 永恒首次部署引导令牌 |
| `HUANYU_SIGN_KEY` | ❌ | — | 跨底座消息签名密钥 |

> **安全建议**：API Key 优先用环境变量注入，避免明文写入配置文件。

### 配置项（config.yaml）

```yaml
role: management                    # management | procurement | sales
host: localhost
organization: acssa

database:
  host: localhost
  port: 5432
  db: qingtian
  user: qingtian
  password: qingtian-2026

service:
  port: 1996

xihe:
  max_agents: 50                     # 同时管理最大 Agent 数
  health_check:
    interval: 30                     # 健康检查间隔（秒）
    failure_threshold: 3             # 连续失败 N 次判定 Unhealthy
  restart:
    max_retries: 4                   # 连续重启失败 N 次 → fatal
    batch_size: 5                    # 批量重启每批最大数
```

完整配置参考 `config.yaml.example`。

---

## 📖 API 参考

> 完整 API 文档见 [docs/api-reference.md](docs/api-reference.md) — 含 17 个模块、80+ 端点、请求/响应示例。
>
> 以下为快速速查：

### 基础

```
GET  /health                    # 健康检查（含 platform 平台能力字段）
GET  /version                   # 版本信息
```

### Agent 管理（羲和）

```
GET    /v1/xihe/agents              # 列出所有 Agent 状态
POST   /v1/xihe/agents/{id}/adopt   # 接管外部进程
POST   /v1/xihe/agents/{id}/stop    # 停止 Agent
POST   /v1/xihe/agents/{id}/pause   # 暂停 Agent
POST   /v1/xihe/agents/{id}/resume  # 恢复 Agent
POST   /v1/xihe/agents/{id}/restart # 重启 Agent
GET    /v1/xihe/stats               # 羲和运行统计
```

### Agent 目录（寰宇）

```
POST   /v1/huanyu/agents/register       # 注册 Agent
GET    /v1/huanyu/agents                # 查询 Agent 列表
GET    /v1/huanyu/agents/{id}           # 获取 Agent 详情
POST   /v1/huanyu/agents/{id}/heartbeat # Agent 心跳
GET    /v1/huanyu/agents/search         # 搜索 Agent
```

### 消息（寰宇）

```
POST   /v1/huanyu/messages              # 发送消息
GET    /v1/huanyu/inbox/{agent_id}      # 查收 inbox
POST   /v1/huanyu/messages/{id}/read    # 标记已读
```

### 记忆（永恒）

```
POST   /v1/yongheng/memories            # 写入记忆
POST   /v1/yongheng/memories/search     # 语义搜索
POST   /v1/yongheng/session/recover     # 恢复会话上下文
```

### 知识（汇川）

```
POST   /v1/huichuan                     # 写入知识条目
GET    /v1/huichuan/search              # 搜索知识库
POST   /v1/huichuan/ingest/file         # 文件入库
```

### 任务（执策）

```
POST   /v1/zhice/tasks                  # 创建任务
GET    /v1/zhice/tasks                  # 任务列表
GET    /v1/zhice/tasks/{id}             # 任务详情
POST   /v1/zhice/steps/{step_id}/submit # 提交步骤结果
```

### 账户（司库）

```
GET    /v1/siku/accounts/{agent_id}     # 账户详情
POST   /v1/siku/accounts/recharge       # 充值
POST   /v1/siku/accounts/check          # 账户校验
```

### 安全管理（镇岳）

```
POST   /v1/zhenyue/tokens               # 创建访问令牌
GET    /v1/zhenyue/audit/log            # 查询审计日志
GET    /v1/zhenyue/audit/chain          # 校验审计链
POST   /v1/zhenyue/keys                 # 创建密钥对
```

---

## 🧪 测试

```bash
# 运行全部测试（需要 PostgreSQL + Redis 运行中）
pytest tests/ -v

# 指定模块
pytest huanyu/tests/ -v
pytest zhice/tests/ -v

# 快速测试（跳过集成测试）
pytest tests/ -v -m "not integration"
```

---

## 📄 开源分层与许可

**开源的是底座和通用 Skill，垂直领域商业 Skill 走 acssa.cn 市场分发。**

| 层 | 内容 | 开源 |
|----|------|:--:|
| 底座内核 | 八模块 + osskill 框架 | ✅ Apache 2.0 |
| 通用 Skill | 文档/翻译/会议/Excel 等 | ✅ |
| 商业 Skill | 投标 / 采购 / 销售 | ❌ 闭源，走 acssa.cn 市场 |
| 证书验证链 | osskill-acssa | ❌ 闭源 |
| acssa.cn 市场 | 网站后端 | ❌ 闭源 |

| 版本 | 许可 | 包含 |
|------|------|------|
| **社区版** | Apache 2.0 | 底座 8 模块 + 基础 HTTP 路由 + 标准 API |
| **企业版** | 商业授权 | 安全审批链、多底座拓扑（OSPF DR + Gossip）、限流守卫、高可用 |

企业版功能需商业授权。详情联系 [acssa.cn](https://acssa.cn)。

**底座遵循 Apache 2.0：可自由使用、修改、二次开发（含商业场景）**，仅需保留版权与许可声明。闭源商业组件经官方渠道提供：

- **垂直 Skill**（投标 / 采购 / 销售）：[acssa.cn](https://acssa.cn) 市场分发，运行需官方签发证书
- **企业版**（安全审批链 / 多底座拓扑 / 限流守卫 / 高可用）：商业授权
- **"ACSSA" / "擎天" 名称与商标**：不在开源许可授予范围内，对外使用需另行授权

### 🛡️ 企业选购指引——认准官方

Apache 2.0 允许任何人对本底座进行分发、修改与二次开发。若您通过非官方渠道获得 ACSSA 或其衍生版本，请注意以下能力差异：

| 能力 | 官方版本 | 非官方衍生版 |
|------|:--:|:--:|
| 官方证书签发（市场 Skill 运行所需） | ✅ | ❌ |
| acssa.cn 市场 Skill 安装与持续更新 | ✅ | ❌ |
| 安全补丁与版本升级 | ✅ 持续跟进 | 取决于维护者 |
| 生产环境技术支持与 SLA | ✅ | ❌ |

衍生版无法签发官方证书、不能使用市场 Skill——采购前请认准 [acssa.cn](https://acssa.cn) 官方渠道。

---

## 🆚 与竞品对比

> 一句话：别人给你的是「Agent 框架/平台」，ACSSA 给你的是「装在企业内网、开箱即用的多智能体操作系统」——多 Agent 协作、私有化部署、生产级安全审计，这三件事框架不做、平台做不到。

| 维度 | **ACSSA** | Dify | Coze / 扣子 | LangChain / LangGraph | AutoGen / CrewAI |
|------|-----------|------|-------------|----------------------|------------------|
| **本质** | 多智能体**操作系统** | LLM 应用开发平台 | Agent 搭建平台（云） | 框架 / 库 | 多 Agent 框架 |
| **数据主权** | ✅ 完全私有化，数据不出网络边界 | ✅ 可私有化部署 | ❌ 以云平台为主 | ✅（自托管库） | ✅（自托管库） |
| **多 Agent 协作** | ✅ 采购⇄销售自动谈判、跨底座联邦通信、跨企业可自动结算 | 工作流编排为主 | 单 Agent 为主 | 需自行实现 | 群聊式 / 任务式协作 |
| **生产级运行时** | ✅ 子进程隔离 + cgroups 资源限制 + OOM 检测 | 部分 | ❌ 平台托管 | ❌ 需自建 | ❌ 需自建 |
| **安全审计** | ✅ 哈希链审计日志，不可篡改、可追溯 | 基础日志 | 平台托管 | ❌ | ❌ |
| **真实企业落地** | ✅ 电力工程企业生产运行 | 有 | 有 | 广泛 | 研究向 |
| **上手方式** | `docker compose up -d` 一键起 | Docker 一键起 | 网页注册 | `pip install` | `pip install` |
| **开源协议** | Apache 2.0（底座）+ 商业 Skill 分层 | Apache 2.0 | 闭源 SaaS | MIT | MIT / MIT |

**核心差异一句话总结**：

- **框架（LangChain / AutoGen / CrewAI）**：给你零件，生产环境（资源隔离、审计、权限、联邦通信）要自己搭。
- **平台（Dify / Coze）**：给你产品，但数据在平台，私有化部署受限。
- **ACSSA**：框架的生产级能力 + 平台的易用性，装在你自己的服务器上，数据主权归你。

---

## 🔗 相关项目

- [OpenClaw SDK](https://github.com/acssa/openclaw) — 智能体开发框架，接入ACSSA 底座的官方 SDK
- [acssa.cn](https://acssa.cn) — 智能体公共信任目录，Agent 注册发现中心
- [GB/Z 185](https://std.samr.gov.cn) — 国家标准《人工智能 智能体互联》
