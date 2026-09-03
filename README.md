# ACSSA Agent Operating System — Production-Grade Multi-Agent Infrastructure for Enterprises

**[简体中文](README.zh-CN.md)** | **English**

> **A complete operating system for putting AI agents to work in the enterprise** — not a framework, not a demo, not a chatbot.

ACSSA (Chinese name: 擎天, "Qingtian") is a multi-agent operating system designed for enterprise production environments. It provides the full infrastructure AI agents need in production: memory retrieval, knowledge evolution, security auditing, cross-instance federation, financial settlement, and a skill-execution runtime. It is running in real production at an electric-power engineering enterprise.

**One-line positioning** — Your data stays yours, never routed through any third-party SaaS; eight core modules work out of the box, deployable on-premises in any environment.

---

## 🔒 Why ACSSA — Data Sovereignty

The biggest concern enterprises have with AI agents is not capability — it's "do we dare hand our data over?" Bids, quotations, customer records, and financial data are the lifeblood of the business and must never be uploaded to someone else's cloud.

**ACSSA's core promise: complete data sovereignty.**

| Deployment | Sovereignty | Best for |
|-----------|:--:|------|
| On-prem physical machine | ✅ Fully air-gapped intranet | Strictest compliance requirements |
| Cloud VM (VPC / dedicated line into intranet) | ✅ Data stays in your own VM | Fast onboarding for smaller teams |
| Private cloud / government cloud | ✅ Under your exclusive control | Government & regulated industries |
| Single-host Docker | ✅ One server runs everything | Evaluation / small teams |

**Three layers of assurance**:
- **Private deployment**: the system runs on your own servers; data never leaves your network boundary
- **Permission isolation**: role-based authorization per position; cross-enterprise data is physically partitioned (`enterprise_id` sharding)
- **Tamper-evident audit**: hash-chained audit logs — who did what, when, is provable and immutable

> Positioning by audience: for business decision-makers — "your data never leaves your intranet"; for technical teams — "data sovereignty with a verifiable boundary".

---

## 🚀 10-Minute Quick Start

> Full walkthrough in [docs/quickstart.md](docs/quickstart.md) — from zero to agent registration, messaging, and task execution.

### Docker one-command deployment (recommended)

```bash
# 1. Clone (repo root contains docker-compose.yml; code lives in the qingtian/ subdirectory)
git clone https://github.com/chnacssa/qingtian.git
cd qingtian

# 2. Provide an LLM API key
export DEEPSEEK_API_KEY=sk-your-key-here

# 3. Start (PostgreSQL + Redis + the ACSSA OS)
docker compose up -d

# 4. Verify
curl http://localhost:1996/health
# → {"status": "ok"}

# 5. Tail the logs
docker compose logs -f qingtian
```

**First launch** automatically performs:

| Step | Automatic | Notes |
|------|:--:|------|
| 1. Database init | ✅ | `init.sql` executes, creating 30+ tables |
| 2. Config generation | ✅ | Default config copied from `config.yaml.example` |
| 3. Health checks | ✅ | Waits for PG, Redis, and ACSSA to be ready |
| 4. OS ready | ✅ | Service listens on port 1996; `/health` returns ok |

> **Requirements**: Docker 24+, Docker Compose v2+. No manual Python / PostgreSQL / Redis installation needed.

### Manual install (bare metal)

```bash
# 1. Requirements
# - Python 3.12+
# - PostgreSQL 16+
# - Redis 7+

# 2. Clone and enter the code directory (code lives in qingtian/ under the repo root)
git clone https://github.com/chnacssa/qingtian.git
cd qingtian/qingtian

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create the database
psql -U postgres -c "CREATE USER qingtian WITH PASSWORD 'qingtian-2026';"
psql -U postgres -c "CREATE DATABASE qingtian OWNER qingtian;"
psql -U qingtian -d qingtian -f scripts/init.sql

# 5. Configure
cp config.yaml.example config.yaml
# Edit config.yaml — at minimum set your LLM API key

# 6. Start
python3 main.py
# → Service runs at http://localhost:1996
```

---

## 🖥️ Platforms & Deployment Environments

> **For production, use Linux bare metal / VM + systemd** — production-grade capabilities such as cgroups resource isolation, OOM detection, outbound detection, and automated deploy/rollback are fully available only in this environment.

| Environment | Positioning | Production capabilities |
|---------|------|:--:|
| Linux bare metal / VM + systemd | **Production (recommended)** | ✅ Full |
| Docker (`docker compose up -d`) | Evaluation / dev integration | ⚠️ Partially degraded |
| Windows / macOS | Local development | ❌ Mostly degraded |

