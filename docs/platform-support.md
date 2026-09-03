# 平台与部署环境支持说明

> ACSSA 的**生产级能力**（资源隔离、安全审计）依赖 Linux 内核特性。本文档明确列出不同部署环境下，哪些能力完整可用、哪些会降级，避免"以为在跑、实则裸奔"。

---

## 一句话结论

| 部署方式 | 定位 | 资源隔离等生产级能力 |
|---------|------|:--:|
| **Linux 裸机 / VM + systemd** | **生产环境（推荐）** | ✅ 完整 |
| Docker（`docker compose up -d`） | 快速体验 / 开发联调 | ⚠️ 部分降级 |
| Windows / macOS | 本地开发调试 | ❌ 基本降级 |

> ACSSA 官方生产部署方式为 **Linux 裸机/VM + systemd**（`qingtian.service`）。Docker 用于快速体验整套系统，其容器隔离会屏蔽部分内核级能力。

---

## 能力对照表

| 能力 | 模块 | Linux + systemd | Docker | Windows / macOS |
|------|------|:--:|:--:|:--:|
| cgroups CPU 权重 / 配额隔离 | 羲和 | ✅ | ❌ | ❌ |
| RLIMIT_AS 内存上限 | 羲和 | ✅ | ✅ | ❌ |
| OOM 检测（dmesg 内核日志） | 羲和 | ✅ | ❌ | ❌ |
| 出站连接检测（/proc/net） | 羲和→镇岳 | ✅ | ✅ 基本可用 | ❌ |
| CPU / 内存使用监控 | 羲和 | ✅ | ✅（/proc 回退） | ❌ |
| 哈希链审计 + Ed25519 签名 | 镇岳 | ✅ | ✅ | ✅ |
| 多 Agent 通信 / 记忆 / 任务编排 | 寰宇/永恒/执策 | ✅ | ✅ | ✅ |
| 自动部署 / 自愈（systemctl） | 吸星 | ✅ | ❌ | ❌ |

---

## 为什么会有降级

ACSSA 的核心安全能力直接依赖 Linux 内核接口：

- **cgroups v2**：写入 `/sys/fs/cgroup/qingtian/` 来限制子进程 CPU。默认 Docker 容器内该路径是**只读挂载**，无法创建 cgroup 子目录 → 隔离静默失效。
- **dmesg**：OOM 检测需读内核日志确认进程被 OOM Killer 杀掉。Docker 容器内 `dmesg` 通常 `Operation not permitted`。
- **systemd**：自动部署依赖 `systemctl restart`，容器内无 systemd。
- **`/proc`、`resource` 模块、`signal.SIGTERM`**：Windows/macOS 上不存在或语义不同。

代码对上述能力均做了**静默降级**（`try/except` 返回失败、跳过），因此系统不会崩溃，但相关能力会静默失效。

---

## 各环境详解

### 1. Linux 裸机 / VM + systemd（生产，完整能力）

```bash
# 手动部署（安装依赖 → 建库 → 配置），代码放在 /opt/qingtian
# 依赖安装与建库步骤见 README「手动安装」；装好后用 systemd 拉起：
sudo cp qingtian/scripts/qingtian.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qingtian
```

- cgroups 资源隔离、OOM 检测、出站检测、自动部署**全部完整可用**
- 需要 root 权限（写 cgroup、读 dmesg、systemctl）

### 2. Docker（快速体验，资源隔离降级）

```bash
git clone https://github.com/chnacssa/qingtian.git && cd qingtian
export DEEPSEEK_API_KEY=sk-your-key
docker compose up -d
curl http://localhost:1996/health
```

- 八模块、多 Agent 通信、记忆、审计日志**完整可用**
- ⚠️ **cgroups CPU 隔离、OOM 检测、systemd 自动部署降级**（容器内 cgroup 只读）
- 适合：快速体验、开发联调、演示；**不适合**依赖资源隔离的生产场景

> 若确需在 Docker 中启用 cgroup 隔离，需给 `qingtian` 服务挂载宿主 cgroup（`- /sys/fs/cgroup:/sys/fs/cgroup:rw`）或 `privileged: true`，二者都有安全代价，生产不推荐。

### 3. Windows / macOS（本地开发，能力降级）

- 系统能启动，八模块基本功能可用
- ❌ cgroups、`/proc` 出站检测、`resource` 内存限制、OOM 检测**全部降级**
- 仅用于**代码调试**，不做任何生产部署

---

## 当前能力自检（手工）

在目标环境跑一次，即可确认关键能力是否可用：

```bash
# cgroups 可写性（能创建子目录 = 隔离可用）
mkdir -p /sys/fs/cgroup/qingtian/test 2>/dev/null && echo "cgroup: OK" && rmdir /sys/fs/cgroup/qingtian/test || echo "cgroup: UNAVAILABLE（资源隔离降级）"

# dmesg 可读性（能读 = OOM 检测可用）
dmesg >/dev/null 2>&1 && echo "dmesg: OK" || echo "dmesg: UNAVAILABLE（OOM 检测降级）"

# systemctl（能调 = 自动部署可用）
command -v systemctl >/dev/null 2>&1 && echo "systemd: OK" || echo "systemd: UNAVAILABLE（自动部署降级）"
```

> 后续版本会在服务启动时内置该自检，并在 `/health` 中显式报告降级状态（而非静默）。