See the full capability matrix in [docs/platform-support.md](docs/platform-support.md) (Chinese).

---

## 🧩 Architecture Overview

### Modules

| Module | Name | Responsibility |
|------|------|------|
| `common/` | Shared infrastructure | Config, DB access, LLM calls, crypto utilities |
| `huanyu/` | **Huanyu** (寰宇) — communication directory | Registration & discovery, message routing, session management, GB/Z 185 compliance |
| `yongheng/` | **Yongheng** (永恒) — memory retrieval | Semantic memory write/search, DREEM gating, embeddings |
| `xixing/` | **Xixing** (吸星) — knowledge evolution | Knowledge acquisition, quality gates, classification, distillation, self-assessment |
| `huichuan/` | **Huichuan** (汇川) — data-asset hub | Multi-channel file ingestion, smart classification, metadata/image extraction, unified search |
| `zhenyue/` | **Zhenyue** (镇岳) — security audit | Hash-chained audit, Ed25519 signing, token management, AES-256-GCM |
| `zhice/` | **Zhice** (执策) — task orchestration | Task decomposition/dispatch, FSM state machines, multi-sig approval |
| `siku/` | **Siku** (司库) — inter-enterprise settlement | Finance agents performing cross-instance fund settlement, accounts, hash-chained financial audit |
| `gateway/` | Gateway | Identity resolution, role checks, middleware chain |
| `xihe/` | **Xihe** (羲和) — agent runtime manager | Process management, health checks, backoff restarts, resource monitoring |
| `bus/` | Bus — unified scheduling | Agent lifecycle orchestration, context injection, auto-register/takeover |

### An operating system: subsystems and enterprise value

Buying ACSSA is not buying any single AI feature — it is buying **a computer that continuously employs digital workers**. Workers (skills / job agents) can be replaced at any time; the machine stays yours, and every worker's experience, data, and audit trail accumulates on it. Each subsystem maps to a classic OS responsibility — and to a concrete enterprise value:

| OS subsystem | ACSSA module | Enterprise value |
|----------|----------|---------|
| Process management | **Xihe** (subprocess isolation / cgroups CPU quotas / OOM detection / 3-tier trust) | N digital workers share one "computer": runaway skills are contained by quotas and sandboxing; one agent failing never takes down the rest |
| Process coordination | **Zhice** (SOP decomposition / FSM state machines / multi-sig approval) | Complex tasks auto-decompose and recover from crashes; critical actions require multi-sig approval — LLM mistakes are caught by the state machine, not left naked |
| Memory management | **Yongheng** (semantic memory / consolidation / agent profiles) | Organizational memory survives staff turnover: how work gets done is captured as searchable memory for all agents |
| Knowledge compiler | **Xixing** (acquire → quality gate → distill → inject) | Organizational knowledge keeps "compiling" into the system and improves with use — an appreciating asset, not depreciating cost |
| File system / data lake | **Huichuan** (Feishu and other ingestion channels / smart classification / metadata & image extraction / unified search) | **Consolidated enterprise data assets**: documents, spreadsheets, drawings, and certificates scattered across the company land in one lake, auto-classified and shared by every agent |
| Network stack | **Huanyu** (message bus / GB/Z 185 standard / cross-instance federation with AIN addressing) | A standard communication protocol for digital workers across departments and enterprises, with unified identity addressing and end-to-end encryption |
| Security subsystem | **Zhenyue** (hash-chained audit / approval / isolation / tokens) | Governance enforced at the kernel level instead of application goodwill — permissions, audit, and isolation configured once, obeyed by every digital worker company-wide |
| Settlement subsystem | **Siku** (finance agents with AIN identities performing cross-instance settlement / hash-chained financial audit) | **Fund settlement between enterprises**: each instance runs a finance agent; payment notices route to the counterparty's bookkeeper agent by AIN, challenge-verified and audit-confirmed — infrastructure for automatic inter-enterprise settlement in the future skill-market ecosystem |

**Three things an OS gives you that app-layer tools cannot**:

1. **No lock-in**: skills are decoupled from the base — swap vendors without swapping the OS; memory, audit trails, and processes all stay;
2. **Governance applied once**: permissions, audit, and data isolation are configured at the OS layer and obeyed by every digital worker in the company;
3. **Rising switching costs**: value is lowest on day one — every position onboarded and every memory/data asset deposited deepens the moat. This is the most churn-resistant shape enterprise software can take.

### Data flow

```
Acquire → Xixing (quality gate) → Distill → Yongheng (memory) → Agent
                                                            ↓
                       Huanyu (communication) ← inter-agent collaboration
                                                            ↓
                       Zhice (tasks) ← LLM decomposition + atomic dispatch
```

### Who uses it

| Role | How |
|------|---------|
| **Agent developers** | Build agents on the OpenClaw SDK; plug into the OS for memory/communication/knowledge |
| **Enterprise admins** | Deploy the OS; manage agent registration, permissions, and audit |
| **Business users** | Interact via IM/Web; agents complete procurement, sales, and analysis tasks |

> **Real deployment**: [an electric-power engineering enterprise](docs/case-study.md) (Chinese) — bid preparation compressed from 3–5 business days to 2–3 hours; automated procurement⇄sales negotiation closes deals.

---

## ⚙️ Configuration

### Environment variables

| Variable | Required | Default | Purpose |
|------|:----:|--------|------|
| `DEEPSEEK_API_KEY` | ✅ | — | LLM API key (DeepSeek / OpenAI-compatible) |
| `QINGTIAN_CONFIG` | ❌ | `config.yaml` | Config file path |
| `DASHSCOPE_API_KEY` | ❌ | — | Embedding API (omit when using fastembed) |
| `ZHENYUE_ADMIN_TOKEN` | ❌ | — | Zhenyue admin API token |
| `YONGHENG_BOOTSTRAP_TOKEN` | ❌ | — | Yongheng first-deploy bootstrap token |
| `HUANYU_SIGN_KEY` | ❌ | — | Cross-instance message signing key |

> **Security note**: inject API keys via environment variables; avoid plaintext in config files.

### config.yaml essentials

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
  max_agents: 50                     # max concurrent agents
  health_check:
    interval: 30                     # seconds
    failure_threshold: 3             # consecutive failures → Unhealthy
  restart:
    max_retries: 4                   # consecutive restart failures → fatal
    batch_size: 5                    # max per restart batch
```

See `config.yaml.example` for the complete reference.

---

## 📖 API Reference

> Full docs at [docs/api-reference.md](docs/api-reference.md) (Chinese) — 17 modules, 80+ endpoints, with request/response examples.
>
> Quick reference below:

### Basics

```
GET  /health                    # Health check (includes platform capability fields)
GET  /version                   # Version info
```

### Agent management (Xihe)

```
GET    /v1/xihe/agents              # List all agent states
POST   /v1/xihe/agents/{id}/adopt   # Adopt an external process
POST   /v1/xihe/agents/{id}/stop    # Stop agent
POST   /v1/xihe/agents/{id}/pause   # Pause agent
POST   /v1/xihe/agents/{id}/resume  # Resume agent
POST   /v1/xihe/agents/{id}/restart # Restart agent
GET    /v1/xihe/stats               # Xihe runtime stats
```

### Agent directory (Huanyu)

```
POST   /v1/huanyu/agents/register       # Register agent
GET    /v1/huanyu/agents                # List agents
GET    /v1/huanyu/agents/{id}           # Agent detail
POST   /v1/huanyu/agents/{id}/heartbeat # Agent heartbeat
GET    /v1/huanyu/agents/search         # Search agents
```

### Messaging (Huanyu)

```
POST   /v1/huanyu/messages              # Send message
GET    /v1/huanyu/inbox/{agent_id}      # Check inbox
POST   /v1/huanyu/messages/{id}/read    # Mark read
```

### Memory (Yongheng)

```
POST   /v1/yongheng/memories            # Write memory
POST   /v1/yongheng/memories/search     # Semantic search
POST   /v1/yongheng/session/recover     # Recover session context
```

### Knowledge (Huichuan)

```
POST   /v1/huichuan                     # Write knowledge entry
GET    /v1/huichuan/search              # Search knowledge base
POST   /v1/huichuan/ingest/file         # Ingest file
```

### Tasks (Zhice)

```
POST   /v1/zhice/tasks                  # Create task
GET    /v1/zhice/tasks                  # List tasks
GET    /v1/zhice/tasks/{id}             # Task detail
POST   /v1/zhice/steps/{step_id}/submit # Submit step result
```

### Accounts (Siku)

```
GET    /v1/siku/accounts/{agent_id}     # Account detail
POST   /v1/siku/accounts/recharge       # Recharge
POST   /v1/siku/accounts/check          # Account check
```

### Security management (Zhenyue)

```
POST   /v1/zhenyue/tokens               # Create access token
GET    /v1/zhenyue/audit/log            # Query audit log
GET    /v1/zhenyue/audit/chain          # Verify audit chain
POST   /v1/zhenyue/keys                 # Create keypair
```

---

## 🧪 Testing

```bash
# Full suite (requires PostgreSQL + Redis running)
pytest tests/ -v

# Single module
pytest huanyu/tests/ -v
pytest zhice/tests/ -v

# Fast run (skip integration tests)
pytest tests/ -v -m "not integration"
```

---

## 📄 Open-Source Layering & Licensing

**The base OS and generic skills are open source; vertical-domain commercial skills are distributed via the acssa.cn marketplace.**

| Layer | Contents | Open source |
|----|------|:--:|
| Base kernel | Eight modules + osskill framework | ✅ Apache 2.0 |
| Generic skills | Docs / translation / meetings / Excel etc. | ✅ |
| Commercial skills | Bidding / procurement / sales | ❌ Closed, via acssa.cn marketplace |
| Certificate chain | osskill-acssa | ❌ Closed |
| acssa.cn marketplace | Website backend | ❌ Closed |

| Edition | License | Includes |
|------|------|------|
| **Community** | Apache 2.0 | Base 8 modules + basic HTTP routing + standard API |
| **Enterprise** | Commercial | Security approval chains, multi-instance topology (OSPF DR + Gossip), rate-limit guards, high availability |

Enterprise features require a commercial license — contact [acssa.cn](https://acssa.cn).

**The base is Apache 2.0: you are free to use, modify, and build derivatives — including commercially** — as long as copyright and license notices are preserved. Closed commercial components are provided through official channels:

- **Vertical skills** (bidding / procurement / sales): distributed via the [acssa.cn](https://acssa.cn) marketplace; running them requires officially issued certificates
- **Enterprise edition** (approval chains / multi-instance topology / rate limiting / HA): commercial license
- **The "ACSSA" name and trademarks**: not granted by the open-source license; external use requires separate authorization

### 🛡️ Enterprise buyer's guide — choose the official channel

Apache 2.0 allows anyone to distribute, modify, and build on this base. If you obtained ACSSA or a derivative through an unofficial channel, note the differences:

| Capability | Official | Unofficial derivatives |
|------|:--:|:--:|
| Official certificate issuance (required by marketplace skills) | ✅ | ❌ |
| acssa.cn marketplace skill install & updates | ✅ | ❌ |
| Security patches & upgrades | ✅ Ongoing | Depends on maintainer |
| Production support & SLA | ✅ | ❌ |

Derivatives cannot issue official certificates or use marketplace skills — before purchasing, verify the source at [acssa.cn](https://acssa.cn).

---

## 🆚 Comparison

> In one line: others give you an "agent framework/platform"; ACSSA gives you **a multi-agent operating system installed in your own intranet, ready out of the box** — multi-agent collaboration, private deployment, and production-grade security audit: frameworks don't do this, platforms can't.

| Dimension | **ACSSA** | Dify | Coze | LangChain / LangGraph | AutoGen / CrewAI |
|------|-----------|------|-------------|----------------------|------------------|
| **What it is** | Multi-agent **operating system** | LLM app platform | Agent builder (cloud) | Framework / library | Multi-agent frameworks |
| **Data sovereignty** | ✅ Fully private; data never leaves your boundary | ✅ Self-hostable | ❌ Mostly cloud | ✅ (self-hosted libs) | ✅ (self-hosted libs) |
| **Multi-agent collaboration** | ✅ Procurement⇄sales auto-negotiation, cross-instance federation, inter-enterprise settlement | Workflow-centric | Mostly single-agent | DIY | Chat/task group styles |
| **Production runtime** | ✅ Subprocess isolation + cgroups + OOM detection | Partial | ❌ Hosted | ❌ DIY | ❌ DIY |
| **Security audit** | ✅ Hash-chained, tamper-evident, traceable | Basic logs | Hosted | ❌ | ❌ |
| **Real enterprise deployment** | ✅ Electric-power engineering production | Yes | Yes | Widespread | Research-grade |
| **Onboarding** | `docker compose up -d` | Docker | Web signup | `pip install` | `pip install` |
| **License** | Apache 2.0 (base) + commercial skill layering | Apache 2.0 | Closed SaaS | MIT | MIT / MIT |

**The core difference**:

- **Frameworks (LangChain / AutoGen / CrewAI)**: give you parts; production concerns (resource isolation, audit, permissions, federation) are DIY.
- **Platforms (Dify / Coze)**: give you a product, but your data lives on their platform, and private deployment is limited.
- **ACSSA**: framework-grade production capabilities with platform-grade usability, installed on your own servers — data sovereignty included.

---

## 🔗 Related Projects

- [OpenClaw SDK](https://github.com/acssa/openclaw) — the agent development framework; official SDK for connecting to the ACSSA base
- [acssa.cn](https://acssa.cn) — the public trust directory for agents; registration & discovery hub
- [GB/Z 185](https://std.samr.gov.cn) — Chinese national standard "Artificial Intelligence — Agent Interconnection"
